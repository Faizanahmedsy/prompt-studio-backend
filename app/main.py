import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import models as _model_registry  # noqa: F401  (registers every ORM model)
from app.api.router import api_router
from app.core.audit_context import set_request_meta
from app.core.config import settings
from app.core.database import engine
from app.core.envelope import ResponseEnvelopeMiddleware
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.redis import close_redis, get_redis
from app.modules.collab.router import router as collab_router

STATIC_DIR = Path(__file__).parent / "static"


async def bind_request_meta(request: Request) -> None:
    """Record the caller's IP and user agent for the audit trail.

    An app-level dependency rather than something the authenticated-user
    dependency does, so anonymous requests — failed sign-ins, most of all — are
    captured too.
    """
    forwarded = request.headers.get("x-forwarded-for")
    ip = (
        forwarded.split(",")[0].strip()
        if forwarded
        else (request.client.host if request.client else None)
    )
    # Truncated to the widest address anyone can have (45 characters — an
    # IPv4-mapped IPv6 literal). The header is attacker-controlled and the
    # column is 64 characters, so an oversized one used to fail the INSERT of
    # the audit row — which shares the request's transaction, so the sign-in
    # attempt rolled back with a 500 *and* left no trace of itself.
    set_request_meta((ip or "")[:45] or None, request.headers.get("user-agent"))


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    configure_logging(settings.LOG_LEVEL)
    logger = logging.getLogger("app.startup")
    logger.info(
        "%s starting — environment=%s database=%s",
        settings.PROJECT_NAME,
        settings.ENVIRONMENT,
        f"{settings.POSTGRES_DB}@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}",
    )
    # Said out loud, because the difference is invisible until two people on
    # different workers cannot see each other. Without Redis, collaboration is
    # correct for ONE worker and silently wrong for more than one.
    if await get_redis() is None:
        logger.warning(
            "Redis unreachable — collaboration is single-worker only. "
            "Run one uvicorn worker, or configure REDIS_* before scaling out."
        )
    else:
        logger.info("Redis connected — collaboration fans out across workers")
    yield
    from app.modules.collab.hub import hub

    await hub.shutdown()
    await close_redis()
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.PROJECT_NAME,
        version="0.1.0",
        debug=settings.APP_DEBUG,
        lifespan=lifespan,
        description=(
            "Accounts, shared projects and live collaboration for Prompt Studio.\n\n"
            "Every response uses one envelope: "
            "`{success, status_code, message, data, errors}`."
        ),
    )

    # Wraps successful JSON in the uniform envelope. Added before CORS so CORS
    # stays the outermost middleware and still answers preflight on errors.
    app.add_middleware(ResponseEnvelopeMiddleware)

    if settings.BACKEND_CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.BACKEND_CORS_ORIGINS,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    register_exception_handlers(app)

    @app.get("/health", tags=["Health"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "environment": settings.ENVIRONMENT}

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "name": settings.PROJECT_NAME,
            "docs": "/docs",
            "admin": "/admin",
            "api": settings.API_V1_PREFIX,
        }

    # A second renderer for the same OpenAPI document FastAPI already serves.
    # Swagger stays at /docs; this only adds a nicer reader.
    @app.get("/scalar", include_in_schema=False)
    async def scalar_docs() -> HTMLResponse:
        return HTMLResponse(
            f"""<!doctype html>
<html>
  <head>
    <title>{settings.PROJECT_NAME}</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
  </head>
  <body>
    <script id="api-reference" data-url="{app.openapi_url}"></script>
    <script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
  </body>
</html>"""
        )

    # The admin panel: one self-contained page that talks to this API with the
    # operator's own token. Served from the API rather than built into the Next
    # app so it works even when the frontend is down — which is exactly when an
    # operator needs it.
    @app.get("/admin", include_in_schema=False)
    async def admin_panel() -> FileResponse:
        return FileResponse(STATIC_DIR / "admin" / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    app.include_router(
        api_router, prefix=settings.API_V1_PREFIX, dependencies=[Depends(bind_request_meta)]
    )
    # Mounted without that dependency — see the note in `app.api.router`.
    app.include_router(collab_router, prefix=settings.API_V1_PREFIX)
    return app


app = create_app()
