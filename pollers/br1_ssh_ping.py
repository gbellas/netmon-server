"""Persistent SSH ping streamer: one long-lived SSH session per target, streaming
ping results in real-time (~1 Hz per reply). Reconnects on failure.
"""

import asyncio
import re
from collections import deque

import pexpect

from pollers.base import BasePoller


# Per-packet reply line -- fires once per second as ping bursts
PACKET_RE = re.compile(
    r"64 bytes from \S+:\s*icmp_\w*=(\d+)\s+ttl=\d+\s+time=([\d.]+)\s*ms"
)
# Batch summary (after 5 packets)
SUMMARY_RE = re.compile(
    r"(\d+)\s+packets transmitted,\s+(\d+)\s+received,\s+(\d+)%\s+packet loss"
)
# RTT summary line
RTT_RE = re.compile(
    r"rtt min/avg/max/mdev\s*=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms"
)


class _IdleBreak(Exception):
    """Internal: raised to cleanly exit the burst loop when clients go away."""
    pass


class PeplinkSshPingPoller(BasePoller):
    """Persistent SSH sessions per target that stream ping replies in real-time.

    Works against any Peplink device (BR1, Balance, etc.) that exposes the
    `support ping` CLI. Multiple instances can coexist for different hosts —
    e.g. one pinging outbound internet from the BR1, another pinging the
    BR1's LAN from the Balance 310 to measure tunnel latency from the home
    side without touching cellular.

    Two options control where results land in state:
      - `poller_name` distinguishes pollers in logs (e.g. "br1_ssh",
        "balance_ssh").
      - `key_prefix_by_role` maps a target's `role` field to a state-key
        prefix (e.g. {"internet": "br1_internet", "tunnel": "balance_tunnel"}).
    """

    # Subclass-friendly defaults the old BR1-only code relied on.
    _DEFAULT_POLLER_NAME = "br1_ssh"
    _DEFAULT_KEY_PREFIXES: dict[str, str] = {
        "tunnel":   "br1_tunnel",
        "internet": "br1_internet",
    }

    def __init__(
        self, config: dict, state, ws_manager, bandwidth_meter=None,
        poller_name: str | None = None,
        key_prefix_by_role: dict[str, str] | None = None,
        state_key_root: str | None = None,
        pause_state=None,
        # Ping-command parameterization. Peplink's CLI exposes
        # `support ping <host>` with a `>` shell prompt. Linux-based
        # devices (e.g. UniFi UDM) use standard `ping -i 1 -c N <host>`
        # against a bash/ash shell prompt (`$` or `#`). The regex
        # defaults keep Peplink working; UniFi driver overrides both.
        ping_command_template: str = "support ping {host}",
        prompt_regex: str = r">",
    ):
        name = poller_name or self._DEFAULT_POLLER_NAME
        super().__init__(name, config, state, ws_manager, bandwidth_meter=bandwidth_meter)
        # Where to publish state keys. If set, each ping target's keys land
        # under `<state_key_root>.<host>.*`; otherwise falls through to the
        # poller's `name` (legacy behavior where name doubled as root).
        self._state_key_root = state_key_root
        self._ping_command_template = ping_command_template
        self._prompt_regex = prompt_regex
        self.host = config["host"]
        self.port = config.get("port", 22)
        self.username = config.get("username", "admin")
        self.password = config.get("password", "")
        self.targets = config.get("targets", [])
        self.ssh_timeout = config.get("ssh_timeout", 10)
        self._window = 30
        # Legacy role-based prefix routing kept for backwards compat with
        # any deployment still shipping `key_prefix_by_role` in config. New
        # code publishes under `<poller_name>.<host>.*` unconditionally —
        # matches the icmp_ping driver's schema so the dashboard enumerates
        # SSH ping targets the same way it does ICMP ones.
        self._key_prefix_by_role = key_prefix_by_role  # None => unified scheme
        # Serializes bursts across targets on THIS device (Peplink CLI has a
        # global lock on `support ping`).
        self._ping_lock = asyncio.Lock()
        # Optional external signal that tells us to back off (the iPhone app
        # sets this when it's on BR1 LAN doing direct polling — we'd be
        # double-pinging over the tunnel otherwise).
        self._pause_state = pause_state

    async def run(self):
        self.logger.info(
            f"Persistent SSH ping streamer: {len(self.targets)} target(s)"
        )
        tasks = [asyncio.create_task(self._stream_one(t)) for t in self.targets]
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            raise

    async def _broadcast(self, updates: dict):
        changed = self.state.update(updates)
        if changed:
            await self.ws.broadcast(changed)

    async def _stream_one(self, target: dict):
        """Run a single persistent SSH session for one ping target."""
        host = target["host"]
        name = target.get("name", host)
        role = target.get("role", "internet")
        # Unified scheme: publish under `<poller_name>.<host>.*` (matches
        # the icmp_ping driver). Legacy role-based routing still honored if
        # caller explicitly passed `key_prefix_by_role` — needed for a
        # one-release grace period while existing deployments catch up.
        if self._key_prefix_by_role:
            root = self._key_prefix_by_role.get(
                role, self._DEFAULT_KEY_PREFIXES.get(role, self.name)
            )
            prefix = f"{root}.{host.replace('.', '_')}"
        else:
            root = self._state_key_root or self.name
            prefix = f"{root}.{host.replace('.', '_')}"
        loop = asyncio.get_running_loop()
        backoff = 1.0
        rtts: deque = deque(maxlen=self._window)
        # Rolling window of burst-summary tuples (transmitted, received).
        # Used to compute a smoothed loss % instead of reporting each burst
        # independently. With default of ~6 bursts the average covers ~30
        # packets, so one flaky burst doesn't spike loss_pct to 100%.
        burst_history: deque = deque(maxlen=6)
        # How many consecutive all-zero bursts we've seen. Only flip the
        # displayed status to "timeout" after 2+ zero bursts — one lost burst
        # on marginal LTE is a blip, not a real outage.
        consec_zero_bursts = 0

        await self._broadcast({
            f"{prefix}.name": name,
            f"{prefix}.host": host,
            f"{prefix}.status": "connecting",
        })

        # Idle threshold: if no clients have been connected for this long,
        # pause the SSH stream entirely to save cellular data.
        idle_threshold = 60.0
        idle_check_interval = 5.0

        while True:
            # Pause when no clients are connected (idle) OR when an external
            # pause signal is set (iPhone on BR1 LAN doing direct polling).
            ws_idle = hasattr(self.ws, "is_idle") and self.ws.is_idle(idle_threshold)
            ext_paused = self._pause_state.is_paused() if self._pause_state else False
            if ws_idle or ext_paused:
                await self._broadcast({f"{prefix}.status": "paused"})
                while True:
                    await asyncio.sleep(idle_check_interval)
                    ws_idle = hasattr(self.ws, "is_idle") and self.ws.is_idle(idle_threshold)
                    ext_paused = self._pause_state.is_paused() if self._pause_state else False
                    if not ws_idle and not ext_paused:
                        break
                await self._broadcast({f"{prefix}.status": "connecting"})

            child = None
            try:
                # Spawn + login (blocking, run in thread)
                def _spawn_and_login():
                    # Allow both `password` and `keyboard-interactive` —
                    # UDM's PAM-based sshd uses keyboard-interactive by
                    # default, so forcing password-only auth got an
                    # EOF immediately. Peplink accepts either.
                    cmd = (
                        f"ssh -p {self.port} -o StrictHostKeyChecking=no "
                        f"-o UserKnownHostsFile=/dev/null "
                        f"-o PreferredAuthentications=password,keyboard-interactive "
                        f"-o PubkeyAuthentication=no "
                        f"-o NumberOfPasswordPrompts=1 "
                        f"-o ServerAliveInterval=15 "
                        f"-o ServerAliveCountMax=3 "
                        f"-o ConnectTimeout={self.ssh_timeout} "
                        f"{self.username}@{self.host}"
                    )
                    c = pexpect.spawn(cmd, timeout=self.ssh_timeout, encoding="utf-8")
                    c.expect(r"[Pp]assword:", timeout=self.ssh_timeout)
                    c.sendline(self.password)
                    c.expect(self._prompt_regex, timeout=self.ssh_timeout)
                    return c

                child = await loop.run_in_executor(None, _spawn_and_login)
                self.logger.info(f"SSH stream open: {name} ({host})")
                backoff = 1.0
                # Clear any prior error so the UI stops showing it.
                await self._broadcast({
                    f"{prefix}.error_category": "",
                    f"{prefix}.error_message": "",
                })

                # Streaming loop: kick off back-to-back ping bursts and parse every line
                patterns = [PACKET_RE.pattern, SUMMARY_RE.pattern, RTT_RE.pattern, self._prompt_regex, pexpect.TIMEOUT]

                def _send_ping():
                    child.sendline(self._ping_command_template.format(host=host))

                def _expect_next(timeout=8):
                    return child.expect(patterns, timeout=timeout)

                while True:
                    # Acquire the BR1 ping lock so we don't collide with
                    # another stream's `support ping` — BR1's CLI starves
                    # collisions and makes them look like flapping.
                    async with self._ping_lock:
                        await loop.run_in_executor(None, _send_ping)
                        start_time = loop.time()
                        max_burst_time = 15.0

                        while True:
                            if loop.time() - start_time > max_burst_time:
                                raise asyncio.TimeoutError("burst exceeded max time")

                            idx = await loop.run_in_executor(None, _expect_next, 10)
                            matched = (child.after or "") if isinstance(child.after, str) else ""

                            if idx == 0:  # Per-packet reply
                                m = PACKET_RE.search(matched) or PACKET_RE.search(child.before or "")
                                if not m:
                                    continue
                                rtt = float(m.group(2))
                                rtts.append(rtt)
                                # Byte accounting: ~200 bytes SSH + ~100 bytes ICMP per reply.
                                self._record_bytes(
                                    "br1_ssh_pings", bytes_in=200, bytes_out=100
                                )
                                upd = {
                                    f"{prefix}.latency_ms": rtt,
                                    f"{prefix}.status": "ok",
                                }
                                if len(rtts) >= 2:
                                    r = list(rtts)
                                    diffs = [abs(r[i] - r[i-1]) for i in range(1, len(r))]
                                    upd[f"{prefix}.jitter_ms"] = round(sum(diffs) / len(diffs), 2)
                                await self._broadcast(upd)

                            elif idx == 1:  # Summary
                                s = SUMMARY_RE.search(matched) or SUMMARY_RE.search(child.before or "")
                                if not s:
                                    continue
                                transmitted = int(s.group(1))
                                received    = int(s.group(2))
                                burst_history.append((transmitted, received))
                                # Rolling loss % over the last ~6 bursts —
                                # smooths out single-burst flakes on a
                                # moving LTE link.
                                tx = sum(t for t, _ in burst_history)
                                rx = sum(r for _, r in burst_history)
                                rolling_loss = (
                                    100.0 * (tx - rx) / tx if tx > 0 else 0.0
                                )
                                upd = {f"{prefix}.loss_pct": round(rolling_loss, 1)}

                                # Status hysteresis: only flip to "timeout"
                                # after 2+ consecutive zero-received bursts.
                                # One bad burst stays as "ok" with the
                                # rolling loss % quietly climbing.
                                if received == 0:
                                    consec_zero_bursts += 1
                                    if consec_zero_bursts >= 2:
                                        upd[f"{prefix}.latency_ms"] = -1
                                        upd[f"{prefix}.status"] = "timeout"
                                else:
                                    consec_zero_bursts = 0
                                await self._broadcast(upd)

                            elif idx == 2:  # RTT stats
                                r = RTT_RE.search(matched) or RTT_RE.search(child.before or "")
                                if r:
                                    upd = {
                                        f"{prefix}.avg_ms": float(r.group(2)),
                                        f"{prefix}.jitter_ms": float(r.group(4)),
                                    }
                                    await self._broadcast(upd)

                            elif idx == 3:  # Prompt ">" — end of burst
                                break

                            elif idx == 4:  # TIMEOUT waiting for output
                                raise asyncio.TimeoutError("no ping output")

                    # Lock released. Tiny yield gives the other streams a
                    # chance to grab the lock before we rush the next burst.
                    await asyncio.sleep(0.1)

                    # Check idle between bursts. If clients have gone away,
                    # close the SSH session and drop back to the outer idle-
                    # wait loop (saves cellular data until a client returns).
                    if hasattr(self.ws, "is_idle") and self.ws.is_idle(idle_threshold):
                        raise _IdleBreak()

            except _IdleBreak:
                # Clean pause: close SSH and loop back to outer idle-wait.
                if child is not None:
                    try: child.close(force=True)
                    except Exception: pass
                continue
            except asyncio.CancelledError:
                if child is not None:
                    try: child.close(force=True)
                    except Exception: pass
                raise
            except Exception as e:
                # Classify so the UI can tell auth failures from network
                # failures from protocol failures. String keys here are
                # stable contract with the iOS app's PingTargetSnapshot.
                err_cls = type(e).__name__
                err_msg = str(e) or err_cls
                # pexpect.EOF on login = bad creds OR SSH disabled on target.
                # pexpect.TIMEOUT on login = host unreachable / port blocked.
                # pexpect.EOF mid-stream = session dropped.
                if err_cls == "EOF" or "authentication" in err_msg.lower() or "permission denied" in err_msg.lower():
                    category = "auth_failed"
                    summary = "SSH auth failed — check username/password"
                elif err_cls == "TIMEOUT":
                    category = "unreachable"
                    summary = "SSH host unreachable — check hostname/port"
                else:
                    category = "error"
                    summary = f"{err_cls}: {err_msg[:120]}"
                self.logger.warning(
                    f"SSH stream {name}: {category}: {err_msg}. Reconnecting in {backoff:.1f}s"
                )
                # Clear stale metrics so the UI doesn't show last-good values while
                # the tunnel/device is actually unreachable. When the ping stream
                # is broken we have no ground truth — treat it as unknown, not OK.
                await self._broadcast({
                    f"{prefix}.status": "reconnecting",
                    f"{prefix}.error_category": category,
                    f"{prefix}.error_message": summary,
                    f"{prefix}.latency_ms": -1,
                    f"{prefix}.jitter_ms": -1,
                    f"{prefix}.avg_ms": -1,
                    f"{prefix}.loss_pct": 100.0,
                })
                rtts.clear()
                if child is not None:
                    try: child.close(force=True)
                    except Exception: pass
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

# Back-compat alias for existing callers (server.py still imports by this name).
BR1SshPingPoller = PeplinkSshPingPoller
