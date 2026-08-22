import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.messages import ErrorMessage

# Starlette renamed this constant. The old name still works but emits a
# deprecation warning *on attribute access*, which is why this is a `hasattr`
# check and not a `getattr` with a default — the default argument would be
# evaluated eagerly and warn every time this module is imported.
UNPROCESSABLE: int = (
    status.HTTP_422_UNPROCESSABLE_CONTENT
    if hasattr(status, "HTTP_422_UNPROCESSABLE_CONTENT")
    else 422
)

logger = logging.getLogger("app.error")


class AppError(Exception):
    """A failure with a message the client is meant to read.

    ``errors`` is the structured half. The envelope carries an ``errors`` list
    (validation failures use it), and routing extras through it is what lets a
    client *branch* instead of string-matching the message — e.g. the current
    version number returned alongside a stale-save conflict. Attributes set on
    the exception object do NOT reach the client; only this does.
    """

    def __init__(
        self,
        message: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        self.message = message
        self.status_code = status_code
        self.errors = errors
        super().__init__(message)


class NotFoundError(AppError):
    def __init__(
        self,
        message: str = ErrorMessage.RESOURCE_NOT_FOUND,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message, status.HTTP_404_NOT_FOUND, errors)


class AuthenticationError(AppError):
    def __init__(
        self,
        message: str = ErrorMessage.COULD_NOT_VALIDATE_CREDENTIALS,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message, status.HTTP_401_UNAUTHORIZED, errors)


class AuthorizationError(AppError):
    def __init__(
        self,
        message: str = ErrorMessage.NOT_ENOUGH_PERMISSIONS,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message, status.HTTP_403_FORBIDDEN, errors)


class ConflictError(AppError):
    def __init__(
        self,
        message: str,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message, status.HTTP_409_CONFLICT, errors)


class ValidationError(AppError):
    def __init__(
        self,
        message: str = ErrorMessage.VALIDATION_FAILED,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message, UNPROCESSABLE, errors)


class RateLimitError(AppError):
    def __init__(
        self,
        message: str,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message, status.HTTP_429_TOO_MANY_REQUESTS, errors)


def _envelope(
    status_code: int, message: str, errors: list[dict[str, Any]] | None = None
) -> JSONResponse:
    """Failure half of the response envelope — the same keys as a success."""
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "status_code": status_code,
            "message": message,
            "data": None,
            "errors": errors or [],
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return _envelope(exc.status_code, exc.message, exc.errors)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                # Drop the leading "body"/"query" segment: the client sent a
                # field, not a location, and "body -> email" reads as noise in a
                # form error.
                "field": ".".join(str(part) for part in error["loc"][1:]) or str(error["loc"][0]),
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        return _envelope(UNPROCESSABLE, ErrorMessage.VALIDATION_FAILED, errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else ErrorMessage.INTERNAL_ERROR
        return _envelope(exc.status_code, detail)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Logged with the traceback; the client is told nothing about internals.
        # In development the class and message are echoed so the browser network
        # tab is enough to debug without tailing the server.
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        if settings.is_production:
            return _envelope(status.HTTP_500_INTERNAL_SERVER_ERROR, ErrorMessage.INTERNAL_ERROR)
        return _envelope(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"{type(exc).__name__}: {exc}",
        )
