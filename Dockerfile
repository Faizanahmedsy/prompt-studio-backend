# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONPATH=/app \
    PATH="/app/.venv/bin:$PATH"

# uv resolves and installs an order of magnitude faster than pip, and the lock
# file makes the image reproducible.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# The user is created and switched to BEFORE anything is written, so every file
# is owned correctly as it lands. A `chown -R /app` at the end instead has to
# rewrite the whole virtualenv — measured at 233 seconds on this project, for a
# result identical to this.
RUN useradd --create-home --uid 10001 appuser && mkdir -p /app && chown appuser:appuser /app
WORKDIR /app
USER appuser

# Dependencies come from the manifests alone, so editing source does not
# invalidate this layer.
COPY --chown=appuser:appuser pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

COPY --chown=appuser:appuser . .

EXPOSE 8000

# The port is read at check time, not baked in. A managed host injects $PORT and
# `scripts/start.sh` binds it — hardcoding 8000 here meant the check hit a port
# nothing was listening on, failed three times, and the platform restarted a
# container that was serving traffic perfectly well. It flapped between 200 and
# "no server" every few seconds. Falls back to 8000 for docker-compose, which
# sets no PORT.
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import os,urllib.request,sys; p=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://localhost:{p}/health', timeout=3).status==200 else 1)"

# Migrate, seed, then serve — the same three steps for every host, so a
# platform that runs the image's default command gets a working application
# rather than a server talking to an empty database.
#
# This was learned the hard way: with no command configured, Render ran plain
# uvicorn. The API answered every request and looked healthy, but no migration
# and no seed had ever run, so there was not a single user to log in as. The
# setup has to live where it cannot be left out.
#
# docker-compose overrides this with its own command, and `make dev` runs
# uvicorn directly, so local development is unchanged.
CMD ["./scripts/start.sh"]
