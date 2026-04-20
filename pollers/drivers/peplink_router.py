"""Peplink router driver.

Covers every Peplink model that speaks the same local REST API:
 - BR1 Pro 5G / BR1 Mini / BR1 Classic / BR1 Slim
 - MAX Transit / MAX HD2 / MAX HD4 / MAX BR2
 - MBX 5G / MBX Mini
 - Balance 20 / 30 / 50 / 310 / 710 / 1350
 - SoHo

The `is_mobile` flag in DeviceSpec toggles cellular-specific parsing
(RAT, signal bars, active bands). Balance-family routers set it to false;
BR1/MAX/MBX-family set it to true. Everything else (REST endpoints, auth
flow, poller cadence) is identical across models.

Optional features via `extra`:
  - `extra["ssh"]`: if present and enabled, spawns a SSH ping streamer
    that runs `support ping <target>` on the router itself. Used for
    tunnel-peer latency and outbound internet pings with per-WAN pinning.
    See pollers/br1_ssh_ping.py for the mechanics.
  - `extra["sf_depends_on_gateway_wan"]`: declares SpeedFusion dependency
    on an upstream gateway's WAN; consumed by the alerts engine to
    produce "BR1 unreachable because home fiber is down" explanations.
"""

from __future__ import annotations

import asyncio
from typing import Any

import ssl

import aiohttp

from .base import DeviceSpec
from ..peplink import PeplinkPoller
from ..br1_ssh_ping import PeplinkSshPingPoller

# Lazy import to avoid pulling controls.py (and its fastapi dependency)
# into the driver module during test collection.
def _make_controller(spec: DeviceSpec) -> Any:
    """Build a PeplinkController bound to this device's host/creds.

    OAuth credentials are resolved from the environment using the
    per-device prefix `NETMON_<ID_UPPER>_OAUTH_CLIENT_ID/SECRET`, which
    matches server.py's `_get_controller` so users can keep their
    existing env vars for the `br1` device id.
    """
    import os
    from controls import PeplinkController
    env_prefix = f"NETMON_{spec.id.upper()}_OAUTH"
    return PeplinkController(
        host=spec.host,
        username=spec.username or "admin",
        password=spec.password or "",
        verify_ssl=spec.verify_ssl,
        oauth_client_id=os.environ.get(f"{env_prefix}_CLIENT_ID"),
        oauth_client_secret=os.environ.get(f"{env_prefix}_CLIENT_SECRET"),
    )


class PeplinkRouterDriver:
    kind = "peplink_router"

    def __init__(self, spec: DeviceSpec) -> None:
        self.spec = spec
        if not spec.host:
            raise ValueError(
                f"peplink_router device {spec.id!r} missing required 'host'"
            )
        if not spec.username:
            raise ValueError(
                f"peplink_router device {spec.id!r} missing required 'username'"
            )
        # Password may be blank on the spec and resolved from env by
        # server.py before this runs. We don't enforce it here — pollers
        # report an auth error at runtime which surfaces cleanly via
        # /api/health.

        # Shared ping lock so the REST poller and SSH ping streams don't
        # both hold the router's `support ping` CLI simultaneously. Peplink
        # routers serialize CLI access globally, and collisions look like
        # flapping latency.
        self._shared_ping_lock = asyncio.Lock()

        # Reference to the REST poller set by `build_pollers`. We reuse
        # its authenticated aiohttp session for WAN toggle requests so
        # we don't force a second login round-trip per click.
        self._rest_poller: PeplinkPoller | None = None

        # Lazily-constructed PeplinkController used for the higher-level
        # control endpoints (carrier / RAT / SF enable). Kept separate
        # from the REST poller's session because PeplinkController owns
        # its own OAuth token + cookie jar lifecycle.
        self._controller: Any | None = None

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

        # Main REST poller (device state: WANs, signal, bands, clients…)
        rest_cfg = {
            "host":           spec.host,
            "username":       spec.username,
            "password":       spec.password,
            "poll_interval":  spec.poll_interval,
            "verify_ssl":     spec.verify_ssl,
            "is_mobile":      spec.is_mobile,
            "wan_carriers":   spec.wan_carriers,
        }
        # Forward any extra SpeedFusion dependency metadata so the
        # peplink poller can attach it to state (for the alerts engine).
        if "sf_depends_on_gateway_wan" in spec.extra:
            rest_cfg["sf_depends_on_gateway_wan"] = spec.extra["sf_depends_on_gateway_wan"]
        # Legacy config keys the existing poller still recognizes.
        if "sf_depends_on_udm_wan" in spec.extra:
            rest_cfg["sf_depends_on_udm_wan"] = spec.extra["sf_depends_on_udm_wan"]

        rest = PeplinkPoller(
            name=spec.id,
            device_name=spec.display_name,
            config=rest_cfg,
            state=state,
            ws_manager=ws_manager,
            bandwidth_meter=bandwidth_meter,
        )
        pollers.append(rest)
        self._rest_poller = rest

        # Optional SSH ping streamer — many users won't enable this. It
        # requires the router's `support ping` CLI to be reachable over
        # SSH which, on BR1 family especially, needs the admin to have
        # explicitly enabled SSH + set a password.
        ssh_cfg = spec.extra.get("ssh") or {}
        if ssh_cfg.get("enabled"):
            ssh = PeplinkSshPingPoller(
                config={
                    "host":         spec.host,
                    "port":         ssh_cfg.get("port", 22),
                    "username":     ssh_cfg.get("username", spec.username),
                    # Password reuses the primary device password unless
                    # overridden (some Peplink setups use a separate
                    # 'radmin' account for SSH).
                    # Use the device's main password when the SSH block
                    # doesn't override it. Treat empty strings the same as
                    # missing — otherwise saves that don't re-type the SSH
                    # password silently fall through to auth with "", which
                    # fails on every Peplink / UniFi device.
                    "password":     (ssh_cfg.get("password") or spec.password),
                    "targets":      ssh_cfg.get("targets", []),
                    "ssh_timeout":  ssh_cfg.get("ssh_timeout", 10),
                    "poll_interval": ssh_cfg.get("poll_interval", 30),
                    "count":         ssh_cfg.get("count", 5),
                },
                state=state,
                ws_manager=ws_manager,
                bandwidth_meter=bandwidth_meter,
                # Unified scheme: SSH pings publish under
                # `<device_id>.<host>.*` (matches icmp_ping devices), so
                # the dashboard enumerates them the same way. Legacy
                # `key_prefix_by_role` still honored if explicitly set in
                # config — one-release backcompat.
                key_prefix_by_role=ssh_cfg.get("key_prefix_by_role"),
                poller_name=f"{spec.id}_ssh",
                state_key_root=spec.id,
                pause_state=pause_state,
            )
            # Share the ping lock with the REST poller if the existing
            # PeplinkSshPingPoller exposes the attribute. (It does as of
            # the additive refactor; older revisions may not.)
            if hasattr(ssh, "_ping_lock") and hasattr(rest, "_ping_lock"):
                # Unify so a lock acquired by one path blocks the other.
                rest._ping_lock = self._shared_ping_lock  # type: ignore[attr-defined]
                ssh._ping_lock = self._shared_ping_lock   # type: ignore[attr-defined]
            pollers.append(ssh)

        return pollers

    async def set_wan_enabled(self, wan_index: int, enabled: bool) -> dict:
        """Toggle a WAN via the Peplink local REST API.

        Strategy: reuse the REST poller's authenticated aiohttp session
        so we don't pay for a separate login. If the REST poller hasn't
        authenticated yet (e.g. the server is very new, or the router
        was down at startup), fall through to a short-lived session that
        logs in just for this call.

        Uses the same `/api/config.wan.connection` endpoint the legacy
        `controls.py` / `PeplinkController` path used. That path is
        known-working on every Peplink firmware the author has tested
        against (BR1 Pro 5G, MAX Transit, Balance 20/50/310, MBX).
        `{id: <wan_index>, enable: <bool>}` is the documented body.
        """
        spec = self.spec
        body = {"id": int(wan_index), "enable": bool(enabled)}

        # Preferred path: piggyback on the poller's live session.
        rest = self._rest_poller
        if rest is not None and rest._session is not None and not rest._session.closed:
            if not rest._authenticated:
                await rest._authenticate()
            session = rest._session
            resp = await session.post(
                f"{rest.base_url}/api/config.wan.connection", json=body,
            )
            if resp.status == 401:
                rest._authenticated = False
                await rest._authenticate()
                resp = await session.post(
                    f"{rest.base_url}/api/config.wan.connection", json=body,
                )
            resp.raise_for_status()
            data = await resp.json()
            # Best-effort apply. Firmwares vary — some auto-apply on the
            # write, others want an explicit config apply. We mirror
            # `PeplinkController.apply_config()` which swallows the error
            # because "apply not required" is benign.
            try:
                apply_resp = await session.post(
                    f"{rest.base_url}/api/cmd.config.apply", json={},
                )
                apply_resp.raise_for_status()
            except Exception:
                pass
            return data

        # Fallback: spin a short-lived session. Happens when the REST
        # poller is present but never successfully authed, or in the
        # unlikely case set_wan_enabled is called before build_pollers.
        ctx = ssl.create_default_context()
        if not spec.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            connector=aiohttp.TCPConnector(ssl=ctx),
            timeout=timeout,
        ) as s:
            base = f"https://{spec.host}"
            login = await s.post(
                f"{base}/api/login",
                json={"username": spec.username, "password": spec.password},
            )
            login.raise_for_status()
            resp = await s.post(f"{base}/api/config.wan.connection", json=body)
            resp.raise_for_status()
            data = await resp.json()
            try:
                apply_resp = await s.post(f"{base}/api/cmd.config.apply", json={})
                apply_resp.raise_for_status()
            except Exception:
                pass
            return data

    def _get_controller(self) -> Any:
        """Return a memoized PeplinkController scoped to this device."""
        if self._controller is None:
            self._controller = _make_controller(self.spec)
        return self._controller

    async def set_carrier(self, carrier: str) -> dict:
        """Switch RoamLink eSIM carrier and force an immediate re-register.
        Accepts "verizon" / "att" / "tmobile" / "auto"."""
        # Local import to avoid a top-level dependency on the server's
        # CARRIERS constant (which also pulls HTTPException via controls).
        carriers = {
            "verizon": {"mcc": "311", "mnc": "480", "name": "Verizon"},
            "att":     {"mcc": "310", "mnc": "410", "name": "AT&T"},
            "tmobile": {"mcc": "310", "mnc": "260", "name": "T-Mobile"},
        }
        key = carrier.lower().strip()
        ctrl = self._get_controller()
        if key == "auto":
            return await ctrl.set_roamlink_auto_and_reconnect()
        if key in carriers:
            c = carriers[key]
            return await ctrl.set_roamlink_carrier_and_reconnect(
                c["mcc"], c["mnc"], c["name"],
            )
        raise ValueError(
            f"Unknown carrier '{carrier}'. Use: verizon, att, tmobile, auto"
        )

    async def set_rat(self, mode: str) -> dict:
        """Lock cellular Radio Access Technology and force an immediate
        re-registration. Accepts "auto" / "LTE" / "LTE+3G" / "3G" / ..."""
        valid = {"auto", "LTE", "LTE+3G", "3G+2G", "3G", "2G",
                 "3G_2G", "2G_3G"}
        if mode not in valid:
            raise ValueError(
                f"Invalid mode '{mode}'. Valid: {', '.join(sorted(valid))}"
            )
        ctrl = self._get_controller()
        return await ctrl.set_cellular_rat_and_reconnect(mode)

    async def set_sf_enable(self, enabled: bool, profile_id: int = 1) -> dict:
        """Toggle a SpeedFusion profile on/off. profile_id defaults to 1
        which matches the BR1's primary tunnel in the default config."""
        ctrl = self._get_controller()
        res = await ctrl.set_sf_profile_enable(int(profile_id), bool(enabled))
        await ctrl.apply_config()
        return res

    @staticmethod
    def _default_key_prefixes(device_id: str) -> dict[str, str]:
        """Per-role state-key prefix defaults.

        For a device with id "truck", internet pings land at
        `truck_internet.<host>.latency_ms` and tunnel pings at
        `truck_tunnel.<host>.latency_ms`. The iPhone app's ping sections
        look up `<id>_internet.*` and `<id>_tunnel.*` when rendering.
        """
        return {
            "internet": f"{device_id}_internet",
            "tunnel":   f"{device_id}_tunnel",
        }
