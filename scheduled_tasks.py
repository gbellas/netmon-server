"""Scheduled background tasks for NetMon.

One coroutine per task; all non-overlapping, all tolerant of exceptions.
Currently supports daily per-WAN speedtests on the UDM.

Persists config to `scheduled_config.json` next to the process CWD.
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("netmon.sched")


@dataclass
class SpeedtestSchedule:
    wan_id: int
    enabled: bool
    hour: int       # 0..23 local time
    minute: int     # 0..59

    def to_dict(self) -> dict: return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SpeedtestSchedule":
        return cls(
            wan_id=int(d.get("wan_id", 1)),
            enabled=bool(d.get("enabled", True)),
            hour=int(d.get("hour", 8)),
            minute=int(d.get("minute", 0)),
        )


class Scheduler:
    def __init__(self, state, ws_manager, udm_controller_factory, config_path: Path):
        self.state = state
        self.ws = ws_manager
        self.get_udm = udm_controller_factory
        self.path = config_path
        self.schedules: dict[str, SpeedtestSchedule] = {}
        self._load()

    def _key(self, wan_id: int) -> str: return f"speedtest_wan{wan_id}"

    def _load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                for k, v in data.items():
                    self.schedules[k] = SpeedtestSchedule.from_dict(v)
            except Exception:
                pass
        # Defaults: WAN2 daily at 08:05 (slightly off the hour to avoid collision
        # with every other cron user). WAN1 disabled by default — fiber usually
        # doesn't need scheduled tests.
        self.schedules.setdefault("speedtest_wan2",
            SpeedtestSchedule(wan_id=2, enabled=True, hour=8, minute=5))
        self.schedules.setdefault("speedtest_wan1",
            SpeedtestSchedule(wan_id=1, enabled=False, hour=2, minute=5))
        self._save()

    def _save(self):
        try:
            self.path.write_text(json.dumps(
                {k: v.to_dict() for k, v in self.schedules.items()}, indent=2))
        except Exception:
            pass

    def list_schedules(self) -> list[dict]:
        return [{"key": k, **v.to_dict()} for k, v in self.schedules.items()]

    def update_schedule(self, key: str, *, enabled: bool | None = None,
                        hour: int | None = None, minute: int | None = None) -> bool:
        s = self.schedules.get(key)
        if s is None: return False
        if enabled is not None: s.enabled = bool(enabled)
        if hour is not None: s.hour = max(0, min(23, int(hour)))
        if minute is not None: s.minute = max(0, min(59, int(minute)))
        self._save()
        return True

    async def run(self):
        """One-minute tick loop. Fires any schedule whose clock has reached
        its configured time AND hasn't fired yet today."""
        last_fired: dict[str, str] = {}   # key → date string (YYYY-MM-DD)
        while True:
            try:
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                for key, s in self.schedules.items():
                    if not s.enabled: continue
                    if last_fired.get(key) == today: continue
                    if now.hour == s.hour and now.minute >= s.minute:
                        last_fired[key] = today
                        asyncio.create_task(self._run_speedtest(s))
            except Exception as e:
                logger.warning(f"scheduler tick error: {e}")
            # Sleep until the top of the next minute (roughly)
            await asyncio.sleep(60 - (time.time() % 60))

    async def _run_speedtest(self, s: SpeedtestSchedule):
        logger.info(f"scheduled speedtest starting for WAN{s.wan_id}")
        try:
            ctrl = self.get_udm()
            result = await ctrl.run_speedtest(s.wan_id, force_standby=False)
            updates = {
                f"udm.wan{s.wan_id}.speedtest.down_mbps":  result["down_mbps"],
                f"udm.wan{s.wan_id}.speedtest.up_mbps":    result["up_mbps"],
                f"udm.wan{s.wan_id}.speedtest.latency_ms": result["latency_ms"],
                f"udm.wan{s.wan_id}.speedtest.timestamp":  int(result.get("timestamp") or 0),
                f"udm.wan{s.wan_id}.speedtest.mode":       "scheduled",
            }
            changed = self.state.update(updates)
            if changed:
                await self.ws.broadcast(changed)
            logger.info(
                f"scheduled speedtest wan{s.wan_id}: "
                f"↓{result['down_mbps']:.1f} ↑{result['up_mbps']:.1f} Mbps "
                f"lat {result['latency_ms']:.0f}ms"
            )
        except Exception as e:
            logger.warning(f"scheduled speedtest wan{s.wan_id} failed: {e}")
