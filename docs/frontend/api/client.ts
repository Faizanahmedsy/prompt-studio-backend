/**
 * The only place in the app that talks to the network.
 *
 * Responsibilities, in order:
 *   1. prefix `NEXT_PUBLIC_API_URL` + `/api/v1`
 *   2. JSON headers + `Authorization: Bearer <access token>`
 *   3. unwrap the response envelope and hand back `data`
 *   4. turn a failure into a typed `ApiError` carrying status/message/errors
 *   5. refresh the access token ONCE on a 401 and replay the original request
 *
 * Everything above the client (hooks, stores, components) sees plain payloads
 * and plain exceptions; nothing else should ever call `fetch` directly.
 */

import { clearTokens, getAccessToken, getRefreshToken, setTokens } from "./token-store"
import type { ApiEnvelope, ApiErrorItem, StaleDocumentDetail, Token } from "./types"

const DEFAULT_BASE = "http://localhost:8010"
const API_PREFIX = "/api/v1"

/** `NEXT_PUBLIC_*` is inlined at build time by Next, so this is a constant in
 *  the bundle, not a runtime lookup. */
export const API_ORIGIN = (process.env.NEXT_PUBLIC_API_URL ?? DEFAULT_BASE).replace(/\/+$/, "")
export const API_BASE = `${API_ORIGIN}${API_PREFIX}`

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE"

export type QueryValue = string | number | boolean | null | undefined
export type QueryParams = Record<string, QueryValue>

export type RequestOptions = {
  method?: HttpMethod
  /** Serialised as JSON. `undefined` sends no body at all. */
  body?: unknown
  query?: QueryParams
  signal?: AbortSignal
  /** Set false for the handful of routes that must not carry a token. */
  auth?: boolean
  headers?: Record<string, string>
}

/**
 * Every failure the client raises — HTTP, transport, or a malformed body.
 *
 * `status` is 0 when the request never reached the server (offline, DNS, CORS
 * preflight refused), which is worth distinguishing in the UI from a 500.
 */
export class ApiError extends Error {
  readonly status: number
  readonly errors: ApiErrorItem[]

  constructor(status: number, message: string, errors: ApiErrorItem[] = []) {
    super(message)
    this.name = "ApiError"
    this.status = status
    this.errors = errors
    // Restores the prototype chain so `err instanceof ApiError` survives a
    // build that downlevels classes (Next still does for some targets).
    Object.setPrototypeOf(this, ApiError.prototype)
  }

  /** Field-keyed map for a 422, ready to feed straight into react-hook-form. */
  fieldErrors(): Record<string, string> {
    const out: Record<string, string> = {}
    for (const item of this.errors) {
      if (item.field && item.message && !(item.field in out)) out[item.field] = item.message
    }
    return out
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError
}

/**
 * The 409 detail from `PUT /projects/{id}/document`, or null if this error is
 * not a stale-document conflict. Branch on this rather than on the message.
 */
export function staleDocumentDetail(error: unknown): StaleDocumentDetail | null {
  if (!isApiError(error) || error.status !== 409) return null
  for (const item of error.errors) {
    if (item.field === "base_version" && typeof item.current_version === "number") {
      return {
        field: "base_version",
        message: typeof item.message === "string" ? item.message : error.message,
        current_version: item.current_version,
        sent_version: typeof item.sent_version === "number" ? item.sent_version : null,
      }
    }
  }
  return null
}

// ── session-expiry hook ──────────────────────────────────────────────────────

type UnauthorizedHandler = () => void
let onUnauthorized: UnauthorizedHandler | null = null

/** Called once when the refresh itself fails, i.e. the session is really over.
 *  The auth store registers here to drop the user and send them to /login. */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  onUnauthorized = handler
}

// ── the single shared refresh ────────────────────────────────────────────────

/**
 * THE PART PEOPLE GET WRONG.
 *
 * A page mount typically fires several requests at once (me, projects, members,
 * activity...). When the access token has expired they all come back 401 within
 * a few milliseconds of each other. The naive implementation refreshes inside
 * each one — so ten requests fire ten `/auth/refresh` calls, and since this
 * server ROTATES refresh tokens (the old one stops working the moment a new
 * pair is issued, see `service.refresh_tokens`), nine of those ten fail and the
 * user is logged out at random.
 *
 * The fix is this module-level promise: the first 401 to arrive starts the
 * refresh and stores the promise; every other 401 in the same window awaits the
 * SAME promise instead of starting its own. The slot is cleared in `finally`
 * so a later expiry can refresh again.
 *
 * The refresh call deliberately uses bare `fetch` rather than `request()`:
 * routing it back through the client would let a 401 on the refresh itself
 * trigger another refresh, forever.
 */
let refreshInFlight: Promise<boolean> | null = null

async function performRefresh(): Promise<boolean> {
  const refreshToken = getRefreshToken()
  if (!refreshToken) return false

  try {
    const response = await fetch(`${API_BASE}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    })
    if (!response.ok) return false

    const parsed: unknown = await response.json()
    const token = unwrapEnvelope<Token>(parsed)
    if (!token || typeof token.access_token !== "string") return false

    setTokens(token)
    return true
  } catch {
    return false
  }
}

/**
 * Force a refresh through the same single-flight slot as the 401 path.
 *
 * Used by the collaboration socket: a websocket only authenticates at connect
 * time, so a socket that drops after the access token expired must get a fresh
 * one before it reconnects — and it must not race the REST layer doing the
 * same thing.
 */
export function ensureFreshToken(): Promise<boolean> {
  return refreshAccessToken()
}

function refreshAccessToken(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight
  refreshInFlight = performRefresh().finally(() => {
    refreshInFlight = null
  })
  return refreshInFlight
}

// ── plumbing ─────────────────────────────────────────────────────────────────

export function buildQuery(query: QueryParams | undefined): string {
  if (!query) return ""
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    // Both null and undefined mean "leave it out and take the server default".
    // A query string has no way to say null, and no route needs it to: the
    // filters that once wanted a tri-state now take an explicit enum instead
    // (`?status=active|archived|all`).
    if (value === undefined || value === null) continue
    search.set(key, String(value))
  }
  const encoded = search.toString()
  return encoded ? `?${encoded}` : ""
}

/** Pull `data` out of the envelope, or pass a bare payload straight through. */
function unwrapEnvelope<T>(payload: unknown): T {
  if (payload !== null && typeof payload === "object" && "success" in payload) {
    return (payload as ApiEnvelope<T>).data
  }
  // `/health` and anything outside `/api/v1` is not enveloped.
  return payload as T
}

function envelopeError(status: number, statusText: string, payload: unknown): ApiError {
  if (payload !== null && typeof payload === "object") {
    const body = payload as Partial<ApiEnvelope<unknown>>
    const message = typeof body.message === "string" ? body.message : statusText
    const errors = Array.isArray(body.errors) ? body.errors : []
    return new ApiError(status, message, errors)
  }
  return new ApiError(status, statusText || `Request failed with status ${status}`)
}

async function readBody(response: Response): Promise<unknown> {
  // Every response under the API prefix is now enveloped JSON — the lone 204
  // (`DELETE /auth/sessions/{id}`) is gone, so there is no status to special-
  // case. An empty body is still handled, because a proxy or a gateway timeout
  // can produce one at any status.
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text) as unknown
  } catch {
    // HTML from a proxy, a stack trace, anything non-JSON.
    return { success: false, status_code: response.status, message: text, data: null, errors: [] }
  }
}

async function send(path: string, options: RequestOptions): Promise<Response> {
  const headers: Record<string, string> = { Accept: "application/json", ...options.headers }
  if (options.body !== undefined) headers["Content-Type"] = "application/json"

  if (options.auth !== false) {
    const token = getAccessToken()
    if (token) headers.Authorization = `Bearer ${token}`
  }

  return fetch(`${API_BASE}${path}${buildQuery(options.query)}`, {
    method: options.method ?? "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
  })
}

/**
 * Issue one request and return the unwrapped `data`.
 *
 * `T` is what the endpoint's `data` field holds. Use `void` for the routes that
 * answer with a null payload (logout, delete, change-password).
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return requestOnce<T>(path, options, false)
}

async function requestOnce<T>(
  path: string,
  options: RequestOptions,
  replayed: boolean
): Promise<T> {
  let response: Response
  try {
    response = await send(path, options)
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error
    throw new ApiError(0, "Could not reach the server. Check your connection.")
  }

  if (
    response.status === 401 &&
    !replayed &&
    options.auth !== false &&
    // Refreshing because the refresh route itself said 401 is a loop.
    !path.startsWith("/auth/refresh")
  ) {
    const refreshed = await refreshAccessToken()
    if (refreshed) {
      // Replayed exactly once. A second 401 means the new token is not the
      // problem — the caller genuinely has no access — and falls through below.
      return requestOnce<T>(path, options, true)
    }
    clearTokens()
    onUnauthorized?.()
  }

  const payload = await readBody(response)
  if (!response.ok) throw envelopeError(response.status, response.statusText, payload)
  return unwrapEnvelope<T>(payload)
}

// Thin verbs, so an endpoint module reads as one line each.

export function get<T>(path: string, query?: QueryParams, options: RequestOptions = {}): Promise<T> {
  return request<T>(path, { ...options, method: "GET", query })
}

export function post<T>(path: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
  return request<T>(path, { ...options, method: "POST", body })
}

export function put<T>(path: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
  return request<T>(path, { ...options, method: "PUT", body })
}

export function patch<T>(path: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
  return request<T>(path, { ...options, method: "PATCH", body })
}

export function del<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return request<T>(path, { ...options, method: "DELETE" })
}
