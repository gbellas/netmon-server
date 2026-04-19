"""Peplink InControl 2 cloud API poller.

Complements the local device pollers with cloud-side data: event log history,
historical usage, Peplink-side GPS trails, underlying carrier names (the MVNO
behind RoamLink, e.g. "Webbing (HK)").

Uses OAuth2 client_credentials flow against api.ic.peplink.com.
"""

import asyncio
import ssl
import time
import certifi
import aiohttp

from pollers.base import BasePoller


class InControlPoller(BasePoller):
    """Polls Peplink InControl 2 for cloud-side data about the organization."""

    def __init__(self, config: dict, state, ws_manager, bandwidth_meter=None):
        super().__init__("ic2", config, state, ws_manager, bandwidth_meter=bandwidth_meter)
        self.client_id = config.get("client_id") or ""
        self.client_secret = config.get("client_secret") or ""
        self.org_id = config.get("org_id") or ""
        self.poll_interval = config.get("poll_interval", 60)
        self.event_limit = config.get("event_limit", 30)
        self.base_url = "https://api.ic.peplink.com"
        self._session: aiohttp.ClientSession | None = None
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    def _ssl_ctx(self):
        return ssl.create_default_context(cafile=certifi.where())

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=self._ssl_ctx()),
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    async def _ensure_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token
        s = await self._sess()
        r = await s.post(
            f"{self.base_url}/api/oauth2/token",
            data={"client_id": self.client_id, "client_secret": self.client_secret,
                  "grant_type": "client_credentials"},
        )
        r.raise_for_status()
        d = await r.json()
        self._access_token = d["access_token"]
        self._token_expires_at = time.time() + int(d.get("expires_in", 3600))
        return self._access_token

    async def _get(self, path: str) -> dict:
        token = await self._ensure_token()
        s = await self._sess()
        r = await s.get(f"{self.base_url}{path}",
                        headers={"Authorization": f"Bearer {token}"})
        if r.status == 401:
            self._access_token = None
            token = await self._ensure_token()
            r = await s.get(f"{self.base_url}{path}",
                            headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        raw = await r.read()
        # IC2 is a cloud API — traffic goes over home WAN (fiber).
        self._record_bytes("ic2_cloud", bytes_in=len(raw) + 800, bytes_out=400)
        import json as _json
        return _json.loads(raw) if raw else {}

    async def poll(self) -> dict:
        updates = {}

        # 1. Discover devices in the group
        org_data = (await self._get(f"/rest/o/{self.org_id}")).get("data") or {}
        updates["ic2.org.name"] = org_data.get("name", "")
        updates["ic2.org.last_activity"] = org_data.get("lastActivityDate", "")

        groups = (await self._get(f"/rest/o/{self.org_id}/g")).get("data") or []
        if not groups:
            return updates
        group = groups[0]
        gid = group["id"]
        updates["ic2.group.name"] = group.get("name", "")
        updates["ic2.group.online"] = group.get("online_device_count", 0)
        updates["ic2.group.offline"] = group.get("offline_device_count", 0)

        devices = (await self._get(f"/rest/o/{self.org_id}/g/{gid}/d")).get("data") or []
        updates["ic2.device_count"] = len(devices)

        # 2. Enrich BR1 with IC2-specific data (underlying MVNO carrier etc.)
        br1 = next((d for d in devices if "BR1" in d.get("product_name", "")
                    or "MAX" in d.get("product_name", "")), None)
        if br1:
            updates["ic2.br1.device_id"] = br1["id"]
            updates["ic2.br1.usage_mb"] = br1.get("usage", 0)
            updates["ic2.br1.tx_mb"] = br1.get("tx", 0)
            updates["ic2.br1.rx_mb"] = br1.get("rx", 0)

            # Full device detail -- contains cellular signals, home carrier, bands
            det = (await self._get(f"/rest/o/{self.org_id}/g/{gid}/d/{br1['id']}")).get("data") or {}
            for iface in det.get("interfaces", []):
                if iface.get("virtualType") == "cellular":
                    updates["ic2.br1.home_carrier"] = iface.get("home_carrier_name", "")
                    updates["ic2.br1.carrier"] = iface.get("carrier_name", "")
                    updates["ic2.br1.data_tech"] = iface.get("gobi_data_tech", "")
                    updates["ic2.br1.imei"] = iface.get("imei", "")
                    sig = iface.get("cellular_signals") or {}
                    updates["ic2.br1.signal.rssi"] = sig.get("rssi")
                    updates["ic2.br1.signal.sinr"] = sig.get("sinr")
                    updates["ic2.br1.signal.rsrp"] = sig.get("rsrp")
                    updates["ic2.br1.signal.rsrq"] = sig.get("rsrq")
                    break

            # 3. Recent event log with GPS
            try:
                ev = await self._get(f"/rest/o/{self.org_id}/g/{gid}/d/{br1['id']}/event_log?limit={self.event_limit}")
                events = ev.get("data") or []
                compact = [{
                    "ts": e.get("ts"),
                    "type": e.get("event_type"),
                    "detail": e.get("detail"),
                    "lat": e.get("latitude"),
                    "lng": e.get("longitude"),
                } for e in events[:self.event_limit]]
                updates["ic2.br1.events"] = compact
            except Exception as e:
                self.logger.debug(f"event_log error: {e}")

            # 4. Per-WAN data usage (daily bars + weekly + monthly totals, per-WAN)
            try:
                for wan_id, label in [(1, "wan1"), (2, "wan2")]:
                    # Daily (last 7 days)
                    d = await self._get(
                        f"/rest/o/{self.org_id}/g/{gid}/d/{br1['id']}/bandwidth"
                        f"?type=daily&wan_id={wan_id}"
                    )
                    usages = (d.get("data") or {}).get("usages") or []
                    usages.sort(key=lambda u: u.get("ts", ""))
                    points = usages[-7:]
                    series = [{
                        "ts": u.get("ts"),
                        "up_mb": round(u.get("up", 0), 1),
                        "down_mb": round(u.get("down", 0), 1),
                    } for u in points]
                    updates[f"ic2.br1.{label}.usage_series"] = series
                    total_up = sum(u.get("up", 0) for u in usages)
                    total_down = sum(u.get("down", 0) for u in usages)
                    today_ts = time.strftime("%Y-%m-%d")
                    today = next((u for u in usages if (u.get("ts") or "").startswith(today_ts)), {})
                    updates[f"ic2.br1.{label}.usage_7d_up_mb"] = round(total_up, 1)
                    updates[f"ic2.br1.{label}.usage_7d_down_mb"] = round(total_down, 1)
                    updates[f"ic2.br1.{label}.usage_today_up_mb"] = round(today.get("up", 0), 1)
                    updates[f"ic2.br1.{label}.usage_today_down_mb"] = round(today.get("down", 0), 1)

                    # Monthly for THIS WAN (current month = latest entry in sorted list)
                    try:
                        dm = await self._get(
                            f"/rest/o/{self.org_id}/g/{gid}/d/{br1['id']}/bandwidth"
                            f"?type=monthly&wan_id={wan_id}"
                        )
                        monthly = (dm.get("data") or {}).get("usages") or []
                        monthly.sort(key=lambda u: u.get("ts", ""))
                        if monthly:
                            latest = monthly[-1]
                            updates[f"ic2.br1.{label}.usage_month_up_mb"] = round(latest.get("up", 0), 1)
                            updates[f"ic2.br1.{label}.usage_month_down_mb"] = round(latest.get("down", 0), 1)
                            updates[f"ic2.br1.{label}.usage_month_label"] = (latest.get("ts") or "")[:7]
                    except Exception as e:
                        self.logger.debug(f"monthly usage wan{wan_id} error: {e}")
            except Exception as e:
                self.logger.debug(f"usage error: {e}")

        return updates
