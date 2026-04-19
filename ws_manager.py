"""WebSocket connection manager with broadcast support."""

import asyncio
import json
import time
import logging
from fastapi import WebSocket

logger = logging.getLogger("netmon.ws")


class WSManager:
    """Manages WebSocket connections and broadcasts state updates."""

    def __init__(self, state, bandwidth_meter=None):
        self._clients: set[WebSocket] = set()
        self._state = state
        self._meter = bandwidth_meter
        # When the last client disconnected. While clients are connected,
        # we hold this at +inf to signal "not idle".
        self._last_disconnect_at: float = time.time()

    def has_clients(self) -> bool:
        return len(self._clients) > 0

    def seconds_since_last_client(self) -> float:
        """0 while clients are connected; otherwise seconds since the last
        client disconnected."""
        if self.has_clients():
            return 0.0
        return max(0.0, time.time() - self._last_disconnect_at)

    def is_idle(self, threshold_sec: float = 60.0) -> bool:
        """True when no clients have been connected for at least `threshold_sec`.
        Used by expensive (cellular-burning) pollers to pause themselves."""
        return self.seconds_since_last_client() >= threshold_sec

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._clients.add(ws)
        logger.info(f"Client connected ({len(self._clients)} total)")
        # Send full state on connect — same timeout protection as broadcast
        # so a slow client can't hang the acceptance handler.
        full = {
            "type": "full_state",
            "timestamp": time.time(),
            "data": self._state.get_all(),
            "history": self._state.get_history(),
        }
        payload = json.dumps(full)
        try:
            await asyncio.wait_for(ws.send_text(payload), timeout=10.0)
            if self._meter is not None:
                # Count bytes per-client on connect (big full_state payload).
                self._meter.record("ws_broadcasts", bytes_out=len(payload))
        except Exception as e:
            logger.warning(f"initial full_state send failed: {e}")
            self._clients.discard(ws)

    def disconnect(self, ws: WebSocket):
        self._clients.discard(ws)
        logger.info(f"Client disconnected ({len(self._clients)} total)")
        if not self._clients:
            # Start the idle countdown from now.
            self._last_disconnect_at = time.time()

    async def broadcast(self, delta: dict):
        if not delta or not self._clients:
            return
        msg = json.dumps({
            "type": "update",
            "timestamp": time.time(),
            "data": delta,
        })
        # IMPORTANT: send to all clients concurrently with a per-client
        # timeout. The old serial `for / await send_text` meant one client
        # with TCP backpressure (phone on marginal wifi, half-closed socket)
        # blocked ALL subsequent broadcasts for every other client. That
        # manifested as "nothing updates in the app" even though pollers
        # were mutating state fine.
        clients = list(self._clients)
        msg_len = len(msg)

        async def _send_one(ws):
            try:
                await asyncio.wait_for(ws.send_text(msg), timeout=3.0)
                return (ws, True)
            except Exception:
                return (ws, False)

        results = await asyncio.gather(*[_send_one(ws) for ws in clients],
                                       return_exceptions=False)
        success = 0
        for ws, ok in results:
            if ok: success += 1
            else: self._clients.discard(ws)
        if self._meter is not None and success > 0:
            # Bytes-out is msg_len × num successful clients.
            self._meter.record("ws_broadcasts", bytes_out=msg_len * success)
