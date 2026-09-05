/**
 * `/projects/{id}/discovery/*` — the question tree weaver writes and the studio
 * answers, plus the artifacts it renders.
 *
 * The types live here rather than in `./types.ts` on purpose: that file is
 * paired with the backend's schema by a drift test, and discovery is written by
 * an agent against these routes, not by the editor against the project
 * document. Keeping them apart means a discovery field added tomorrow does not
 * read as the project API having changed.
 *
 * Everything is snake_case, exactly as the server sends it. Renaming to camel
 * on the way in buys nothing and costs a mapping layer that goes stale.
 */

import { get, post, put } from "./client"

/** Every id here is a server id — the remote project id, not the local one. */
export type Uuid = string

export type ItemFamily = "RULE" | "QUESTION" | "ISSUE" | "M"
export type Severity = "high" | "med" | "low"

export type ModuleProgress = { total: number; answered: number }

export type RunProgress = {
  total: number
  answered: number
  needs_user: number
  needs_user_answered: number
  modules: Record<string, ModuleProgress>
}

/** What `GET /runs` returns: no progress, no `done` — those cost a count query
 *  each and the list route does not pay for them. */
export type RunSummary = {
  id: Uuid
  label: string
  source: string
  created_at: string
  updated_at: string
}

/** `GET /runs/{id}`, and the 201 from creating one. */
export type RunDetail = RunSummary & {
  progress: RunProgress
  /** Every item answered. The gate, and the only thing that closes a run. */
  done: boolean
}

export type ItemOption = {
  key: string
  label: string
  consequence: string
  /** What answering with this option records as the decision. */
  decision: string
}

/** The current decision on an item — the newest of however many were written.
 *  It has no id of its own: the item is what you address. */
export type ItemAnswer = {
  decision: string
  choice_key: string | null
  note: string
  created_at: string
}

export type DiscoveryItem = {
  id: Uuid
  key: string
  family: ItemFamily
  kind: string
  severity: Severity | null
  title: string
  body: string
  modules: string[]
  options: ItemOption[]
  proposed: string | null
  proposed_key: string | null
  needs_user: boolean
  depends_on: string[]
  position: number
  answer: ItemAnswer | null
}

export type AnswerCreate = {
  decision: string
  choice_key?: string
  note?: string
}

export type BulkAnswerResult = { accepted: number; skipped: string[] }

export type ArtifactSummary = {
  id: Uuid
  name: string
  kind: string
  sha: string
  updated_at: string
}

/** The same row with its body. Listing deliberately omits it. */
export type Artifact = ArtifactSummary & { body: string }

export type ItemQuery = {
  module?: string
  family?: ItemFamily
  needs_user?: boolean
  unanswered?: boolean
}

const base = (projectId: Uuid) => `/projects/${projectId}/discovery`

/** Newest first — the studio only ever opens `[0]`. */
export function listRuns(projectId: Uuid): Promise<RunSummary[]> {
  return get<RunSummary[]>(`${base(projectId)}/runs`)
}

export function getRun(projectId: Uuid, runId: Uuid): Promise<RunDetail> {
  return get<RunDetail>(`${base(projectId)}/runs/${runId}`)
}

/** Unpaginated by design — a run is a few hundred rows and the tree needs all
 *  of them to work out which questions depend on which. */
export function listItems(
  projectId: Uuid,
  runId: Uuid,
  query: ItemQuery = {}
): Promise<DiscoveryItem[]> {
  return get<DiscoveryItem[]>(`${base(projectId)}/runs/${runId}/items`, { ...query })
}

/** Answers with the whole updated item, not just the answer — so the caller
 *  reconciles from `item.answer` and cannot drift from the server's view. */
export function answerItem(
  projectId: Uuid,
  runId: Uuid,
  itemId: Uuid,
  body: AnswerCreate
): Promise<DiscoveryItem> {
  return post<DiscoveryItem>(`${base(projectId)}/runs/${runId}/items/${itemId}/answer`, body)
}

/** Accept each item's `proposed` default. An id with nothing to accept — no
 *  `proposed` on the item — comes back in `skipped` instead. */
export function acceptDefaults(
  projectId: Uuid,
  runId: Uuid,
  itemIds: Uuid[]
): Promise<BulkAnswerResult> {
  return post<BulkAnswerResult>(`${base(projectId)}/runs/${runId}/answers/bulk`, {
    item_ids: itemIds,
  })
}

export function listArtifacts(projectId: Uuid): Promise<ArtifactSummary[]> {
  return get<ArtifactSummary[]>(`${base(projectId)}/artifacts`)
}

/** `name` is a path (`flow-orders.mmd`, `kb/entities.md`) so it is encoded per
 *  segment — encoding the slashes too would address a file nobody has. */
export function getArtifact(projectId: Uuid, name: string): Promise<Artifact> {
  return get<Artifact>(`${base(projectId)}/artifacts/${encodePath(name)}`)
}

export function saveArtifact(
  projectId: Uuid,
  name: string,
  body: { kind: string; body: string }
): Promise<Artifact> {
  return put<Artifact>(`${base(projectId)}/artifacts/${encodePath(name)}`, body)
}

function encodePath(name: string): string {
  return name.split("/").map(encodeURIComponent).join("/")
}
