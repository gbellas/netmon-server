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

    # --- Full CRUD (new) -------------------------------------------------
    #
    # The legacy `update_schedule` (partial-field updater) stays for the
    # existing POST /api/schedule/{key} endpoint; the RESTful CRUD below
    # is what the new /api/scheduler/tasks endpoints call.

    def _validate_schedule_dict(self, d: dict) -> SpeedtestSchedule:
        """Parse + validate an incoming dict; raise ValueError on bad data.
        Kept here (not on the dataclass) so errors can be surfaced to the
        API as clean 400s rather than generic TypeErrors."""
        if not isinstance(d, dict):
            raise ValueError("task body must be an object")
        if "wan_id" not in d:
            raise ValueError("task requires 'wan_id'")
        try:
            wan_id = int(d["wan_id"])
        except (TypeError, ValueError):
            raise ValueError("'wan_id' must be an integer")
        if wan_id < 1 or wan_id > 8:
            raise ValueError("'wan_id' out of range (1..8)")
        try:
            hour = int(d.get("hour", 8))
            minute = int(d.get("minute", 0))
        except (TypeError, ValueError):
            raise ValueError("'hour' / 'minute' must be integers")
        if not (0 <= hour <= 23):
            raise ValueError("'hour' out of range (0..23)")
        if not (0 <= minute <= 59):
            raise ValueError("'minute' out of range (0..59)")
        return SpeedtestSchedule(
            wan_id=wan_id,
            enabled=bool(d.get("enabled", True)),
            hour=hour, minute=minute,
        )

    def get_task(self, key: str) -> dict | None:
        s = self.schedules.get(key)
        if s is None:
            return None
        return {"key": key, **s.to_dict()}

    def create_task(self, key: str, body: dict) -> dict:
        """Add a new schedule. Raises ValueError on id collision / bad body."""
        if not key or not isinstance(key, str):
            raise ValueError("task 'key' must be a non-empty string")
        if key in self.schedules:
            raise ValueError(f"task {key!r} already exists")
        s = self._validate_schedule_dict(body)
        self.schedules[key] = s
        self._save()
        return {"key": key, **s.to_dict()}

    def replace_task(self, key: str, body: dict) -> dict | None:
        if key not in self.schedules:
            return None
        s = self._validate_schedule_dict(body)
        self.schedules[key] = s
        self._save()
        return {"key": key, **s.to_dict()}

    def delete_task(self, key: str) -> bool:
        if key not in self.schedules:
            return False
        del self.schedules[key]
        self._save()
        return True

    def reload(self) -> None:
        """Re-read the config file from disk. Use after an external
        editor mutates it; the run loop picks up changes on the next
        minute tick without needing this, but tests call it to reassert
        known state."""
        self.schedules.clear()
        self._load()

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
