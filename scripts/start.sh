#!/usr/bin/env sh
# Start the API on a platform that hands us a port and nothing else.
#
# Render, Railway, Fly and friends inject $PORT and expect the process to bind
# it. They also have no separate place to run migrations on a free plan, so the
# schema is brought up here, in front of the server, rather than in a deploy
# hook that does not exist.
#
# Local development does not use this — docker-compose overrides the command
# with its own, and `make dev` runs uvicorn directly.
set -e

PORT="${PORT:-8000}"

# Before anything touches the database. A bad value otherwise kills uvicorn at
# import with a bare pydantic traceback, and the platform reports it as a failed
# deploy with no clue which setting was wrong. This names it.
#
# It refuses to start on an insecure configuration — a default SECRET_KEY above
# all, since the committed value is public and would let anyone forge a
# superadmin token. A failed deploy that says why beats a healthy-looking one
# that is wide open.
echo "[start] checking configuration"
uv run --no-dev --frozen python scripts/check_env.py

echo "[start] migrating"
uv run --no-dev --frozen alembic upgrade head

# The seed only creates the superadmin named in the environment, and is
# idempotent. A failure here must not keep the API down: a seed that cannot run
# is a login you have to fix, while an API that will not boot is everything
# broken at once.
echo "[start] seeding"
uv run --no-dev --frozen python -m app.seed || echo "[start] seed failed, continuing"

echo "[start] serving on :${PORT}"
# One worker, deliberately. Collaboration fans out through Redis between
# workers, and on a free plan there is no Redis — so a second worker would mean
# two people on one project silently not seeing each other. Scale by adding
# Redis first, then workers.
exec uv run --no-dev --frozen uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --workers 1 \
    --proxy-headers \
    --forwarded-allow-ips '*'
