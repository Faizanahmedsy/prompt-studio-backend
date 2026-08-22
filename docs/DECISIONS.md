# Why it is built this way

The choices worth arguing about, and what the alternative would have cost.

---

## The project document is one JSON column, not a schema

`projects.doc` holds the entire `ProjectDoc` the editor works on — views,
screens, modules, edges, theme, per-surface stacks, conventions, requirements.

The frontend already owns that shape. `types/project.ts` defines it in zod, it
is versioned by `SCHEMA_VERSION`, and it is migrated client-side on load. The
editor reads and writes it whole; there is no operation in the product that
touches one screen without rewriting the document.

Shredding it into `screens`, `modules` and `edges` tables would buy queries
nobody makes, and cost a lockstep migration on both sides for every field the
editor gains. The columns beside `doc` are exactly the ones the *server* needs
to answer questions the JSON cannot: who may see it (`project_members`), what
changed (`project_activity`), whether the copy being saved is stale
(`doc_version`), and how big it is without parsing it (`screen_count`).

**The cost, stated plainly:** the server cannot query inside a diagram. "Find
every project using the `table-advanced` layout" is not an indexed question
today. When it becomes one, JSONB is already the column type, and a GIN index
plus a `jsonb_path_query` answers it without a schema change.

---

## Membership is by email address, not user id

A `project_members` row can exist with `user_id IS NULL` and
`status = INVITED`. Registering with that address claims it.

The alternative — a foreign key to `users` — makes an invitation impossible to
write until the invitee has signed up. The person doing the inviting knows an
email address and nothing else; asking them to wait for the other person to
register first inverts the whole flow.

The consequence is that "the same address" needs one definition, so
`users.service.normalise_email` lower-cases and trims, and every write and
lookup goes through it. Without that, `Faizan@x.com` and `faizan@x.com` are two
accounts fighting over one membership row.

---

## Platform admins cannot read projects

This is the decision most likely to be questioned, so: an `ADMIN` and a
`SUPERADMIN` can create users, change roles, read the audit trail and see that a
project exists — and cannot open one they were not added to.

The product's promise to a user is "the people I add can see this". An admin
override makes that promise false, and it would be false silently, which is
worse than not making it. Administering accounts and reading work are different
jobs, and the admin surface is built for the first one:
`admin.schemas.AdminProjectRow` has no `doc` field, and nothing in
`admin/service.py` selects it.

The one platform power that reaches inside a project is **deletion**. That is
destructive rather than revealing — it removes something without showing it —
and it is written to the audit log with the actor.

`tests/test_membership.py::test_a_platform_superadmin_still_cannot_open_a_project`
is the executable version of this paragraph. If someone ever needs the override,
it should arrive as a deliberate, audited, user-visible feature, not as a
convenience someone added to an existing endpoint.

---

## RBAC is a table in code, not four tables in the database

`app/modules/rbac/permissions.py` is a dict from role to a frozenset of
permission codes. There is no `roles` / `permissions` / `role_permissions` /
`user_roles` schema.

This product has three platform roles and a fixed matrix. A normalised RBAC
schema would add four tables, four joins on every authenticated request, and a
second source of truth that drifts from the code checking it — and the payoff,
runtime-editable roles, is not a feature anyone asked for. `GET /admin/roles`
serves the matrix so the admin panel renders the real thing rather than a copy.

Per-**project** standing is a genuinely different axis and lives where it
belongs: `project_members.role`.

If runtime-editable roles ever become a requirement, this file is the migration
target — one table seeded from this dict — and every call site already goes
through `role_has()`.

---

## Collaboration is last-write-wins, not CRDT or OT

The editor holds one Zustand document and rewrites it wholesale. There are no
character-level operations to transform, so operational transformation has
nothing to operate on, and a CRDT would mean reshaping the client's entire state
model to earn a guarantee this product does not need — two or three people on
one diagram, mostly working on different screens.

What it does instead: every save carries the `doc_version` it started from. A
save that is behind is refused with the current document attached, and the
client reconciles. Concurrent edits to *different* screens still lose one side's
change if both save from the same base — the honest description is
"conflict-detecting", not "conflict-free", and the 400ms client-side debounce
plus the live relay means the window is small enough that it is rare in practice.

If it stops being rare, the upgrade path is per-screen versioning before it is a
CRDT.

---

## Timestamps are generated in Python

`created_at` and `updated_at` have `default=now, onupdate=now` in Python **and**
a `server_default` as a backstop.

Not a style preference. A column whose value the database computes is marked
*expired* on the ORM object after the INSERT or UPDATE, because SQLAlchemy does
not know what the server put there. Reading it then triggers a lazy SELECT — and
in async code, a lazy SELECT from a plain function is a `MissingGreenlet` crash,
not a slow query. Every response that serialises `updated_at` immediately after
a save would hit it. This one cost an afternoon the first time.

---

## Redis is optional, up to a point

In-process fan-out is correct and complete for one worker: the room is a dict,
delivery is a loop, and there is no broker to fail. Requiring Redis there would
make the common case depend on a service it does not need.

Past one worker it is a requirement, not an accelerator — two people on
different workers cannot see each other without it, and `presence_snapshot`
falls back to the local view, which undercounts. Rather than pretend, `app.main`
logs which mode it came up in at start-up.

---

## The response envelope

Every response, success or failure, is
`{success, status_code, message, data, errors}`.

Two things follow. The client has one unwrapping function instead of a shape per
endpoint. And `errors` gives failures a *structured* half, which is what lets a
client branch instead of matching on an English sentence — a stale save carries
`current_version` and `sent_version`, so the reload prompt can say what happened.

The cost is that `data` is one level deeper than a plain REST body, and Swagger
shows the unwrapped shape rather than what actually goes over the wire. The
middleware skips `/docs` and `/openapi.json` for exactly that reason.

---

## Tests run on real Postgres

A throwaway database per session, created and dropped, rather than SQLite.

The schema uses JSONB, Postgres defaults, and index shapes SQLite does not have.
A SQLite suite would be faster and would pass while production did something
else, which is the one thing a test must not do.

The websocket tests go further and start a real uvicorn in a **subprocess**. Two
independent clients seeing each other is the entire feature; an in-thread server
would share this process's engine while running its own event loop, and asyncpg
connections are bound to the loop that opened them.

Teardown is `DELETE`, not `TRUNCATE` — TRUNCATE takes an ACCESS EXCLUSIVE lock
and forces an fsync, measured here at ~2.8s per test, which was slower than every
test in the file put together.
