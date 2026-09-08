# Prompt Studio API — knowledge transfer

Read this once and you can pick the service up cold. It describes what is on
disk and working, not a plan. `CLAUDE.md` is the short version and the rules
that override everything; this is the map and the reasoning.

Last verified: 8 September 2026, against `sprint-2` at `818a499`. Working tree
clean.

---

## 1. What this service is for

The backend for [`../prompt-studio`](../prompt-studio), a visual editor that
turns flow diagrams into build prompts for coding agents. The editor is
local-first and works with no network at all. This service adds the three
things a browser cannot do for itself:

1. **Accounts** — so a diagram survives a new laptop.
2. **Projects shared with named people** — by email address, including
   addresses that have no account yet.
3. **Two people editing the same diagram at once** — over a websocket.

It stores the diagram as one opaque JSON column. **This service does not
understand the document.** It does not parse it, validate its shape, or know
what a screen is. That is deliberate: the editor's schema changes every sprint
and the API should not need a migration each time. If you find yourself
reaching into `projects.doc` to read a field, stop and think again.

FastAPI · SQLAlchemy 2 (async) · PostgreSQL 16 · Alembic · Redis · uv.

---

## 2. The one rule everything else is built around

> **A project is visible to the email addresses added to it. Nobody else.**

Not other users. Not a platform ADMIN. Not a SUPERADMIN.

Administering *accounts* is a different job from being able to read what people
wrote. The admin surface answers "does this project exist, how big is it, who
owns it" — which is what operating a platform actually needs — and never
exposes `projects.doc`. The single platform power that reaches into a project
is **deletion**, which is destructive rather than revealing, and it is audited.

`app/modules/projects/access.py` is the only place that decides this.
`tests/test_membership.py` is the executable version of the paragraph above.

**If a change would let someone read a project they were not added to, it is
wrong even if a ticket asked for it.**

---

## 3. Running it

You need Docker and [uv](https://docs.astral.sh/uv/). Nothing else.

```bash
cp .env.example .env           # set SECRET_KEY and the passwords
docker compose up -d db redis  # Postgres 5442, Redis 6382
uv sync
uv run alembic upgrade head
uv run python -m app.seed      # superadmin from .env, idempotent
uv run uvicorn app.main:app --reload --port 8010
```

`make help` lists shortcuts. `make check` is lint + types + tests, which is
what CI runs. `make reset` destroys the database and rebuilds it.

**Ports are non-standard on purpose.** 5432 and 6380 are taken by other stacks
on this machine, so Postgres is on **5442** and Redis on **6382**. The API is
on **8010**.

### The Docker trap

Use the `default` Docker context. The `api` service **bakes the source into the
image** — it does not mount it — so after changing code you must
`docker compose up -d --build api`. A plain `restart` re-runs the old image and
you will debug a fix that was never deployed.

---

## 4. Layout

```
app/
  main.py          app assembly, lifespan, middleware, socket mount
  models.py        imports every ORM model exactly once
  api/router.py    every HTTP route
  core/            anything two modules need
  modules/<name>/  models.py · schemas.py · service.py · router.py
  seed/            the superadmin
alembic/versions/  4 migrations
tests/             19 files, 203 tests, real Postgres
docs/              API.md · DECISIONS.md · DEPLOYMENT.md · DEPLOY-FREE.md · frontend/
scripts/           start.sh (Render entrypoint), smoke.py
```

**The layering rule: a router must not build a query; a service must not know
that HTTP exists.** Services take and return domain objects and raise
`AppError` subclasses. Routers translate.

A new module needs four edits: the four files, an import in `app/models.py` if
it has tables, and an `include_router` in `app/api/router.py`.

### Modules

| Module | Owns |
|---|---|
| `auth` | Register, login, refresh, logout, password reset, email verification. `Session`, `RevokedToken`. |
| `users` | The account. `User`, `UserCredentials` (split so a password hash is never loaded with a user by accident). |
| `rbac` | Platform roles — SUPERADMIN, ADMIN, USER. Unrelated to project roles. |
| `projects` | The product. Projects, members, versions, activity, comments, public links, access resolution. |
| `collab` | The websocket hub. Not mounted on `api_router` — see below. |
| `admin` | Platform operations. Metadata only, never `doc`. |
| `audit` | `AuditLog`, written through `core/audit_context.py`. |

The websocket is mounted separately in `app.main`, not on `api_router`. That
router carries a request-scoped dependency taking a `Request`, and FastAPI
hands a websocket route a `WebSocket` instead — the dependency would never be
filled and the socket would silently fail to open.

---

## 5. The data model

Eleven tables. `Project.doc` is the whole editor document as JSONB.

- **`Project`** — name, `doc`, `doc_version`, `updated_by`, `is_archived`,
  `public_token`.
- **`ProjectMember`** — the sharing table. Carries **either** a `user_id`
  **or** an email with a null `user_id` (`MemberStatus.INVITED`). Registering
  with that address claims the row. Roles: `OWNER` > `EDITOR` > `COMMENTER` >
  `VIEWER`, ordered so a check reads "at least EDITOR" rather than enumerating.
- **`ProjectVersion`** — history. `created_by` is `project.updated_by or
  user.id`, because a snapshot records who wrote the content, not who triggered
  the save.
- **`ProjectActivity`** — "who changed what", coalesced into sessions of
  `EDIT_SESSION_MINUTES = 10` so a person editing for an hour is one row, not
  four hundred.
- **`ProjectComment`** — rows exist; **there is no comment UI**. The
  `COMMENTER` role is offered in the frontend and does nothing yet.
- **`Session` / `RevokedToken`** — one row per sign-in; revocation.
- **`AuditLog`**, **`User`**, **`UserCredentials`**.

`ProjectRole` and the platform role are **unrelated**. A SUPERADMIN with no
membership sees nothing.

---

## 6. Concurrency — the part that is easy to break

Two clients edit one document. The scheme is optimistic:

- Every project row has `doc_version`.
- A write sends `base_version`. If it does not match, the server refuses and
  returns the newer document; the client snapshots locally before replacing.
- **`save_doc` and `restore_version` must hold the row lock from `_lock()`
  before reading `doc_version`.** Reading it outside the lock is a lost update
  that looks like a colleague's work vanishing.

`tests/test_concurrency.py` and `tests/test_multiworker.py` cover this. The
multiworker one runs a real uvicorn in a subprocess, because two clients seeing
each other is the whole feature and a TestClient cannot prove it.

---

## 7. Security invariants

Each has a regression test. **If one starts failing, the fix is the code, not
the test.**

- A platform ADMIN may not manage another ADMIN or a SUPERADMIN
  (`_assert_may_manage`). Re-issuing a password hands over a live credential.
- Ending a session must revoke its `access_jti`, not only the refresh token.
  Setting `revoked_at` alone leaves the access token live for its full hour —
  **that mistake was made four separate times.** Go through
  `revoke_all_sessions` or `_revoke_session_access`.
- Any route that takes project access away must call `_evict_open_sockets`.
  Membership is resolved once, at the handshake.
- `authenticate_websocket` must keep the `must_change_password` check. The HTTP
  wall does not cover sockets.
- The socket's `REVALIDATE_SECONDS` loop must stay. It is the backstop for
  every revocation path nobody wired an eviction into, and it is why the
  exposure window is thirty seconds rather than "until the tab closes".
- Anything that creates an account must call `claim_invites`. A NULL `user_id`
  on `project_members` is an invisible failure, not an error.
- **Membership matching uses the email half only when the address is
  verified.** Without that, registering with someone else's invited address was
  a project takeover. `invite_member` binds `user_id` only for verified
  accounts, and `verify_email` claims invites on confirmation.
- The password-reset token is returned in the response body **only when
  `ENVIRONMENT == "test"`**. It used to be returned in any non-production
  environment, which included staging.
- `public_token` is serialized for `OWNER` only. A VIEWER could read it and
  reshare the project.
- `update_project` rejects `is_archived` from non-owners — archiving killed
  public links, so an EDITOR could take a project offline.

Every unauthenticated route needs `dependencies=[limit(...)]` from
`core/rate_limit.py`. `test_rate_limit.py::test_the_auth_routes_carry_a_limit`
walks the route table and fails if one is missing.

---

## 8. Traps that have bitten people

- **`app/models.py` must import every ORM model.** Alembic compares
  `Base.metadata` to the database; a model nobody imported is a table it will
  offer to **drop**.
- **Never write a migration by hand.** Change the models, then
  `uv run alembic revision --autogenerate -m "…"`, then *read the generated
  file*. `tests/test_schema.py` fails if models and migrations drift.
- **Timestamps are generated in Python, not by the database**
  (`core/base_model.py`). A server-side default leaves the attribute expired
  after INSERT/UPDATE, and reading it from a plain function in async code is a
  `MissingGreenlet` crash rather than a lazy load. A new column with a server
  default needs a Python default too.
- **Commit *after* building a response that writes rows.** Registration issues
  a refresh token by inserting a `sessions` row; committing before that step
  returned a token that looked valid and failed on first use.
- **Use `JSONType` from `core/base_model.py`**, not `JSONB` directly. It is
  JSONB on Postgres and JSON elsewhere, which keeps the models portable.
- **Never parametrise `Page[T]` with an ORM class.** `Page` is a pydantic
  model; subscripting one builds a schema for the parameter, which fails for a
  SQLAlchemy class. Python 3.14 evaluates the annotation lazily so it appears
  to work; 3.13 raises at import. Services return `PageResult[T]` (a
  dataclass); routers build `Page[Schema]`. This shipped once and only died in
  the container, which is why `.python-version` pins 3.13 to match the image.
- **New user-facing strings go in `core/messages.py`.** They are part of the
  API contract; a reworded sentence is a change the frontend can see.
- **Errors are `AppError` subclasses, never `HTTPException`.** The handlers in
  `core/exceptions.py` put them in the envelope; a raw `HTTPException` escapes
  the shape every client depends on.
- **Set the success message with `set_response_message(request, …)`**, not by
  returning a message in the payload.

---

## 9. Tests

```bash
uv run pytest          # 203 tests across 19 files
make check             # ruff + mypy strict + pytest
```

Real Postgres, created and dropped per session. Websocket tests run a real
uvicorn subprocess.

`tests/conftest.py`'s `register(..., verify: bool = True)` confirms the address
using `create_verify_token`. When email verification became load-bearing, 193
tests broke at once; the fixture verifying by default is what fixed them.
`test_email_verification` passes `verify=False` deliberately.

**`tests/test_client_drift.py`** compares `docs/frontend/collab/use-collaboration.ts`
— a copy of the frontend's collab client kept in this repo — against the real
one. If you change the socket protocol on either side, sync that file. Its
imports differ (`@/lib/api/` → `../api/`).

Write the test that would have caught the bug, not the test that exercises the
line you changed.

---

## 10. Deployment

Render, free plan, Docker runtime, `scripts/start.sh` (migrate → seed → serve).
`render.yaml` is the blueprint and states `branch: main`.

**The live service was created by hand and therefore ignores `render.yaml`.**
Its dashboard still deploys `feat/backend` while everything merges into `main`,
so a finished sprint deploys nothing and the repo gives no clue why. Someone
with dashboard access must change **Settings → Build & Deploy → Branch** to
`main`. This has been reported repeatedly and is still open.

The database is deliberately **not** declared in the blueprint. Render's free
Postgres is deleted 30 days after creation — a data-loss trap dressed as a free
tier. Point `DATABASE_URL_OVERRIDE` at Neon instead. The service runs in
**Ohio** to sit beside a Neon project in AWS `us-east-2`; every query crosses
that gap.

`REDIS_URL` is empty on the free plan. Collaboration then fans out in-process,
which is correct for the single worker that plan runs — see `app/core/redis.py`.
Set it if a second worker ever appears, or two people on different workers will
not see each other.

Branches: **`sprint-2` → `develop` → `main`**, all kept identical. Push by
refspec:

```bash
git push origin sprint-2
git push origin sprint-2:develop
git push origin sprint-2:main
```

Commit as `faizanahmed.s@devstree.in`.

---

## 11. Known issues nobody has fixed

Reported and acknowledged, not scheduled:

- HTTP saves and version restores do not notify open sockets, so a save made
  over HTTP is invisible to a collaborator until they reload.
- `restore_version` takes no `base_version` — a restore can silently clobber a
  concurrent edit.
- `X-Forwarded-For` is trusted at the first hop, so rate limits can be evaded
  behind a proxy that does not strip it.
- `/auth/register` enumerates accounts: the error differs for a taken address.
- A platform ADMIN can reset a member's password and then sign in as them.
  Auditing records it; nothing prevents it.
- Redis presence can ghost. The TTL is key-level, so a hard-closed tab lingers.
- The `COMMENTER` role is offered with no comment UI behind it, though
  `ProjectComment` rows exist.
- Version-history deletion is local-only in the frontend; the server row stays.

---

## 12. Working agreements

- **Never run `git commit`, `git push` or `git add` unless asked.** The
  developer commits their own work. Leave the tree dirty and say what changed.
  The prompt-studio repositories are the one place they do ask for
  commit / push / deploy — but wait to be asked.
- **Never edit anything outside this repository.** `../prompt-studio` is the
  frontend and `../backend` belongs to a different team.
- Run `make check` before claiming anything is done, and report failures with
  the output rather than describing them.
- When handing work to a subagent, paste the commit-authorship rule and the
  no-hand-written-migrations rule into its prompt. Subagents do not inherit
  `CLAUDE.md`.
