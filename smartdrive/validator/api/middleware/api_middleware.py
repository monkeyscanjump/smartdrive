#  MIT License
#
#  Copyright (c) 2024 Dezen | freedom block by block
#
#  Permission is hereby granted, free of charge, to any person obtaining a copy
#  of this software and associated documentation files (the "Software"), to deal
#  in the Software without restriction, including without limitation the rights
#  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#  copies of the Software, and to permit persons to whom the Software is
#  furnished to do so, subject to the following conditions:
#
#  The above copyright notice and this permission notice shall be included in all
#  copies or substantial portions of the Software.
#
#  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
#  SOFTWARE.

import json
from urllib.parse import parse_qs
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp
from typing import Awaitable, Callable

from substrateinterface import Keypair
from communex.compat.key import classic_load_key
from communex.balance import from_nano

from smartdrive.commune.connection_pool import get_staketo
from smartdrive.commune.errors import CommuneNetworkUnreachable
from smartdrive.commune.request import get_filtered_modules
from smartdrive.commune.utils import get_ss58_address_from_public_key
from smartdrive.sign import verify_data_signature
from smartdrive.utils import MINIMUM_STAKE
from smartdrive.validator.api.endpoints import PING_ENDPOINT
from smartdrive.validator.config import config_manager
from smartdrive.validator.models.models import ModuleType

Callback = Callable[[Request], Awaitable[Response]]
exclude_paths = [PING_ENDPOINT]


# TODO: Should be refactorized
class APIMiddleware(BaseHTTPMiddleware):

    _key: Keypair = None

    def __init__(self, app: ASGIApp):
        super().__init__(app)
        self._key = classic_load_key(config_manager.config.key)

    async def dispatch(self, request: Request, call_next: Callback) -> Response:
        """
        Middleware function to handle request authentication and authorization.

        Params:
            request (Request): The incoming request object.
            call_next (Callback): The next request handler in the middleware chain.

        Returns:
            Response: The response object.
        """
        def _error_response(code: int, detail: str) -> JSONResponse:
            """
            Generate an unauthorized response.

            Params:
                detail (str): The detail message for the unauthorized response.

            Returns:
                JSONResponse: The unauthorized response object with status code 401.
            """
            return JSONResponse(
                status_code=code,
                content={"detail": detail}
            )

        if request.url.path in exclude_paths:
            return await call_next(request)

        if request.client is None:
            return _error_response(401, "Address should be present in request")

        key = request.headers.get('X-Key')
        if not key:
            return _error_response(401, "Valid X-Key not provided on headers")

        ss58_address = get_ss58_address_from_public_key(key)
        if not ss58_address:
            return _error_response(401, "Not a valid public key provided")

        try:
            staketo_modules = await get_staketo(ss58_address, config_manager.config.netuid)
            validators = await get_filtered_modules(config_manager.config.netuid, ModuleType.VALIDATOR)
        except CommuneNetworkUnreachable:
            return _error_response(404, "Currently the Commune network is unreachable")

        validator_addresses = {validator.ss58_address for validator in validators}
        active_stakes = {address: from_nano(stake) for address, stake in staketo_modules.items() if address in validator_addresses and address != str(ss58_address)}
        total_stake = sum(active_stakes.values())

        if total_stake < MINIMUM_STAKE:
            return _error_response(401, f"You must stake at least {MINIMUM_STAKE} COMAI in total to active validators")

        request.state.total_stake = total_stake

        signature = request.headers.get('X-Signature')
        body = {}
        if request.method == "GET" or request.method == "DELETE":
            body = dict(request.query_params)
        else:
            content_type = request.headers.get("Content-Type")
            if content_type and "application/json" in content_type:
                body_bytes = await request.body()
                if body_bytes:
                    try:
                        body = await request.json()
                    except json.JSONDecodeError:
                        return _error_response(401, "Invalid JSON")
                else:
                    body = {}
            elif content_type and "application/octet-stream" not in content_type:
                body_bytes = await request.body()
                try:
                    body = {key: value[0] if isinstance(value, list) else value for key, value in parse_qs(body_bytes.decode("utf-8")).items()}
                except UnicodeDecodeError:
                    body = body_bytes

        is_verified_signature = verify_data_signature(body, signature, ss58_address)
        if not is_verified_signature:
            return _error_response(401, "Valid X-Signature not provided on headers")

        response = await call_next(request)
        return response
