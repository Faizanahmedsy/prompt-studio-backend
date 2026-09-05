/**
 * `/users/me/tokens` — personal API tokens.
 *
 * The other way into this account. The studio uses a session; `weaver push`,
 * curl and CI use one of these instead, with the same rights as the person who
 * made it. The secret is shown once, at creation, and never again — the server
 * only stores a fingerprint of it.
 *
 * Not in `./types.ts` for the same reason as `./discovery.ts`: that file is
 * paired with the project API by a drift test.
 */

import { del, get, post } from "./client"

export type ApiTokenSummary = {
  id: string
  name: string
  created_at: string
  revoked_at: string | null
}

/** Only ever returned by `createToken`. `token` is a `pst_…` secret. */
export type ApiTokenCreated = {
  id: string
  name: string
  token: string
  created_at: string
}

export function listTokens(): Promise<ApiTokenSummary[]> {
  return get<ApiTokenSummary[]>("/users/me/tokens")
}

export function createToken(name: string): Promise<ApiTokenCreated> {
  return post<ApiTokenCreated>("/users/me/tokens", { name })
}

export function revokeToken(id: string): Promise<void> {
  return del<void>(`/users/me/tokens/${id}`)
}
