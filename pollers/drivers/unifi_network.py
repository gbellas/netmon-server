"""UniFi Network driver.

Covers UniFi OS gateways with the standard Network application:
 - UDM / UDM Pro / UDM SE / UDM Max
 - Dream Machine (base model)
 - Cloud Gateway Ultra / Cloud Gateway Max
 - Standalone UniFi Network controllers (CloudKey, self-hosted)

The underlying `UniFiPoller` talks to `/api/auth/login` and the v2
proxy network endpoints on the gateway's HTTPS port 443. That API is
stable across UDM firmware generations; if UI/UniFi OS changes ever
break something, the fix goes in `pollers/unifi.py`, not this driver.
"""

from __future__ import annotations

from typing import Any

from .base import DeviceSpec
from ..unifi import UniFiPoller


class UniFiNetworkDriver:
    kind = "unifi_network"

    def __init__(self, spec: DeviceSpec) -> None:
        self.spec = spec
        if not spec.host:
            raise ValueError(
                f"unifi_network device {spec.id!r} missing required 'host'"
            )
        if not spec.username:
            raise ValueError(
                f"unifi_network device {spec.id!r} missing required 'username'"
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
            "host":          spec.host,
            "username":      spec.username,
            "password":      spec.password,
            "poll_interval": spec.poll_interval,
            "verify_ssl":    spec.verify_ssl,
            "wan_carriers":  spec.wan_carriers,
        }
        # UniFiPoller historically hardcoded its `name` (state-key
        # prefix) to "udm". We patch the instance's `name` attribute
        # + logger after construction rather than changing the poller
        # class signature, so this refactor stays additive.
        import logging
        poller = UniFiPoller(
            config=cfg,
            state=state,
            ws_manager=ws_manager,
            bandwidth_meter=bandwidth_meter,
        )
        poller.name = spec.id
        poller.logger = logging.getLogger(f"netmon.{spec.id}")
        return [poller]
