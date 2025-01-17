# -*- coding: utf-8 -*-
# Copyright 2020 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import html
import json
import logging
import urllib.parse
from typing import Any, Optional
from uuid import uuid4

import pkg_resources
from twisted.web.resource import Resource
from twisted.web.server import NOT_DONE_YET, Request
from twisted.web.static import File

import synapse.module_api
from synapse.module_api import run_in_background
from synapse.module_api.errors import SynapseError

from matrix_synapse_saml_touchstone._sessions import (
    SESSION_COOKIE_NAME,
    get_mapping_session,
    displayname_mapping_sessions,
    DisplayNameMappingSession,
)

# maximum number of times to try to register if a username is not available
# (adding a number sequentially to disambiguate)
MAX_FAILURES = 9

"""
This file implements the "display name picker" resource, which is mapped as an
additional_resource into the synapse resource tree.

The top-level resource is just a File resource which serves up the static files in the
"res" directory, but it has a couple of children:

   * "submit", which does the mechanics of registering the new user, and redirects the
     browser back to the client URL

   * "check": checks if a userid is free.
"""

logger = logging.getLogger(__name__)


def pick_displayname_resource(
    parsed_config, module_api: synapse.module_api.ModuleApi
) -> Resource:
    """Factory method to generate the top-level display name picker resource"""
    base_path = pkg_resources.resource_filename("matrix_synapse_saml_touchstone", "res")
    res = File(base_path)
    res.putChild(b"submit", SubmitResource(module_api))
    res.putChild(b"", FormResource(module_api, os.path.join(base_path, "index.html")))
    return res


def parse_config(config: dict):
    return None


pick_displayname_resource.parse_config = parse_config


HTML_ERROR_TEMPLATE = """<!DOCTYPE html>
<html lang=en>
  <head>
    <meta charset="utf-8">
    <title>Error {code}</title>
  </head>
  <body>
     <p>{msg}</p>
  </body>
</html>
"""


def _wrap_for_html_exceptions(f):
    async def wrapped(self, request, *args):
        try:
            return await f(self, request, *args)
        except Exception:
            logger.exception("Error handling request %s" % (request,))
            _return_html_error(500, "Internal server error", request)

    return wrapped


def _wrap_for_text_exceptions(f):
    async def wrapped(self, request, *args):
        try:
            return await f(self, request, *args)
        except Exception:
            logger.exception("Error handling request %s" % (request,))
            body = b"Internal server error"
            request.setResponseCode(500)
            request.setHeader(b"Content-Type", b"text/plain; charset=utf-8")
            request.setHeader(b"Content-Length", b"%i" % (len(body),))
            request.write(body)
            request.finish()

    return wrapped


class AsyncResource(Resource):
    """Extends twisted.web.Resource to add support for async_render_X methods"""

    def render(self, request: Request):
        method = request.method.decode("ascii")
        m = getattr(self, "async_render_" + method, None)
        if not m and method == "HEAD":
            m = getattr(self, "async_render_GET", None)
        if not m:
            return super().render(request)

        async def run():
            with request.processing():
                return await m(request)

        run_in_background(run)
        return NOT_DONE_YET


class FormResource(AsyncResource):
    """
    Simple resource that replies with index.html, replacing KERB_PLACEHOLDER with the user's
    kerb and DISPLAYNAME_PLACEHOLDER with the user's display name.
    """

    def __init__(self, module_api: synapse.module_api.ModuleApi, base_path: str):
        super().__init__()
        self._module_api = module_api
        with open(base_path, 'r') as f:
            self.html = f.read()

    @_wrap_for_html_exceptions
    async def async_render_GET(self, request: Request):
        _, session = _get_session(request)
        kerb = session.email.split('@')[0]
        body = self.html.format(kerb=kerb, displayname=session.displayname)
        request.setResponseCode(200)
        request.setHeader(b"Content-Type", b"text/html; charset=utf-8")
        request.setHeader(b"Content-Length", b"%i" % (len(body),))
        request.write(body.encode("utf-8"))
        try:
            request.finish()
        except RuntimeError as e:
            logger.info("Connection disconnected before response was written: %r", e)


class SubmitResource(AsyncResource):
    def __init__(self, module_api: synapse.module_api.ModuleApi):
        super().__init__()
        self._module_api = module_api

    @_wrap_for_html_exceptions
    async def async_render_POST(self, request: Request, num_failures: int = 0):
        session_id, session = _get_session(request)

        # we don't clear the session from the dict until the ID is successfully
        # registered, so the user can go round and have another go if need be.
        #
        # this means there's theoretically a race where a single user can register
        # two accounts. I'm going to assume that's not a dealbreaker.

        # TODO: uhhhhh yeah I don't like that. but ok.

        if b"displayname" not in request.args:
            _return_html_error(400, "missing display name", request)
            return

        # We don't need people choosing their own usernames right now.
        # if b"username" not in request.args:
        #     _return_html_error(400, "missing username", request)
        #     return
        # localpart = request.args[b"username"][0].decode("utf-8", errors="replace")
        
        # People can have their kerb as their username
        localpart = session.email.split('@')[0]

        # Add a number if the username is already taken
        if num_failures > 0:
            localpart = f'{localpart}{num_failures}'

        # Get user's desired display name
        displayname = request.args[b"displayname"][0].decode("utf-8", errors="replace")

        logger.info("Registering username %s", localpart)
        try:
            registered_user_id = await self._module_api.register_user(
                localpart=localpart, displayname=displayname, emails=[session.email],
            )
        except SynapseError as e:
            if num_failures < MAX_FAILURES:
                return await self.async_render_POST(request, num_failures + 1)
            logger.warning("Error during registration: %s", e)
            _return_html_error(e.code, e.msg, request)
            return

        await self._module_api.record_user_external_id(
            "saml", session.remote_user_id, registered_user_id
        )

        # in case we want to make student-only rooms
        # HACK: we abuse synapse's SSO mapping table and the fact that it
        # doesn't check that the sign-on method exists. This way, we don't need
        # to create a database table just for this, and the admin endpoints/GUI
        # can show someone's affiliation. We concatenate a UUID since these have to
        # be unique. We can drop it when we need to access it from other modules
        await self._module_api.record_user_external_id(
            "affiliation", f"{session.affiliation}|{uuid4()}", registered_user_id
        )

        del displayname_mapping_sessions[session_id]

        # delete the cookie
        request.addCookie(
            SESSION_COOKIE_NAME,
            b"",
            expires=b"Thu, 01 Jan 1970 00:00:00 GMT",
            path=b"/",
        )

        await self._module_api.complete_sso_login_async(
            registered_user_id,
            request,
            session.client_redirect_url,
        )


def _get_session(request: Request) -> tuple[str, DisplayNameMappingSession]:
    session_id = request.getCookie(SESSION_COOKIE_NAME)
    if not session_id:
        _return_html_error(400, "missing session_id", request)
        return

    session_id = session_id.decode("ascii", errors="replace")
    session = get_mapping_session(session_id)
    if not session:
        logger.info("Session ID %s not found", session_id)
        _return_html_error(403, "Unknown session", request)
        return

    return session_id, session


def _add_login_token_to_redirect_url(url, token):
    url_parts = list(urllib.parse.urlparse(url))
    query = dict(urllib.parse.parse_qsl(url_parts[4]))
    query.update({"loginToken": token})
    url_parts[4] = urllib.parse.urlencode(query)
    return urllib.parse.urlunparse(url_parts)


def _return_html_error(code: int, msg: str, request: Request):
    """Sends an HTML error page"""
    msg += " Please email matrix@mit.edu for help."
    body = HTML_ERROR_TEMPLATE.format(code=code, msg=html.escape(msg)).encode("utf-8")
    request.setResponseCode(code)
    request.setHeader(b"Content-Type", b"text/html; charset=utf-8")
    request.setHeader(b"Content-Length", b"%i" % (len(body),))
    request.write(body)
    try:
        request.finish()
    except RuntimeError as e:
        logger.info("Connection disconnected before response was written: %r", e)


def _return_json(json_obj: Any, request: Request):
    json_bytes = json.dumps(json_obj).encode("utf-8")

    request.setHeader(b"Content-Type", b"application/json")
    request.setHeader(b"Content-Length", b"%d" % (len(json_bytes),))
    request.setHeader(b"Cache-Control", b"no-cache, no-store, must-revalidate")
    request.setHeader(b"Access-Control-Allow-Origin", b"*")
    request.setHeader(
        b"Access-Control-Allow-Methods", b"GET, POST, PUT, DELETE, OPTIONS"
    )
    request.setHeader(
        b"Access-Control-Allow-Headers",
        b"Origin, X-Requested-With, Content-Type, Accept, Authorization",
    )
    request.write(json_bytes)
    try:
        request.finish()
    except RuntimeError as e:
        logger.info("Connection disconnected before response was written: %r", e)
