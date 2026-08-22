# API reference

Base URL `/api/v1`. Interactive versions: [`/docs`](http://localhost:8010/docs)
(Swagger) and [`/scalar`](http://localhost:8010/scalar).

Every response has the same envelope:

```json
{ "success": true, "status_code": 200, "message": "OK", "data": null, "errors": [] }
```

`data` is the payload; `errors` carries the structured half of a failure.
Authenticate with `Authorization: Bearer <access_token>`.

---

## Auth — `/auth`

| | | |
|---|---|---|
| `POST` | `/register` | Create an account **and sign in**. Body `{email, password, full_name?, invite_token?}`. Returns tokens + `user`. An `invite_token` from a share link marks the address confirmed. |
| `POST` | `/login` | `{email, password}` → tokens + `user` + `must_change_password`. |
| `POST` | `/refresh` | `{refresh_token}` → a **new pair**. The old refresh token dies. |
| `POST` | `/logout` | `{refresh_token?}`. Revokes the access token immediately. Body optional. |
| `POST` | `/logout-everywhere` | Ends every session on the account. |
| `GET` | `/sessions` | Devices currently signed in. |
| `DELETE` | `/sessions/{id}` | End one of them. |
| `POST` | `/change-password` | `{current_password, new_password}`. |
| `POST` | `/set-initial-password` | `{new_password}` — replaces an issued password. The only route a flagged account may reach. |
| `POST` | `/forgot-password` | `{email}`. Same answer whether or not the account exists. Outside production the token comes back in `data.reset_token`. |
| `POST` | `/reset-password` | `{token, new_password}`. Single use; also ends every open session. |
| `POST` | `/verify-email` | `{token}`. |
| `POST` | `/resend-verification` | — |

Passwords: at least 8 characters, at most 72 **bytes** (bcrypt's real limit),
containing a letter and a digit. Eight consecutive failures lock the account for
fifteen minutes.

`/register`, `/login`, `/forgot-password` and `/resend-verification` are also
rate limited **per caller IP** — 20 sign-ins per 5 minutes, 10 registrations and
5 reset requests per hour by default. Over the line they answer `429` with a
message safe to show the user. Lockout guards one account; these guard against
spraying, registration floods and mail bombing a third party.

## Users — `/users`

| | | |
|---|---|---|
| `GET` | `/me` | The caller, plus `permissions[]` and `must_change_password`. |
| `PATCH` | `/me` | `{full_name?, avatar_url?, accent_color?, timezone?, theme?}`. Profile only — role and status are not editable here. |

## Projects — `/projects`

| | | |
|---|---|---|
| `GET` | `` | Your projects. `?page&size&search&status=active\|archived\|all&owned_only&sort=recent\|updated\|created\|name\|screens` |
| `POST` | `` | `{name, description?, doc?, schema_version?, member_emails?[]}` |
| `GET` | `/{id}` | The project **with its document**. |
| `PATCH` | `/{id}` | `{name?, description?, is_archived?}` — editor. |
| `PUT` | `/{id}/document` | `{doc, base_version?, schema_version?, label?}` — editor. See below. |
| `DELETE` | `/{id}` | Trash it — owner. Recoverable. |
| `POST` | `/{id}/restore` | Bring it back — owner. |
| `POST` | `/{id}/leave` | Remove yourself. |
| `GET` | `/{id}/presence` | Who has it open right now, across all workers. |

A document is capped at 8MB of serialised JSON — about twenty times the largest
realistic diagram. Over that the save is `422` naming the size.

### Saving a document

Send the `doc_version` you loaded as `base_version`. If the stored document has
moved on, the save is refused rather than silently winning:

```json
{ "success": false, "status_code": 409,
  "message": "Someone else saved a newer version of this project. Reload before saving",
  "errors": [{ "field": "base_version", "current_version": 8, "sent_version": 6 }] }
```

Omitting `base_version` is an explicit "write it anyway".

### Members — `/{id}/members`

| | | |
|---|---|---|
| `GET` | `` | Everyone on the project, pending invitations included. |
| `POST` | `` | `{email, role?}` — owner. The address needs no account. |
| `POST` | `/bulk` | `{emails[], role?}`. Addresses already on the project are skipped, not rejected. |
| `PATCH` | `/{member_id}` | `{role}` — owner. |
| `DELETE` | `/{member_id}` | — owner. |

Roles: `VIEWER` → `COMMENTER` → `EDITOR` → `OWNER`. A project always keeps at
least one owner; the step that would remove the last one returns 409.

### Versions, activity, comments

| | | |
|---|---|---|
| `GET`/`POST` | `/{id}/versions` | List, or take a named snapshot (`{label?}`). |
| `GET` | `/{id}/versions/{vid}` | One snapshot, with its document. |
| `POST` | `/{id}/versions/{vid}/restore` | Roll back. Snapshots the live document first, so this is itself undoable. |
| `GET` | `/{id}/activity` | Who did what, newest first. |
| `GET`/`POST` | `/{id}/comments` | `?include_resolved`. Post needs COMMENTER. |
| `PATCH`/`DELETE` | `/{id}/comments/{cid}` | `{body?, resolved?}`. Only the author may reword; anyone on the project may resolve. |

## Admin — `/admin`

Requires `ADMIN` or `SUPERADMIN`. **Project documents are never exposed here.**

| | | |
|---|---|---|
| `GET` | `/stats` | Platform counters. |
| `GET` | `/roles` | The role/permission matrix. |
| `GET` | `/users` | `?page&size&search&role&is_active&sort` |
| `POST` | `/users` | `{email, full_name?, role?, password?, is_active?, send_invite_email?}`. Omit `password` and one is generated: it comes back **once** in `data.temporary_password` and the account must replace it before it can use anything. |
| `PATCH` | `/users/{id}` | `{full_name?, role?, is_active?, reset_password?}`. Deactivating or re-issuing a password also ends that account's open sessions. |
| `DELETE` | `/users/{id}` | Superadmin only. |
| `GET` | `/projects` | Metadata only — `?search&include_deleted`. |
| `DELETE` | `/projects/{id}` | Superadmin only. |
| `GET` | `/audit` | `?action&actor_id&entity_type&entity_id`. `action` is a **prefix** match, so `action=auth` returns the whole namespace. |

## Roles — `/rbac/roles`

Both role vocabularies and the permission list, so a picker never hardcodes them.

---

## Collaboration — `WS /api/v1/ws/projects/{id}?token=<access token>`

**→ server**

| type | payload |
|---|---|
| `doc.update` | `{doc, base_version?, schema_version?}` — needs EDITOR |
| `cursor` / `selection` | `{payload}` — relayed, never stored |
| `ping` | — also refreshes your presence entry |

**← server**

| type | meaning |
|---|---|
| `hello` | `{connection_id, you, doc, doc_version, members}` — the greeting carries the document, so a joining editor needs no second request |
| `doc.updated` | **someone else** saved: `{doc, doc_version, by, at}` |
| `doc.ack` | **your** save landed: `{doc_version, ack: true}` — no document, deliberately |
| `doc.conflict` | your `base_version` was behind: `{doc, doc_version, sent_version}` |
| `presence` | `{members}` — the authoritative roster after every join and leave |
| `member.joined` / `member.left` | what changed |
| `pong`, `error` | — |

Render the roster from `presence`, not by accumulating the deltas — a client
that accumulates drifts the first time it misses one.

Close codes: `4400` malformed, `4401` not signed in, `4403` role too low,
`4404` no such project (or you are not on it).
