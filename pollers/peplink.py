"""Peplink router API poller. Used for BR1 Pro 5G (and any Peplink with API access).

The Balance 310 is not directly polled when InControl-managed; its state is derived
from the BR1's SpeedFusion peer info plus an ICMP ping.
"""

import json
import ssl
import aiohttp

from pollers.base import BasePoller


def _dict_to_list(data):
    """Peplink APIs often return {order: [1,2], "1": {...}, "2": {...}}.
    Convert to a list preserving order when possible."""
    if not isinstance(data, dict):
        return data if isinstance(data, list) else []
    order = data.get("order")
    if isinstance(order, list):
        result = []
        for key in order:
            item = data.get(str(key)) or data.get(key)
            if isinstance(item, dict):
                # inject id for reference
                item = {**item, "_id": key}
                result.append(item)
        return result
    # Fall back: numeric-keyed dict
    numeric_items = []
    for k, v in data.items():
        if str(k).isdigit() and isinstance(v, dict):
            numeric_items.append({**v, "_id": int(k)})
    if numeric_items:
        numeric_items.sort(key=lambda x: x["_id"])
        return numeric_items
    return [data]


# Common North American carrier PLMN codes (MCC-MNC)
_OPERATORS = {
    # USA
    ("310", "030"): "AT&T",
    ("310", "070"): "AT&T",
    ("310", "090"): "AT&T",
    ("310", "150"): "AT&T",
    ("310", "170"): "AT&T",
    ("310", "280"): "AT&T",
    ("310", "380"): "AT&T",
    ("310", "410"): "AT&T",
    ("310", "560"): "AT&T",
    ("310", "680"): "AT&T",
    ("310", "980"): "AT&T",
    ("310", "260"): "T-Mobile",
    ("310", "160"): "T-Mobile",
    ("310", "200"): "T-Mobile",
    ("310", "210"): "T-Mobile",
    ("310", "220"): "T-Mobile",
    ("310", "240"): "T-Mobile",
    ("310", "250"): "T-Mobile",
    ("310", "310"): "T-Mobile",
    ("310", "660"): "T-Mobile",
    ("311", "480"): "Verizon",
    ("310", "004"): "Verizon",
    ("310", "005"): "Verizon",
    ("310", "012"): "Verizon",
    ("310", "013"): "Verizon",
    ("311", "110"): "Verizon",
    ("311", "270"): "Verizon",
    ("311", "271"): "Verizon",
    ("311", "272"): "Verizon",
    ("311", "273"): "Verizon",
    ("311", "274"): "Verizon",
    ("311", "275"): "Verizon",
    ("311", "276"): "Verizon",
    ("311", "277"): "Verizon",
    ("311", "278"): "Verizon",
    ("311", "279"): "Verizon",
    ("311", "280"): "Verizon",
    ("311", "281"): "Verizon",
    ("311", "282"): "Verizon",
    ("311", "283"): "Verizon",
    ("311", "284"): "Verizon",
    ("311", "285"): "Verizon",
    ("311", "286"): "Verizon",
    ("311", "287"): "Verizon",
    ("311", "288"): "Verizon",
    ("311", "289"): "Verizon",
    # Canada
    ("302", "220"): "Telus",
    ("302", "610"): "Bell",
    ("302", "720"): "Rogers",
    # Mexico
    ("334", "020"): "Telcel",
    ("334", "030"): "AT&T Mexico",
    ("334", "050"): "AT&T Mexico",
}


def _decode_operator(mcc: str, mnc: str) -> str:
    if not mcc or not mnc:
        return ""
    # Pad MNC to 3 digits
    mnc_padded = str(mnc).zfill(3)
    return _OPERATORS.get((str(mcc), mnc_padded), f"PLMN {mcc}{mnc_padded}")


def _normalize_status(raw) -> str:
    if raw is None:
        return "unknown"
    if isinstance(raw, str):
        s = raw.strip().lower()
        # Peplink uses "CONNECTED", "green", "red", "yellow", etc.
        return {"green": "connected", "red": "disconnected", "yellow": "warning"}.get(s, s)
    return "unknown"


class PeplinkPoller(BasePoller):
    # The BR1 REST poll rides the SF tunnel + BR1's cellular WAN. Skip it when
    # nobody's looking to save cellular data. Balance 310 is home-LAN so it
    # overrides this back to False below.
    pause_when_idle: bool = True

    """Polls a Peplink router via its local web admin API."""

    def __init__(self, name: str, device_name: str, config: dict, state, ws_manager,
                 is_mobile: bool = False, bandwidth_meter=None):
        super().__init__(name, config, state, ws_manager, bandwidth_meter=bandwidth_meter)
        self.device_name = device_name
        self.host = config["host"]
        self.username = config.get("username", "admin")
        self.password = config.get("password", "")
        self.verify_ssl = config.get("verify_ssl", False)
        self.is_mobile = is_mobile
        self.base_url = f"https://{self.host}"
        self._session: aiohttp.ClientSession | None = None
        self._authenticated = False

    def _ssl_context(self):
        if not self.verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                connector=aiohttp.TCPConnector(ssl=self._ssl_context()),
                timeout=aiohttp.ClientTimeout(total=10),
            )
            self._authenticated = False
        return self._session

    async def _authenticate(self):
        session = await self._get_session()
        resp = await session.post(
            f"{self.base_url}/api/login",
            json={"username": self.username, "password": self.password},
        )
        if resp.status == 200:
            data = await resp.json()
            if data.get("stat") == "ok":
                self._authenticated = True
                self.logger.info(f"Authenticated with {self.device_name}")
                return
        text = await resp.text()
        raise ConnectionError(f"{self.device_name} auth failed ({resp.status}): {text[:200]}")

    async def _api_get(self, path: str) -> dict:
        session = await self._get_session()
        if not self._authenticated:
            await self._authenticate()

        try:
            resp = await session.get(f"{self.base_url}{path}")
            if resp.status == 401:
                self._authenticated = False
                await self._authenticate()
                resp = await session.get(f"{self.base_url}{path}")
            resp.raise_for_status()
            raw = await resp.read()
            # bytes_in ≈ response body + ~600 bytes TLS/headers overhead
            self._record_bytes("br1_rest_polls", bytes_in=len(raw) + 600, bytes_out=300)
            data = json.loads(raw) if raw else {}
        except Exception:
            # Any networking-level failure (timeouts, connection reset, SSL,
            # stale pooled connection after a BR1 reboot) invalidates the
            # cached session and auth state. Close it hard so the next poll
            # rebuilds from scratch. Without this, a transient blip could
            # leave the poller permanently wedged on a zombie session.
            await self._reset_session()
            raise

        if data.get("stat") != "ok":
            code = data.get("code")
            msg = data.get("message")
            raise ConnectionError(f"{path} returned stat=fail (code {code}): {msg}")
        return data.get("response", {})

    async def _reset_session(self) -> None:
        """Force-teardown the aiohttp session + clear auth state."""
        self._authenticated = False
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None

    # -------- parsers --------

    def _parse_wan(self, response: dict) -> dict:
        """Parse /api/status.wan.connection response."""
        updates = {}
        wans = _dict_to_list(response)
        for i, wan in enumerate(wans, 1):
            prefix = f"{self.name}.wan{i}"
            status = _normalize_status(wan.get("statusLed") or wan.get("status"))
            updates[f"{prefix}.status"] = status
            updates[f"{prefix}.name"] = wan.get("name", f"WAN {i}")
            updates[f"{prefix}.ip"] = wan.get("ip", "")
            updates[f"{prefix}.uptime"] = wan.get("uptime", 0)
            updates[f"{prefix}.type"] = wan.get("virtualType") or wan.get("type", "")
            updates[f"{prefix}.message"] = wan.get("message", "")

            # Cellular extras
            cell = wan.get("cellular") or {}
            if cell:
                updates[f"{prefix}.network_type"] = cell.get("network") or cell.get("mobileType", "")
                updates[f"{prefix}.signal"] = cell.get("signalLevel", 0)
                updates[f"{prefix}.data_tech"] = cell.get("dataTechnology", "")
                updates[f"{prefix}.network_mode"] = cell.get("networkMode", "")
                # RAT lock state derived from dataTechnology (active tech):
                # - "5G NSA" / "5G SA" → auto (5G available means lock is off)
                # - "LTE" / "LTE-A" → either locked to LTE, or auto with no 5G available
                # - "3G" / "HSPA" → locked to 3G or fallback
                dt = (cell.get("dataTechnology") or "").upper()
                if "5G" in dt: rat_active = "auto"
                elif "LTE" in dt: rat_active = "LTE"
                elif "3G" in dt or "HSPA" in dt: rat_active = "3G"
                elif "2G" in dt or "GPRS" in dt or "EDGE" in dt: rat_active = "2G"
                else: rat_active = "unknown"
                updates[f"{prefix}.rat_mode"] = rat_active

                # Carrier info (carrier.name may say "RoamLink VZW" etc.)
                carr = cell.get("carrier") or {}
                updates[f"{prefix}.carrier_name"] = carr.get("name", "")
                updates[f"{prefix}.country"] = carr.get("country", "")

                # Underlying operator from MCC/MNC
                mcc = cell.get("mcc", "")
                mnc = cell.get("mnc", "")
                updates[f"{prefix}.mcc"] = mcc
                updates[f"{prefix}.mnc"] = mnc
                updates[f"{prefix}.operator"] = _decode_operator(mcc, mnc)

                # Modem info
                updates[f"{prefix}.modem"] = cell.get("model", "")
                updates[f"{prefix}.imei"] = cell.get("imei", "")

                # All active radio access technologies (RATs) + their bands.
                # Exposes every band the modem is currently aggregating on, with its
                # own RSRP/RSRQ/RSSI/SINR so the UI can show band-by-band quality.
                rat_list = cell.get("rat") or []
                # Peplink firmware quirk: in LTE-locked mode the `rat.name`
                # field is "" instead of "LTE". Fall back to a prefix derived
                # from dataTechnology ("LTE-A" → "LTE", "5G NSA" → "5G") so
                # the UI always has a meaningful label.
                dt = (cell.get("dataTechnology") or "").upper()
                default_rat = (
                    "5G" if "5G" in dt
                    else "LTE" if "LTE" in dt
                    else "3G" if "3G" in dt or "HSPA" in dt
                    else ""
                )
                all_bands = []
                for i, rat in enumerate(rat_list):
                    rat_name = rat.get("name") or default_rat
                    for j, b in enumerate(rat.get("band") or []):
                        sig = b.get("signal") or {}
                        all_bands.append({
                            "rat": rat_name,
                            "name": b.get("name", ""),
                            "channel": b.get("channel"),
                            "rssi": sig.get("rssi"),
                            "rsrp": sig.get("rsrp"),
                            "rsrq": sig.get("rsrq"),
                            "sinr": sig.get("sinr"),
                            # Flag the first band of the first RAT as primary.
                            # LTE-A CA secondary cells report RSRP+RSRQ only.
                            "primary": (i == 0 and j == 0),
                        })
                updates[f"{prefix}.bands"] = all_bands

                # Pick "primary" band = first 5G if any, else first LTE, else first.
                # This feeds the existing Signal/Quality bars.
                primary = None
                for rat in rat_list:
                    if rat.get("name") == "5G":
                        primary = rat; break
                if primary is None and rat_list:
                    primary = rat_list[0]
                if primary:
                    updates[f"{prefix}.rat"] = primary.get("name", "")
                    bands = primary.get("band") or []
                    if bands:
                        b = bands[0]
                        updates[f"{prefix}.band"] = b.get("name", "")
                        sig = b.get("signal") or {}
                        updates[f"{prefix}.rsrp"] = sig.get("rsrp", 0)
                        updates[f"{prefix}.rsrq"] = sig.get("rsrq", 0)
                        updates[f"{prefix}.rssi"] = sig.get("rssi", 0)
                        updates[f"{prefix}.sinr"] = sig.get("sinr", 0)

        updates[f"{self.name}.wan_count"] = len(wans)
        return updates

    def _parse_traffic(self, response: dict) -> dict:
        """Parse /api/status.traffic bandwidth section into per-WAN rates."""
        updates = {}
        bw = response.get("bandwidth", {}) if isinstance(response, dict) else {}
        wans = _dict_to_list(bw)
        for wan in wans:
            idx = wan.get("_id") or wan.get("id")
            if idx is None:
                continue
            prefix = f"{self.name}.wan{idx}"
            overall = wan.get("overall", {})
            # Values in kbps, convert to bps for consistent formatter
            updates[f"{prefix}.rx_bps"] = (overall.get("download", 0) or 0) * 1000
            updates[f"{prefix}.tx_bps"] = (overall.get("upload", 0) or 0) * 1000
        # Total bandwidth
        lifetime = response.get("lifetime", {}) if isinstance(response, dict) else {}
        overall_all = lifetime.get("all", {}).get("overall", {})
        updates[f"{self.name}.data_dl_mb"] = overall_all.get("download", 0)
        updates[f"{self.name}.data_ul_mb"] = overall_all.get("upload", 0)
        return updates

    def _parse_pepvpn(self, response: dict) -> dict:
        """Parse /api/status.pepvpn.  response = {profile: {order, "1": {...}, siteId}, peer: [...]}"""
        updates = {}
        profiles = _dict_to_list(response.get("profile", {}))
        peers = response.get("peer", [])
        if not isinstance(peers, list):
            peers = []

        # Primary profile / first peer drives the main SpeedFusion status
        if profiles:
            p = profiles[0]
            updates[f"{self.name}.sf.name"] = p.get("name", "SpeedFusion")
            updates[f"{self.name}.sf.status"] = _normalize_status(p.get("status"))
            updates[f"{self.name}.sf.peer_count"] = p.get("peerCount", 0)
            updates[f"{self.name}.sf.type"] = p.get("type", "")
            updates[f"{self.name}.sf.profile_id"] = p.get("_id", 1)

        if peers:
            peer0 = peers[0]
            updates[f"{self.name}.sf.peer_name"] = peer0.get("name", "")
            updates[f"{self.name}.sf.peer_serial"] = peer0.get("serialNumber", "")
            updates[f"{self.name}.sf.peer_status"] = _normalize_status(peer0.get("status"))
            updates[f"{self.name}.sf.peer_routes"] = peer0.get("route", [])
        updates[f"{self.name}.sf.peer_list"] = [
            {
                "name": p.get("name", ""),
                "serial": p.get("serialNumber", ""),
                "status": _normalize_status(p.get("status")),
            }
            for p in peers
        ]
        return updates

    def _parse_system_info(self, response: dict) -> dict:
        """Parse /api/status.system.info."""
        updates = {}
        dev = response.get("device", {})
        updates[f"{self.name}.model"] = dev.get("model", "")
        updates[f"{self.name}.firmware"] = dev.get("firmwareVersion", "")
        updates[f"{self.name}.serial"] = dev.get("serialNumber", "")
        updates[f"{self.name}.pepvpn_version"] = dev.get("pepvpnVersion", "")

        cpu = response.get("cpuLoad", {})
        updates[f"{self.name}.cpu"] = float(cpu.get("percentage", 0) or 0)

        uptime = response.get("uptime", {})
        updates[f"{self.name}.uptime"] = uptime.get("second", 0)
        return updates

    def _parse_location(self, response: dict) -> dict:
        """Parse /api/info.location."""
        updates = {}
        loc = response.get("location", {}) if isinstance(response, dict) else {}
        if loc:
            updates[f"{self.name}.gps.lat"] = float(loc.get("latitude", 0) or 0)
            updates[f"{self.name}.gps.lng"] = float(loc.get("longitude", 0) or 0)
            updates[f"{self.name}.gps.speed"] = float(loc.get("speed", 0) or 0)
            updates[f"{self.name}.gps.altitude"] = float(loc.get("altitude", 0) or 0)
            updates[f"{self.name}.gps.heading"] = float(loc.get("heading", 0) or 0)
        updates[f"{self.name}.gps.has_fix"] = bool(response.get("gps"))
        return updates

    def _parse_clients(self, response: dict) -> dict:
        clients = response.get("list", []) if isinstance(response, dict) else []
        active = [c for c in clients if c.get("active")]
        return {
            f"{self.name}.clients": len(clients),
            f"{self.name}.clients_active": len(active),
        }

    def _parse_latency(self, response: dict) -> dict:
        """Parse /api/status.wan.latency — per-WAN latency history.

        Zero out latency when the WAN is not currently connected (the Peplink API
        keeps returning stale numbers from the last good connection)."""
        updates = {}
        if not isinstance(response, dict):
            return updates
        for wid, wdata in response.items():
            if wid == "order" or not isinstance(wdata, dict):
                continue
            # Check current WAN status before trusting the latency data
            wan_status = self.state.get(f"{self.name}.wan{wid}.status")
            if wan_status not in ("connected", "ok"):
                # WAN is disconnected/down — clear the latency so UI doesn't mislead
                updates[f"{self.name}.wan{wid}.latency_ms"] = -1
                updates[f"{self.name}.wan{wid}.latency_avg_ms"] = -1
                continue

            lat = wdata.get("latency")
            if not isinstance(lat, dict):
                continue
            data_arr = lat.get("data", [])
            if not data_arr:
                continue
            # Only use the trailing samples; older ones could be from a previous
            # connection session that ended when the WAN dropped
            recent_window = data_arr[-10:]
            current = None
            for v in reversed(recent_window):
                if v and v > 0:
                    current = v
                    break
            if current is None:
                updates[f"{self.name}.wan{wid}.latency_ms"] = -1
                continue
            recent = [v for v in data_arr[-20:] if v and v > 0]
            avg = round(sum(recent) / len(recent), 1) if recent else current
            updates[f"{self.name}.wan{wid}.latency_ms"] = current
            updates[f"{self.name}.wan{wid}.latency_avg_ms"] = avg
        return updates

    async def poll(self) -> dict:
        updates = {f"{self.name}.device_name": self.device_name}
        if self.is_mobile:
            updates[f"{self.name}.is_mobile"] = True
        api_success = False

        endpoints = [
            ("/api/status.system.info", self._parse_system_info),
            ("/api/status.wan.connection", self._parse_wan),
            ("/api/status.traffic", self._parse_traffic),
            ("/api/status.wan.latency", self._parse_latency),
            ("/api/status.pepvpn", self._parse_pepvpn),
            ("/api/status.client", self._parse_clients),
        ]
        if self.is_mobile:
            endpoints.append(("/api/info.location", self._parse_location))

        for path, parser in endpoints:
            try:
                data = await self._api_get(path)
                updates.update(parser(data))
                api_success = True
            except Exception as e:
                self.logger.warning(f"{path} error: {e}")

        if not api_success:
            raise ConnectionError(f"All {self.device_name} API calls failed")

        updates[f"{self.name}.status"] = "online"
        return updates
