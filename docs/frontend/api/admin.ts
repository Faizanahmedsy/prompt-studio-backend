/**
 * `/admin/*` and `/rbac/roles`.
 *
 * Reaching any of these needs a platform role (ADMIN or SUPERADMIN); the
 * destructive half (deleting accounts and projects) is SUPERADMIN only. Hide
 * the links with `user.permissions`, but expect a 403 anyway — the permission
 * list is a convenience, and the server is the boundary.
 *
 * Note what is deliberately absent: there is no admin route that returns a
 * project's document. A project is visible to the addresses on it and to nobody
 * else, admins included.
 */

import { del, get, patch, post } from "./client"
import type {
  AdminProjectListQuery,
  AdminProjectRow,
  AdminUserCreate,
  AdminUserListQuery,
  AdminUserUpdate,
  AuditListQuery,
  AuditLogRead,
  Page,
  PasswordIssued,
  PlatformStats,
  RoleDescription,
  RoleVocabulary,
  UserListItem,
  Uuid,
} from "./types"

/** Dashboard numbers for the admin panel. */
export function getPlatformStats(): Promise<PlatformStats> {
  return get<PlatformStats>("/admin/stats")
}

/** The role/permission matrix, so the panel renders the server's copy instead
 *  of a second one that drifts. */
export function listAdminRoles(): Promise<RoleDescription[]> {
  return get<RoleDescription[]>("/admin/roles")
}

/** `/rbac/roles` — available to any signed-in user, for populating pickers.
 *  Two independent axes: `platform` and `project`. */
export function getRoleVocabulary(): Promise<RoleVocabulary> {
  return get<RoleVocabulary>("/rbac/roles")
}

// ── user management ──────────────────────────────────────────────────────────

export function listUsers(query: AdminUserListQuery = {}): Promise<Page<UserListItem>> {
  return get<Page<UserListItem>>("/admin/users", { ...query })
}

/**
 * 201. Leave `password` out and one is generated: it comes back **once** in
 * `temporary_password` and is never retrievable again.
 */
export function createUser(body: AdminUserCreate): Promise<PasswordIssued> {
  return post<PasswordIssued>("/admin/users", body)
}

/** Rename, change role, activate/deactivate, or re-issue a password. The last
 *  two also end every session that account has open. */
export function updateUser(userId: Uuid, body: AdminUserUpdate): Promise<PasswordIssued> {
  return patch<PasswordIssued>(`/admin/users/${userId}`, body)
}

/** SUPERADMIN only. */
export function deleteUser(userId: Uuid): Promise<void> {
  return del<void>(`/admin/users/${userId}`)
}

// ── projects & audit ─────────────────────────────────────────────────────────

/** Every project on the platform — metadata only, never the document. */
export function listAllProjects(
  query: AdminProjectListQuery = {}
): Promise<Page<AdminProjectRow>> {
  return get<Page<AdminProjectRow>>("/admin/projects", { ...query })
}

/** SUPERADMIN only. */
export function deleteAnyProject(projectId: Uuid): Promise<void> {
  return del<void>(`/admin/projects/${projectId}`)
}

/** Requires the `audit.read` permission (ADMIN and above). */
export function listAuditLog(query: AuditListQuery = {}): Promise<Page<AuditLogRead>> {
  return get<Page<AuditLogRead>>("/admin/audit", { ...query })
}
