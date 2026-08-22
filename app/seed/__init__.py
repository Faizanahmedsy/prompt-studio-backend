"""Idempotent bootstrap data.

Run on every container start. Everything here checks before it writes, so a
restart is a no-op rather than a duplicate-key crash — which is what lets the
startup command run it unconditionally.
"""
