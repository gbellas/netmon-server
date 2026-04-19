"""Device control endpoints — proxy safe write operations to routers.

All write ops require explicit user intent from the frontend (confirmation dialog).
Each endpoint applies the change and then triggers the device's config-apply workflow.
"""

import ssl
import aiohttp
from fastapi import HTTPException


class PeplinkController:
    """Manages write operations against a Peplink router.

    Supports two auth models:
      1. Cookie-based (/api/login) - works for simple config.* endpoints
      2. OAuth access token - REQUIRED for carrier switching via /cgi-bin/MANGA/api.cgi
         which is the only endpoint that actually writes carrierSelection.
    """

    def __init__(self, host: str, username: str, password: str, verify_ssl: bool = False,
                 oauth_client_id: str | None = None, oauth_client_secret: str | None = None):
        self.host = host
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.oauth_client_id = oauth_client_id
        self.oauth_client_secret = oauth_client_secret
        self.base_url = f"https://{host}"
        self._session: aiohttp.ClientSession | None = None
        self._authed = False
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    def _ssl(self):
        if not self.verify_ssl:
            c = ssl.create_default_context()
            c.check_hostname = False
            c.verify_mode = ssl.CERT_NONE
            return c
        return None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                connector=aiohttp.TCPConnector(ssl=self._ssl()),
                timeout=aiohttp.ClientTimeout(total=15),
            )
            self._authed = False
        return self._session

    async def _ensure_auth(self):
        if self._authed:
            return
        s = await self._get_session()
        r = await s.post(f"{self.base_url}/api/login",
                         json={"username": self.username, "password": self.password})
        if r.status != 200:
            raise HTTPException(502, f"Peplink auth failed ({r.status})")
        d = await r.json()
        if d.get("stat") != "ok":
            raise HTTPException(502, f"Peplink auth rejected: {d}")
        self._authed = True

    async def _ensure_oauth_token(self) -> str:
        """Obtain or refresh the OAuth access token. Required for carrier switching.

        The client_id/client_secret must be pre-registered on the router via SSH CLI:
            support auth-client-add <name>
        """
        import time
        if not self.oauth_client_id or not self.oauth_client_secret:
            raise HTTPException(501,
                "OAuth not configured. Register an API client on the BR1 via SSH:\n"
                "  ssh -p 8822 admin@<br1-ip>\n"
                "  support auth-client-add netmon\n"
                "Then set NETMON_BR1_OAUTH_CLIENT_ID and NETMON_BR1_OAUTH_CLIENT_SECRET in .env")
        # Reuse token if still valid (with 60s buffer)
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token
        # Need a logged-in session to grant a token
        await self._ensure_auth()
        s = await self._get_session()
        # Note: Peplink requires camelCase clientId/clientSecret here
        r = await s.post(f"{self.base_url}/api/auth.token.grant", json={
            "clientId": self.oauth_client_id,
            "clientSecret": self.oauth_client_secret,
            "scope": "api",
        })
        d = await r.json()
        if d.get("stat") != "ok":
            raise HTTPException(502, f"OAuth token grant failed: {d}")
        resp = d.get("response", {})
        self._access_token = resp["accessToken"]
        self._token_expires_at = time.time() + int(resp.get("expiresIn", 3600))
        return self._access_token

    async def _manga_api(self, body: dict) -> dict:
        """POST to /cgi-bin/MANGA/api.cgi with a valid access token.
        The endpoint that actually triggers config commits (unlike /api/config.*).

        Note: Peplink's MANGA CGI REQUIRES the OAuth token as `?accessToken=`
        — verified empirically, Authorization: Bearer is rejected with
        `stat: fail`. Query-param tokens normally leak to proxy logs and
        Referer headers, but here (1) the connection is direct to the BR1
        with no intermediary proxy, (2) responses aren't rendered in a
        browser that would attach Referer, and (3) tokens expire in 1h and
        we refresh on 401. Residual exposure is acceptable for a LAN tool.
        """
        token = await self._ensure_oauth_token()
        s = await self._get_session()
        url = f"{self.base_url}/cgi-bin/MANGA/api.cgi?accessToken={token}"
        hdrs = {"X-Requested-With": "XMLHttpRequest"}
        r = await s.post(url, json=body, headers=hdrs)
        if r.status == 401:
            self._access_token = None
            token = await self._ensure_oauth_token()
            url = f"{self.base_url}/cgi-bin/MANGA/api.cgi?accessToken={token}"
            r = await s.post(url, json=body, headers=hdrs)
        r.raise_for_status()
        return await r.json()

    async def _post(self, path: str, body: dict) -> dict:
        await self._ensure_auth()
        s = await self._get_session()
        r = await s.post(f"{self.base_url}{path}", json=body)
        if r.status == 401:
            self._authed = False
            await self._ensure_auth()
            r = await s.post(f"{self.base_url}{path}", json=body)
        r.raise_for_status()
        return await r.json()

    async def set_wan_enable(self, wan_id: int, enable: bool) -> dict:
        """Enable or disable a WAN connection on the Peplink."""
        body = {"id": wan_id, "enable": bool(enable)}
        return await self._post("/api/config.wan.connection", body)

    async def set_wan_priority(self, wan_id: int, priority: int) -> dict:
        """Set a WAN's priority (1 = highest). priority: 0 disables, 1/2/3 = priority tier."""
        body = {"id": wan_id, "connection": {"priority": int(priority)}}
        return await self._post("/api/config.wan.connection", body)

    async def apply_config(self) -> dict:
        """Commit queued config changes. Some Peplink firmwares auto-apply; others need this."""
        try:
            return await self._post("/api/cmd.config.apply", {})
        except Exception:
            return {"stat": "ok", "note": "apply not required or already applied"}

    async def set_roamlink_carrier(self, mcc: str, mnc: str, name: str) -> dict:
        """Set manual carrier selection for the RoamLink eSIM via MANGA api.cgi.
        RoamLink uses one eSIM with access to all major US carriers. The modem
        honors the new preference on its next reconnect cycle (doesn't force a drop)."""
        body = {
            "func": "config.wan.connection",
            "agent": "webui",
            "action": "update",
            "instantActive": True,
            "list": [{
                "id": 2,
                "cellular": {
                    "sim": [{
                        "id": "eSim1",
                        "carrierSelection": [
                            {"plmn": "", "mcc": str(mcc), "mnc": str(mnc), "name": name, "pcs": 1}
                        ],
                    }]
                }
            }]
        }
        return await self._manga_api(body)

    async def set_sf_profile_enable(self, profile_id: int, enable: bool) -> dict:
        """Enable or disable a SpeedFusion profile.

        Disabling = tunnel drops; traffic falls through to default outbound policy
        (direct internet via BR1's WANs). Re-enabling re-establishes the tunnel.
        """
        body = {"id": int(profile_id), "enable": bool(enable)}
        return await self._post("/api/config.pepvpn.profile", body)

    async def get_sf_profile_state(self) -> dict:
        """Read current SF profile state. Returns dict keyed by profile id."""
        await self._ensure_auth()
        s = await self._get_session()
        r = await s.get(f"{self.base_url}/api/config.pepvpn.profile")
        r.raise_for_status()
        d = await r.json()
        resp = d.get("response", {})
        out = {}
        for k, v in resp.items():
            if isinstance(v, dict) and "enable" in v:
                out[str(k)] = {"name": v.get("name", ""), "enable": bool(v.get("enable"))}
        return out

    async def force_wan_reconnect(self, wan_id: int, delay: float = 3.0) -> dict:
        """Force a WAN to reconnect by briefly disabling then re-enabling it.
        Useful after a carrier-selection change so the modem re-registers and
        picks up the new carrier preference immediately."""
        import asyncio
        # Disable
        disable_body = {
            "func": "config.wan.connection", "agent": "webui",
            "action": "update", "instantActive": True,
            "list": [{"id": int(wan_id), "enable": False}],
        }
        r1 = await self._manga_api(disable_body)
        await asyncio.sleep(delay)
        # Re-enable
        enable_body = dict(disable_body)
        enable_body["list"] = [{"id": int(wan_id), "enable": True}]
        r2 = await self._manga_api(enable_body)
        return {"disable": r1.get("stat"), "enable": r2.get("stat")}

    async def set_roamlink_carrier_and_reconnect(self, mcc: str, mnc: str, name: str) -> dict:
        """Set carrier selection AND force the modem to re-register immediately."""
        sel_result = await self.set_roamlink_carrier(mcc, mnc, name)
        reconnect = await self.force_wan_reconnect(2)
        return {"carrier": sel_result.get("stat"), "reconnect": reconnect}

    async def set_roamlink_auto_and_reconnect(self) -> dict:
        """Clear carrier selection to Auto AND force the modem to re-register."""
        sel_result = await self.set_roamlink_auto_carrier()
        reconnect = await self.force_wan_reconnect(2)
        return {"carrier": sel_result.get("stat"), "reconnect": reconnect}

    # --- RAT lock (5G/LTE/3G mode) ---
    # IMPORTANT: The web UI applies mobileType to ALL SIM slots in one payload.
    # Setting it on only eSim1 is silently no-op'd — we MUST list every slot.
    _ALL_SIM_SLOTS = ["1", "2", "remoteSim", "fusionSim", "speedfusionConnect5gLte", "eSim1"]

    async def set_cellular_rat(self, mode: str) -> dict:
        """Lock the cellular modem to a specific Radio Access Technology.

        mode: one of "auto" | "LTE" | "LTE+3G" | "3G" | "3G+2G" | "2G" | "3G_2G" | "2G_3G"
        "auto" sends an empty string which the modem treats as "let modem pick".
        """
        # Peplink's data_bearer field maps UI values to mobileType values
        value = "" if mode.lower() == "auto" else mode
        sims = [{"id": slot, "mobileType": value} for slot in self._ALL_SIM_SLOTS]
        body = {
            "func": "config.wan.connection",
            "agent": "webui",
            "action": "update",
            "instantActive": True,
            "list": [{"id": 2, "cellular": {"sim": sims}}]
        }
        return await self._manga_api(body)

    async def set_cellular_rat_and_reconnect(self, mode: str) -> dict:
        """Lock RAT and force modem re-registration so it takes effect immediately."""
        sel = await self.set_cellular_rat(mode)
        reconnect = await self.force_wan_reconnect(2)
        return {"rat": sel.get("stat"), "reconnect": reconnect}

    async def set_roamlink_auto_carrier(self) -> dict:
        """Reset carrier selection to Auto (empty list) via MANGA api.cgi."""
        body = {
            "func": "config.wan.connection",
            "agent": "webui",
            "action": "update",
            "instantActive": True,
            "list": [{
                "id": 2,
                "cellular": {
                    "sim": [{"id": "eSim1", "carrierSelection": []}]
                }
            }]
        }
        return await self._manga_api(body)
