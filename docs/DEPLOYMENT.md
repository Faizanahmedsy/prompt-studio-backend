# Running it for real

The compose stack is a working deployment, not just a dev convenience:
`docker compose up -d` builds the image, waits for Postgres and Redis, runs
`check_env` → `alembic upgrade head` → `app.seed`, and only then starts uvicorn.
A broken migration crash-loops the container rather than serving a half-migrated
schema — that is the intended signal.

```bash
docker compose up -d
docker compose logs -f api
```

---

## Before the first production start

`scripts/check_env.py` refuses to start on several of these, but not all.

- [ ] **`SECRET_KEY`** — `openssl rand -hex 32`. It signs every token; changing
      it later signs everyone out.
- [ ] **`ENVIRONMENT=production`** and **`APP_DEBUG=false`**. In production the
      500 handler stops echoing the exception, and `/auth/forgot-password` stops
      returning the reset token in the body.
- [ ] **`SUPERADMIN_PASSWORD`** — not the example value. The seed creates this
      account once and never touches it again, so change it here *before* the
      first start, not after.
- [ ] **`BACKEND_CORS_ORIGINS`** — the exact frontend origins. Empty means the
      browser blocks every call and the failure looks like a network error three
      layers away.
- [ ] **`POSTGRES_PASSWORD`** and **`REDIS_PASSWORD`** — the compose defaults are
      development credentials.
- [ ] **`EMAIL_TRANSPORT=smtp`** plus the `SMTP_*` values. Left on `console`,
      invitations and password resets are written to the log and delivered to
      nobody.
- [ ] **`FRONTEND_BASE_URL`** — every link in every email is built from it.

## Behind a proxy

Terminate TLS at nginx/ALB and forward to the container.

Two headers matter. `X-Forwarded-For` becomes the audit trail's IP **and** the
rate limiter's identity, so the proxy must **overwrite** it rather than append
to a client-supplied one — otherwise anyone can spend someone else's quota, or
put a false address in the audit log. And websockets need the upgrade headers
passed through, or collaboration silently falls back to nothing:

```nginx
location /api/v1/ws/ {
    proxy_pass http://api:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header X-Forwarded-For $remote_addr;   # overwrite, not append
    proxy_read_timeout 3600s;   # an idle editor must outlive the default 60s
}
```

`proxy_read_timeout` is the one people miss. The client pings every 25 seconds,
which is inside nginx's default — but a proxy configured with a shorter idle
timeout will cut live editing sessions with no error anyone can see.

## More than one worker

Fine, **once Redis is configured**. Up to that point the collaboration hub is
in-process: two people on the same diagram who land on different workers cannot
see each other, and `/presence` reports only the room belonging to whichever
worker answered.

The app says which mode it came up in, so check the log rather than guessing:

```
INFO  [app.startup] Redis connected — collaboration fans out across workers
WARN  [app.startup] Redis unreachable — collaboration is single-worker only.
```

`tests/test_multiworker.py` runs four workers and asserts the fan-out; run it
against a candidate configuration if you are unsure.

## Migrations

They run on start-up. To run them by hand:

```bash
docker compose run --rm api uv run --no-dev --frozen alembic upgrade head
docker compose run --rm api uv run --no-dev --frozen alembic downgrade -1
```

Every migration is generated with `--autogenerate` and reviewed before it is
kept; `tests/test_schema.py` fails if the models and the migrations drift.

## Backups

Everything of value is in Postgres — the documents, their history, the
memberships. Redis holds only transient collaboration state and can be lost
without consequence.

```bash
docker compose exec -T db pg_dump -U intelliwealth prompt_studio | gzip > backup.sql.gz
gunzip -c backup.sql.gz | docker compose exec -T db psql -U intelliwealth prompt_studio
```

## Checking it afterwards

```bash
curl -s https://api.example.com/health
API=https://api.example.com uv run python scripts/smoke.py   # writes real rows
```

`/health` reports the process. `scripts/smoke.py` exercises sign-up, sharing,
concurrent saves, the admin surface and two live websockets — which is the
difference between "the process is up" and "the product works". It creates real
accounts and projects, so run it against staging, not production.

## What is exposed

| | |
|---|---|
| `/api/v1/**` | the API |
| `/api/v1/ws/projects/{id}` | the collaboration socket |
| `/admin` | the operator panel — a static page that talks to the API with the operator's own token |
| `/docs`, `/scalar`, `/openapi.json` | the API documentation |
| `/health` | liveness |

The docs and the admin page are served by the app itself, so they keep working
when the frontend is down — which is exactly when an operator needs them. If
that is not wanted in production, block `/docs`, `/scalar` and `/openapi.json`
at the proxy; `/admin` is safe to leave up, since it holds no secrets and every
call it makes is authorised server-side.
