# Working in this repo

Prompt Studio's backend. FastAPI, SQLAlchemy 2 async, PostgreSQL, Alembic, uv.
Read `README.md` first — it explains what the service is for. This file is about
how to change it.

## Rules that override everything else

- **Never run `git commit`, `git push`, `git add`, or any git write command.**
  The developer commits their own work, always. Leave the tree dirty and say
  what you changed.
- **Never edit anything outside this repository.** `../prompt-studio` is the
  frontend and `../backend` belongs to a different team.
- **Never write a migration by hand.** Change the models, then
  `uv run alembic revision --autogenerate -m "..."` and read the generated file
  before keeping it. `tests/test_schema.py` fails if the two drift.

## Commands

```bash
uv sync                                  # install
docker compose up -d db redis            # dependencies
uv run alembic upgrade head              # migrate
uv run python -m app.seed                # superadmin from .env, idempotent
uv run uvicorn app.main:app --reload --port 8010

uv run pytest                            # must be green before you stop
uv run ruff check . && uv run ruff format .
uv run mypy app scripts                  # strict; must be clean
```

Postgres is on **5442** and Redis on **6382** — 5432 and 6380 are taken by other
stacks on this machine.

## Where things go

`app/modules/<name>/` is `models.py` (rows) / `schemas.py` (shapes) /
`service.py` (rules) / `router.py` (routes). A router must not build a query; a
service must not know that HTTP exists. Anything two modules need lives in
`app/core/`.

A new module needs four edits: the four files, an import in `app/models.py` if
it has tables, and an `include_router` in `app/api/router.py`.

## Things that will bite you

- **`app/models.py` must import every ORM model.** Alembic compares
  `Base.metadata` to the database; a model nobody imported is a table it will
  offer to *drop*.
- **Timestamps are generated in Python, not by the database** (see
  `core/base_model.py`). A server-side default leaves the attribute expired
  after INSERT/UPDATE, and reading it from a plain function in async code is a
  `MissingGreenlet` crash rather than a lazy load. If you add a column with a
  server default, give it a Python default too.
- **Commit *after* building a response that writes rows.** Registration issues
  a refresh token by inserting a `sessions` row; committing before that step
  returned a token that looked valid and failed the first time it was used.
- **Use `JSONType` from `core/base_model.py`**, not `JSONB` directly — it is
  JSONB on Postgres and JSON elsewhere, which is what keeps the models portable.
- **New user-facing strings go in `core/messages.py`.** They are part of the API
  contract; a reworded sentence is a change the frontend can see.
- **Errors are `AppError` subclasses**, never `HTTPException`. The handlers in
  `core/exceptions.py` put them in the envelope; a raw `HTTPException` escapes
  the shape every client depends on.
- **Set the success message with `set_response_message(request, ...)`**, not by
  returning a message in the payload.

## Two more traps

- **Never parametrise `Page[T]` with an ORM class.** `Page` is a pydantic model,
  and subscripting one builds a schema for the parameter — which fails for a
  SQLAlchemy class. Python 3.14 evaluates the annotation lazily so it appears to
  work; 3.13 raises at import. Services return `PageResult[T]` (a dataclass);
  routers build `Page[Schema]`. This shipped once and only died in the
  container, which is why `.python-version` pins 3.13 to match the image.
- **New unauthenticated routes need a rate limit.**
  `dependencies=[limit(...)]` from `core/rate_limit.py`.
  `tests/test_rate_limit.py::test_the_auth_routes_carry_a_limit` walks the route
  table and fails if one is missing.

## The rule the product rests on

A project is visible to the email addresses on it, and to nobody else —
including platform admins. `app/modules/projects/access.py` is the only place
that decides this, and the admin surface must never expose `projects.doc`. If a
change would let someone read a project they were not added to, it is wrong even
if a ticket asked for it. `tests/test_membership.py` is the spec.

## Security invariants — do not weaken these without saying so

Each has a regression test. If one starts failing, the fix is the code, not the
test.

- A platform ADMIN may not manage another ADMIN or a SUPERADMIN
  (`_assert_may_manage`). Re-issuing a password hands over a live credential.
- Ending a session must revoke its `access_jti`, not only the refresh token.
- Any route that takes project access away must call `_evict_open_sockets` —
  membership is resolved once, at the websocket handshake.
- `save_doc` and `restore_version` must hold the row lock from `_lock()` before
  reading `doc_version`.
- `authenticate_websocket` must keep the `must_change_password` check; the HTTP
  wall does not cover sockets.
- The socket's `REVALIDATE_SECONDS` loop must stay. It is the backstop for every
  revocation path nobody wired an eviction into, and it is why the window is
  thirty seconds rather than "until the tab closes".
- Anything that ends a session must go through `revoke_all_sessions` or
  `_revoke_session_access`. Setting `revoked_at` alone leaves the access token
  live for its full hour — that mistake was made four separate times.
- Anything that creates an account must call `claim_invites`. `user_id` on
  `project_members` is the key every eviction path uses; a NULL there is an
  invisible failure, not an error.

## Tests

Real Postgres, created and dropped per session. Websocket tests run a real
uvicorn in a subprocess, because two clients seeing each other is the whole
feature. Write the test that would have caught the bug, not the test that
exercises the line you changed.
