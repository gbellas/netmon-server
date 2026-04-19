"""NetMon alerts engine.

Evaluates a set of rules against the live state dict on every tick and
publishes firing/resolving alerts into state (so the app picks them up
over the WebSocket) plus an append-only event log.

Design:
 - Rules are defined in code (this file), configurable at runtime via
   /api/alerts endpoints (enabled/threshold/dedup). State persisted to
   `alerts_config.json` next to the process CWD.
 - A rule's `evaluate(data)` returns an `Alert` (firing) or None.
 - Dedup: a rule that's fired is not re-fired within its dedup window,
   even if the condition remains true. "Resolved" event is sent when
   the condition becomes false.
 - Output in state:
     alerts.active            list[Alert]   (currently firing)
     alerts.log               list[Alert]   (recent, capped at 100)
     alerts.last_fire_ts.<id> float         (for dedup book-keeping in app UI)
"""
from __future__ import annotations
import json
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass
class Alert:
    rule_id: str
    title: str
    detail: str
    severity: str          # "info" | "warning" | "critical"
    timestamp: float
    value: float | str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AlertRule:
    id: str
    name: str
    description: str
    severity: str
    default_enabled: bool
    default_threshold: float | None         # None = no threshold param
    threshold_unit: str                     # "mbps", "ms", "%" etc. (UI display only)
    min_duration_sec: int                   # how long condition must be true before firing
    dedup_sec: int                          # don't re-fire within this window
    evaluate: Callable[[dict, float], Optional[Alert]] = field(repr=False)


# ---------- Rule catalog ----------

def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None


def _str(v: Any) -> str:
    return str(v) if v is not None else ""


def _make_threshold_rule(
    rule_id: str, name: str, description: str, severity: str,
    metric_key: str, compare: str, default_threshold: float,
    unit: str, min_dur: int = 30, dedup: int = 3600,
    value_fmt: str = "{:.1f}",
) -> AlertRule:
    """Generic builder: 'metric <compare> threshold' rules."""
    def _eval(data: dict, threshold: float) -> Optional[Alert]:
        v = _num(data.get(metric_key))
        if v is None:
            return None
        hit = {
            "<": v < threshold,
            "<=": v <= threshold,
            ">": v > threshold,
            ">=": v >= threshold,
        }.get(compare, False)
        if not hit:
            return None
        return Alert(
            rule_id=rule_id,
            title=name,
            detail=f"{metric_key} = {value_fmt.format(v)}{unit} ({compare} {threshold}{unit})",
            severity=severity,
            timestamp=time.time(),
            value=v,
        )

    return AlertRule(
        id=rule_id, name=name, description=description, severity=severity,
        default_enabled=True, default_threshold=default_threshold,
        threshold_unit=unit, min_duration_sec=min_dur, dedup_sec=dedup,
        evaluate=_eval,
    )


def _make_status_rule(
    rule_id: str, name: str, description: str, severity: str,
    status_key: str, bad_values: set[str], min_dur: int = 30, dedup: int = 600,
) -> AlertRule:
    def _eval(data: dict, _: float) -> Optional[Alert]:
        v = _str(data.get(status_key)).lower()
        if v and v in bad_values:
            return Alert(
                rule_id=rule_id, title=name,
                detail=f"{status_key} = {v}",
                severity=severity, timestamp=time.time(), value=v,
            )
        return None
    return AlertRule(
        id=rule_id, name=name, description=description, severity=severity,
        default_enabled=True, default_threshold=None,
        threshold_unit="", min_duration_sec=min_dur, dedup_sec=dedup,
        evaluate=_eval,
    )


def build_rule_catalog() -> list[AlertRule]:
    return [
        # Speedtest-based (server-side scheduled or user-triggered)
        _make_threshold_rule(
            "wan2_speedtest_slow_down", "WAN2 speedtest slow (down)",
            "Fires when the latest UDM WAN2 speedtest download is below threshold.",
            "warning", "udm.wan2.speedtest.down_mbps", "<", 10.0, " Mbps",
            dedup=3600, value_fmt="{:.1f}"),
        _make_threshold_rule(
            "wan2_speedtest_slow_up", "WAN2 speedtest slow (up)",
            "Fires when the latest UDM WAN2 speedtest upload is below threshold.",
            "info", "udm.wan2.speedtest.up_mbps", "<", 2.0, " Mbps", dedup=3600),
        _make_threshold_rule(
            "wan2_speedtest_high_latency", "WAN2 speedtest high latency",
            "Fires when the latest UDM WAN2 speedtest latency is above threshold.",
            "warning", "udm.wan2.speedtest.latency_ms", ">", 150.0, " ms", dedup=3600),

        # Router / WAN status
        _make_status_rule(
            "wan1_down", "Fiber WAN1 down",
            "UDM's primary WAN has gone offline.",
            "critical", "udm.wan1.status",
            bad_values={"disconnected", "down", "offline"},
            min_dur=30, dedup=300),
        _make_status_rule(
            "wan2_down", "Cellular WAN2 down",
            "UDM's failover cellular WAN has gone offline.",
            "critical", "udm.wan2.status",
            bad_values={"disconnected", "down", "offline"},
            min_dur=30, dedup=600),
        _make_status_rule(
            "sf_tunnel_down", "SpeedFusion tunnel down",
            "The home↔truck SpeedFusion tunnel is disconnected.",
            "warning", "bal310.sf.status",
            bad_values={"down", "disconnected", "offline"},
            min_dur=60, dedup=600),
        _make_status_rule(
            "br1_unreachable", "BR1 Pro 5G unreachable",
            "NetMon can no longer reach the BR1.",
            "info", "br1.status",
            bad_values={"unreachable", "offline"},
            min_dur=60, dedup=900),

        # BR1 cellular health
        _make_threshold_rule(
            "br1_poor_rsrp", "BR1 poor cellular signal",
            "Cellular signal strength (RSRP) is very weak.",
            "info", "br1.wan2.rsrp", "<", -115.0, " dBm",
            min_dur=300, dedup=1800, value_fmt="{:.0f}"),
        _make_threshold_rule(
            "br1_low_sinr", "BR1 low cellular SINR",
            "Signal-to-noise ratio is low; expect degraded speeds.",
            "info", "br1.wan2.sinr", "<", 0.0, " dB",
            min_dur=300, dedup=1800, value_fmt="{:.1f}"),

        # Router resources
        _make_threshold_rule(
            "udm_high_cpu", "UDM CPU high",
            "UDM CPU usage is above threshold.",
            "warning", "udm.cpu", ">", 90.0, "%",
            min_dur=300, dedup=1800),
        _make_threshold_rule(
            "udm_high_mem", "UDM memory high",
            "UDM memory usage is above threshold.",
            "warning", "udm.mem", ">", 95.0, "%",
            min_dur=300, dedup=3600),

        # Ping health
        _make_threshold_rule(
            "ping_1111_high", "Ping 1.1.1.1 slow",
            "UDM's UDM→1.1.1.1 latency is above threshold.",
            "info", "udm.wan1.mon.1_1_1_1.latency_ms", ">", 250.0, " ms",
            min_dur=180, dedup=1800),

        # Data usage (BR1 monthly, via IC2)
        _make_threshold_rule(
            "br1_wan2_usage_high", "BR1 WAN2 cellular usage high",
            "Monthly cellular data for BR1 WAN2 above threshold.",
            "warning", "ic2.br1.wan2.usage_month_down_mb", ">", 50000.0, " MB",
            min_dur=0, dedup=86400),
    ]


# ---------- Runtime state ----------

@dataclass
class RuleConfig:
    enabled: bool
    threshold: float | None

    def to_dict(self) -> dict:
        return {"enabled": self.enabled, "threshold": self.threshold}

    @classmethod
    def from_dict(cls, d: dict, default_enabled: bool, default_threshold: float | None) -> "RuleConfig":
        return cls(
            enabled=bool(d.get("enabled", default_enabled)),
            threshold=d.get("threshold", default_threshold),
        )


class AlertsEngine:
    def __init__(self, state, ws_manager, config_path: Path):
        self.state = state
        self.ws = ws_manager
        self.config_path = config_path
        self.rules = build_rule_catalog()
        self._cfg: dict[str, RuleConfig] = {}
        self._first_seen_at: dict[str, float] = {}          # when rule's condition first became true
        self._last_fired_at: dict[str, float] = {}
        self._currently_firing: dict[str, Alert] = {}
        self._log: list[Alert] = []
        self._load()

    # --- config persistence ---
    def _load(self):
        data = {}
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text())
            except Exception:
                pass
        for r in self.rules:
            cfg = data.get(r.id, {})
            self._cfg[r.id] = RuleConfig.from_dict(
                cfg, default_enabled=r.default_enabled,
                default_threshold=r.default_threshold,
            )

    def _save(self):
        try:
            self.config_path.write_text(
                json.dumps({k: v.to_dict() for k, v in self._cfg.items()}, indent=2)
            )
        except Exception:
            pass

    def catalog_view(self) -> list[dict]:
        return [
            {
                "id": r.id,
                "name": r.name,
                "description": r.description,
                "severity": r.severity,
                "default_enabled": r.default_enabled,
                "default_threshold": r.default_threshold,
                "threshold_unit": r.threshold_unit,
                "enabled": self._cfg[r.id].enabled,
                "threshold": self._cfg[r.id].threshold,
            }
            for r in self.rules
        ]

    def update_rule(self, rule_id: str, enabled: bool | None = None,
                    threshold: float | None = None) -> bool:
        cfg = self._cfg.get(rule_id)
        if cfg is None:
            return False
        if enabled is not None: cfg.enabled = bool(enabled)
        if threshold is not None: cfg.threshold = float(threshold)
        self._save()
        return True

    # --- evaluation tick ---
    def tick(self) -> dict:
        """Evaluate all rules once. Returns state updates to publish."""
        now = time.time()
        data = self.state.get_all()
        updates: dict = {}
        newly_firing: list[Alert] = []
        newly_resolved: list[str] = []

        for r in self.rules:
            cfg = self._cfg[r.id]
            if not cfg.enabled:
                # If rule is disabled but was firing, resolve it.
                if r.id in self._currently_firing:
                    newly_resolved.append(r.id)
                    self._currently_firing.pop(r.id, None)
                    self._first_seen_at.pop(r.id, None)
                continue

            alert = r.evaluate(data, cfg.threshold if cfg.threshold is not None else 0.0)
            if alert:
                # Condition is currently true. Track first-seen timestamp.
                if r.id not in self._first_seen_at:
                    self._first_seen_at[r.id] = now

                elapsed = now - self._first_seen_at[r.id]
                last_fired = self._last_fired_at.get(r.id, 0)
                cooldown_ok = (now - last_fired) >= r.dedup_sec

                # Fire if min duration has passed AND dedup allows
                if elapsed >= r.min_duration_sec and cooldown_ok:
                    self._last_fired_at[r.id] = now
                    self._currently_firing[r.id] = alert
                    self._log.append(alert)
                    if len(self._log) > 100:
                        self._log = self._log[-100:]
                    newly_firing.append(alert)
                else:
                    # Track active without firing (so UI can show "pending" if wanted)
                    self._currently_firing.setdefault(r.id, alert)
            else:
                # Condition is false. Clear tracking; emit resolve if was firing.
                if r.id in self._first_seen_at:
                    self._first_seen_at.pop(r.id, None)
                if r.id in self._currently_firing:
                    self._currently_firing.pop(r.id, None)
                    newly_resolved.append(r.id)

        # Publish state
        updates["alerts.active"] = [a.to_dict() for a in self._currently_firing.values()]
        updates["alerts.log"] = [a.to_dict() for a in self._log[-30:]]
        if newly_firing:
            # A distinct "fired just now" key that flips every time, so the
            # client can trigger a local notification on each delta.
            updates["alerts.fired"] = [a.to_dict() for a in newly_firing]
        if newly_resolved:
            updates["alerts.resolved"] = newly_resolved
        return updates
