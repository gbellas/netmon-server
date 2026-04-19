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


# Comparison operators allowed for custom threshold rules.
_ALLOWED_COMPARES = {"<", "<=", ">", ">="}
_ALLOWED_SEVERITIES = {"info", "warning", "critical"}


def _build_custom_rule(spec: dict) -> AlertRule:
    """Materialize a user-authored rule from a serializable spec.

    Two flavors, selected by the presence of `comparison`/`threshold`
    vs `bad_values`:
      - threshold: {metric, comparison (<|<=|>|>=), threshold, unit}
      - status:    {metric, bad_values: [str, ...]}

    Raises ValueError with a useful message if the spec is malformed
    (so the API can bubble a 400 back to the client).
    """
    rid = spec.get("id")
    if not rid or not isinstance(rid, str):
        raise ValueError("rule requires non-empty string 'id'")
    name = spec.get("name") or rid
    description = spec.get("description") or ""
    severity = spec.get("severity", "warning")
    if severity not in _ALLOWED_SEVERITIES:
        raise ValueError(
            f"severity must be one of {sorted(_ALLOWED_SEVERITIES)}"
        )
    metric = spec.get("metric")
    if not metric or not isinstance(metric, str):
        raise ValueError("rule requires non-empty string 'metric'")
    min_dur = int(spec.get("min_duration_sec", 30))
    dedup = int(spec.get("dedup_sec", 600))

    # Status-style rule if bad_values present; otherwise threshold.
    bad_values = spec.get("bad_values")
    if bad_values:
        if not isinstance(bad_values, (list, tuple)):
            raise ValueError("bad_values must be a list of strings")
        bvs = {str(v).lower() for v in bad_values}
        return _make_status_rule(
            rid, name, description, severity,
            metric, bvs, min_dur=min_dur, dedup=dedup,
        )

    compare = spec.get("comparison") or spec.get("compare")
    if compare not in _ALLOWED_COMPARES:
        raise ValueError(
            f"comparison must be one of {sorted(_ALLOWED_COMPARES)}"
        )
    threshold = spec.get("threshold")
    if threshold is None:
        raise ValueError("threshold-style rule requires 'threshold'")
    try:
        threshold_f = float(threshold)
    except (TypeError, ValueError):
        raise ValueError("threshold must be numeric")
    unit = str(spec.get("unit", spec.get("threshold_unit", "")))
    return _make_threshold_rule(
        rid, name, description, severity,
        metric, compare, threshold_f, unit,
        min_dur=min_dur, dedup=dedup,
    )


def _custom_rule_to_dict(r: AlertRule, extra: dict) -> dict:
    """Serialize a custom rule back to its file/API dict shape.

    `extra` is the original spec (so we keep fields we don't round-trip
    through AlertRule, like bad_values). Merged over a canonical view.
    """
    d = {
        "id": r.id,
        "name": r.name,
        "description": r.description,
        "severity": r.severity,
        "metric": extra.get("metric"),
        "min_duration_sec": r.min_duration_sec,
        "dedup_sec": r.dedup_sec,
        "unit": r.threshold_unit,
    }
    if "bad_values" in extra:
        d["bad_values"] = list(extra["bad_values"])
    else:
        d["comparison"] = extra.get("comparison") or extra.get("compare")
        d["threshold"] = r.default_threshold
    return d


class AlertsEngine:
    def __init__(self, state, ws_manager, config_path: Path):
        self.state = state
        self.ws = ws_manager
        self.config_path = config_path
        self.rules = build_rule_catalog()
        self._builtin_ids = {r.id for r in self.rules}
        self._cfg: dict[str, RuleConfig] = {}
        # Specs for user-authored rules, kept so CRUD can round-trip
        # fields (metric, bad_values, comparison) that don't live on
        # AlertRule verbatim.
        self._custom_specs: dict[str, dict] = {}
        self._first_seen_at: dict[str, float] = {}          # when rule's condition first became true
        self._last_fired_at: dict[str, float] = {}
        self._currently_firing: dict[str, Alert] = {}
        self._log: list[Alert] = []
        self._load()

    # --- config persistence ---
    def _load(self):
        """Load persisted config.

        File schema (backwards compatible):
          {
            "<builtin-rule-id>": {"enabled": bool, "threshold": num|null},
            ...
            "_custom": [ {id, name, severity, metric, ...}, ... ]
          }
        """
        data = {}
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text())
            except Exception:
                pass
        # Built-in overrides.
        for r in self.rules:
            cfg = data.get(r.id, {})
            self._cfg[r.id] = RuleConfig.from_dict(
                cfg, default_enabled=r.default_enabled,
                default_threshold=r.default_threshold,
            )
        # Custom rules — append to the catalog.
        for spec in data.get("_custom", []) or []:
            try:
                rule = _build_custom_rule(spec)
            except Exception:
                continue
            if rule.id in self._builtin_ids or rule.id in self._custom_specs:
                continue  # id collision — skip to avoid shadowing.
            self.rules.append(rule)
            self._custom_specs[rule.id] = spec
            rc_raw = data.get(rule.id, {})
            self._cfg[rule.id] = RuleConfig.from_dict(
                rc_raw, default_enabled=rule.default_enabled,
                default_threshold=rule.default_threshold,
            )

    def _save(self):
        try:
            payload: dict[str, Any] = {
                k: v.to_dict() for k, v in self._cfg.items()
            }
            if self._custom_specs:
                payload["_custom"] = list(self._custom_specs.values())
            self.config_path.write_text(json.dumps(payload, indent=2))
        except Exception:
            pass

    def reload_rules(self) -> None:
        """Rebuild the catalog from the built-in builders + whatever
        custom specs we currently hold, then re-apply persisted cfg.

        Called after in-process mutations so callers don't have to
        worry about stale `self.rules` / `self._cfg` state.
        """
        self.rules = build_rule_catalog()
        self._builtin_ids = {r.id for r in self.rules}
        # Re-materialize customs from their specs.
        survived: dict[str, dict] = {}
        for rid, spec in self._custom_specs.items():
            try:
                rule = _build_custom_rule(spec)
            except Exception:
                continue
            self.rules.append(rule)
            survived[rid] = spec
        self._custom_specs = survived
        # Rebuild _cfg: keep existing values where possible, add defaults.
        new_cfg: dict[str, RuleConfig] = {}
        for r in self.rules:
            old = self._cfg.get(r.id)
            if old is not None:
                new_cfg[r.id] = old
            else:
                new_cfg[r.id] = RuleConfig(
                    enabled=r.default_enabled,
                    threshold=r.default_threshold,
                )
        self._cfg = new_cfg

    # --- CRUD for custom rules ---

    def _rule_by_id(self, rule_id: str) -> Optional[AlertRule]:
        for r in self.rules:
            if r.id == rule_id:
                return r
        return None

    def rule_view(self, rule_id: str) -> Optional[dict]:
        r = self._rule_by_id(rule_id)
        if r is None:
            return None
        cfg = self._cfg[r.id]
        is_custom = r.id in self._custom_specs
        view = {
            "id": r.id,
            "name": r.name,
            "description": r.description,
            "severity": r.severity,
            "default_enabled": r.default_enabled,
            "default_threshold": r.default_threshold,
            "threshold_unit": r.threshold_unit,
            "min_duration_sec": r.min_duration_sec,
            "dedup_sec": r.dedup_sec,
            "enabled": cfg.enabled,
            "threshold": cfg.threshold,
            "custom": is_custom,
        }
        if is_custom:
            spec = self._custom_specs[r.id]
            view["metric"] = spec.get("metric")
            if "bad_values" in spec:
                view["bad_values"] = list(spec["bad_values"])
            else:
                view["comparison"] = (
                    spec.get("comparison") or spec.get("compare")
                )
        return view

    def catalog_view(self) -> list[dict]:
        return [self.rule_view(r.id) for r in self.rules]

    def create_rule(self, spec: dict) -> dict:
        """Add a user-authored rule. Raises ValueError on bad input or
        id collision."""
        rule = _build_custom_rule(spec)
        if rule.id in self._builtin_ids:
            raise ValueError(
                f"rule id {rule.id!r} shadows a built-in rule"
            )
        if self._rule_by_id(rule.id) is not None:
            raise ValueError(f"rule id {rule.id!r} already exists")
        self._custom_specs[rule.id] = dict(spec)
        self.reload_rules()
        # Honor any initial enabled/threshold override in the spec too.
        if "enabled" in spec or "threshold" in spec:
            cfg = self._cfg[rule.id]
            if "enabled" in spec:
                cfg.enabled = bool(spec["enabled"])
            if "threshold" in spec and spec["threshold"] is not None:
                cfg.threshold = float(spec["threshold"])
        self._save()
        return self.rule_view(rule.id) or {}

    def replace_rule(self, rule_id: str, spec: dict) -> Optional[dict]:
        """Replace a custom rule in place. Built-in rules can't be
        replaced this way (use `update_rule` for enable/threshold)."""
        if rule_id in self._builtin_ids:
            raise ValueError(
                f"built-in rule {rule_id!r} cannot be replaced; "
                "only `enabled`/`threshold` can be updated"
            )
        if rule_id not in self._custom_specs:
            return None
        # Force the id in the spec to match the URL segment.
        spec = dict(spec)
        spec["id"] = rule_id
        rule = _build_custom_rule(spec)
        self._custom_specs[rule.id] = spec
        # Clear any stale firing state so the new rule starts clean.
        self._first_seen_at.pop(rule_id, None)
        self._currently_firing.pop(rule_id, None)
        self.reload_rules()
        if "enabled" in spec:
            self._cfg[rule_id].enabled = bool(spec["enabled"])
        if "threshold" in spec and spec["threshold"] is not None:
            self._cfg[rule_id].threshold = float(spec["threshold"])
        self._save()
        return self.rule_view(rule_id)

    def delete_rule(self, rule_id: str) -> bool:
        """Delete a custom rule. Returns False if unknown or built-in."""
        if rule_id in self._builtin_ids:
            return False
        if rule_id not in self._custom_specs:
            return False
        self._custom_specs.pop(rule_id, None)
        self._cfg.pop(rule_id, None)
        self._first_seen_at.pop(rule_id, None)
        self._currently_firing.pop(rule_id, None)
        self._last_fired_at.pop(rule_id, None)
        self.reload_rules()
        self._save()
        return True

    def update_rule(self, rule_id: str, enabled: bool | None = None,
                    threshold: float | None = None) -> bool:
        cfg = self._cfg.get(rule_id)
        if cfg is None:
            return False
        if enabled is not None: cfg.enabled = bool(enabled)
        if threshold is not None: cfg.threshold = float(threshold)
        self._save()
        return True

    def test_rule(self, rule_id: str) -> Optional[dict]:
        """Evaluate a single rule once against current state.

        Returns {fires, value, alert} or None if unknown. Does NOT update
        firing state, does NOT emit notifications — this is a
        "would-this-work?" dry-run for the UI.
        """
        r = self._rule_by_id(rule_id)
        if r is None:
            return None
        cfg = self._cfg[rule_id]
        data = self.state.get_all() if hasattr(self.state, "get_all") else {}
        threshold = cfg.threshold if cfg.threshold is not None else 0.0
        alert = r.evaluate(data, threshold)
        return {
            "rule_id": rule_id,
            "fires": alert is not None,
            "enabled": cfg.enabled,
            "alert": alert.to_dict() if alert else None,
        }

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
