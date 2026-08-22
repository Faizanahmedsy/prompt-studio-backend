"use client"

/**
 * The session, as a Zustand v5 store.
 *
 * Holds the *user*; the tokens live in `api/token-store.ts` and are never put
 * in here. That split matters: this store is what components re-render on, and
 * a token rotating every fifteen minutes would re-render the entire tree for a
 * value no component reads.
 *
 * Not persisted, on purpose. `bootstrap()` re-derives the user from the stored
 * token on mount, so there is exactly one source of truth for "am I signed in"
 * — the server — instead of a cached user object that outlives the account it
 * describes.
 */

import { create } from "zustand"

import * as authApi from "../api/auth"
import { isApiError, setUnauthorizedHandler } from "../api/client"
import { clearTokens, getRefreshToken, hasSession, setTokens } from "../api/token-store"
import type { LoginResponse, RegisterRequest, UserMe } from "../api/types"

/**
 * "loading" is the honest initial value: on the client's first paint we have a
 * token but not yet a user, and rendering the signed-out shell in that gap is
 * the flash of login screen every app of this shape starts with.
 *
 * "offline" is the fourth state, and it is not a nicety. This editor is
 * local-first — the whole document lives in `localStorage` and the canvas never
 * needed a network — so an unreachable API must not throw someone out of work
 * they can still perfectly well do. It means: a token is stored, the server
 * could not be asked about it, so run locally and stop trying to sync. Only a
 * server that actually *answers* 401 ends a session.
 */
export type AuthStatus = "loading" | "authed" | "offline" | "anon"

export type AuthState = {
  user: UserMe | null
  status: AuthStatus
  /** Last failure, for a form to render. Cleared at the start of every action. */
  error: string | null

  login: (email: string, password: string) => Promise<UserMe>
  register: (input: RegisterRequest) => Promise<UserMe>
  logout: () => Promise<void>
  /** Turn a stored token back into a user. Safe to call more than once. */
  bootstrap: () => Promise<void>
  /** After a profile PATCH, or after set-initial-password clears the flag. */
  setUser: (user: UserMe) => void
  clearError: () => void
}

/**
 * Forget everything the previous account left on this machine.
 *
 * The project store and the sync links persist under fixed keys, so without
 * this they simply outlive the session: sign out, sign in as somebody else, and
 * their editor opens on the previous person's diagrams — read off disk, fully
 * rendered, for a project their address was never added to. That is the one
 * rule this product makes, broken on the client side.
 *
 * Imported lazily. Neither store imports this one, so there is no cycle today,
 * and keeping it that way is cheaper than remembering not to create one.
 */
async function tearDownLocalData(): Promise<void> {
  const { useProjectStore } = await import("@/stores/use-project-store")
  const { useSyncStore } = await import("@/stores/use-sync-store")

  useProjectStore.setState({ projects: [], activeId: null, past: {}, future: {} })
  useSyncStore.setState({ links: {}, versions: {}, state: {}, errors: {} })
  await Promise.allSettled([
    useProjectStore.persist.clearStorage(),
    useSyncStore.persist.clearStorage(),
  ])
}

function messageOf(error: unknown): string {
  if (isApiError(error)) return error.message
  if (error instanceof Error) return error.message
  return "Something went wrong"
}

export const useAuthStore = create<AuthState>()((set, get) => ({
  user: null,
  status: "loading",
  error: null,

  login: async (email, password) => {
    set({ error: null })
    try {
      const result: LoginResponse = await authApi.login({ email, password })
      setTokens(result)
      set({ user: result.user, status: "authed", error: null })
      return result.user
    } catch (error) {
      clearTokens()
      set({ user: null, status: "anon", error: messageOf(error) })
      throw error
    }
  },

  register: async (input) => {
    set({ error: null })
    try {
      const result: LoginResponse = await authApi.register(input)
      setTokens(result)
      set({ user: result.user, status: "authed", error: null })
      return result.user
    } catch (error) {
      set({ error: messageOf(error) })
      throw error
    }
  },

  logout: async () => {
    // The server call goes first, while the tokens still exist.
    //
    // Clearing them beforehand sent the request with no Authorization header,
    // so an authenticated route answered 401 and the session was never
    // revoked — the refresh token stayed mintable for its full thirty days
    // while the person was told they had signed out. The local sign-out is
    // still unconditional; that is what `finally` is for.
    const refreshToken = getRefreshToken()
    try {
      await authApi.logout(refreshToken)
    } catch {
      // Best effort. Being unable to tell the server must not keep someone
      // signed in on this machine.
    } finally {
      set({ user: null, status: "anon", error: null })
      clearTokens()
      await tearDownLocalData()
    }
  },

  bootstrap: async () => {
    if (!hasSession()) {
      set({ user: null, status: "anon" })
      return
    }
    set({ status: get().user ? "authed" : "loading" })
    try {
      const user = await authApi.getMe()
      set({ user, status: "authed", error: null })
    } catch (error) {
      // A 401 here means the access token was dead AND the client's single
      // refresh could not save it — the session is genuinely over.
      if (isApiError(error) && (error.status === 401 || error.status === 403)) {
        clearTokens()
        set({ user: null, status: "anon" })
        return
      }
      // Anything else means we could not *ask*. That is not a logout, and
      // treating it as one used to bounce a signed-in person to /login and put
      // their local work out of reach the moment the API blipped. Keep the
      // token, open the app, and let the sync layer stay quiet until the
      // server is answering again.
      set({ status: "offline", error: messageOf(error) })
    }
  },

  setUser: (user) => set({ user, status: "authed" }),

  clearError: () => set({ error: null }),
}))

/**
 * When the client's one refresh attempt fails, every screen must agree the
 * session is over. Registering the handler at module scope means that happens
 * once, wherever the failure occurred, without a component having to subscribe.
 */
setUnauthorizedHandler(() => {
  useAuthStore.setState({ user: null, status: "anon" })
  // A session that ended on its own leaves exactly the same residue on disk as
  // one the person ended deliberately.
  void tearDownLocalData()
})

// Selectors — subscribing to a slice rather than the whole store keeps a
// component that only needs the role from re-rendering when `error` changes.
export const selectUser = (state: AuthState): UserMe | null => state.user
export const selectStatus = (state: AuthState): AuthStatus => state.status
export const selectIsAdmin = (state: AuthState): boolean =>
  state.user?.role === "ADMIN" || state.user?.role === "SUPERADMIN"
/** True while the account is using an admin-issued password: every route other
 *  than `/auth/set-initial-password` answers 403 until it is replaced. */
export const selectMustChangePassword = (state: AuthState): boolean =>
  state.user?.must_change_password === true
