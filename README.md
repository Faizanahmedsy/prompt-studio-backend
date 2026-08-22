# Prompt Studio API

The backend for [Prompt Studio](../prompt-studio) — the tool that turns editable
flow diagrams into build prompts. It adds the three things a browser-only app
cannot do for itself: **accounts**, **projects shared with named people**, and
**two people editing the same diagram at once**.

FastAPI · SQLAlchemy 2 (async) · PostgreSQL 16 · Alembic · Redis · uv.
The layout and conventions follow the IntelliWealth backend in the sibling
directory, so anyone who has worked in that repo already knows where things are.

---

## The one rule everything else is built around

> **A project is visible to the email addresses added to it. Nobody else.**

Not other users. Not a platform ADMIN. Not a SUPERADMIN. Administering
*accounts* is a different job from being able to read what people wrote, and the
admin surface never exposes `projects.doc` — it answers "does this project
exist, how big is it, who owns it", which is what operating a platform actually
needs. The single platform power that reaches into a project is **deletion**,
which is destructive rather than revealing, and it is audited.

`tests/test_membership.py` is the executable version of that paragraph.

---

## Run it

You need Docker and [uv](https://docs.astral.sh/uv/). Nothing else.

```bash
cp .env.example .env          # then set SECRET_KEY and the passwords
docker compose up -d db redis # Postgres on 5442, Redis on 6382
uv sync
uv run alembic upgrade head
uv run python -m app.seed     # creates the superadmin from .env
uv run uvicorn app.main:app --reload --port 8010
```

| | |
|---|---|
| API | <http://localhost:8010/api/v1> |
| Swagger | <http://localhost:8010/docs> |
| Scalar | <http://localhost:8010/scalar> |
| **Admin panel** | <http://localhost:8010/admin> |
| Health | <http://localhost:8010/health> |

Or run the whole stack in Docker:

```bash
docker compose up -d          # db + redis + api, migrated and seeded on start
```

### Ports

`5442` and `6382` rather than the usual `5432`/`6379`: this machine already runs
a system Postgres on 5432 and the IntelliWealth stack claims 5433 and 6380.
Both are published on `127.0.0.1` only — without the explicit host IP Docker
publishes on every interface *and* writes its own iptables rule, so the database
would be reachable from the internet regardless of the host firewall.

---

## Layout

```
app/
├── main.py                 app factory, middleware, lifespan
├── models.py               imports every ORM model exactly once (Alembic reads this)
├── api/router.py           every HTTP route, assembled
├── core/                   things every module needs and no module owns
│   ├── config.py           Settings (pydantic-settings), the only source of the DB URL
│   ├── database.py         async engine + session dependency
│   ├── base_model.py       Base, UUIDMixin, TimestampMixin, AuditMixin, JSONType
│   ├── security.py         hashing, JWTs, password rules
│   ├── envelope.py         the uniform success envelope
│   ├── exceptions.py       AppError family + handlers (the failure half)
│   ├── pagination.py       Page[T] and paginate()
│   ├── messages.py         every user-facing string
│   ├── constants.py        roles, token types, the websocket vocabulary
│   ├── mailer.py           console or SMTP; never raises into a request
│   └── redis.py            optional — absent means single-worker
└── modules/
    ├── auth/               register, sign in, refresh, sessions, passwords
    ├── users/              identity, profile, /users/me
    ├── rbac/               permissions.py — the role matrix, as data
    ├── projects/           projects, members, documents, versions, comments
    │   └── access.py       who may open a project, and with what standing
    ├── collab/             the websocket and the fan-out hub
    ├── admin/              user management, platform stats, metadata-only projects
    └── audit/              the security trail
```

Each module is `models.py` / `schemas.py` / `service.py` / `router.py`: rows,
shapes, rules, routes. A router never queries; a service never knows about HTTP.

---

## The response envelope

Every response — success or failure — has the same five keys:

```json
{ "success": true, "status_code": 200, "message": "Project saved", "data": {}, "errors": [] }
```

`data` is the payload. `errors` carries the structured half of a failure, which
is what lets a client *branch* instead of matching on the message. A stale save,
for instance:

```json
{ "success": false, "status_code": 409,
  "message": "Someone else saved a newer version of this project. Reload before saving",
  "data": null,
  "errors": [{ "field": "base_version", "current_version": 8, "sent_version": 6 }] }
```

---

## Two kinds of role, and they do not mix

**Platform role** (`users.role`) decides who administers accounts.

| | MEMBER | ADMIN | SUPERADMIN |
|---|:-:|:-:|:-:|
| use the product | ✓ | ✓ | ✓ |
| read users, stats, audit | | ✓ | ✓ |
| create / edit users | | ✓ | ✓ |
| grant admin roles | | | ✓ |
| delete users and projects | | | ✓ |

**Project role** (`project_members.role`) decides what you may do on one diagram.

| | VIEWER | COMMENTER | EDITOR | OWNER |
|---|:-:|:-:|:-:|:-:|
| open and read | ✓ | ✓ | ✓ | ✓ |
| comment | | ✓ | ✓ | ✓ |
| save the document | | | ✓ | ✓ |
| invite, change roles, delete | | | | ✓ |

Holding a platform role grants **nothing** on any project. The matrix lives in
`app/modules/rbac/permissions.py` as data, and `GET /api/v1/admin/roles` serves
it — so the admin panel renders the real thing rather than a second copy that
drifts.

---

## Sharing by email

The invited address does not need an account. That is the design, not a
shortcut: the row is written with `user_id = NULL` and `status = INVITED`, and
registering with that address claims every such row
(`projects.service.claim_invites`). Storing only a user id would mean an
invitation could not be written until the invitee had signed up — and the person
doing the inviting would have no way to say "let this address in".

Addresses are lower-cased on the way in, so an invitation to `Faizan@X.com` is
found by an account created as `faizan@x.com`.

---

## Live collaboration

`WS /api/v1/ws/projects/{id}?token=<access token>`

The token is a query parameter because a browser cannot attach an
`Authorization` header to a websocket handshake. That means it can land in
access logs, which is why access tokens are short-lived and why a refresh token
is never accepted here.

Client → server: `doc.update`, `cursor`, `selection`, `ping`.
Server → client: `hello`, `doc.updated`, `doc.ack`, `doc.conflict`, `presence`,
`member.joined`, `member.left`, `pong`, `error`.

- `hello` carries the current document and the roster, so a joining editor is up
  to date without a second request that could race an edit arriving in between.
- `doc.updated` is always **someone else's** save and always carries the
  document. Your own save comes back as `doc.ack` with the authoritative version
  and *no* document — echoing it would clobber whatever you typed while the
  round trip was in flight.
- `presence` is the authoritative roster after every join and leave;
  `member.joined` / `member.left` say what changed. Render from `presence`: a
  client that only accumulates deltas drifts the first time it misses one.
- Concurrency is last-write-wins over the whole document, guarded by
  `doc_version`. That is the honest match for an editor that holds one Zustand
  document and rewrites it wholesale — there are no character-level operations
  to transform. A save whose `base_version` is behind gets `doc.conflict` with
  the current document attached.

**Redis is optional.** With one worker, in-process fan-out is already correct.
With several, each process publishes what it broadcasts and replays what it
receives, and presence lives in a Redis hash so `GET /projects/{id}/presence` is
right no matter which worker answers it. Without Redis the app logs once and
stays local.

---

## Tests

```bash
uv run pytest              # 134 tests, ~23s
uv run ruff check .
uv run mypy app scripts
```

They run against a **real Postgres database**, created and dropped per session,
not SQLite — the schema uses JSONB and Postgres defaults, and a SQLite suite
would pass while production did something else. The websocket tests start a real
uvicorn in a subprocess, because the feature only means anything with two
independent clients seeing each other.

`tests/test_multiworker.py` goes further still and starts **four** workers behind
one port. With a single process the in-memory hub is already correct, so every
bug in the Redis fan-out and the shared presence hash would be invisible.

`tests/test_schema.py` upgrades a scratch database through the migrations and
diffs the result against the models, so a column added without a migration fails
here rather than on a deploy.

There is also `scripts/smoke.py` — an end-to-end check against a *running*
server, for answering "is what I just deployed actually working":

```bash
API=https://api.example.com uv run python scripts/smoke.py
```

It writes real rows, so point it at a throwaway environment.

---

## What stops abuse

Two layers, because they defend different things.

**Account lockout** (`MAX_FAILED_LOGINS`, `LOCKOUT_MINUTES`) protects one
account from being guessed. The lock is short and self-clearing, so someone who
mistyped is not stranded, and a password reset clears it — otherwise the
recovery path leaves you locked out of the account you just recovered.

**Per-IP quotas** (`RATE_LIMIT_*`) protect everything lockout cannot see:
spraying one common password across thousands of addresses, which never trips
any single account's counter; registration floods, which cost a bcrypt hash
each; and reset floods, where the thing being attacked is somebody else's inbox.
Redis-backed, so four workers share one counter instead of allowing four times
the quota. With Redis absent it allows everything — a cache outage must not
become a sign-in outage.

There is also a ceiling on one project document (8MB of JSON, roughly twenty
times the largest honest diagram), so a single request cannot push the server
into swap.

---

## Security notes

Things that are true because they were made true, and would not be by default:

- **A platform ADMIN cannot manage another admin account.** Re-issuing a
  password hands over a live credential, so only a superadmin may aim one at a
  privileged account (`admin/service.py::_assert_may_manage`). Without that
  check the ADMIN/SUPERADMIN split was decorative — an ADMIN could reset a
  superadmin's password, read the plaintext from the response, and take the
  platform.
- **Revoking a session ends its access token**, not just its refresh token. The
  session row records the `jti` of the access token it minted, so "sign out this
  device", "sign out everywhere" and an admin password re-issue all take effect
  at once rather than an hour later.
- **Removing or demoting a member closes their open editor socket** — across
  workers, over the same Redis bus collaboration uses. Membership is resolved at
  the handshake, so without this the fan-out kept sending a removed member the
  whole document while the REST API correctly answered 404.
- **The issued-password wall covers the websocket too.** It lives in the HTTP
  dependency, which a socket never passes through; the one route that reads and
  writes the document was the one route it did not cover.
- **Saves take a row lock.** The version check and the bump are one step, or two
  saves that both read version N both write N+1 and one document is lost with no
  conflict raised.
- **Completing a password reset invalidates every other outstanding link**, not
  only the one used.
- **Mail is never printed to the log as a fallback.** A half-configured SMTP
  setup fails loudly instead of putting reset links and issued passwords into
  `docker logs`.
- **An open websocket re-checks itself every 30 seconds.** A socket
  authenticates once, at the handshake, and then lives as long as the tab does.
  Immediate eviction is a *push* — and a push only reaches the paths somebody
  remembered to wire up. The tick is the pull: three indexed queries per socket
  per half minute, which bounds every revocation, including the ones nobody
  thought of. Writes are refused at once rather than waiting for it.
- **A password reset, a refresh, and choosing your own password all retire the
  tokens they replace.** Each was a separate hole: marking a session row revoked
  does nothing to the access token it minted.

Most of these came out of an adversarial audit of this repo; each has a
regression test named after the failure rather than the function.

---

## Configuration

Everything is in `.env` (`.env.example` is the template). The ones that matter:

| Variable | Why |
|---|---|
| `SECRET_KEY` | signs every token. Change it and everyone is signed out |
| `POSTGRES_*`, `DB_HOST_PORT` | the database |
| `REDIS_*` | optional; only for multi-worker collaboration |
| `BACKEND_CORS_ORIGINS` | JSON array or comma list. Empty means the frontend is blocked |
| `EMAIL_TRANSPORT` | `console` logs the mail (with the link) instead of sending |
| `SUPERADMIN_*` | the account `python -m app.seed` creates, once |
| `BCRYPT_ROUNDS` | 12 in production; the suite drops it to 4 |
| `MAX_FAILED_LOGINS`, `LOCKOUT_MINUTES` | per-account guessing throttle |
| `RATE_LIMIT_*` | per-IP quotas, `"<requests>/<seconds>"`. Set `RATE_LIMIT_ENABLED=false` behind a shared NAT |

`scripts/check_env.py` runs before migrations in the container and refuses to
start on a production config with a development secret, `APP_DEBUG` on, or no
CORS origins.

---

## Connecting the frontend

`docs/frontend/` holds the TypeScript client — typed DTOs, a fetch wrapper with
single-flight token refresh, a Zustand auth store, and the collaboration hook.
`docs/frontend/README.md` says where each file goes.

It is already applied: the app in `../prompt-studio` has a copy, and
`tests/test_client_drift.py` fails if the two diverge. This repo's copy is the
canonical one — it lives next to the pydantic schemas it mirrors.
