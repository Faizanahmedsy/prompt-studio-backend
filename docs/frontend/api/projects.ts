/**
 * `/projects/*` — one thin function per endpoint.
 *
 * Only the routes your address is on are visible; the server filters, so a 404
 * here means "not yours" as often as it means "gone".
 */

import { del, get, patch, post, put } from "./client"
import type {
  ActivityRead,
  CommentCreate,
  CommentRead,
  CommentUpdate,
  MemberBulkInvite,
  MemberInvite,
  MemberRead,
  MemberUpdate,
  Page,
  PageParams,
  PresenceMember,
  ProjectCreate,
  ProjectDetail,
  ProjectDocSave,
  ProjectListQuery,
  ProjectSummary,
  ProjectUpdate,
  Uuid,
  VersionCreate,
  VersionDetail,
  VersionSummary,
} from "./types"

// ── the project itself ───────────────────────────────────────────────────────

/** `status` defaults to "active" server-side; pass "archived" or "all"
 *  explicitly for the other two. */
export function listProjects(query: ProjectListQuery = {}): Promise<Page<ProjectSummary>> {
  return get<Page<ProjectSummary>>("/projects", { ...query })
}

/** 201. Returns the full detail including the document and the member list. */
export function createProject(body: ProjectCreate): Promise<ProjectDetail> {
  return post<ProjectDetail>("/projects", body)
}

/** The project including its full document. Also marks it opened for you. */
export function getProject(projectId: Uuid): Promise<ProjectDetail> {
  return get<ProjectDetail>(`/projects/${projectId}`)
}

/** Metadata only — name, description, archived. Never the document. */
export function updateProject(projectId: Uuid, body: ProjectUpdate): Promise<ProjectSummary> {
  return patch<ProjectSummary>(`/projects/${projectId}`, body)
}

/**
 * Save the diagram.
 *
 * Send `base_version` (the `doc_version` you loaded) and a save that would
 * overwrite someone else's newer work is refused with 409 rather than silently
 * winning. Read the detail with `staleDocumentDetail(error)` from `./client`.
 */
export function saveDocument(projectId: Uuid, body: ProjectDocSave): Promise<ProjectSummary> {
  return put<ProjectSummary>(`/projects/${projectId}/document`, body)
}

/** Move to trash — recoverable via `restoreProject`. Owner only. */
export function deleteProject(projectId: Uuid): Promise<void> {
  return del<void>(`/projects/${projectId}`)
}

export function restoreProject(projectId: Uuid): Promise<ProjectSummary> {
  return post<ProjectSummary>(`/projects/${projectId}/restore`)
}

// ── membership ───────────────────────────────────────────────────────────────

export function listMembers(projectId: Uuid): Promise<MemberRead[]> {
  return get<MemberRead[]>(`/projects/${projectId}/members`)
}

/** 201. The address does not need an account yet — the invitation waits for it. */
export function addMember(projectId: Uuid, body: MemberInvite): Promise<MemberRead> {
  return post<MemberRead>(`/projects/${projectId}/members`, body)
}

/** Addresses already on the project are skipped, not errors — so the returned
 *  array can be shorter than the one you sent. */
export function addMembers(projectId: Uuid, body: MemberBulkInvite): Promise<MemberRead[]> {
  return post<MemberRead[]>(`/projects/${projectId}/members/bulk`, body)
}

/** `memberId` is the membership row's id (`MemberRead.id`), not the user's. */
export function updateMember(
  projectId: Uuid,
  memberId: Uuid,
  body: MemberUpdate
): Promise<MemberRead> {
  return patch<MemberRead>(`/projects/${projectId}/members/${memberId}`, body)
}

export function removeMember(projectId: Uuid, memberId: Uuid): Promise<void> {
  return del<void>(`/projects/${projectId}/members/${memberId}`)
}

export function leaveProject(projectId: Uuid): Promise<void> {
  return post<void>(`/projects/${projectId}/leave`)
}

/** Who has this project open right now — for clients that poll instead of
 *  holding the collaboration socket open. */
export function getPresence(projectId: Uuid): Promise<PresenceMember[]> {
  return get<PresenceMember[]>(`/projects/${projectId}/presence`)
}

// ── versions ─────────────────────────────────────────────────────────────────

export function listVersions(
  projectId: Uuid,
  query: PageParams = {}
): Promise<Page<VersionSummary>> {
  return get<Page<VersionSummary>>(`/projects/${projectId}/versions`, { ...query })
}

/** 201. A named snapshot of the document as it stands. The body is optional
 *  server-side; an unlabelled snapshot is a valid thing to ask for. */
export function saveVersion(projectId: Uuid, body: VersionCreate = {}): Promise<VersionSummary> {
  return post<VersionSummary>(`/projects/${projectId}/versions`, { label: body.label ?? "" })
}

export function getVersion(projectId: Uuid, versionId: Uuid): Promise<VersionDetail> {
  return get<VersionDetail>(`/projects/${projectId}/versions/${versionId}`)
}

/** Rolls the live document back to this snapshot and bumps `doc_version`. */
export function restoreVersion(projectId: Uuid, versionId: Uuid): Promise<ProjectSummary> {
  return post<ProjectSummary>(`/projects/${projectId}/versions/${versionId}/restore`)
}

// ── activity & comments ──────────────────────────────────────────────────────

export function listActivity(projectId: Uuid, query: PageParams = {}): Promise<Page<ActivityRead>> {
  return get<Page<ActivityRead>>(`/projects/${projectId}/activity`, { ...query })
}

/** Not paginated. `include_resolved` defaults to false. */
export function listComments(
  projectId: Uuid,
  options: { include_resolved?: boolean } = {}
): Promise<CommentRead[]> {
  return get<CommentRead[]>(`/projects/${projectId}/comments`, { ...options })
}

/** 201. Requires at least COMMENTER on the project. */
export function addComment(projectId: Uuid, body: CommentCreate): Promise<CommentRead> {
  return post<CommentRead>(`/projects/${projectId}/comments`, body)
}

/** Editing the body is author-only; `resolved` may be set by any commenter. */
export function updateComment(
  projectId: Uuid,
  commentId: Uuid,
  body: CommentUpdate
): Promise<CommentRead> {
  return patch<CommentRead>(`/projects/${projectId}/comments/${commentId}`, body)
}

export function deleteComment(projectId: Uuid, commentId: Uuid): Promise<void> {
  return del<void>(`/projects/${projectId}/comments/${commentId}`)
}
