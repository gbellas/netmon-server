"""Base poller with retry/backoff logic."""

import asyncio
import logging
import time


class BasePoller:
    """Abstract base class for device pollers."""

    def __init__(self, name: str, config: dict, state, ws_manager, bandwidth_meter=None):
        self.name = name
        self.config = config
        self.state = state
        self.ws = ws_manager
        self.poll_interval = config.get("poll_interval", 10)
        self.logger = logging.getLogger(f"netmon.{name}")
        self._consecutive_failures = 0
        self._max_backoff = 60
        self.meter = bandwidth_meter   # Optional[BandwidthMeter]
        # Health bookkeeping so the server can expose a /api/poller-status
        # endpoint + the watchdog can kick a stuck poller without operator
        # intervention.
        self._last_success_at: float = 0.0
        self._last_error: str = ""

    def _record_bytes(self, subsystem: str, bytes_in: int = 0, bytes_out: int = 0) -> None:
        if self.meter is not None:
            self.meter.record(subsystem, bytes_in=bytes_in, bytes_out=bytes_out)

    def health(self) -> dict:
        """Snapshot of this poller's health, safe to serialize to JSON."""
        now = time.time()
        last = self._last_success_at
        return {
            "name": self.name,
            "last_success_at": last or None,
            "seconds_since_success": (now - last) if last else None,
            "consecutive_failures": self._consecutive_failures,
            "poll_interval": self.poll_interval,
            "idle_aware": bool(getattr(self, "pause_when_idle", False)),
            "last_error": self._last_error,
        }

    async def _interruptible_sleep(self, seconds: float, check_interval: float = 5.0) -> None:
        """Sleep for up to `seconds`, but break early if a client connects
        (exiting idle state). Polling pipes need to resume FAST when the user
        opens the app, not 5 minutes later."""
        end = time.time() + seconds
        while time.time() < end:
            remaining = end - time.time()
            chunk = min(check_interval, remaining)
            await asyncio.sleep(chunk)
            # Wake up early if we left idle state (someone connected).
            if hasattr(self.ws, "is_idle") and not self.ws.is_idle(
                getattr(self, "idle_threshold_sec", 60.0)
            ):
                return

    def _current_interval(self) -> float:
        if self._consecutive_failures <= 2:
            return self.poll_interval
        backoff = min(self.poll_interval * (2 ** (self._consecutive_failures - 2)), self._max_backoff)
        return backoff

    async def poll(self) -> dict:
        """Override: return dict of key->value updates. Raise on failure."""
        raise NotImplementedError

    def _stale_subfields_update(self) -> dict:
        """Produce updates that clear any sub-fields under this device's namespace
        so stale 'connected' values don't linger when the device is unreachable."""
        prefix = f"{self.name}."
        clearers = {}
        all_keys = list(self.state.get_all().keys())
        for k in all_keys:
            if not k.startswith(prefix):
                continue
            # Preserve status + last_seen + identity metadata
            tail = k[len(prefix):]
            if tail in ("status", "last_seen", "device_name", "is_mobile", "model",
                        "firmware", "serial", "host"):
                continue
            cur = self.state.get(k)
            if isinstance(cur, str):
                # status-like strings -> "unknown"; everything else -> ""
                if tail.endswith(".status") or tail.endswith(".peer_status"):
                    clearers[k] = "unknown"
                else:
                    clearers[k] = ""
            elif isinstance(cur, bool):
                clearers[k] = False
            elif isinstance(cur, (int, float)):
                clearers[k] = 0
            elif isinstance(cur, list):
                clearers[k] = []
        return clearers

    # Subclasses set this True if they want to back off when nobody's watching.
    # Pollers that burn cellular (BR1 REST, BR1 SSH pings) should opt in.
    # Home-LAN pollers (UDM, Balance, Mac pings) should stay running.
    pause_when_idle: bool = False
    # How long, after the last client disconnects, before we consider the
    # system idle and start backing off.
    idle_threshold_sec: float = 60.0
    # What poll interval to use in idle mode.
    idle_interval_sec: float = 300.0

    async def run(self):
        self.logger.info(f"Poller started (interval={self.poll_interval}s)")
        while True:
            try:
                # Idle back-off: if we opt in AND no clients have been connected
                # for a while, poll much less frequently. CRITICAL: the sleep
                # must be interruptible so that when a client reconnects, we
                # resume polling within ~5s — not after the full idle_interval.
                if self.pause_when_idle and hasattr(self.ws, "is_idle") \
                   and self.ws.is_idle(self.idle_threshold_sec):
                    await self._interruptible_sleep(self.idle_interval_sec)
                    continue

                updates = await self.poll()
                was_unreachable = self._consecutive_failures >= 3
                self._consecutive_failures = 0
                self._last_success_at = time.time()
                self._last_error = ""
                changed = self.state.update(updates)
                if changed:
                    await self.ws.broadcast(changed)
                if was_unreachable:
                    self.logger.info(f"Back online")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._consecutive_failures += 1
                self._last_error = f"{type(e).__name__}: {e}"[:200]
                self.logger.warning(
                    f"Poll failed ({self._consecutive_failures}x): {e}"
                )
                if self._consecutive_failures == 3:
                    unreachable = self._stale_subfields_update()
                    unreachable[f"{self.name}.status"] = "unreachable"
                    unreachable[f"{self.name}.last_seen"] = time.time()
                    changed = self.state.update(unreachable)
                    if changed:
                        await self.ws.broadcast(changed)

                # Auto-recovery escalation: if we've been failing for >5 min
                # AND we're not in idle mode (so pollers should be running),
                # force the subclass to rebuild its networking state. Prevents
                # the zombie-session bug where a wedged aiohttp session never
                # naturally recovers.
                elif self._consecutive_failures >= 6 \
                     and hasattr(self, "_reset_session"):
                    self.logger.warning(
                        f"{self._consecutive_failures} consecutive failures; "
                        f"forcing session reset"
                    )
                    try:
                        await self._reset_session()
                    except Exception:
                        pass

            # Interruptible retry-sleep too, so a just-reconnected client sees
            # a poll attempt quickly even if the poller was in a backoff window.
            await self._interruptible_sleep(self._current_interval())
