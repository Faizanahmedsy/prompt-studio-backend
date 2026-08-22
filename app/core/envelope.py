"""Global success-response envelope.

Every successful JSON response from an application route is wrapped so the
frontend always reads one shape::

    {"success": true, "status_code": 200, "message": "OK", "data": <payload>}

Errors use the same keys (with ``success: false`` and ``data: null``) but are
produced by the handlers in :mod:`app.core.exceptions`, so this middleware only
ever touches successful responses.

Deliberately NOT wrapped:

- ``/docs``, ``/redoc``, ``/openapi.json`` — wrapping the schema breaks Swagger
  UI. They live at the app root, outside ``API_V1_PREFIX``, so ``_should_wrap``
  already skips them.
- the admin panel and its assets, for the same reason.
- responses of status >= 400 — already enveloped by the exception handlers.
- non-JSON responses (files, 204 No Content, HTML) and websocket traffic.
"""

import json
from http import HTTPStatus
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import settings


def status_message(status_code: int) -> str:
    """Human-readable reason phrase for an HTTP status (201 -> 'Created')."""
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "OK"


def set_response_message(request: Request, message: str) -> None:
    """Override the default status-phrase message for this response.

    Call inside an endpoint that takes a ``request: Request`` parameter::

        set_response_message(request, ResponseMessage.PROJECT_CREATED)
    """
    request.state.response_message = message


class ResponseEnvelopeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        if not self._should_wrap(request, response):
            return response

        raw = await _read_body(response)
        content_type = response.headers.get("content-type", "")
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            # Claimed to be JSON but wasn't — pass it through untouched.
            return Response(
                content=raw,
                status_code=response.status_code,
                headers=_passthrough_headers(response),
                media_type=content_type,
            )

        message = getattr(request.state, "response_message", None)
        wrapped: dict[str, Any] = {
            "success": True,
            "status_code": response.status_code,
            "message": message or status_message(response.status_code),
            "data": payload,
            "errors": [],
        }
        return Response(
            content=json.dumps(wrapped, default=str),
            status_code=response.status_code,
            headers=_passthrough_headers(response),
            media_type="application/json",
        )

    def _should_wrap(self, request: Request, response: Response) -> bool:
        path = request.url.path
        is_app_path = path == "/health" or path.startswith(settings.API_V1_PREFIX)
        if not is_app_path:
            return False
        if response.status_code >= 400:
            return False
        return response.headers.get("content-type", "").startswith("application/json")


async def _read_body(response: Response) -> bytes:
    # Starlette's BaseHTTPMiddleware hands back a streaming-style response that
    # subclasses Response (not StreamingResponse) and exposes `body_iterator`.
    body_iterator = getattr(response, "body_iterator", None)
    if body_iterator is not None:
        chunks: list[bytes] = []
        async for chunk in body_iterator:
            chunks.append(chunk.encode() if isinstance(chunk, str) else bytes(chunk))
        return b"".join(chunks)
    return bytes(getattr(response, "body", b"") or b"")


def _passthrough_headers(response: Response) -> dict[str, str]:
    headers = dict(response.headers)
    headers.pop("content-length", None)  # recomputed for the new body
    return headers
