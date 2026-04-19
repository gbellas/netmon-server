"""Lightweight byte counters attributed to the NetMon subsystems that use
network bandwidth. Three buckets:

  - "lan": traffic over the home LAN (UDM polls, Balance polls, local pings).
           Free — doesn't touch cellular or WAN.
  - "wan": internet traffic that leaves home but is cheap (IC2 cloud polls,
           client WebSockets from off-LAN, REST state fetches from clients).
           Burns home ISP quota (usually unlimited fiber).
  - "cellular": traffic that crosses the SpeedFusion tunnel to the BR1 AND
                rides the BR1's cellular WAN. This is the expensive one when
                the truck's on LTE. Includes BR1 REST polls + all SSH ping
                streams.

Per-subsystem breakdown lets the app show exactly which poller is eating
data. The meter is monotonic since process start; counters reset on restart.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class _Bucket:
    bytes_in:  int = 0
    bytes_out: int = 0
    def add(self, inb: int, outb: int) -> None:
        self.bytes_in  += int(inb)
        self.bytes_out += int(outb)
    def total(self) -> int: return self.bytes_in + self.bytes_out
    def as_dict(self) -> dict:
        return {"bytes_in": self.bytes_in, "bytes_out": self.bytes_out,
                "total": self.total()}


class BandwidthMeter:
    """One instance per NetMon process. Thread-safe."""

    # Traffic category labels. Each category corresponds to a different
    # network path; `sf_tunnel` is the expensive one because the tunnel rides
    # the BR1's bonded WANs (Starlink + cellular — the split is up to the
    # BR1's SpeedFusion policy, not something NetMon can infer).
    TIER_LAN       = "lan"
    TIER_WAN       = "wan"
    TIER_SF_TUNNEL = "sf_tunnel"

    SUBSYSTEM_TIER: dict[str, str] = {
        "udm_polls":       TIER_LAN,
        "balance_polls":   TIER_LAN,
        "mac_pings":       TIER_LAN,
        "br1_rest_polls":  TIER_SF_TUNNEL,
        "br1_ssh_pings":   TIER_SF_TUNNEL,
        "ic2_cloud":       TIER_WAN,
        "ws_broadcasts":   TIER_WAN,
        "rest_api_clients":TIER_WAN,
    }

    def __init__(self):
        self._lock = Lock()
        self._started_at = time.time()
        self._buckets: dict[str, _Bucket] = {
            k: _Bucket() for k in self.SUBSYSTEM_TIER.keys()
        }

    def record(self, subsystem: str, bytes_in: int = 0, bytes_out: int = 0) -> None:
        b = self._buckets.get(subsystem)
        if b is None:
            # Unknown subsystem — track it anyway but mark as WAN.
            b = _Bucket()
            self._buckets[subsystem] = b
            self.SUBSYSTEM_TIER[subsystem] = self.TIER_WAN
        with self._lock:
            b.add(bytes_in, bytes_out)

    def snapshot(self) -> dict:
        """Return the full bandwidth accounting as a JSON-friendly dict."""
        with self._lock:
            started = self._started_at
            per_subsys = {k: b.as_dict() for k, b in self._buckets.items()}
        elapsed = max(1, time.time() - started)
        # Aggregate per tier
        tiers: dict[str, dict] = {}
        grand_in = grand_out = 0
        for sub, stats in per_subsys.items():
            tier = self.SUBSYSTEM_TIER.get(sub, self.TIER_WAN)
            t = tiers.setdefault(tier, {"bytes_in": 0, "bytes_out": 0, "total": 0})
            t["bytes_in"]  += stats["bytes_in"]
            t["bytes_out"] += stats["bytes_out"]
            t["total"]     += stats["total"]
            grand_in  += stats["bytes_in"]
            grand_out += stats["bytes_out"]
            stats["tier"] = tier
            stats["bytes_per_sec"] = round(stats["total"] / elapsed, 1)
        return {
            "started_at": started,
            "elapsed_seconds": int(elapsed),
            "per_subsystem": per_subsys,
            "per_tier":      tiers,
            "total_bytes_in":  grand_in,
            "total_bytes_out": grand_out,
            "total_bytes":     grand_in + grand_out,
            "total_bytes_per_sec": round((grand_in + grand_out) / elapsed, 1),
        }
