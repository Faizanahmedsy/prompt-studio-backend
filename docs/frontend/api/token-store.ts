/**
 * Where the access and refresh tokens live.
 *
 * Every single read and write is wrapped in try/catch, and that is not
 * defensive noise: `window.localStorage` **throws** rather than returning null
 * in a Safari private window, in a Chrome profile with "block third-party
 * cookies and site data", and inside a cross-origin iframe. An unguarded
 * `localStorage.getItem` on those browsers takes the whole app down at boot,
 * before anything has had a chance to render an error.
 *
 * A module-level mirror backs the storage, so when persistence is unavailable
 * the session still works normally — it just does not survive a reload.
 *
 * SSR: `window` is undefined during Next's server render, so every accessor
 * checks for it. Never read tokens in a Server Component.
 */

import type { Token } from "./types"

const ACCESS_KEY = "prompt-studio.access_token"
const REFRESH_KEY = "prompt-studio.refresh_token"

/** Survives a storage failure; does not survive a reload. */
let memoryAccess: string | null = null
let memoryRefresh: string | null = null

type Listener = (tokens: { access: string | null; refresh: string | null }) => void
const listeners = new Set<Listener>()

// Whether `localStorage` can be *used*, not merely whether it exists. A browser
// set to block site data, and any cross-origin iframe, throws on the property
// access itself — so `typeof window.localStorage` is not a safe question to ask.
// Probed once and remembered, because the answer cannot change mid-session.
let storageUsable: boolean | null = null

function canUseStorage(): boolean {
  if (storageUsable !== null) return storageUsable
  if (typeof window === "undefined") return false
  try {
    window.localStorage.getItem("__prompt-studio-probe")
    storageUsable = true
  } catch {
    storageUsable = false
  }
  return storageUsable
}

function readKey(key: string): string | null {
  if (!canUseStorage()) return null
  try {
    return window.localStorage.getItem(key)
  } catch {
    // Private window / storage blocked. The in-memory mirror is the answer.
    return null
  }
}

function writeKey(key: string, value: string | null): void {
  if (!canUseStorage()) return
  try {
    if (value === null) window.localStorage.removeItem(key)
    else window.localStorage.setItem(key, value)
  } catch {
    // Quota exceeded or storage blocked — the mirror already holds it.
  }
}

function notify(): void {
  const snapshot = { access: getAccessToken(), refresh: getRefreshToken() }
  for (const listener of listeners) {
    try {
      listener(snapshot)
    } catch {
      // A misbehaving subscriber must not break token persistence.
    }
  }
}

// Storage first, mirror second — and the order is the whole point.
//
// The mirror exists for a browser that cannot persist at all. Consulted first,
// it also shadows a rotation performed by another tab: tab A keeps sending a
// pair that tab B has already rotated away, the server rejects it as revoked,
// the refresh fails on an equally stale refresh token, and `clearTokens()` then
// wipes the *valid* pair out of the storage both tabs share — signing out every
// window at once. Two tabs open is the ordinary way this editor is used.
export function getAccessToken(): string | null {
  return readKey(ACCESS_KEY) ?? memoryAccess
}

export function getRefreshToken(): string | null {
  return readKey(REFRESH_KEY) ?? memoryRefresh
}

/** Store a fresh pair. Accepts anything token-shaped (login, register, refresh). */
export function setTokens(token: Pick<Token, "access_token" | "refresh_token">): void {
  memoryAccess = token.access_token
  memoryRefresh = token.refresh_token
  writeKey(ACCESS_KEY, token.access_token)
  writeKey(REFRESH_KEY, token.refresh_token)
  notify()
}

export function clearTokens(): void {
  memoryAccess = null
  memoryRefresh = null
  writeKey(ACCESS_KEY, null)
  writeKey(REFRESH_KEY, null)
  notify()
}

export function hasSession(): boolean {
  return getAccessToken() !== null
}

/** Notified on every set/clear — used to close the collaboration socket when
 *  the session ends. Returns an unsubscribe function. */
export function subscribeTokens(listener: Listener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export const tokenStore = {
  getAccessToken,
  getRefreshToken,
  setTokens,
  clearTokens,
  hasSession,
  subscribeTokens,
}
