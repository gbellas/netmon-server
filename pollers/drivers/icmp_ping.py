"""ICMP ping driver.

Not a device per se — this driver represents a named collection of
ping targets polled from the NetMon server host itself. Useful for LAN
reachability ("is the gateway up?") and sanity checks ("can the server
reach 1.1.1.1?"). No auth required.

Config shape:

    devices:
      lan_checks:
        kind: icmp_ping
        name: "LAN reachability"
        targets:
          - { name: "Gateway",     host: "192.168.1.1" }
          - { name: "Cloudflare",  host: "1.1.1.1" }
        interval: 5
        timeout: 2

The underlying `PingPoller` remains in use; this driver just wraps it
into the DeviceDriver protocol so it can coexist in the generic
devices: list.
"""

from __future__ import annotations

from typing import Any

from .base import DeviceSpec
from ..ping import PingPoller


class IcmpPingDriver:
    kind = "icmp_ping"

    def __init__(self, spec: DeviceSpec) -> None:
        self.spec = spec
        if not spec.extra.get("targets"):
            raise ValueError(
                f"icmp_ping device {spec.id!r} needs at least one target "
                f"under 'targets:'"
            )

    def build_pollers(
        self,
        *,
        state: Any,
        ws_manager: Any,
        bandwidth_meter: Any = None,
        pause_state: Any = None,
    ) -> list[Any]:
        spec = self.spec
        cfg = {
            "targets":       spec.extra["targets"],
            "count":         spec.extra.get("count", 1),
            "timeout":       spec.extra.get("timeout", 2),
            "poll_interval": spec.extra.get("interval", 5),
        }
        # PingPoller hardcodes its `name` to "ping". Patch to the device id
        # so state keys are `<id>.<metric>` instead of `ping.<metric>`.
        import logging
        poller = PingPoller(
            config=cfg,
            state=state,
            ws_manager=ws_manager,
            bandwidth_meter=bandwidth_meter,
        )
        poller.name = spec.id
        poller.logger = logging.getLogger(f"netmon.{spec.id}")
        return [poller]
