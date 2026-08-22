# Prompt Studio API client (drop-in for the Next app)

> **Already applied.** The app in `../prompt-studio` has a copy of every file
> below. This folder is the canonical one — it sits next to the pydantic schemas
> it mirrors — and `tests/test_client_drift.py` fails if the two diverge, so a
> change here has to be carried across (and vice versa).

Hand-written TypeScript client for this backend, kept in the backend repo so it
stays next to the contracts it mirrors. Nothing here imports anything from the
Next app, so the files can be copied across as they are.

No new dependencies: `fetch`, `zustand` (already there) and `zod` (already
there). No axios, no TanStack Query.

## Where each file goes

| File here | Copy to | What it is |
| --- | --- | --- |
| `api/types.ts` | `lib/api/types.ts` | Every DTO the server sends or takes, `snake_case`, mirroring the pydantic schemas. |
| `api/client.ts` | `lib/api/client.ts` | The `fetch` wrapper: base URL, bearer token, envelope unwrapping, `ApiError`, single-flight 401 refresh + replay. |
| `api/token-store.ts` | `lib/api/token-store.ts` | Access/refresh tokens in `localStorage`, every access in try/catch. |
| `api/auth.ts` | `lib/api/auth.ts` | `/auth/*` and `/users/me`, one function per endpoint. |
| `api/projects.ts` | `lib/api/projects.ts` | `/projects/*`: documents, members, versions, activity, comments. |
| `api/admin.ts` | `lib/api/admin.ts` | `/admin/*` and `/rbac/roles`. |
| `stores/auth-store.ts` | `stores/use-auth-store.ts` | Zustand v5 session store: `user`, `status`, `login`/`register`/`logout`/`bootstrap`. |
| `collab/use-collaboration.ts` | `features/collab/use-collaboration.ts` | The websocket hook: presence, debounced saves, conflicts, backoff, keepalive. |
| `react-shim.d.ts` | **nowhere** | Typecheck-only stub so this folder compiles inside the backend repo, where `react`/`zustand`/`@types/node` are not installed. Do not copy it — it would shadow the real types. |

The relative imports inside these files (`./types`, `../api/client`) assume the
`api/`, `stores/` and `collab/` folders keep their relative positions. Moving
`stores/auth-store.ts` to `stores/use-auth-store.ts` alongside a `lib/api/`
folder means fixing two import paths — or switch them to the `@/` alias the app
already has.

## Env vars

```bash
# .env.local
NEXT_PUBLIC_API_URL=http://localhost:8010   # ORIGIN ONLY — the client appends /api/v1
NEXT_PUBLIC_WS_URL=ws://localhost:8010      # optional; defaults to API_URL with http→ws
```

In production these become `https://…` and `wss://…`. Both are `NEXT_PUBLIC_*`,
so they are inlined at build time and must be present when the app is *built*,
not only when it runs.

On the server side, `BACKEND_CORS_ORIGINS` must list the Next origin
(`http://localhost:3000`) or every request fails at preflight.

## Wire it up

### (a) Gate the app behind login

`bootstrap()` turns a stored token back into a user. `status` starts as
`"loading"` precisely so the app does not flash the login screen during that
round trip.

```tsx
// app/(app)/layout.tsx
"use client"

import { useEffect } from "react"
import { useRouter } from "next/navigation"
import { selectStatus, useAuthStore } from "@/stores/use-auth-store"

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter()
  const status = useAuthStore(selectStatus)
  const bootstrap = useAuthStore((s) => s.bootstrap)
  const mustChangePassword = useAuthStore((s) => s.user?.must_change_password === true)

  useEffect(() => {
    void bootstrap()
  }, [bootstrap])

  useEffect(() => {
    if (status === "anon") router.replace("/login")
    // An admin-issued password: every other route answers 403 until it is
    // replaced, so there is nothing to render but the set-password screen.
    else if (mustChangePassword) router.replace("/set-password")
  }, [status, mustChangePassword, router])

  if (status !== "authed" || mustChangePassword) return <FullPageSpinner />
  return <>{children}</>
}
```

The login form calls `useAuthStore.getState().login(email, password)` — it
stores the tokens and flips `status` itself. Sign-out is `logout()`, which
clears local state first and revokes server-side on a best-effort basis.

If the client's one refresh attempt fails mid-session, `setUnauthorizedHandler`
(registered at the bottom of `auth-store.ts`) drops the user to `"anon"` and the
effect above sends them to `/login`. Nothing else needs to watch for that.

### (b) Load a project into the existing doc store

The document the server stores **is** the app's `ProjectDoc`, verbatim and still
camelCase — the server treats it as opaque JSON. Everything *around* it is
`snake_case`. Parse the doc with zod at the boundary and never anywhere else:

```ts
import { getProject } from "@/lib/api/projects"
import { useProjectStore } from "@/stores/use-project-store"
import { SCHEMA_VERSION, projectDocSchema } from "@/types/project"

export async function openProject(serverId: string) {
  const detail = await getProject(serverId)
  const doc = projectDocSchema.parse(detail.doc) // migrations/defaults happen here

  useProjectStore.getState().importProject({
    ...doc,
    // The server's uuid becomes the local id, so the two never need a lookup
    // table. Projects created offline keep their `prj_…` id until first save.
    id: detail.id,
    createdAt: Date.parse(detail.created_at),
    updatedAt: Date.parse(detail.updated_at),
    schemaVersion: detail.schema_version || SCHEMA_VERSION,
    versions: [], // history is server-side now: GET /projects/{id}/versions
  })

  return detail.doc_version // keep this — it is what makes the next save safe
}
```

`detail.my_role` tells you what this user may do; `roleAtLeast(detail.my_role,
"EDITOR")` from `api/types.ts` is the check for "may save". A VIEWER's editor
should be read-only in the UI, not merely 403'd on save.

### (c) Save with `base_version`, and handle the 409

`doc_version` is the whole concurrency story. Send the version you loaded; if
the stored document has moved past it the save is refused rather than silently
overwriting someone else's work.

```ts
import { staleDocumentDetail } from "@/lib/api/client"
import { getProject, saveDocument } from "@/lib/api/projects"
import { SCHEMA_VERSION, projectDocSchema } from "@/types/project"

async function save(projectId: string, doc: ProjectDoc, baseVersion: number) {
  try {
    const summary = await saveDocument(projectId, {
      doc,
      base_version: baseVersion,
      schema_version: SCHEMA_VERSION,
    })
    return { ok: true as const, version: summary.doc_version }
  } catch (error) {
    const stale = staleDocumentDetail(error)
    if (!stale) throw error // a real failure: 403, 422, network

    // Somebody saved while this tab was editing. `stale.current_version` says
    // where the server is; fetch it and let the person choose.
    const fresh = await getProject(projectId)
    return {
      ok: false as const,
      serverDoc: projectDocSchema.parse(fresh.doc),
      serverVersion: fresh.doc_version,
    }
  }
}
```

Branch on `staleDocumentDetail(error)`, never on `error.message` — the message
is prose and will be reworded. Anything else is a plain `ApiError` with
`status`, `message` and `errors`; `error.fieldErrors()` turns a 422 into a
`{ field: message }` map.

Omitting `base_version` means "write it anyway". That is a real option (it is
what the collaboration socket uses after it has already reconciled) but it
should be a deliberate user choice — an "overwrite theirs" button — not the
default.

### (d) Live collaboration (optional, but it replaces most of (c))

```tsx
const { members, status, sendDoc, lastConflict, docVersion } = useCollaboration({
  projectId,
  onHello: (hello) => replaceDoc(projectDocSchema.parse(hello.doc), { silent: true }),
  onRemoteDoc: (doc) => replaceDoc(projectDocSchema.parse(doc), { silent: true }),
  onConflict: (c) => replaceDoc(projectDocSchema.parse(c.doc), { silent: true }),
})

// wherever the editor mutates the document
useEffect(() => {
  if (status === "open") sendDoc(doc as unknown as ProjectDocPayload)
}, [doc, status, sendDoc])
```

Three things worth knowing before you rely on it:

1. **Your own saves never come back.** The author of a save gets
   `{type:"doc.ack", doc_version}` — version only, no document — while everyone
   else gets `doc.updated` with the document. Two message types, so there is no
   flag to forget to check: `onRemoteDoc` fires only for somebody else's work.
   If you ever collapse them back into one type, you reintroduce the bug where a
   save echoes back and wipes the character typed while it was in flight.
2. **Saves are debounced (400ms) and last-write-wins.** The hook sends the
   newest document in the window with the version it last heard from the
   server. Two people dragging the same node do not merge — the later save wins
   whole-document. That is the server's model too, not a client shortcut.
3. **It saves for real.** A `doc.update` is persisted exactly like the REST
   save; snapshots are taken on a 90s timer rather than per message, and
   autosaves are deliberately kept out of the activity feed. With the socket
   open you do not also need the REST save on a timer — keep it for explicit
   "Save version" (`POST /projects/{id}/versions`) and as the offline fallback.

Presence is `members`: one entry per person, `connections` counting their open
tabs, and it includes you — filter on `you.user_id` for an avatar stack. It is
assigned from `hello` and from the `presence` frame the server sends after every
join and leave, and from nothing else. `onMemberJoined` / `onMemberLeft` are
there for a toast; do not rebuild a roster out of them, because a roster
accumulated from deltas drifts the first time one is missed and then quietly
shows a ghost for the rest of the session.

Cursors go out with `sendCursor(payload)` and arrive via `onCursor`; the server
attaches the sender's identity itself, so a client cannot draw a cursor labelled
as somebody else.

The socket reconnects with jittered exponential backoff capped at 15s, and pings
every 25s. The ping does double duty — it keeps a proxy from reaping an idle
socket, and it refreshes this tab's presence entry, which the server expires
after 120s. Raising `pingIntervalMs` past that makes a live editor disappear
from everyone else's avatar stack. Close code 4403/4404 (no access / no project)
stops it permanently; 4401 refreshes the token through the same single-flight
slot the REST client uses and retries once.

## Things the server does that will surprise you

- **Every JSON response is enveloped**: `{success, status_code, message, data,
  errors}`. The client returns `data` and throws on `success: false`, so you
  never see the wrapper. No exceptions — every route under `/api/v1` answers
  this way, including the deletes, which return 200 with `data: null`.
- **Refresh tokens rotate.** Using an old one after a refresh fails. This is why
  the client has exactly one in-flight refresh promise: ten parallel 401s must
  produce one refresh, not ten.
- **`must_change_password` accounts can log in and do nothing else** — every
  authenticated route except `/auth/set-initial-password` answers 403.
- **A member row's `id` is not a user id.** `MemberRead.id` identifies the
  membership; `MemberRead.user` is null until an invited address registers.
- **Platform role ≠ project role.** An ADMIN administers accounts; it grants
  nothing on anyone's project, and no admin route ever returns a document.
- **`GET /projects` filters with `?status=active|archived|all`**, defaulting to
  `active`. There is no `archived` boolean — a tri-state flag could not express
  "both" in a query string, which is the bug that produced this enum.
