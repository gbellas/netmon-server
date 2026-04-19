"""ICMP ping poller using system ping command."""

import asyncio
import re
from collections import deque

from pollers.base import BasePoller

# Parse RTT from ping output: "round-trip min/avg/max/stddev = 11.123/12.456/13.789/1.234 ms"
# or "rtt min/avg/max/mdev = ..." on Linux
RTT_PATTERN = re.compile(r"(?:round-trip|rtt)\s+\S+\s*=\s*[\d.]+/([\d.]+)/")
# Also handle single ping: "time=12.3 ms"
TIME_PATTERN = re.compile(r"time[=<]([\d.]+)\s*ms")


class PingPoller(BasePoller):
    """Pings multiple targets and reports latency, jitter, and packet loss."""

    def __init__(self, config: dict, state, ws_manager, bandwidth_meter=None):
        super().__init__("ping", config, state, ws_manager, bandwidth_meter=bandwidth_meter)
        self.targets = config.get("targets", [])
        self.count = config.get("count", 1)
        self.timeout = config.get("timeout", 2)
        # Rolling windows for jitter and packet loss calculation
        self._history: dict[str, deque] = {}
        self._window_size = 20  # last 20 pings for stats

    async def _ping_host(self, host: str) -> tuple[float | None, bool]:
        """Ping a host, return (rtt_ms, success)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ping", "-c", str(self.count), "-W", str(self.timeout), host,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.timeout + 5)
            output = stdout.decode()

            if proc.returncode == 0:
                # Try to extract RTT
                m = TIME_PATTERN.search(output)
                if m:
                    return float(m.group(1)), True
                m = RTT_PATTERN.search(output)
                if m:
                    return float(m.group(1)), True
                return 0.0, True  # Ping succeeded but couldn't parse RTT
            return None, False
        except asyncio.TimeoutError:
            return None, False
        except Exception:
            return None, False

    def _update_stats(self, target_key: str, rtt: float | None, success: bool) -> dict:
        """Compute jitter and packet loss from rolling window."""
        if target_key not in self._history:
            self._history[target_key] = deque(maxlen=self._window_size)

        self._history[target_key].append({"rtt": rtt, "ok": success})
        window = self._history[target_key]

        # Packet loss
        total = len(window)
        losses = sum(1 for p in window if not p["ok"])
        loss_pct = round((losses / total) * 100, 1) if total > 0 else 0

        # Jitter (mean absolute difference between consecutive RTTs)
        rtts = [p["rtt"] for p in window if p["rtt"] is not None]
        jitter = 0.0
        if len(rtts) >= 2:
            diffs = [abs(rtts[i] - rtts[i - 1]) for i in range(1, len(rtts))]
            jitter = round(sum(diffs) / len(diffs), 2)

        return {
            f"{self.name}.{target_key}.jitter_ms": jitter,
            f"{self.name}.{target_key}.packet_loss_pct": loss_pct,
        }

    async def poll(self) -> dict:
        updates = {}

        # Ping all targets concurrently
        tasks = {}
        for target in self.targets:
            host = target["host"]
            tasks[host] = asyncio.create_task(self._ping_host(host))

        for target in self.targets:
            host = target["host"]
            name = target.get("name", host)
            key = host.replace(".", "_")

            rtt, success = await tasks[host]

            # Use self.name as the state-key prefix so driver-based
            # pollers with custom IDs publish under `<id>.*` and the
            # UI's UserDeviceSection / PingTargetsSection can find them.
            # Legacy PingPoller instances pass name="ping" and keep
            # publishing `ping.*` — backwards compatible.
            updates[f"{self.name}.{key}.name"] = name
            updates[f"{self.name}.{key}.host"] = host
            updates[f"{self.name}.{key}.hidden"] = bool(target.get("hidden", False))
            updates[f"{self.name}.{key}.status"] = "ok" if success else "timeout"
            updates[f"{self.name}.{key}.latency_ms"] = round(rtt, 2) if rtt is not None else -1

            stats = self._update_stats(key, rtt, success)
            updates.update(stats)

        return updates
