"""UDM-SE WAN control: change priority via UniFi networkconf REST."""

import asyncio
import ssl
import time
import aiohttp
from fastapi import HTTPException


class UdmController:
    def __init__(self, host: str, username: str, password: str, verify_ssl: bool = False):
        self.host = host
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.base_url = f"https://{host}"
        self._session: aiohttp.ClientSession | None = None
        self._authed = False
        self._csrf: str | None = None
        # Remember last non-disabled wan_type per networkconf _id so we can restore on Enable
        self._prior_wan_type: dict[str, str] = {}

    def _ssl(self):
        if not self.verify_ssl:
            c = ssl.create_default_context(); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE
            return c
        return None

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                connector=aiohttp.TCPConnector(ssl=self._ssl()),
                timeout=aiohttp.ClientTimeout(total=15),
            )
            self._authed = False
        return self._session

    async def _auth(self):
        if self._authed and self._csrf:
            return
        s = await self._sess()
        r = await s.post(f"{self.base_url}/api/auth/login",
                         json={"username": self.username, "password": self.password})
        if r.status != 200:
            raise HTTPException(502, f"UDM auth failed ({r.status})")
        # UniFi OS returns CSRF token in these headers on login
        self._csrf = r.headers.get("X-CSRF-Token") or r.headers.get("X-Updated-CSRF-Token")
        self._authed = True

    def _write_headers(self) -> dict:
        """Headers required for state-changing requests."""
        return {"X-CSRF-Token": self._csrf or "", "Content-Type": "application/json"}

    async def list_wans(self) -> list[dict]:
        """Return simplified list of WAN entries."""
        await self._auth()
        s = await self._sess()
        r = await s.get(f"{self.base_url}/proxy/network/api/s/default/rest/networkconf")
        r.raise_for_status()
        data = await r.json()
        wans = []
        for nc in data.get("data", []):
            if nc.get("purpose") != "wan": continue
            wans.append({
                "id": nc.get("_id"),
                "name": nc.get("name"),
                "networkgroup": nc.get("wan_networkgroup"),
                "priority": nc.get("wan_failover_priority"),
                "type": nc.get("wan_type"),
            })
        return wans

    async def _find_wan_conf(self, wan_id: int) -> dict:
        """Return the networkconf dict for UDM WAN1/WAN2 (wan_networkgroup WAN/WAN2)."""
        await self._auth()
        s = await self._sess()
        r = await s.get(f"{self.base_url}/proxy/network/api/s/default/rest/networkconf")
        r.raise_for_status()
        data = await r.json()
        target_group = "WAN" if wan_id == 1 else f"WAN{wan_id}"
        for nc in data.get("data", []):
            if nc.get("purpose") == "wan" and nc.get("wan_networkgroup") == target_group:
                return nc
        raise HTTPException(404, f"UDM WAN{wan_id} (group={target_group}) not found")

    async def _put_wan(self, wan_id: int, update: dict) -> dict:
        """PUT an update to the given WAN's networkconf entry, retrying once on CSRF issues."""
        await self._auth()
        nc = await self._find_wan_conf(wan_id)
        url = f"{self.base_url}/proxy/network/api/s/default/rest/networkconf/{nc['_id']}"
        s = await self._sess()
        r = await s.put(url, json=update, headers=self._write_headers())
        if r.status in (401, 403):
            self._authed = False; self._csrf = None
            await self._auth()
            r = await s.put(url, json=update, headers=self._write_headers())
        new_csrf = r.headers.get("X-Updated-CSRF-Token") or r.headers.get("X-CSRF-Token")
        if new_csrf:
            self._csrf = new_csrf
        text = await r.text()
        return {"status": r.status, "body": text[:400], "nc_id": nc["_id"]}

    async def set_wan_enable(self, wan_id: int, enable: bool) -> dict:
        """Enable/disable a UDM WAN by toggling wan_type.

        UniFi has no direct enabled flag on WAN networkconf; the way to disable a
        WAN is to set wan_type=disabled. To re-enable, restore the prior wan_type
        (dhcp/static/pppoe). We cache the last-known non-disabled type per WAN."""
        nc = await self._find_wan_conf(wan_id)
        nc_id = nc["_id"]
        current_type = nc.get("wan_type")
        if enable:
            target_type = self._prior_wan_type.get(nc_id) or (
                current_type if current_type and current_type != "disabled" else "dhcp"
            )
            return await self._put_wan(wan_id, {"wan_type": target_type})
        else:
            # Remember current type so we can restore cleanly on re-enable
            if current_type and current_type != "disabled":
                self._prior_wan_type[nc_id] = current_type
            return await self._put_wan(wan_id, {"wan_type": "disabled"})

    async def set_wan_priority(self, wan_id: int, priority: int) -> dict:
        """Set failover priority on a UDM WAN. priority=1 primary, 2=failover, etc."""
        return await self._put_wan(wan_id, {"wan_failover_priority": int(priority)})

    # UniFi Network 10.2 has a non-disruptive per-WAN speedtest at
    #   POST /proxy/network/api/s/default/cmd/devmgr/speedtest
    #   body: {"interface_name": "<ethN>", "cmd": "speedtest"}
    # (Captured from the UI via Web Inspector — the public /cmd/devmgr with a
    # wan/uplink parameter silently ignores the WAN selector and always tests
    # the active uplink. This sub-path is the one the UI actually uses.)

    async def _resolve_wan_ifname(self, wan_id: int) -> str:
        """Look up the kernel interface name for a given WAN id (eth8/eth9/etc.).

        Cache off stat/device; uplink-assignment can change, but ifname for a
        given WAN slot is stable. No point per-call — one fetch covers both WANs.
        """
        cached = getattr(self, "_wan_ifname_cache", None)
        if cached and wan_id in cached:
            return cached[wan_id]
        await self._auth()
        s = await self._sess()
        r = await s.get(f"{self.base_url}/proxy/network/api/s/default/stat/device")
        data = await r.json()
        mapping: dict[int, str] = {}
        for dev in data.get("data", []):
            model = (dev.get("model") or "").upper()
            if not (dev.get("type") == "ugw" or model.startswith("UDM")):
                continue
            for slot in (1, 2):
                wan = dev.get(f"wan{slot}") or {}
                ifn = wan.get("ifname")
                if ifn:
                    mapping[slot] = ifn
            break
        if not mapping:
            raise HTTPException(502, "Could not resolve WAN interface names")
        self._wan_ifname_cache = mapping
        if wan_id not in mapping:
            raise HTTPException(404, f"No interface for WAN{wan_id}")
        return mapping[wan_id]

    async def _trigger_speedtest_cmd(self, wan_id: int | None = None) -> None:
        await self._auth()
        s = await self._sess()
        url = f"{self.base_url}/proxy/network/api/s/default/cmd/devmgr/speedtest"
        body: dict = {"cmd": "speedtest"}
        if wan_id is not None:
            body["interface_name"] = await self._resolve_wan_ifname(wan_id)
        r = await s.post(url, json=body, headers=self._write_headers())
        if r.status in (401, 403):
            self._authed = False; self._csrf = None
            await self._auth()
            r = await s.post(url, json=body, headers=self._write_headers())
        if r.status != 200:
            err = (await r.text())[:300]
            raise HTTPException(502, f"Speedtest trigger failed: {r.status} {err}")

    async def _wait_for_new_speedtest(
        self, after_ms: int, timeout: float = 120,
        wan_filter: str | None = None,
    ) -> dict:
        """Poll the v2 speedtest endpoint until a row newer than `after_ms` shows
        up. If `wan_filter` ("WAN" or "WAN2") is set, match only that group so we
        don't accidentally return a concurrent scheduled test for the other WAN.
        """
        await self._auth()
        s = await self._sess()
        deadline = time.time() + timeout
        url = f"{self.base_url}/proxy/network/v2/api/site/default/speedtest"
        while time.time() < deadline:
            await asyncio.sleep(5)
            try:
                r = await s.get(url)
                if r.status != 200: continue
                rows = (await r.json()).get("data", []) or []
                for row in rows:
                    t = int(row.get("time", 0))
                    if t <= after_ms: continue
                    if wan_filter and (row.get("wan_networkgroup") or "").upper() != wan_filter:
                        continue
                    return {
                        "down_mbps":  float(row.get("download_mbps", 0) or 0),
                        "up_mbps":    float(row.get("upload_mbps", 0) or 0),
                        "latency_ms": float(row.get("latency_ms", 0) or 0),
                        "timestamp":  t // 1000,
                        "wan_networkgroup": row.get("wan_networkgroup", ""),
                        "interface_name":   row.get("interface_name", ""),
                    }
            except Exception:
                continue
        raise HTTPException(504, "Speedtest did not produce a result in time")

    async def get_speedtest_history(self) -> list[dict]:
        """Return normalized per-WAN speedtest history from the v2 endpoint."""
        await self._auth()
        s = await self._sess()
        url = f"{self.base_url}/proxy/network/v2/api/site/default/speedtest"
        r = await s.get(url)
        if r.status != 200:
            return []
        rows = (await r.json()).get("data", []) or []
        out = []
        for row in rows:
            out.append({
                "time": row.get("time", 0),
                "down_mbps": float(row.get("download_mbps", 0) or 0),
                "up_mbps":   float(row.get("upload_mbps", 0) or 0),
                "latency_ms":float(row.get("latency_ms", 0) or 0),
                "wan_networkgroup": row.get("wan_networkgroup", ""),
                "interface_name":   row.get("interface_name", ""),
            })
        out.sort(key=lambda x: x["time"], reverse=True)
        return out

    async def run_speedtest(self, wan_id: int, force_standby: bool = False) -> dict:
        """Run a non-disruptive per-WAN speedtest and wait for the v2 result.

        `force_standby` is retained for API symmetry but no longer used; the
        per-WAN endpoint works against the standby WAN without disabling
        the primary. Flag is ignored.
        """
        _ = force_standby  # obsolete; kept so existing clients don't break
        started_ms = int(time.time() * 1000)
        await self._trigger_speedtest_cmd(wan_id=wan_id)
        wan_filter = "WAN" if wan_id == 1 else f"WAN{wan_id}"
        result = await self._wait_for_new_speedtest(
            after_ms=started_ms, wan_filter=wan_filter
        )
        ng = (result.get("wan_networkgroup") or "").upper()
        observed_wan = 1 if ng in ("WAN", "WAN1") else 2 if ng == "WAN2" else wan_id
        result["wan_id"] = observed_wan
        result["requested_wan_id"] = wan_id
        result["mode"] = "per_wan"
        return result
