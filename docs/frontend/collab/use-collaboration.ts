"use client"

/**
 * The collaboration websocket, as a hook.
 *
 * One socket per open editor, at
 * `${NEXT_PUBLIC_WS_URL}/api/v1/ws/projects/{projectId}?token=…`. It carries
 * three kinds of traffic (see `app/modules/collab/router.py`):
 *
 *   - document saves, persisted and versioned before anyone is told;
 *   - ephemeral presence (cursors, selections), relayed and never stored;
 *   - housekeeping (hello, ping/pong, join/leave).
 *
 * The concurrency model is last-write-wins over the whole document, guarded by
 * `doc_version`. There are no character-level operations to transform: the
 * editor holds one Zustand document and rewrites it wholesale.
 *
 * THE ECHO RULE, which is the thing to understand before changing this file:
 * when you save, the server broadcasts `doc.updated` (with the document) to
 * *everyone else*, and sends *you* a `doc.ack` carrying only the authoritative
 * `doc_version`. Echoing your own save back would clobber whatever you typed in
 * the milliseconds since, so the ack deliberately has no `doc` field — and
 * because they are two message types rather than one type with a flag, there is
 * no shape to misread: `doc.updated` is always somebody else's work.
 *
 * THE ROSTER RULE: `presence` is authoritative and arrives after every join and
 * leave. `member.joined` / `member.left` are notifications — good for a toast,
 * useless as a source of truth, because a client that only accumulates deltas
 * drifts the first time it misses one. `members` here is only ever assigned
 * from `hello` or `presence`.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import { API_ORIGIN, ensureFreshToken } from "../api/client"
import { getAccessToken } from "../api/token-store"
import type { PresenceMember, ProjectDocPayload, ProjectRole, Uuid } from "../api/types"

// ── wire vocabulary (app/core/constants.py :: WsMessage) ─────────────────────

export type CollabActor = {
  user_id: Uuid
  name: string
  accent_color: string
}

/** A person in the room, collapsed across their tabs. `connections` counts
 *  those tabs; `connection_id` is whichever socket the server described first. */
export type CollabMember = PresenceMember & { connection_id?: string }

/** One *socket*, as `member.joined` / `member.left` describe it. No
 *  `connections` count — a delta is about a tab, not about a person. */
export type CollabMemberRef = Omit<PresenceMember, "connections"> & { connection_id: string }

export type HelloMessage = {
  type: "hello"
  connection_id: string
  you: {
    user_id: Uuid
    email: string
    name: string
    accent_color: string
    role: ProjectRole
    can_edit: boolean
  }
  /** The document travels with the greeting, so joining costs no extra HTTP
   *  round trip that could race an edit arriving in between. */
  doc: ProjectDocPayload
  doc_version: number
  /** The full roster, including you — built after your socket was added. */
  members: CollabMember[]
}

/** Somebody ELSE saved. Always carries the document; never sent to the author
 *  of the save (who gets `doc.ack` instead). */
export type DocUpdatedMessage = {
  type: "doc.updated"
  doc: ProjectDocPayload
  doc_version: number
  by: CollabActor
  at: string
}

/** Your own save was accepted. Carries the authoritative version and, on
 *  purpose, no document. */
export type DocAckMessage = {
  type: "doc.ack"
  doc_version: number
  /** Always true. Kept because the server sends it; nothing branches on it. */
  ack: true
}

export type DocConflictMessage = {
  type: "doc.conflict"
  message: string
  /** The server's current document, sent with the refusal so the client can
   *  reconcile without a second request. */
  doc: ProjectDocPayload
  doc_version: number
  sent_version: number | null
}

export type CursorMessage = {
  type: "cursor" | "selection"
  user_id: Uuid
  connection_id: string
  accent_color: string
  name: string
  /** Whatever the sender put in `payload` — opaque to the server. */
  payload: unknown
}

/** A notification that one socket came or went. Says what CHANGED; the
 *  `presence` frame that follows says what the room now IS. */
export type MemberPresenceMessage = {
  type: "member.joined" | "member.left"
  member: CollabMemberRef
  at: string
}

/** The authoritative roster, sent after every join and every leave. Read from
 *  shared storage, so it is correct across workers. */
export type PresenceSnapshotMessage = {
  type: "presence"
  members: CollabMember[]
  at: string
}

export type PongMessage = { type: "pong"; at: string }
export type ServerErrorMessage = { type: "error"; message: string }

export type ServerMessage =
  | HelloMessage
  | DocUpdatedMessage
  | DocAckMessage
  | DocConflictMessage
  | CursorMessage
  | MemberPresenceMessage
  | PresenceSnapshotMessage
  | PongMessage
  | ServerErrorMessage

type ClientMessage =
  | {
      type: "doc.update"
      doc: ProjectDocPayload
      base_version: number | null
      schema_version?: number
    }
  | { type: "cursor" | "selection"; payload: unknown }
  | { type: "ping" }

// Application close codes (4000-4999) the server uses, so the browser can tell
// "you were signed out" from "the server restarted" without parsing a string.
export const WS_UNAUTHORISED = 4401
export const WS_FORBIDDEN = 4403
export const WS_NOT_FOUND = 4404
export const WS_BAD_MESSAGE = 4400

/** Nothing a retry can fix: no access, or no project. */
const FATAL_CLOSE_CODES: ReadonlySet<number> = new Set([WS_FORBIDDEN, WS_NOT_FOUND])

// ── hook surface ─────────────────────────────────────────────────────────────

export type CollabStatus = "idle" | "connecting" | "open" | "reconnecting" | "closed"

export type UseCollaborationOptions = {
  /** Server-side project id (uuid). Falsy disables the socket. */
  projectId: Uuid | null | undefined
  /** Defaults to the stored access token, re-read on every connect attempt. */
  token?: string | null
  /** Set false to keep the hook mounted without opening a socket. */
  enabled?: boolean
  /** Quiet period before a `doc.update` is sent. */
  debounceMs?: number
  /** Trailing throttle on cursor frames. */
  cursorThrottleMs?: number
  /**
   * Idle keepalive. Two ceilings, and the default clears both: the shortest
   * proxy read timeout in front of the API (nginx defaults to 60s), and the
   * server's presence TTL (120s) — the ping is what refreshes this tab's
   * presence entry, so a longer interval makes a live editor vanish from
   * everyone else's avatar stack.
   */
  pingIntervalMs?: number
  /** Reconnect backoff ceiling. */
  maxBackoffMs?: number
  /** Sent with every save so the server can record the document's schema. */
  schemaVersion?: number

  /** The room greeted you: `doc` is authoritative, including after a reconnect. */
  onHello?: (message: HelloMessage) => void
  /** SOMEBODY ELSE saved. Never fires for your own writes — see the echo rule. */
  onRemoteDoc?: (doc: ProjectDocPayload, docVersion: number, by: CollabActor) => void
  /** Your save was refused as stale. `message.doc` is the server's current one. */
  onConflict?: (message: DocConflictMessage) => void
  onCursor?: (message: CursorMessage) => void
  /** Somebody opened the project. A notification — `members` is updated by the
   *  `presence` frame that follows, not by this. */
  onMemberJoined?: (member: CollabMemberRef) => void
  /** Somebody closed a tab. Same caveat: notification, not roster. */
  onMemberLeft?: (member: CollabMemberRef) => void
  /** A server-side `error` frame, or a close the hook could not recover from. */
  onError?: (message: string) => void
  /** The socket was rejected as unauthenticated and refreshing did not help. */
  onAuthExpired?: () => void
}

export type UseCollaborationResult = {
  /** The room, as the server last reported it. Includes you. */
  members: CollabMember[]
  status: CollabStatus
  /** Queue a document save. Debounced; the last call inside the window wins. */
  sendDoc: (doc: ProjectDocPayload, options?: { immediate?: boolean }) => void
  /** Relay a cursor/selection frame. Throttled, never persisted. */
  sendCursor: (payload: unknown, kind?: "cursor" | "selection") => void
  lastConflict: DocConflictMessage | null
  /** The authoritative version, as last confirmed by the server. */
  docVersion: number
  /** Your own identity and role in the room, from `hello`. */
  you: HelloMessage["you"] | null
  connectionId: string | null
  /** Last error message, whether from an `error` frame or a fatal close. */
  error: string | null
  clearConflict: () => void
  /** Force a reconnect — after a fatal close, or a manual "retry" button. */
  reconnect: () => void
}

const DEFAULTS = {
  debounceMs: 400,
  cursorThrottleMs: 60,
  pingIntervalMs: 25_000,
  maxBackoffMs: 15_000,
  baseBackoffMs: 500,
}

/** `ws://` / `wss://` origin. Falls back to the API origin with the scheme
 *  swapped, which is right whenever the socket is served by the same host. */
function wsOrigin(): string {
  const explicit = process.env.NEXT_PUBLIC_WS_URL
  if (explicit) return explicit.replace(/\/+$/, "")
  return API_ORIGIN.replace(/^http/, "ws")
}

function asServerMessage(value: unknown): ServerMessage | null {
  if (typeof value !== "object" || value === null) return null
  const type = (value as { type?: unknown }).type
  if (typeof type !== "string") return null
  return value as ServerMessage
}

/** Defensive only: the server already collapses and counts. */
function normaliseRoster(members: CollabMember[]): CollabMember[] {
  return members.map((m) => ({ ...m, connections: m.connections || 1 }))
}

export function useCollaboration(options: UseCollaborationOptions): UseCollaborationResult {
  const { projectId, enabled = true } = options

  const [members, setMembers] = useState<CollabMember[]>([])
  const [status, setStatus] = useState<CollabStatus>("idle")
  const [lastConflict, setLastConflict] = useState<DocConflictMessage | null>(null)
  const [docVersion, setDocVersion] = useState<number>(0)
  const [you, setYou] = useState<HelloMessage["you"] | null>(null)
  const [connectionId, setConnectionId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [attemptKey, setAttemptKey] = useState(0)

  /**
   * Options are read through a ref rather than captured by the socket effect.
   * Callers pass inline arrow functions (`onRemoteDoc={(doc) => …}`), which are
   * a new identity every render; in the effect's dependency list they would
   * tear the socket down and rebuild it on every keystroke.
   */
  const optionsRef = useRef<UseCollaborationOptions>(options)
  optionsRef.current = options

  const socketRef = useRef<WebSocket | null>(null)
  /** The authoritative version. Kept in a ref as well as in state because the
   *  send path must read it synchronously, not one render later. */
  const docVersionRef = useRef<number>(0)
  const pendingDocRef = useRef<ProjectDocPayload | null>(null)
  /**
   * The version the queued document was edited from.
   *
   * A save queued while the socket was down is flushed on `hello` — but by
   * then `docVersionRef` holds the version `hello` just delivered, so the save
   * goes out claiming to be based on work it has never seen. That is a forced
   * overwrite of whatever the other members did during the outage, through the
   * one code path the `doc.conflict` guard exists to catch.
   */
  const pendingBaseRef = useRef<number | null>(null)
  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const cursorTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const lastCursorAtRef = useRef<number>(0)
  const pendingCursorRef = useRef<{ payload: unknown; kind: "cursor" | "selection" } | null>(null)

  const sendRaw = useCallback((message: ClientMessage): boolean => {
    const socket = socketRef.current
    if (!socket || socket.readyState !== WebSocket.OPEN) return false
    try {
      socket.send(JSON.stringify(message))
      return true
    } catch {
      return false
    }
  }, [])

  /** Send whatever `sendDoc` last queued, if the socket can take it. */
  const flushDoc = useCallback((): void => {
    if (debounceTimerRef.current !== null) {
      clearTimeout(debounceTimerRef.current)
      debounceTimerRef.current = null
    }
    const doc = pendingDocRef.current
    if (doc === null) return
    const sent = sendRaw({
      type: "doc.update",
      doc,
      // The version this edit was written against — captured when it was
      // queued, not read again now.
      //
      // Reading `docVersionRef` here defeated the whole guard: a colleague's
      // save landing inside the 400ms debounce advanced it, so the flush
      // announced a base the server agreed with and overwrote their work with
      // no conflict, no 409 and no snapshot. The base has to be the one the
      // pending document actually saw.
      base_version: pendingBaseRef.current ?? docVersionRef.current ?? null,
      schema_version: optionsRef.current.schemaVersion,
    })
    // Left queued when the socket is down, so a reconnect flushes it on `open`.
    if (sent) {
      pendingDocRef.current = null
      pendingBaseRef.current = null
    }
  }, [sendRaw])

  const sendDoc = useCallback(
    (doc: ProjectDocPayload, sendOptions?: { immediate?: boolean }): void => {
      pendingDocRef.current = doc
      // Recorded now, while it is still true.
      if (pendingBaseRef.current === null) {
        pendingBaseRef.current = docVersionRef.current || null
      }
      if (debounceTimerRef.current !== null) {
        clearTimeout(debounceTimerRef.current)
        debounceTimerRef.current = null
      }
      if (sendOptions?.immediate) {
        flushDoc()
        return
      }
      const wait = optionsRef.current.debounceMs ?? DEFAULTS.debounceMs
      debounceTimerRef.current = setTimeout(flushDoc, wait)
    },
    [flushDoc]
  )

  const sendCursor = useCallback(
    (payload: unknown, kind: "cursor" | "selection" = "cursor"): void => {
      const throttle = optionsRef.current.cursorThrottleMs ?? DEFAULTS.cursorThrottleMs
      const now = Date.now()
      const since = now - lastCursorAtRef.current
      if (since >= throttle) {
        lastCursorAtRef.current = now
        sendRaw({ type: kind, payload })
        return
      }
      // Trailing edge: keep only the newest frame, so a fast pointer sends one
      // message per window instead of one per mousemove.
      pendingCursorRef.current = { payload, kind }
      if (cursorTimerRef.current !== null) return
      cursorTimerRef.current = setTimeout(() => {
        cursorTimerRef.current = null
        const queued = pendingCursorRef.current
        pendingCursorRef.current = null
        if (!queued) return
        lastCursorAtRef.current = Date.now()
        sendRaw({ type: queued.kind, payload: queued.payload })
      }, throttle - since)
    },
    [sendRaw]
  )

  const handleMessage = useCallback((message: ServerMessage): void => {
    const handlers = optionsRef.current
    switch (message.type) {
      case "hello": {
        docVersionRef.current = message.doc_version
        setDocVersion(message.doc_version)
        setMembers(normaliseRoster(message.members))
        setYou(message.you)
        setConnectionId(message.connection_id)
        setError(null)
        // Also fires after a reconnect, and the document it carries is the
        // server's — the consumer decides whether to adopt it wholesale.
        handlers.onHello?.(message)
        // A save queued while the socket was down goes out here rather than in
        // `onopen`: only now do we know the version to base it on. But it may
        // be based on a version the server has already moved past, and flushing
        // it regardless would overwrite whatever happened during the outage
        // without ever raising the conflict this protocol exists to raise.
        if (pendingDocRef.current !== null) {
          const basedOn = pendingBaseRef.current
          if (basedOn !== null && basedOn !== message.doc_version) {
            // Dropped, not forced. `onHello` has just replaced the local
            // document with the server's, and the render that follows re-queues
            // whatever the person still has on screen — this time based on a
            // version that exists.
            pendingDocRef.current = null
            pendingBaseRef.current = null
          } else {
            flushDoc()
          }
        }
        break
      }

      case "doc.ack": {
        // THE ECHO RULE, half one. The receipt for our OWN save: take the
        // version, and there is nothing else to take. Re-applying what we sent
        // would undo whatever the user typed while the save was in flight,
        // which is exactly why the server does not send it back.
        docVersionRef.current = message.doc_version
        setDocVersion(message.doc_version)
        break
      }

      case "doc.updated": {
        // Half two. This type is only ever somebody else's save, and it always
        // carries the document — no shape check needed.
        docVersionRef.current = message.doc_version
        setDocVersion(message.doc_version)
        handlers.onRemoteDoc?.(message.doc, message.doc_version, message.by)
        break
      }

      case "doc.conflict": {
        // Our save lost. The server's current document rides along with the
        // refusal, so adopt its version and let the consumer reconcile.
        docVersionRef.current = message.doc_version
        setDocVersion(message.doc_version)
        setLastConflict(message)
        handlers.onConflict?.(message)
        break
      }

      case "cursor":
      case "selection": {
        handlers.onCursor?.(message)
        break
      }

      // THE ROSTER RULE. These two are notifications and nothing more: they
      // fire the callback and deliberately do NOT touch `members`. Rebuilding a
      // roster from deltas drifts the moment one is missed (a reconnect, a
      // dropped frame, a worker restart) and the drift is invisible until
      // somebody's avatar is stuck on screen an hour after they left.
      case "member.joined": {
        handlers.onMemberJoined?.(message.member)
        break
      }

      case "member.left": {
        handlers.onMemberLeft?.(message.member)
        break
      }

      // ...and this is the authoritative answer, sent right after each of them.
      case "presence": {
        setMembers(normaliseRoster(message.members))
        break
      }

      case "pong":
        // Nothing to do. Its only job is to prove the socket is still alive.
        break

      case "error": {
        setError(message.message)
        handlers.onError?.(message.message)
        break
      }
    }
  }, [flushDoc])

  // biome-ignore lint/correctness/useExhaustiveDependencies: `attemptKey` is not read in the body — it exists to re-run this effect, which is what the retry lever is
  useEffect(() => {
    if (!enabled || !projectId) {
      setStatus("idle")
      return
    }

    let disposed = false
    let attempt = 0
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let pingTimer: ReturnType<typeof setInterval> | null = null

    const stopPing = (): void => {
      if (pingTimer !== null) {
        clearInterval(pingTimer)
        pingTimer = null
      }
    }

    const scheduleReconnect = (): void => {
      if (disposed || reconnectTimer !== null) return
      const max = optionsRef.current.maxBackoffMs ?? DEFAULTS.maxBackoffMs
      const base = DEFAULTS.baseBackoffMs * 2 ** attempt
      // Jittered so a server restart does not bring every open editor back in
      // the same millisecond, and capped so a long outage still retries.
      const delay = Math.min(max, Math.round(base * (0.7 + Math.random() * 0.6)))
      attempt += 1
      setStatus("reconnecting")
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null
        open()
      }, delay)
    }

    const open = (): void => {
      if (disposed) return
      // Read at connect time, not at mount: an access token refreshed while the
      // editor was open must be the one a reconnect uses.
      const token = optionsRef.current.token ?? getAccessToken()
      if (!token) {
        setStatus("closed")
        setError("Not signed in")
        return
      }

      setStatus(attempt === 0 ? "connecting" : "reconnecting")

      let socket: WebSocket
      try {
        socket = new WebSocket(
          `${wsOrigin()}/api/v1/ws/projects/${projectId}?token=${encodeURIComponent(token)}`
        )
      } catch {
        scheduleReconnect()
        return
      }
      socketRef.current = socket

      socket.onopen = () => {
        if (disposed) return
        attempt = 0
        setStatus("open")
        setError(null)
        // An idle socket is reaped by proxies (nginx: 60s of silence), and the
        // server also treats each ping as this tab's presence heartbeat,
        // pushing out a 120s TTL. Two reasons the interval must stay short.
        stopPing()
        pingTimer = setInterval(() => {
          sendRaw({ type: "ping" })
        }, optionsRef.current.pingIntervalMs ?? DEFAULTS.pingIntervalMs)
        // A queued save is NOT flushed here — `hello` arrives a moment later
        // with the authoritative version, and that handler flushes it.
      }

      socket.onmessage = (event: MessageEvent) => {
        const raw: unknown = event.data
        if (typeof raw !== "string") return
        let parsed: unknown
        try {
          parsed = JSON.parse(raw)
        } catch {
          return
        }
        const message = asServerMessage(parsed)
        if (message) handleMessage(message)
      }

      socket.onerror = () => {
        // A close event always follows; reconnecting is handled there so the
        // two paths cannot both schedule a retry.
      }

      socket.onclose = (event: CloseEvent) => {
        stopPing()
        if (socketRef.current === socket) socketRef.current = null
        if (disposed) return
        setMembers([])

        if (FATAL_CLOSE_CODES.has(event.code)) {
          setStatus("closed")
          setError(event.reason || "You cannot open this project")
          optionsRef.current.onError?.(event.reason || "You cannot open this project")
          return
        }

        if (event.code === WS_UNAUTHORISED) {
          // The token expired while the editor was open. Refresh through the
          // client's shared single-flight slot — so this does not stampede
          // alongside REST calls hitting 401 at the same moment — and retry
          // only if a new token actually arrived.
          setStatus("reconnecting")
          void ensureFreshToken().then((refreshed) => {
            if (disposed) return
            if (refreshed) {
              attempt = 0
              open()
              return
            }
            setStatus("closed")
            setError("Your session has expired")
            optionsRef.current.onAuthExpired?.()
          })
          return
        }

        scheduleReconnect()
      }
    }

    open()

    return () => {
      disposed = true
      stopPing()
      if (reconnectTimer !== null) clearTimeout(reconnectTimer)
      if (debounceTimerRef.current !== null) {
        // Unmounting with an edit still in the debounce window would silently
        // lose it, so it goes out now if the socket can still take it.
        clearTimeout(debounceTimerRef.current)
        debounceTimerRef.current = null
        flushDoc()
      }
      if (cursorTimerRef.current !== null) clearTimeout(cursorTimerRef.current)
      const socket = socketRef.current
      socketRef.current = null
      if (socket) {
        socket.onclose = null
        socket.onerror = null
        socket.onmessage = null
        socket.onopen = null
        // 1000 = normal closure. Anything else makes the server log a failure
        // for what is just a tab being closed.
        if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
          socket.close(1000, "Editor closed")
        }
      }
      setStatus("idle")
    }
    // `attemptKey` is the manual-retry lever; the callbacks are read through
    // `optionsRef`, so they deliberately do not appear here.
  }, [projectId, enabled, attemptKey, flushDoc, handleMessage, sendRaw])

  const clearConflict = useCallback(() => setLastConflict(null), [])
  const reconnect = useCallback(() => setAttemptKey((n) => n + 1), [])

  return useMemo(
    () => ({
      members,
      status,
      sendDoc,
      sendCursor,
      lastConflict,
      docVersion,
      you,
      connectionId,
      error,
      clearConflict,
      reconnect,
    }),
    [
      members,
      status,
      sendDoc,
      sendCursor,
      lastConflict,
      docVersion,
      you,
      connectionId,
      error,
      clearConflict,
      reconnect,
    ]
  )
}
