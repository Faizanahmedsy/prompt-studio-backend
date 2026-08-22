/**
 * `/auth/*` and `/users/me` — one thin function per endpoint.
 *
 * `/users/me` lives here rather than in a users module because it is part of
 * the session: it is what `bootstrap()` calls to turn a stored token back into
 * a signed-in user.
 *
 * These functions do NOT touch the token store. Persisting what a login
 * returned is the auth store's job (`stores/auth-store.ts`) — keeping the two
 * apart is what makes it possible to call `login()` from a test without
 * mutating global state.
 */

import { del, get, patch, post } from "./client"
import type {
  ChangePasswordRequest,
  ForgotPasswordRequest,
  ForgotPasswordResponse,
  LoginRequest,
  LoginResponse,
  LogoutEverywhereResponse,
  RegisterRequest,
  ResetPasswordRequest,
  SessionRead,
  SetInitialPasswordRequest,
  Token,
  UserMe,
  UserUpdate,
  Uuid,
  VerifyEmailRequest,
} from "./types"

/** Create an account and sign in (201). Pass `invite_token` from an invitation
 *  link and the address is treated as confirmed. */
export function register(body: RegisterRequest): Promise<LoginResponse> {
  return post<LoginResponse>("/auth/register", body, { auth: false })
}

export function login(body: LoginRequest): Promise<LoginResponse> {
  return post<LoginResponse>("/auth/login", body, { auth: false })
}

/**
 * Exchange a refresh token for a new pair; the old one stops working.
 *
 * Exported for completeness — the client refreshes on its own on a 401 and
 * nothing in the app should normally need to call this by hand.
 */
export function refresh(refreshToken: string): Promise<Token> {
  return post<Token>("/auth/refresh", { refresh_token: refreshToken }, { auth: false })
}

/** Revokes this session. The body is optional server-side — signing out of a
 *  browser that already dropped its refresh token still revokes the access
 *  token rather than answering 422 — but sending it revokes both. */
export function logout(refreshToken?: string | null): Promise<void> {
  return post<void>("/auth/logout", { refresh_token: refreshToken ?? null })
}

export function logoutEverywhere(): Promise<LogoutEverywhereResponse> {
  return post<LogoutEverywhereResponse>("/auth/logout-everywhere")
}

/** Every device currently signed in as you. */
export function listSessions(): Promise<SessionRead[]> {
  return get<SessionRead[]>("/auth/sessions")
}

/** Sign one device out. 200 enveloped, like everything else. */
export function revokeSession(sessionId: Uuid): Promise<void> {
  return del<void>(`/auth/sessions/${sessionId}`)
}

export function changePassword(body: ChangePasswordRequest): Promise<void> {
  return post<void>("/auth/change-password", body)
}

/**
 * Replace an admin-issued password with one the user chose. The only
 * authenticated route a `must_change_password` account may reach — every other
 * one answers 403 until this succeeds.
 */
export function setInitialPassword(body: SetInitialPasswordRequest): Promise<void> {
  return post<void>("/auth/set-initial-password", body)
}

/** Always answers the same whether or not the address has an account. Outside
 *  production `reset_token` comes back in the body. */
export function forgotPassword(body: ForgotPasswordRequest): Promise<ForgotPasswordResponse> {
  return post<ForgotPasswordResponse>("/auth/forgot-password", body, { auth: false })
}

export function resetPassword(body: ResetPasswordRequest): Promise<void> {
  return post<void>("/auth/reset-password", body, { auth: false })
}

export function verifyEmail(body: VerifyEmailRequest): Promise<void> {
  return post<void>("/auth/verify-email", body, { auth: false })
}

export function resendVerification(): Promise<void> {
  return post<void>("/auth/resend-verification")
}

// ── /users/me ────────────────────────────────────────────────────────────────

/** The signed-in account, plus the permission list the UI renders against. */
export function getMe(): Promise<UserMe> {
  return get<UserMe>("/users/me")
}

export function updateMe(body: UserUpdate): Promise<UserMe> {
  return patch<UserMe>("/users/me", body)
}
