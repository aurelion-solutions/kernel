# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ASGI middleware that reads or generates the X-Correlation-ID request header."""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from src.core.context import correlation_id_var

CORRELATION_HEADER = 'X-Correlation-ID'


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Echo or generate ``X-Correlation-ID`` for every HTTP request.

    Pipeline (per request):
    1. Read ``X-Correlation-ID`` from request headers; strip whitespace; treat empty as absent.
    2. If absent → generate ``str(uuid.uuid4())``.
    3. Store the value in :data:`~src.core.context.correlation_id_var` for the duration of the request.
    4. Echo the value in the response header.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        raw = request.headers.get(CORRELATION_HEADER, '')
        value = raw.strip() if raw else ''
        if not value:
            value = str(uuid.uuid4())

        token = correlation_id_var.set(value)
        try:
            response: Response = await call_next(request)
        finally:
            correlation_id_var.reset(token)

        response.headers[CORRELATION_HEADER] = value
        return response
