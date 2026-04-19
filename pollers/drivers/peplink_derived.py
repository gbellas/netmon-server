"""Peplink derived driver — InControl-managed Balance routers.

Some Peplink routers (notably the Balance 310 in the author's original
deployment) are managed exclusively through InControl 2 and expose no
reachable local REST API. For those, the only ground-truth we have is:

  - ICMP ping to the router's WAN IP (local reachability)
  - SpeedFusion peer info from the router on the OTHER side of the
    tunnel (BR1 in the original deployment, or any `peplink_router`
    entry flagged `is_mobile: true`).

The `Balance310DerivedPoller` synthesizes state keys from those inputs
under the `bal310.*` namespace. This driver wraps it into the
DeviceDriver protocol.

Config shape (minimum):

    devices:
      balance310:
        kind: peplink_derived
        host: "192.168.2.1"            # Balance 310 WAN IP (for ping+SSH)
        sf_depends_on_udm_wan: 1       # optional — for risk explanations
        ssh:
          enabled: true                # optional — enables tunnel ping
          username: "admin"
          port: 22
          targets:
            - { name: "BR1 LAN", host: "192.168.50.1", role: "tunnel" }

The "derived" in the name is load-bearing: unlike `peplink_router`, this
driver never hits the Balance's REST API. Pointing it at a router that
DOES speak local REST won't break anything, but you'd be throwing away
data — use `peplink_router` for those.

Key derivation:
  - ping_key:        derived from this device's host
    (`ping.<host-with-dots-as-underscores>`). The icmp_ping driver at
    `ping_targets` is expected to be pinging the Balance's IP; if it
    isn't, reachability reads `unknown` which is the safe default.
  - tunnel_ping_key: derived from the first `peplink_router` device
    flagged `is_mobile: true` (the BR1). Its host is the ping target
    that traverses the tunnel when the Balance SSH-pings it.

Both derivations match what the legacy startup code did verbatim (see
commit 28ad7f9^'s server.py balance310 block). That's the behaviour
contract here — changing it would silently break the Balance's UI
status on the author's live deployment.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import DeviceSpec
from ..derived import Balance310DerivedPoller
from ..br1_ssh_ping import PeplinkSshPingPoller


class PeplinkDerivedDriver:
    kind = "peplink_derived"

    def __init__(self, spec: DeviceSpec) -> None:
        self.spec = spec
        if not spec.host:
            raise ValueError(
                f"peplink_derived device {spec.id!r} missing required 'host' "
                f"(needed for ICMP ping → reachability derivation)"
            )

    # WAN toggle is not supported — this driver never talks to the
    # router directly. Surface a clear 501 at the API layer.
    async def set_wan_enabled(self, wan_index: int, enabled: bool) -> dict:
        raise NotImplementedError(
            "peplink_derived devices are InControl-managed; no local REST "
            "API is reachable. Use InControl or the router's web UI."
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
        pollers: list[Any] = []

        # --- Derive ping_key + tunnel_ping_key --------------------------
        # `ping.<host-dots-as-underscores>` matches the key-naming scheme
        # the icmp_ping PingPoller publishes under. See pollers/ping.py.
        ping_key = "ping." + spec.host.replace(".", "_")

        # Tunnel peer = first peplink_router in the parent config that is
        # flagged is_mobile. We pull that from the spec's `extra` because
        # the driver doesn't see sibling device configs; the server wires
        # it in via `extra["_peer_host"]` if it found one. Fall back to a
        # legacy-friendly default (empty → tunnel ping reports unknown).
        peer_host = spec.extra.get("_peer_host") or ""
        tunnel_ping_key = "ping." + peer_host.replace(".", "_") if peer_host else "ping."

        # Main derived poller — name matches the legacy "bal310" state-key
        # prefix for backward compatibility with dashboards / history.
        derived_cfg = {
            "host":                  spec.host,
            "name":                  spec.display_name,
            "poll_interval":         spec.poll_interval,
            "sf_depends_on_udm_wan": int(spec.extra.get("sf_depends_on_udm_wan", 0) or 0),
        }
        derived = Balance310DerivedPoller(
            config=derived_cfg,
            state=state,
            ws_manager=ws_manager,
            ping_key=ping_key,
            tunnel_ping_key=tunnel_ping_key,
            br1_name=spec.extra.get("_peer_id") or "br1",
        )
        pollers.append(derived)

        # Optional SSH ping streamer — pings tunnel peers from the
        # Balance-side router. This is how we get authoritative tunnel
        # latency (BR1 → Balance and Balance → BR1 both traverse the
        # SpeedFusion tunnel). See derived.py for how the `balance_tunnel.*`
        # keys are consumed.
        ssh_cfg = spec.extra.get("ssh") or {}
        if ssh_cfg.get("enabled"):
            ssh_poller_cfg = {
                "host":          spec.host,
                "port":          ssh_cfg.get("port", 22),
                "username":      ssh_cfg.get("username", spec.username or "admin"),
                "password":      ssh_cfg.get("password", spec.password),
                "targets":       ssh_cfg.get("targets", []),
                "ssh_timeout":   ssh_cfg.get("ssh_timeout", 10),
                "poll_interval": ssh_cfg.get("poll_interval", 30),
                "count":         ssh_cfg.get("count", 5),
            }
            ssh = PeplinkSshPingPoller(
                config=ssh_poller_cfg,
                state=state,
                ws_manager=ws_manager,
                bandwidth_meter=bandwidth_meter,
                # Unified scheme: SSH pings publish under
                # `<device_id>.<host>.*`. The dashboard enumerates them
                # the same way it does icmp_ping targets, so
                # Balance-SSH-pings-BR1 shows up under the Balance 310
                # card as a regular ping target. Legacy
                # `key_prefix_by_role` in config still honored for
                # one-release backcompat.
                poller_name=f"{spec.id}_ssh",
                state_key_root=spec.id,
                key_prefix_by_role=ssh_cfg.get("key_prefix_by_role"),
                # DO NOT pass `pause_state` here. This poller measures
                # tunnel latency from the Balance side — an iPhone on
                # the BR1 LAN can't replace it, so honoring the pause
                # lease would silently drop tunnel-health visibility.
                pause_state=None,
            )
            pollers.append(ssh)

        return pollers
