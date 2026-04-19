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

import ssl
from typing import Any

import aiohttp

from .base import DeviceSpec
from ..unifi import UniFiPoller


class UniFiNetworkDriver:
    kind = "unifi_network"

    # The UniFi REST endpoints this driver targets all live under the
    # default site. The site slug is "default" for every UDM install the
    # author has seen; leaving it hardcoded keeps the config surface
    # smaller. Operators with multi-site controllers can override via
    # spec.extra["site"].
    DEFAULT_SITE = "default"

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
        # Reference to the UniFi poller, set by build_pollers(). Gives
        # set_wan_enabled a free ride on the already-authenticated session
        # instead of logging in again.
        self._poller: UniFiPoller | None = None

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
        self._poller = poller
        return [poller]

    # ---- WAN enable/disable -------------------------------------------
    #
    # UniFi's networkconf API requires a read-modify-write: you fetch the
    # full WAN doc, flip `enabled`, PUT the *entire* doc back. Partial
    # PATCH-style bodies get 400s. We also have to preserve the CSRF
    # cookie/header dance that `/api/auth/login` set up for us.

    async def set_wan_enabled(self, wan_index: int, enabled: bool) -> dict:
        """Enable/disable a WAN network via read-modify-write on the
        UniFi networkconf object.

        Steps:
          1. GET /proxy/network/api/s/{site}/rest/networkconf → list.
          2. Filter to WAN entries (purpose == "wan" or wan_networkgroup set).
          3. Match by `wan_networkgroup` — "WAN" → wan_index 1,
             "WAN2" → 2, and so on.
          4. PUT /proxy/network/api/s/{site}/rest/networkconf/{_id} with
             the full doc, `enabled:` overridden. UniFi rejects partial
             docs so we must preserve every other field verbatim.

        Returns {ok, wan_index, enabled, ui_name} on success. Raises
        ValueError if no matching WAN network is found; ConnectionError
        on HTTP auth failure.
        """
        site = self.spec.extra.get("site") or self.DEFAULT_SITE
        base_path = f"/proxy/network/api/s/{site}/rest/networkconf"

        # Acquire an authenticated session. Prefer reuse; fall through
        # to a short-lived one only if the poller hasn't authed yet.
        session, base_url, owns_session = await self._acquire_session()
        try:
            # 1. GET the networkconf list.
            resp = await session.get(f"{base_url}{base_path}")
            if resp.status == 401:
                raise ConnectionError(
                    "UniFi authentication failed while listing networkconf"
                )
            resp.raise_for_status()
            listing = await resp.json()
            networks = (
                listing.get("data", [])
                if isinstance(listing, dict) else listing
            )
            if not isinstance(networks, list):
                networks = []

            # 2 + 3. Find the WAN whose wan_networkgroup maps to wan_index.
            wan_target = None
            for n in networks:
                if not isinstance(n, dict):
                    continue
                purpose = (n.get("purpose") or "").lower()
                ng = (n.get("wan_networkgroup") or "").upper()
                is_wan = (
                    purpose == "wan"
                    or bool(n.get("wan_type"))
                    or bool(n.get("wan_type_v6"))
                    or ng.startswith("WAN")
                )
                if not is_wan:
                    continue
                # "WAN" (no digit) == WAN1; "WAN2"/"WAN3"/... are 2+.
                idx = 1 if ng in ("WAN", "WAN1") else None
                if idx is None and ng.startswith("WAN"):
                    try:
                        idx = int(ng[3:])
                    except ValueError:
                        idx = None
                if idx == int(wan_index):
                    wan_target = n
                    break

            if wan_target is None:
                raise ValueError(
                    f"no WAN network with wan_index={wan_index} found on UniFi "
                    f"(site={site!r}); available groups: "
                    f"{[(n.get('wan_networkgroup') or '').upper() for n in networks if isinstance(n, dict) and ((n.get('purpose') or '').lower() == 'wan' or (n.get('wan_networkgroup') or '').upper().startswith('WAN'))]}"
                )

            # 4. Merge `enabled` into the existing doc and PUT it back.
            updated = dict(wan_target)
            updated["enabled"] = bool(enabled)
            wan_id = updated.get("_id")
            if not wan_id:
                raise ValueError(
                    f"UniFi WAN entry missing _id field; cannot update"
                )

            put_resp = await session.put(
                f"{base_url}{base_path}/{wan_id}",
                json=updated,
            )
            if put_resp.status == 401:
                raise ConnectionError(
                    "UniFi authentication failed while updating networkconf"
                )
            put_resp.raise_for_status()
            # Don't bother parsing the reply body — UniFi returns the
            # updated doc but we already know what we set.
            try:
                await put_resp.json()
            except Exception:
                pass

            return {
                "ok":        True,
                "wan_index": int(wan_index),
                "enabled":   bool(enabled),
                "ui_name":   updated.get("name")
                              or updated.get("wan_networkgroup")
                              or f"WAN{wan_index}",
            }
        finally:
            if owns_session and session is not None:
                await session.close()

    async def _acquire_session(self) -> tuple[Any, str, bool]:
        """Return (session, base_url, owns_session).

        `owns_session=True` means the caller must close it; False means
        it belongs to the poller and must be left alone.
        """
        poller = self._poller
        if poller is not None and poller._session is not None \
                and not poller._session.closed:
            if not poller._authenticated:
                await poller._authenticate()
            return poller._session, poller.base_url, False

        # Short-lived session with the same SSL posture the poller uses.
        spec = self.spec
        ctx = ssl.create_default_context()
        if not spec.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        timeout = aiohttp.ClientTimeout(total=10)
        session = aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            connector=aiohttp.TCPConnector(ssl=ctx),
            timeout=timeout,
        )
        base_url = f"https://{spec.host}"
        login = await session.post(
            f"{base_url}/api/auth/login",
            json={"username": spec.username, "password": spec.password},
        )
        if login.status != 200:
            await session.close()
            raise ConnectionError(
                f"UniFi auth failed ({login.status}) during WAN toggle"
            )
        return session, base_url, True
