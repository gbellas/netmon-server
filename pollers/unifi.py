"""UniFi OS API poller for UDM-SE."""

import ssl
import aiohttp

from pollers.base import BasePoller


class UniFiPoller(BasePoller):
    """Polls a UDM-SE via the UniFi OS local API."""

    def __init__(self, config: dict, state, ws_manager, bandwidth_meter=None):
        super().__init__("udm", config, state, ws_manager, bandwidth_meter=bandwidth_meter)
        self.host = config["host"]
        self.username = config.get("username", "")
        self.password = config.get("password", "")
        self.verify_ssl = config.get("verify_ssl", False)
        self.base_url = f"https://{self.host}"
        self._session: aiohttp.ClientSession | None = None
        self._authenticated = False
        # Byte counters for rate calculation per-WAN
        self._prev_rx = {"wan1": None, "wan2": None}
        self._prev_tx = {"wan1": None, "wan2": None}
        # Carrier labels (from config.yaml) so the app can render brand pills.
        # Keys are coerced to strings because YAML ints vs str both acceptable.
        raw_wan_carriers = config.get("wan_carriers", {}) or {}
        self.wan_carriers = {str(k): str(v) for k, v in raw_wan_carriers.items()}

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
            f"{self.base_url}/api/auth/login",
            json={"username": self.username, "password": self.password},
        )
        if resp.status == 200:
            self._authenticated = True
            self.logger.info("Authenticated with UDM")
        else:
            text = await resp.text()
            raise ConnectionError(f"UDM auth failed ({resp.status}): {text[:200]}")

    async def _api_get(self, path: str) -> dict:
        session = await self._get_session()
        if not self._authenticated:
            await self._authenticate()

        resp = await session.get(f"{self.base_url}{path}")
        if resp.status == 401:
            self._authenticated = False
            await self._authenticate()
            resp = await session.get(f"{self.base_url}{path}")

        resp.raise_for_status()
        raw = await resp.read()
        # UDM lives on home LAN — traffic doesn't hit cellular, but we still
        # account for it so the UI can show the full picture.
        self._record_bytes("udm_polls", bytes_in=len(raw) + 600, bytes_out=300)
        import json as _json
        return _json.loads(raw) if raw else {}

    def _extract_wan(self, wan_dict: dict, key: str, health_avail: float | None = None) -> dict:
        """Extract useful fields from dev.wan1/dev.wan2, plus health availability."""
        if not wan_dict:
            return {}
        out = {}
        enable = bool(wan_dict.get("enable"))
        is_uplink = bool(wan_dict.get("is_uplink"))
        physical_up = wan_dict.get("up")  # port link state (cable connected)

        out[f"udm.{key}.enable"] = enable
        out[f"udm.{key}.is_uplink"] = is_uplink
        out[f"udm.{key}.up"] = bool(physical_up)
        out[f"udm.{key}.latency"] = wan_dict.get("latency", 0)
        out[f"udm.{key}.availability"] = (
            health_avail if health_avail is not None else wan_dict.get("availability", 0)
        )
        out[f"udm.{key}.ifname"] = wan_dict.get("uplink_ifname", "")
        out[f"udm.{key}.max_speed"] = wan_dict.get("max_speed", 0)
        out[f"udm.{key}.type"] = wan_dict.get("type", "")
        out[f"udm.{key}.full_duplex"] = bool(wan_dict.get("full_duplex"))

        # Derive true status from enable + link + health availability + uplink role
        if not enable:
            status = "disabled"
        elif physical_up is False:
            status = "disconnected"       # cable unplugged
        elif health_avail is not None and health_avail < 50:
            # monitors report this WAN can't reach the internet
            status = "down"
        elif is_uplink:
            status = "connected"
        elif health_avail is None:
            # Enabled, link up, but no reachability data - treat as down until proven healthy
            status = "down"
        else:
            status = "standby"            # healthy backup, not primary
        out[f"udm.{key}.status"] = status

        # Throughput calc from byte counters
        rx = wan_dict.get("rx_bytes", 0)
        tx = wan_dict.get("tx_bytes", 0)
        prev_rx = self._prev_rx.get(key)
        prev_tx = self._prev_tx.get(key)
        if prev_rx is not None and rx >= prev_rx and tx >= prev_tx:
            dt = self.poll_interval
            out[f"udm.{key}.rx_bps"] = max(0, (rx - prev_rx) * 8 / dt)
            out[f"udm.{key}.tx_bps"] = max(0, (tx - prev_tx) * 8 / dt)
        self._prev_rx[key] = rx
        self._prev_tx[key] = tx

        out[f"udm.{key}.rx_bytes_total"] = rx
        out[f"udm.{key}.tx_bytes_total"] = tx
        return out

    async def poll(self) -> dict:
        updates = {}
        api_success = False
        wan_availability = {"wan1": None, "wan2": None}  # populated from health.uptime_stats

        # Site health -- primary source for the active WAN's IP + per-WAN availability
        try:
            health_data = await self._api_get("/proxy/network/api/s/default/stat/health")
            api_success = True
            for subsys in health_data.get("data", []):
                name = subsys.get("subsystem")
                if name == "wan":
                    wan_ip = subsys.get("wan_ip", "")
                    gateways = subsys.get("gateways") or []
                    if gateways:
                        wan_ip = gateways[0].get("wan_ip", wan_ip)
                    updates["udm.active_wan_ip"] = wan_ip
                    # ISP info for the currently-carrying uplink. UniFi only
                    # exposes this for the active WAN. We stamp it onto whichever
                    # of wan1/wan2 has `is_uplink:true` (set later from device
                    # data), but also publish at the udm.* level for convenience.
                    updates["udm.active_isp_name"] = subsys.get("isp_name", "") or ""
                    updates["udm.active_isp_org"] = subsys.get("isp_organization", "") or ""
                    updates["udm.active_asn"] = subsys.get("asn", 0) or 0
                    # Per-WAN availability from uptime_stats
                    us = subsys.get("uptime_stats") or {}
                    if "WAN" in us:
                        wan_availability["wan1"] = us["WAN"].get("availability")
                        updates["udm.wan1.downtime"] = us["WAN"].get("downtime") or 0
                    if "WAN2" in us:
                        wan_availability["wan2"] = us["WAN2"].get("availability")
                        updates["udm.wan2.downtime"] = us["WAN2"].get("downtime") or 0

                    # Per-WAN, per-target ping monitors (1.1.1.1, google.com, etc.)
                    # These are actually measured by the UDM, so they represent the
                    # real home-to-internet latency on each WAN.
                    for us_key, wan_slot in (("WAN", "wan1"), ("WAN2", "wan2")):
                        stats = us.get(us_key) or {}
                        monitors = stats.get("monitors") or []
                        targets = []
                        for m in monitors:
                            target = m.get("target", "")
                            if not target:
                                continue
                            tkey = target.replace(".", "_")
                            prefix = f"udm.{wan_slot}.mon.{tkey}"
                            updates[f"{prefix}.target"] = target
                            updates[f"{prefix}.availability"] = float(m.get("availability") or 0)
                            updates[f"{prefix}.latency_ms"] = int(m.get("latency_average") or 0)
                            updates[f"{prefix}.type"] = m.get("type", "icmp")
                            targets.append(target)
                        updates[f"udm.{wan_slot}.monitor_targets"] = targets
                elif name == "wlan":
                    updates["udm.wlan_clients"] = subsys.get("num_user", 0)
                elif name == "lan":
                    updates["udm.lan_clients"] = subsys.get("num_user", 0)
                elif name == "www":
                    updates["udm.internet.latency"] = subsys.get("latency", 0)
                    updates["udm.internet.uptime"] = subsys.get("uptime", 0)
                    updates["udm.internet.drops"] = subsys.get("drops", 0)
                    updates["udm.internet.xput_up"] = subsys.get("xput_up", 0)
                    updates["udm.internet.xput_down"] = subsys.get("xput_down", 0)
        except Exception as e:
            self.logger.warning(f"Health endpoint error: {e}")

        # Device detail -- source for BOTH WAN1 and WAN2 dicts (including standby)
        try:
            dev_data = await self._api_get("/proxy/network/api/s/default/stat/device")
            api_success = True
            for dev in dev_data.get("data", []):
                model = (dev.get("model") or "").upper()
                if not (dev.get("type") == "ugw" or model.startswith("UDM")):
                    continue
                updates["udm.uptime"] = dev.get("uptime", 0)
                sys_stats = dev.get("system-stats", {})
                updates["udm.cpu"] = float(sys_stats.get("cpu", 0))
                updates["udm.mem"] = float(sys_stats.get("mem", 0))
                updates["udm.model"] = dev.get("model", "")
                updates["udm.version"] = dev.get("version", "")

                # WAN1 / WAN2 dicts (pass per-WAN availability from health)
                updates.update(self._extract_wan(dev.get("wan1") or {}, "wan1", wan_availability["wan1"]))
                updates.update(self._extract_wan(dev.get("wan2") or {}, "wan2", wan_availability["wan2"]))
                break
        except Exception as e:
            self.logger.warning(f"Device endpoint error: {e}")

        # v2 speedtest history — publish latest per-WAN result so both WAN
        # rows show a number even without clicking Run. UniFi's scheduled
        # speedtest populates this automatically; manual runs append to it too.
        try:
            st_rows = await self._api_get("/proxy/network/v2/api/site/default/speedtest")
            if isinstance(st_rows, dict):
                rows = st_rows.get("data", []) or []
            else:
                rows = st_rows or []
            # Most recent per WAN
            latest = {}  # wan_id -> row
            for row in rows:
                ng = (row.get("wan_networkgroup") or "").upper()
                wan_id = 1 if ng in ("WAN", "WAN1") else 2 if ng == "WAN2" else None
                if wan_id is None:
                    continue
                prev = latest.get(wan_id)
                if not prev or row.get("time", 0) > prev.get("time", 0):
                    latest[wan_id] = row
            for wan_id, row in latest.items():
                prefix = f"udm.wan{wan_id}.speedtest"
                updates[f"{prefix}.down_mbps"]  = float(row.get("download_mbps", 0) or 0)
                updates[f"{prefix}.up_mbps"]    = float(row.get("upload_mbps", 0) or 0)
                updates[f"{prefix}.latency_ms"] = float(row.get("latency_ms", 0) or 0)
                updates[f"{prefix}.timestamp"]  = int(row.get("time", 0)) // 1000
                updates[f"{prefix}.mode"]       = "scheduled"
        except Exception as e:
            self.logger.debug(f"v2 speedtest fetch skipped: {e}")

        if not api_success:
            raise ConnectionError("All UDM API calls failed")

        # IP belongs to whichever WAN is currently serving traffic.
        # If a WAN is down, show no IP on it.
        active_ip = updates.get("udm.active_wan_ip", "")
        wan1_up = updates.get("udm.wan1.status") == "connected"
        wan2_up = updates.get("udm.wan2.status") == "connected"
        updates["udm.wan1.ip"] = active_ip if wan1_up else ""
        updates["udm.wan2.ip"] = active_ip if wan2_up else ""

        updates["udm.status"] = "online"

        # Attach the configured carrier tag to each WAN so the app can brand
        # them. "fiber"/"att"/"verizon"/"tmobile"/"starlink"/"other".
        for slot, tag in self.wan_carriers.items():
            updates[f"udm.wan{slot}.carrier"] = tag

        # Total clients
        wlan = updates.get("udm.wlan_clients", self.state.get("udm.wlan_clients", 0)) or 0
        lan = updates.get("udm.lan_clients", self.state.get("udm.lan_clients", 0)) or 0
        updates["udm.clients"] = wlan + lan

        return updates
