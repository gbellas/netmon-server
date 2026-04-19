"""Derived poller - synthesizes device state from other sources.

Used for the Balance 310, which cannot be polled directly (InControl-managed).
Its status is derived from:
  - ICMP ping to its IP (reachability)
  - BR1's SpeedFusion peer info (tunnel health, peer name, routes)
"""

import asyncio

from pollers.base import BasePoller


class Balance310DerivedPoller(BasePoller):
    """Synthesizes Balance 310 state from ping + BR1 peer info."""

    def __init__(self, config: dict, state, ws_manager, ping_key: str,
                 tunnel_ping_key: str, br1_name: str = "br1"):
        super().__init__("bal310", config, state, ws_manager)
        self.poll_interval = config.get("poll_interval", 5)
        self.host = config.get("host", "")
        self.ping_key = ping_key              # Local ping to Balance 310 WAN IP (reachability)
        self.tunnel_ping_key = tunnel_ping_key  # Ping to BR1 LAN IP (traverses SF tunnel)
        self.br1 = br1_name
        # Which UDM WAN the remote BR1 tunnel peer is configured to reach
        self.sf_depends_on_udm_wan = int(config.get("sf_depends_on_udm_wan", 0) or 0)

    async def poll(self) -> dict:
        updates = {
            "bal310.device_name": self.config.get("name", "Balance 310"),
            "bal310.host": self.host,
            "bal310.sf.depends_on_udm_wan": self.sf_depends_on_udm_wan,
        }

        # Reachability from ping
        ping_status = self.state.get(f"{self.ping_key}.status")
        ping_latency = self.state.get(f"{self.ping_key}.latency_ms")
        ping_loss = self.state.get(f"{self.ping_key}.packet_loss_pct", 0)

        if ping_status == "ok":
            updates["bal310.status"] = "online"
        elif ping_status == "timeout":
            updates["bal310.status"] = "unreachable"
        else:
            updates["bal310.status"] = "unknown"

        updates["bal310.ping_latency_ms"] = ping_latency if isinstance(ping_latency, (int, float)) else -1
        updates["bal310.ping_loss_pct"] = ping_loss

        # SpeedFusion info from BR1's perspective (we're the peer on the other side)
        # If BR1 is unreachable, the tunnel status is necessarily unknown/down from here.
        br1_status = self.state.get(f"{self.br1}.status", "unknown")
        if br1_status == "unreachable":
            updates["bal310.sf.status"] = "down"
            updates["bal310.sf.peer_status"] = "down"
        else:
            updates["bal310.sf.status"] = self.state.get(f"{self.br1}.sf.status", "unknown")
            updates["bal310.sf.peer_status"] = self.state.get(f"{self.br1}.sf.peer_status", "unknown")
        updates["bal310.sf.peer_name"] = self.state.get(f"{self.br1}.sf.peer_name", "")

        # "At risk" when the UDM WAN that the tunnel rides on is down: tunnel may
        # still look connected temporarily, but it cannot re-establish after drop.
        at_risk = False
        risk_reason = ""
        if self.sf_depends_on_udm_wan:
            dep_key = f"udm.wan{self.sf_depends_on_udm_wan}.status"
            dep_status = self.state.get(dep_key)
            if dep_status in ("down", "disconnected", "disabled"):
                at_risk = True
                risk_reason = f"UDM WAN{self.sf_depends_on_udm_wan} is {dep_status} — tunnel cannot reconnect if it drops"
        updates["bal310.sf.at_risk"] = at_risk
        updates["bal310.sf.risk_reason"] = risk_reason

        # --- Tunnel metrics ---
        # Preferred source: streaming SSH ping from BR1 → Balance 310 LAN (192.168.2.1).
        # That ping ALWAYS traverses the SpeedFusion tunnel by definition regardless
        # of where NetMon is running (home vs truck LAN), making it the authoritative
        # tunnel latency. Fall back to MacBook-sourced pings if the BR1 SSH stream
        # isn't available.
        tunnel_latency = -1
        tunnel_status = "unknown"
        tunnel_loss = 0
        tunnel_jitter = 0
        used_key = None

        # Tunnel latency source is EXCLUSIVELY `balance_tunnel.*` — the
        # Balance 310 SSH-pinging BR1's LAN over the SpeedFusion tunnel.
        # We don't fall back to the legacy `br1_tunnel.*` measurement: if
        # the Balance side isn't measuring, we'd rather show "unknown" than
        # fake it with a number from a different device.
        used_key = None
        keys = [k for k in self.state.get_all().keys()
                if k.startswith("balance_tunnel.") and k.endswith(".latency_ms")]
        if keys:
            key_base = keys[0].rsplit(".", 1)[0]
            val = self.state.get(keys[0])
            status = self.state.get(f"{key_base}.status", "unknown")
            if status == "ok" and isinstance(val, (int, float)) and val > 0:
                used_key = key_base
                tunnel_latency = float(val)
                tunnel_status = status
                tunnel_loss = self.state.get(f"{key_base}.loss_pct", 0) or 0
                tunnel_jitter = self.state.get(f"{key_base}.jitter_ms", 0) or 0

        # NOTE: We deliberately do NOT fall back to the MacBook→Balance ping
        # when the SSH tunnel ping is unavailable. That ping goes over the
        # home LAN (not the tunnel), so using it as "tunnel latency" would
        # be a lie that's worse than reporting "unknown". If the BR1 SSH
        # stream is broken we simply have no tunnel-latency ground truth.

        if used_key:
            updates["bal310.sf.tunnel_ping_source"] = used_key

        if br1_status == "unreachable" or tunnel_status != "ok" or \
           not isinstance(tunnel_latency, (int, float)) or tunnel_latency < 0:
            updates["bal310.sf.latency_ms"] = -1
        else:
            updates["bal310.sf.latency_ms"] = float(tunnel_latency)
        updates["bal310.sf.packet_loss_pct"] = float(tunnel_loss or 0)
        updates["bal310.sf.jitter_ms"] = float(tunnel_jitter or 0)

        # Tunnel throughput: if BR1 unreachable, zero it out (can't carry traffic anyway)
        if br1_status == "unreachable":
            updates["bal310.sf.rx_bps"] = 0
            updates["bal310.sf.tx_bps"] = 0
        else:
            wan1_rx = self.state.get(f"{self.br1}.wan1.rx_bps", 0) or 0
            wan1_tx = self.state.get(f"{self.br1}.wan1.tx_bps", 0) or 0
            wan2_rx = self.state.get(f"{self.br1}.wan2.rx_bps", 0) or 0
            wan2_tx = self.state.get(f"{self.br1}.wan2.tx_bps", 0) or 0
            updates["bal310.sf.rx_bps"] = wan1_rx + wan2_rx
            updates["bal310.sf.tx_bps"] = wan1_tx + wan2_tx

        return updates

    async def run(self):
        """Override base run: this poller never truly 'fails', it just reports derived state."""
        self.logger.info(f"Derived poller started (interval={self.poll_interval}s)")
        while True:
            try:
                updates = await self.poll()
                changed = self.state.update(updates)
                if changed:
                    await self.ws.broadcast(changed)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.warning(f"Derived poll error: {e}")
            await asyncio.sleep(self.poll_interval)
