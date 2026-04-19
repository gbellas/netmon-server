"""Shared pause state for the SSH ping pollers.

When a client (currently: the iPhone app when it's on the BR1's LAN) is
handling BR1 polling locally, it asks the server to pause its own SSH ping
streams so they don't burn cellular data redundantly.

The pause is a lease: clients set a "paused until" timestamp, and pollers
check the timestamp each cycle. If the client disconnects or crashes, the
lease expires naturally — no "server forgot to resume" bug class.
"""
from __future__ import annotations
import time
from threading import Lock


class SshPauseState:
    """Thread-safe pause timestamp. Default: not paused (0)."""

    # Hard cap on how long the phone can request a single pause for. Keeps a
    # misbehaving client from silencing alerts indefinitely.
    MAX_LEASE_SECONDS = 300

    def __init__(self):
        self._lock = Lock()
        self._paused_until: float = 0.0
        self._paused_by: str = ""
        self._last_request: float = 0.0

    def is_paused(self) -> bool:
        with self._lock:
            return time.time() < self._paused_until

    def seconds_remaining(self) -> float:
        with self._lock:
            return max(0.0, self._paused_until - time.time())

    def request_pause(self, duration_sec: float, client_label: str = "") -> float:
        """Extend the pause by up to `duration_sec` seconds from now.
        Returns the resulting paused-until timestamp."""
        d = max(0.0, min(self.MAX_LEASE_SECONDS, float(duration_sec)))
        with self._lock:
            self._paused_until = time.time() + d
            self._paused_by = client_label
            self._last_request = time.time()
            return self._paused_until

    def clear(self) -> None:
        with self._lock:
            self._paused_until = 0.0

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            return {
                "paused": now < self._paused_until,
                "paused_until": self._paused_until if self._paused_until > now else None,
                "seconds_remaining": max(0.0, self._paused_until - now),
                "paused_by": self._paused_by,
                "last_request_at": self._last_request or None,
            }
