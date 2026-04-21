"""NetMon - Network Monitoring Dashboard."""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

import yaml

# Load .env if present (for local dev)
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import auth
from models import AppState
from ws_manager import WSManager
# The concrete poller classes (PingPoller, UniFiPoller, PeplinkPoller,
# BR1SshPingPoller, InControlPoller) are no longer imported here — they're
# constructed by driver classes under pollers/drivers/*. server.py only
# knows the registry.
from controls import PeplinkController
from controls_udm import UdmController
from alerts import AlertsEngine
from apns import APNsClient, DeviceTokenRegistry
from scheduled_tasks import Scheduler
from bandwidth_meter import BandwidthMeter
from ssh_pause import SshPauseState
from fastapi import HTTPException
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("netmon")

# Load config. Prefer `config.local.yaml` when present — that's the
# gitignored file where operators keep their real IPs/creds. `config.yaml`
# is the committed example that ships in the public repo and won't match
# any real deployment, so checking local first prevents the server from
# silently starting with example data after a clean `git pull`.
_here = Path(__file__).parent
config_path = _here / "config.local.yaml"
if not config_path.exists():
    config_path = _here / "config.yaml"
with open(config_path) as f:
    config = yaml.safe_load(f)


def _migrate_legacy_config(cfg: dict) -> dict:
    """Rewrite pre-driver config shapes into the generic devices:-with-kind
    shape, in-memory only (the YAML file on disk is untouched).

    This is how the driver registry becomes the single source of truth:
    after this function runs, every monitored thing — ping targets,
    InControl cloud integration, the original hardcoded udm/br1/balance310
    devices — lives as an entry in `cfg["devices"]` with a `kind:` field.
    The startup code can then drop every legacy branch and just walk the
    devices map.

    Three migrations, all idempotent (re-running on an already-migrated
    config is a no-op):

    1. Devices named "udm" / "br1" / "balance310" without a `kind:` field
       (the names hardcoded in the author's original deployment) get
       inferred kinds: udm → unifi_network, br1 → peplink_router with
       is_mobile=true, balance310 → peplink_router (wired Balance family).
       A Balance that isn't reachable via local REST should either be
       deleted from devices: or re-added as an icmp_ping target for
       reachability + tunnel latency — there's no longer a "derived"
       kind. See commit that removed peplink_derived.
    2. A non-empty top-level `ping_targets:` list becomes a synthesized
       icmp_ping device at id "ping_targets", carrying over the `ping:`
       block's count/timeout/interval defaults.
    3. `incontrol: {enabled: true, ...}` becomes a synthesized incontrol
       device at id "incontrol".

    After each migration the source fields are popped so the legacy
    startup branches become unreachable.
    """
    if not isinstance(cfg, dict):
        return cfg

    devices = cfg.setdefault("devices", {})
    if not isinstance(devices, dict):
        # Malformed config — bail rather than silently dropping entries.
        return cfg

    # 1) Infer `kind:` for the three legacy-named device entries.
    #
    # `balance310` now maps to `peplink_router` (the "derived" kind has
    # been removed). Deployments whose Balance isn't reachable via local
    # REST should either delete this entry or re-add it as an icmp_ping
    # target — ICMP to the router's LAN IP + SSH-ping-to-tunnel-peer
    # observations give us the same reachability + tunnel-latency data
    # the old derived poller inferred.
    _legacy_kind_map = {
        "udm":        {"kind": "unifi_network"},
        "br1":        {"kind": "peplink_router", "is_mobile": True},
        "balance310": {"kind": "peplink_router"},
    }
    for dev_id, raw in list(devices.items()):
        if not isinstance(raw, dict):
            continue
        if "kind" in raw:
            continue
        if dev_id in _legacy_kind_map:
            inferred = _legacy_kind_map[dev_id]
            # Preserve every field the operator had — just add `kind:`
            # (and, for br1, is_mobile if it wasn't set). Never clobber
            # an explicit is_mobile=false on br1.
            raw["kind"] = inferred["kind"]
            if "is_mobile" in inferred and "is_mobile" not in raw:
                raw["is_mobile"] = inferred["is_mobile"]

    # 2) Top-level ping_targets → synthesized icmp_ping device.
    ping_targets = cfg.get("ping_targets")
    if isinstance(ping_targets, list) and ping_targets:
        ping_cfg = cfg.get("ping") or {}
        # Don't clobber a user-authored `ping_targets` device entry.
        if "ping_targets" not in devices:
            devices["ping_targets"] = {
                "kind":     "icmp_ping",
                "name":     "Ping targets",
                "targets":  ping_targets,
                "count":    int(ping_cfg.get("count", 1)),
                "timeout":  int(ping_cfg.get("timeout", 2)),
                "interval": int(ping_cfg.get("interval", 5)),
            }
    # Pop whether or not we migrated — the legacy startup branch should
    # never see these keys after load-time migration. A user-authored
    # ping_targets device is preserved because it lives under devices:.
    cfg.pop("ping_targets", None)
    cfg.pop("ping", None)

    # 3) InControl is an integration, not a device. If an older config
    #    migrated it into devices["incontrol"], pull it back out to the
    #    top-level `incontrol:` block so the integration startup branch
    #    picks it up.
    legacy_device_ic = devices.pop("incontrol", None)
    if isinstance(legacy_device_ic, dict) and "incontrol" not in cfg:
        cfg["incontrol"] = {
            "enabled":       bool(legacy_device_ic.get("enabled", False)),
            "org_id":        legacy_device_ic.get("org_id", ""),
            "poll_interval": int(legacy_device_ic.get("poll_interval", 60)),
            "event_limit":   int(legacy_device_ic.get("event_limit", 30)),
        }

    # 4) Legacy top-level direct_host/direct_port → br1 device's
    #    extra["direct"] block. Older deployments had app-side direct-mode
    #    knobs written as top-level yaml keys; this folds them into the
    #    per-device shape the API now exposes.
    legacy_direct_host = cfg.pop("direct_host", None)
    legacy_direct_port = cfg.pop("direct_port", None)
    if (legacy_direct_host or legacy_direct_port) and "br1" in devices:
        br1 = devices["br1"]
        if isinstance(br1, dict):
            direct = br1.setdefault("direct", {})
            direct.setdefault("enabled", True)
            if legacy_direct_host and "host" not in direct:
                direct["host"] = str(legacy_direct_host)
            if legacy_direct_port is not None and "port" not in direct:
                try:
                    direct["port"] = int(legacy_direct_port)
                except (TypeError, ValueError):
                    pass

    # 5) Per-device `wan_carriers` → `wan_overrides[idx].carrier_override`
    #    when wan_overrides isn't already set. Keeps the legacy dict readable
    #    by clients that still expect it; the new field is the write path.
    for dev_id, raw in devices.items():
        if not isinstance(raw, dict):
            continue
        if raw.get("wan_overrides"):
            continue
        legacy = raw.get("wan_carriers")
        if not isinstance(legacy, dict) or not legacy:
            continue
        migrated: dict = {}
        for idx, carrier in legacy.items():
            if carrier is None:
                continue
            migrated[str(idx)] = {"carrier_override": str(carrier)}
        if migrated:
            raw["wan_overrides"] = migrated

    return cfg


config = _migrate_legacy_config(config)

# Resolve passwords from env vars
for dev_key, dev_cfg in config.get("devices", {}).items():
    if not dev_cfg.get("password"):
        env_key = f"NETMON_{dev_key.upper()}_PASSWORD"
        dev_cfg["password"] = os.environ.get(env_key, "")

# App state and WebSocket manager
state = AppState(max_history=config.get("history", {}).get("max_points", 120))
bandwidth_meter = BandwidthMeter()
ssh_pause = SshPauseState()
ws_manager = WSManager(state, bandwidth_meter=bandwidth_meter)
apns = APNsClient()
push_tokens = DeviceTokenRegistry(Path(__file__).parent / "secrets" / "push_tokens.json")

app = FastAPI(title="NetMon")

# Initialize auth early so token is ready before routes are hit.
auth.init_token()


# Paths that bypass auth (no bearer token needed).
_AUTH_OPEN_PATHS = {
    "/api/health",         # watchdog endpoint; must stay open
}


@app.middleware("http")
async def _auth_middleware(request, call_next):
    """Gate every /api/* route behind the bearer token, except explicit
    opens above. Static assets, index, PWA manifest, service worker all
    pass through untouched so the web client can bootstrap and prompt for
    the token."""
    path = request.url.path
    if path.startswith("/api/") and path not in _AUTH_OPEN_PATHS:
        authz = request.headers.get("authorization")
        qtok = request.query_params.get("token")
        provided = None
        if authz and authz.lower().startswith("bearer "):
            provided = authz.split(None, 1)[1].strip()
        provided = provided or qtok
        import secrets as _secrets
        if not provided or not _secrets.compare_digest(provided, auth.current_token()):
            return JSONResponse(
                {"detail": "unauthorized — set Authorization: Bearer <token>"},
                status_code=401,
            )
    return await call_next(request)


# Registry of all pollers; /api/health walks it to report per-poller
# health. Populated at startup, drained at shutdown.
_registered_pollers: list = []

# Per-device (id → list[asyncio.Task]) map for hot-reload. When a client
# POSTs to /api/devices we spin up the driver's pollers and stash their
# Tasks here; on PUT/DELETE we cancel the old ones first. Driver
# devices only — legacy-shaped entries are started at boot via the
# fallback path below and aren't tracked here.
_device_tasks: dict[str, list] = {}

# Per-device (id → driver-instance) map. Endpoints that need to invoke
# a driver method (e.g. POST /api/devices/{id}/wan/{n}/enable calling
# `driver.set_wan_enabled`) look up the live instance here rather than
# rebuilding it from config — that way any in-memory state the driver
# attached during build_pollers (cached session references, shared
# locks) stays available.
_device_drivers: dict[str, Any] = {}


def _start_driver_device(dev_id: str, raw: dict) -> tuple[int, str]:
    """Build + start the pollers for a single driver-backed device.

    Returns (poller_count, error). On error, (0, message); poller_count
    is zero and nothing is scheduled.

    Called from both startup (for pre-existing devices) and the
    POST/PUT endpoints (for runtime additions).
    """
    from pollers.drivers import DeviceSpec, get_driver
    try:
        spec = DeviceSpec.from_config(dev_id, raw)
        driver = get_driver(spec.kind)(spec)
    except (KeyError, ValueError) as e:
        return 0, str(e)
    new_pollers = driver.build_pollers(
        state=state,
        ws_manager=ws_manager,
        bandwidth_meter=bandwidth_meter,
        pause_state=ssh_pause,
    )
    tasks = []
    for p in new_pollers:
        _registered_pollers.append(p)
        tasks.append(asyncio.create_task(p.run()))
    _device_tasks[dev_id] = tasks
    _device_drivers[dev_id] = driver
    return len(new_pollers), ""


def _stop_driver_device(dev_id: str) -> int:
    """Cancel all running pollers for a device and clear its state-key
    namespace. Returns the number of tasks that were cancelled.

    Safe to call for unknown ids (no-op). Does NOT touch config on disk
    — callers do that separately."""
    tasks = _device_tasks.pop(dev_id, [])
    _device_drivers.pop(dev_id, None)
    for t in tasks:
        t.cancel()
    # Drop the device's state entries (`<id>.*` keys) so the dashboard
    # doesn't keep rendering stale data after removal. We also drop
    # sibling namespaces like `<id>_internet.*` / `<id>_tunnel.*` that
    # peplink_router uses for per-role SSH ping state.
    keys_to_drop = [
        k for k in list(state.get_all().keys())
        if k == dev_id or k.startswith(f"{dev_id}.")
        or k.startswith(f"{dev_id}_internet.")
        or k.startswith(f"{dev_id}_tunnel.")
    ]
    if keys_to_drop:
        state.delete(*keys_to_drop)
    # Prune the poller registry so /api/health doesn't keep listing
    # dead pollers.
    global _registered_pollers
    _registered_pollers = [
        p for p in _registered_pollers
        if not getattr(p, "name", "").startswith(dev_id)
    ]
    return len(tasks)


_SECRET_KEYS = ("password", "client_secret", "secret", "auth_token")


def _merge_preserving_secrets(previous: dict, incoming: dict) -> dict:
    """Overlay `incoming` onto `previous`, with two guarantees:

    1. Any key present in `previous` but *absent* from `incoming` is
       preserved. Pre-2026-04-20 behavior iterated only `incoming`
       keys, silently dropping anything the editor didn't re-send —
       which was every legacy key and, more painfully, the device
       password (the editor leaves the password field blank when it
       isn't being changed and writes no `password` key at all). The
       result: save-without-retyping-password wiped REST auth and
       killed the device.
    2. An empty-string value for a secret key (`password`,
       `client_secret`, etc.) falls back to the previous value —
       supports the "password field shows blank but don't clobber"
       UX when the client DOES send an explicit empty string.

    Explicit `null` survives, which is how a caller deliberately
    clears a password. Recurses into nested dicts (catches
    `ssh.password`) but NOT into lists; our secrets don't live in
    list elements.
    """
    import copy
    if not isinstance(incoming, dict) or not isinstance(previous, dict):
        return copy.deepcopy(incoming)

    # Start from a deep copy of previous, then overlay incoming.
    merged: dict = copy.deepcopy(previous)
    for k, v in incoming.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            # Empty dict = "wipe this container entirely". Without this
            # carve-out, e.g. PUT'ing wan_overrides={} would preserve
            # whatever was there before (user can't remove keys).
            if not v:
                merged[k] = {}
            else:
                merged[k] = _merge_preserving_secrets(merged[k], v)
        elif k in _SECRET_KEYS and v == "":
            # Preserve previous value when the client sends empty
            # (sentinel for "don't change").
            merged[k] = previous.get(k, "")
        else:
            merged[k] = copy.deepcopy(v)
    return merged


def _persist_config() -> None:
    """Atomically rewrite config.local.yaml with the current in-memory
    `config` dict. Preserves the 0600 perms we rely on for secrets."""
    import yaml as _yaml
    target = Path(__file__).parent / "config.local.yaml"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(_yaml.safe_dump(config, default_flow_style=False))
    tmp.chmod(0o600)
    tmp.replace(target)


@app.get("/api/version")
async def version():
    """Build / release metadata. The iOS app polls this after connect to
    detect when the user's Mac is running an older server than what the
    app was shipped against (triggers the in-app "update available"
    banner). Unauthenticated? No — kept behind auth so the listing
    isn't a free reconnaissance surface. See `/api/health` for the
    unauthenticated liveness probe."""
    import sys
    from datetime import datetime, timezone
    try:
        from version import __version__, GIT_SHA
    except Exception:
        __version__, GIT_SHA = "0.0.0", "unknown"
    # build_date: mtime of version.py — stable across restarts on the
    # same installed build, rolls forward on each reinstall.
    try:
        mtime = Path(__file__).with_name("version.py").stat().st_mtime
        build_date = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    except Exception:
        build_date = ""
    return {
        "version":        __version__,
        "git_sha":        GIT_SHA,
        "build_date":     build_date,
        "python_version": sys.version.split()[0],
    }


@app.get("/api/health")
async def health():
    """Unauthenticated liveness probe for watchdog scripts.

    Returns 200 if the process is alive. Also reports the staleness of the
    most-lagging poller so the watchdog can escalate (kick the service) when
    a poller is stuck even though the HTTP server itself is up."""
    snapshot = bandwidth_meter.snapshot()
    pollers = [p.health() for p in _registered_pollers]
    max_stale = 0.0
    for h in pollers:
        ssss = h.get("seconds_since_success")
        if isinstance(ssss, (int, float)) and ssss > max_stale:
            max_stale = ssss
    return {
        "ok": True,
        "uptime_seconds": int(snapshot["elapsed_seconds"]),
        "max_stale_poller_seconds": int(max_stale),
        "pollers": pollers,
    }

# Serve static files
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}


@app.get("/")
async def index():
    return FileResponse(static_dir / "index.html", headers=_NO_CACHE)


@app.get("/manifest.json")
async def manifest():
    return FileResponse(static_dir / "manifest.json", headers=_NO_CACHE)


@app.get("/sw.js")
async def service_worker():
    return FileResponse(static_dir / "sw.js", media_type="application/javascript", headers=_NO_CACHE)


@app.get("/api/state")
async def get_state():
    # `ui_prefs` is embedded here (instead of requiring a second request)
    # because clients need them on every reconnect to render correctly.
    # Keeps wire traffic to one round-trip per full-state refresh.
    return JSONResponse({
        "data": state.get_all(),
        "history": state.get_history(),
        "ui_prefs": _ui_prefs_view(),
    })


@app.get("/api/devices")
async def list_devices():
    """Enumerate the devices the server is configured to poll.

    The iPhone app uses this during its first-launch setup and to render
    the dashboard's device list without hardcoding names. Returns one
    entry per configured device with enough metadata for the client to
    build a card:
      - id: the state-key prefix (e.g. "br1", "udm")
      - kind: the driver kind, or "legacy_<id>" for pre-driver entries
      - display_name: human label
      - is_mobile: hint for the UI to pick cellular-specific views
      - capabilities: list of strings like ["rest", "ssh_ping",
        "per_wan_carriers"] so the client knows which sub-views apply

    Passwords and other secrets are NEVER included.
    """
    result: list[dict] = []
    for dev_id, raw in (config.get("devices") or {}).items():
        if not isinstance(raw, dict):
            continue
        # Post-migration every entry has `kind:` — the legacy fallback
        # that used to live here is dead (see `_migrate_legacy_config`).
        kind = raw.get("kind", "unknown")
        capabilities: list[str] = []
        if (raw.get("ssh") or {}).get("enabled"):
            capabilities.append("ssh_ping")
        if raw.get("wan_carriers"):
            capabilities.append("per_wan_carriers")
        if raw.get("host"):
            capabilities.append("rest")
        if kind == "icmp_ping":
            capabilities.append("icmp_ping")
        result.append({
            "id":            dev_id,
            "kind":          kind,
            "display_name":  raw.get("name") or dev_id,
            "host":          raw.get("host", ""),
            "is_mobile":     bool(raw.get("is_mobile", False)),
            "enabled":       bool(raw.get("enabled", True)),
            "capabilities":  capabilities,
        })
    return JSONResponse({"devices": result})


def _device_edit_view(dev_id: str, raw: dict) -> dict:
    """Produce the full edit-form view of a device's config.

    The iPhone and web editors need every field a driver *could* read —
    not just the ones the operator happened to set — so the form can
    render populated controls for every option. This function:

      1. Deep-copies the raw config so we never mutate config.yaml.
      2. Fills in defaults for every known field per `kind:` (so a
         device with no `poll_interval:` in YAML still shows 10 in the
         form instead of a blank field).
      3. Redacts secrets. Passwords are replaced with an empty string;
         the client is expected to re-type only when changing them
         (PUT preserves the previous password if the empty-string
         sentinel is sent).

    Kept as a helper so tests can exercise it without spinning up the
    HTTP stack.
    """
    import copy
    clean = copy.deepcopy(raw)

    kind = clean.get("kind", "")

    # Common fields every driver inspects via DeviceSpec.from_config.
    # poll_interval is set per-kind below (InControl's default is 60,
    # not 10) so it isn't filled in here.
    clean.setdefault("kind",          kind)
    clean.setdefault("name",          clean.get("name") or dev_id)
    clean.setdefault("host",          "")
    clean.setdefault("username",      "")
    clean.setdefault("password",      "")
    clean.setdefault("verify_ssl",    False)
    clean.setdefault("is_mobile",     False)
    clean.setdefault("wan_carriers",  {})
    clean.setdefault("wan_overrides", {})
    # `direct` is a per-device app-side mode. The server doesn't poll via
    # it — the iOS app does — but we persist the knobs here so the same
    # settings UI edits them. Defaults match an "off" state so pre-existing
    # deployments look identical on upgrade.
    direct = clean.setdefault("direct", {})
    if isinstance(direct, dict):
        direct.setdefault("enabled",    False)
        direct.setdefault("host",       "")
        direct.setdefault("port",       0)
        direct.setdefault("auth_mode",  "none")
        direct.setdefault("auth_token", "")
        direct.setdefault("timeout_ms", 2000)

    # Driver-specific defaults. Kept inline (no per-driver "describe
    # your fields" hook) because the field set is small and the
    # benefit of a declarative schema doesn't outweigh the indirection
    # for four kinds.
    if kind in ("peplink_router", "unifi_network", "icmp_ping"):
        clean.setdefault("poll_interval", 10)

    if kind == "peplink_router":
        ssh = clean.setdefault("ssh", {})
        ssh.setdefault("enabled",       False)
        ssh.setdefault("port",          22)
        ssh.setdefault("username",      clean.get("username", ""))
        ssh.setdefault("password",      "")
        ssh.setdefault("targets",       [])
        ssh.setdefault("count",         5)
        ssh.setdefault("ssh_timeout",   10)
        ssh.setdefault("poll_interval", 30)
    elif kind == "icmp_ping":
        clean.setdefault("targets",  [])
        clean.setdefault("count",    1)
        clean.setdefault("timeout",  2)
        clean.setdefault("interval", 5)

    # Secret redaction, recursive — covers both top-level password and
    # ssh.password / any driver-specific nested secret.
    def _strip(node: Any) -> None:
        if not isinstance(node, dict):
            return
        for k in list(node.keys()):
            if k in ("password", "client_secret", "secret", "auth_token"):
                node[k] = ""
            else:
                _strip(node[k])
    _strip(clean)
    return clean


@app.get("/api/devices/{dev_id}")
async def get_device(dev_id: str):
    """Return the full config for a single device, with secrets stripped
    and every driver-recognized field present (defaults filled in).

    Used by the iPhone Edit-device form to prefill non-secret fields
    (ICMP targets, port, SSH config flag, etc.) that the summary
    endpoint at GET /api/devices deliberately omits. Passwords are
    ALWAYS redacted on the wire — the editor shows them as blank and
    the user re-types if they want to change them. If the client sends
    the empty string back on PUT, the server keeps the previously-stored
    password (see `update_device`).
    """
    devices = config.get("devices") or {}
    raw = devices.get(dev_id)
    if raw is None:
        raise HTTPException(404, f"no device with id {dev_id!r}")
    return JSONResponse({"id": dev_id, "config": _device_edit_view(dev_id, raw)})


@app.get("/api/driver-kinds")
async def list_driver_kinds():
    """Return the driver kinds this server knows about. The iPhone app's
    'Add device' wizard uses this to populate the kind dropdown."""
    from pollers.drivers import DRIVERS
    return JSONResponse({"kinds": sorted(DRIVERS.keys())})


# ---- Device CRUD --------------------------------------------------------
#
# Writes persist to config.local.yaml (the operator's gitignored copy)
# and hot-start / hot-stop the relevant pollers. No server restart
# needed — the client gets back the updated device list immediately
# and the dashboard fills with fresh state as soon as the driver's
# first poll lands.
#
# Legacy-shaped devices (no `kind:` in config) can't be edited through
# this API — they need to be rewritten with a `kind:` field first. The
# app surfaces a "legacy" chip so users know to migrate.

class _DeviceBody(BaseModel):
    """Payload for POST/PUT on /api/devices.

    `id` is the state-key prefix and config dict key. Required on POST,
    ignored on PUT (PUT uses the URL path segment for identity).
    `config` is the full per-device YAML dict — kind, host, username,
    password, and any driver-specific keys under `extra`.
    """
    id: str | None = None
    config: dict


def _validate_device_config(dev_id: str, raw: dict) -> None:
    """Raise HTTPException(400) if the device dict isn't something we
    could spin up a driver for. Used before writing to disk so a bad
    POST doesn't leave config.local.yaml half-edited."""
    from pollers.drivers import DeviceSpec, get_driver
    if not isinstance(raw, dict):
        raise HTTPException(400, "device config must be an object")
    if "kind" not in raw:
        raise HTTPException(
            400,
            "device config must include a `kind:` field. "
            f"GET /api/driver-kinds for valid values."
        )
    try:
        spec = DeviceSpec.from_config(dev_id, raw)
        # Instantiate to trigger driver-level required-field checks
        # (missing host, missing username, etc.) without starting any tasks.
        get_driver(spec.kind)(spec)
    except KeyError as e:
        raise HTTPException(400, f"unknown device kind: {e}")
    except ValueError as e:
        raise HTTPException(400, str(e))


_ID_RE = __import__("re").compile(r"^[a-z][a-z0-9_]{0,31}$")


@app.post("/api/devices")
async def add_device(body: _DeviceBody):
    """Create a new device entry in config.local.yaml and start its
    pollers immediately. Rejects duplicate ids and malformed configs.

    Device id constraints: lowercase, alphanumeric + underscore, 1-32
    chars, first char a letter. Restrictive on purpose — the id is used
    as a state-key prefix + appears in JSON paths, so whitespace /
    Unicode weirdness would create surprises downstream."""
    # Validate exactly what the client sent (no silent normalization):
    # if they hand us "UPPER" it's a bug in their form, not something
    # we should quietly coerce.
    dev_id = (body.id or "").strip()
    if not _ID_RE.match(dev_id):
        raise HTTPException(
            400,
            "id must be lowercase a-z / 0-9 / _, starting with a letter, "
            "max 32 chars"
        )
    if dev_id in (config.get("devices") or {}):
        raise HTTPException(409, f"device id {dev_id!r} already exists")
    _validate_device_config(dev_id, body.config)

    # Commit: update in-memory config, persist to disk, start pollers.
    # Order matters: if pollers fail to start after a disk write,
    # the config file is still consistent — restart will try to boot
    # the same pollers and log the error rather than running stale
    # state against disk.
    config.setdefault("devices", {})[dev_id] = body.config
    _persist_config()
    count, err = _start_driver_device(dev_id, body.config)
    if err:
        # Rollback in-memory + on-disk state; we didn't actually land
        # a working device.
        del config["devices"][dev_id]
        _persist_config()
        raise HTTPException(500, f"device failed to start: {err}")
    return {"ok": True, "id": dev_id, "pollers_started": count}


@app.put("/api/devices/{dev_id}")
async def update_device(dev_id: str, body: _DeviceBody):
    """Replace an existing device's config atomically. Cancels the old
    pollers, writes the new config, starts fresh pollers.

    Accepts a full config dict — partial updates aren't supported (by
    design: a PATCH interface would double the validation surface and
    the UI already has the full object to round-trip)."""
    devices = config.get("devices") or {}
    if dev_id not in devices:
        raise HTTPException(404, f"no device with id {dev_id!r}")
    # Don't let PUT change the id; that's what DELETE+POST is for.
    if body.id is not None and body.id != dev_id:
        raise HTTPException(400, "id cannot be changed via PUT")
    _validate_device_config(dev_id, body.config)

    # Stop old pollers BEFORE writing the config so a failing restart
    # doesn't leave two sets running against the same id.
    previous = devices[dev_id]
    merged = _merge_preserving_secrets(previous, body.config)
    # If the PUT provided wan_overrides, the legacy wan_carriers dict is
    # considered obsolete for this device — wipe it so subsequent GETs
    # don't return both a stale carrier and a fresh override.
    if isinstance(merged.get("wan_overrides"), dict) and merged["wan_overrides"]:
        merged.pop("wan_carriers", None)
    _stop_driver_device(dev_id)
    config["devices"][dev_id] = merged
    _persist_config()
    count, err = _start_driver_device(dev_id, merged)
    if err:
        # Roll config back so the next launch isn't broken, and restart
        # the PREVIOUS pollers so the user isn't left with a dead device.
        config["devices"][dev_id] = previous
        _persist_config()
        _start_driver_device(dev_id, previous)
        raise HTTPException(500, f"new config failed to start: {err}")
    return {"ok": True, "id": dev_id, "pollers_started": count}


@app.delete("/api/devices/{dev_id}")
async def delete_device(dev_id: str):
    """Remove a device: cancel its pollers, drop its state-key namespace,
    delete the config entry, persist.

    Returns 404 if the device doesn't exist, so clients can distinguish
    "never existed" from "deleted successfully"."""
    devices = config.get("devices") or {}
    if dev_id not in devices:
        raise HTTPException(404, f"no device with id {dev_id!r}")
    cancelled = _stop_driver_device(dev_id)
    del config["devices"][dev_id]
    _persist_config()
    # Broadcast the state-key removals so connected clients drop
    # stale cards without waiting for a reconnect.
    await ws_manager.broadcast({f"_removed.{dev_id}": True})
    return {"ok": True, "id": dev_id, "pollers_cancelled": cancelled}


@app.get("/api/config/export")
async def export_config():
    """Return the server's active config as JSON, with all secrets stripped.

    The iPhone's 'Export config' button hits this to produce a share-sheet
    JSON file that friends can import on their own NetMon installs. We
    strip every field that could leak a password — even ones the server
    populated itself from env vars — to prevent accidentally handing
    someone admin access to the original operator's routers.

    Fields stripped per-device: `password`, `ssh.password`,
    `oauth.client_secret`, and anything starting with `_`. Top-level
    `incontrol.client_secret` (if present) also redacted.
    """
    import copy
    exported = copy.deepcopy(config)

    def _strip_secrets(node: Any) -> None:
        if not isinstance(node, dict):
            return
        for k in list(node.keys()):
            if k.startswith("_"):
                del node[k]
                continue
            if k in ("password", "client_secret", "api_token",
                     "secret", "auth_token"):
                # Replace with a placeholder so the JSON shape is
                # preserved — makes importers' validation trivial.
                node[k] = ""
                continue
            _strip_secrets(node[k])

    _strip_secrets(exported)
    return JSONResponse(exported)


class _ImportConfigBody(BaseModel):
    """Body for POST /api/config/import. Passed through yaml.safe_dump
    verbatim (secrets stay in env vars where they belong — the import
    doesn't write passwords)."""
    config: dict


@app.post("/api/config/import")
async def import_config(body: _ImportConfigBody):
    """Replace the server's `config.local.yaml` with the provided dict.
    The imported config is validated (every device must have a known
    `kind:` or be a legacy shape) before being written to disk.

    Does NOT hot-reload — the server keeps running with its current
    config until the operator restarts. This is intentional: a bad
    import shouldn't be able to take the server offline mid-request.
    The response tells the client to prompt the user to restart.
    """
    import yaml
    imported = body.config
    # Validation pass: every device either has a known `kind:` that maps
    # to a registered driver, or is missing `kind:` (legacy shape — the
    # import allows that but warns the client).
    from pollers.drivers import DRIVERS
    legacy_count = 0
    for dev_id, raw in (imported.get("devices") or {}).items():
        if not isinstance(raw, dict):
            raise HTTPException(400, f"device {dev_id!r}: not an object")
        kind = raw.get("kind")
        if kind is None:
            legacy_count += 1
            continue
        if kind not in DRIVERS:
            raise HTTPException(400,
                f"device {dev_id!r}: unknown kind {kind!r}. "
                f"Known: {sorted(DRIVERS.keys())}")
    # Write to config.local.yaml (the gitignored operator copy). The
    # committed config.yaml stays untouched as the public example.
    target = Path(__file__).parent / "config.local.yaml"
    target.write_text(yaml.safe_dump(imported, default_flow_style=False))
    target.chmod(0o600)
    return {
        "ok": True,
        "legacy_device_count": legacy_count,
        "message": (
            "Imported. Restart the server (launchctl kickstart or run.sh) "
            "to apply." if legacy_count == 0 else
            f"Imported. {legacy_count} device(s) use legacy shape; they'll "
            "still work but won't show up as editable in the app until "
            "you add a `kind:` field."
        ),
    }


_controllers: dict[str, PeplinkController] = {}
_udm_controller: UdmController | None = None


def _get_udm_controller() -> UdmController:
    global _udm_controller
    if _udm_controller: return _udm_controller
    dev = config.get("devices", {}).get("udm")
    if not dev or not dev.get("host"):
        raise HTTPException(404, "UDM not configured")
    _udm_controller = UdmController(
        host=dev["host"],
        username=dev.get("username", "netmon"),
        password=dev.get("password", ""),
        verify_ssl=dev.get("verify_ssl", False),
    )
    return _udm_controller


def _get_controller(device_key: str) -> PeplinkController:
    if device_key in _controllers:
        return _controllers[device_key]
    dev = config.get("devices", {}).get(device_key)
    if not dev or not dev.get("host"):
        raise HTTPException(404, f"Device '{device_key}' not configured")
    # OAuth credentials from env (required for carrier switching; optional otherwise)
    env_prefix = f"NETMON_{device_key.upper()}_OAUTH"
    ctrl = PeplinkController(
        host=dev["host"],
        username=dev.get("username", "admin"),
        password=dev.get("password", ""),
        verify_ssl=dev.get("verify_ssl", False),
        oauth_client_id=os.environ.get(f"{env_prefix}_CLIENT_ID"),
        oauth_client_secret=os.environ.get(f"{env_prefix}_CLIENT_SECRET"),
    )
    _controllers[device_key] = ctrl
    return ctrl


class WanEnableBody(BaseModel):
    enable: bool


class WanPriorityBody(BaseModel):
    priority: int


class CarrierBody(BaseModel):
    carrier: str  # "verizon", "att", "tmobile", or "auto"


class RatBody(BaseModel):
    mode: str  # "auto", "LTE", "LTE+3G", "3G", etc.


class SfEnableBody(BaseModel):
    enable: bool
    profile_id: int = 1


class SpeedtestBody(BaseModel):
    # When True, disable the other WAN so this one becomes the active uplink,
    # run the test, then re-enable. Disruptive; user must opt in per call.
    force_standby: bool = False


@app.post("/api/control/{device}/wan/{wan_id}/enable")
async def control_wan_enable(device: str, wan_id: int, body: WanEnableBody):
    if device == "udm":
        ctrl = _get_udm_controller()
        res = await ctrl.set_wan_enable(wan_id, body.enable)
        return {"ok": True, "result": res}
    ctrl = _get_controller(device)
    res = await ctrl.set_wan_enable(wan_id, body.enable)
    await ctrl.apply_config()
    return {"ok": True, "result": res}


# ---- Driver-backed WAN toggle (generic, any router kind) ----------------
#
# Unlike the `/api/control/{device}/wan/{n}/enable` endpoint above (which
# hardcodes a BR1/UDM controller pair), these route through the
# `DeviceDriver.set_wan_enabled` protocol method — so adding a new router
# kind only requires implementing that method on the new driver. The iOS
# app targets these endpoints to show a uniform toggle UI across device
# kinds.

async def _driver_wan_toggle(dev_id: str, wan_index: int, enabled: bool) -> dict:
    """Shared implementation for the enable/disable endpoints."""
    driver = _device_drivers.get(dev_id)
    if driver is None:
        raise HTTPException(404, f"no running driver for device {dev_id!r}")
    try:
        result = await driver.set_wan_enabled(wan_index, enabled)
    except NotImplementedError as e:
        # 501 is the right code for "this device kind doesn't support
        # the operation." The client can use it to hide the toggle.
        raise HTTPException(501, str(e))
    logger.info(
        f"driver {type(driver).__name__} WAN{wan_index} "
        f"{'enabled' if enabled else 'disabled'} on device '{dev_id}'"
    )
    return {
        "ok":        True,
        "wan_index": int(wan_index),
        "enabled":   bool(enabled),
        "result":    result,
    }


@app.post("/api/devices/{dev_id}/wan/{wan_index}/enable")
async def device_wan_enable(dev_id: str, wan_index: int):
    """Enable a WAN interface on the given device via its driver.
    Returns 501 if the device's driver doesn't support WAN toggling."""
    return await _driver_wan_toggle(dev_id, wan_index, True)


@app.post("/api/devices/{dev_id}/wan/{wan_index}/disable")
async def device_wan_disable(dev_id: str, wan_index: int):
    """Disable a WAN interface on the given device via its driver.
    Returns 501 if the device's driver doesn't support WAN toggling."""
    return await _driver_wan_toggle(dev_id, wan_index, False)


# ---- Graceful drain-then-run helpers --------------------------------------
#
# Any disruptive WAN action (disable, carrier switch, RAT lock) risks
# dropping live flows that happen to be pinned to the affected interface.
# `drain_and_run` demotes the WAN to standby priority first, publishes a
# countdown so the UI can show it, waits N seconds for new flows to route
# off the interface, runs the disruptive action, and then restores the
# previous priority so the next re-enable has its original tier. If the
# user cancels mid-countdown, the disruptive action is skipped but the
# priority is still restored so the WAN is never left demoted.

# Active drains keyed by (dev_id, wan_index) → asyncio.Task. Used so a
# second drain request on the same WAN either returns "already draining"
# or can cancel the prior one.
_active_drains: dict[tuple[str, int], asyncio.Task] = {}

# Lowest priority tier we demote to during drain. Peplink standard is
# 3 (Standby on BR1 / MAX family); UDM-style failover_priority treats
# higher numbers as lower-priority too. A single constant keeps the
# driver protocol simple at the cost of some vendor idiom leakage —
# revisit if we add a driver that numbers priorities the other way.
DRAIN_PRIORITY = 3


async def _drain_and_run(
    dev_id: str,
    wan_index: int,
    driver: Any,
    wait_seconds: float,
    action: Callable[[], Any],
    action_label: str,
) -> None:
    """Run the drain flow: demote → wait → action → restore.

    `action` is an async callable with no args that performs the
    disruptive operation (e.g., `driver.set_wan_enabled(n, False)`).
    State keys `<dev_id>.wan<n>.drain_*` are published so the UI can
    render a countdown. Exceptions from the action are logged and
    broadcast as `drain_error`; priority restore runs regardless.
    """
    key = (dev_id, int(wan_index))
    wan_key_prefix = f"{dev_id}.wan{int(wan_index)}"
    # 1. Snapshot current priority so we can restore it later.
    prev_priority = 1
    try:
        if hasattr(driver, "get_wan_priority"):
            prev_priority = await driver.get_wan_priority(int(wan_index))
    except Exception as e:
        logger.warning(
            f"drain: failed to read prior priority on {dev_id} wan{wan_index}: {e}"
        )

    drain_ends_at = time.time() + float(wait_seconds)
    try:
        # 2. Demote. If the driver doesn't support priority control, skip
        #    demote and go straight to the action — the user still gets
        #    the countdown; they just don't get the bleed-off benefit.
        try:
            if hasattr(driver, "set_wan_priority"):
                await driver.set_wan_priority(int(wan_index), DRAIN_PRIORITY)
        except Exception as e:
            logger.warning(
                f"drain: demote failed on {dev_id} wan{wan_index}: {e}"
            )

        # 3. Publish drain state for the UI countdown.
        updates = {
            f"{wan_key_prefix}.drain_status":   "draining",
            f"{wan_key_prefix}.drain_label":    action_label,
            f"{wan_key_prefix}.drain_ends_at":  drain_ends_at,
            f"{wan_key_prefix}.drain_prev_priority": int(prev_priority),
        }
        state.update(updates)
        await ws_manager.broadcast(updates)

        # 4. Wait (cancellable).
        await asyncio.sleep(float(wait_seconds))

        # 5. Run the disruptive action.
        state.update({f"{wan_key_prefix}.drain_status": "running"})
        await ws_manager.broadcast({f"{wan_key_prefix}.drain_status": "running"})
        try:
            await action()
        except Exception as e:
            logger.error(
                f"drain: action {action_label!r} failed on {dev_id} "
                f"wan{wan_index}: {e}"
            )
            err_updates = {
                f"{wan_key_prefix}.drain_status": "error",
                f"{wan_key_prefix}.drain_error":  str(e)[:300],
            }
            state.update(err_updates)
            await ws_manager.broadcast(err_updates)
            # Still fall through to restore priority — better to leave
            # the router cleanly configured than in a demoted-forever
            # state just because one HTTP call failed.

    except asyncio.CancelledError:
        # Client called DELETE — skip the action, restore priority.
        state.update({f"{wan_key_prefix}.drain_status": "canceling"})
        await ws_manager.broadcast({f"{wan_key_prefix}.drain_status": "canceling"})

    finally:
        # 6. Always restore the prior priority, even on error or cancel.
        #    Best-effort: if this also fails, log + broadcast but don't
        #    raise (the task is already winding down).
        try:
            if hasattr(driver, "set_wan_priority"):
                await driver.set_wan_priority(int(wan_index), int(prev_priority))
        except Exception as e:
            logger.warning(
                f"drain: priority restore failed on {dev_id} "
                f"wan{wan_index}: {e}"
            )

        # 7. Clear drain state keys so the UI returns to normal.
        clear = {
            f"{wan_key_prefix}.drain_status":        "",
            f"{wan_key_prefix}.drain_label":         "",
            f"{wan_key_prefix}.drain_ends_at":       0,
            f"{wan_key_prefix}.drain_prev_priority": 0,
            f"{wan_key_prefix}.drain_error":         "",
        }
        state.update(clear)
        await ws_manager.broadcast(clear)
        _active_drains.pop(key, None)


def _start_drain(
    dev_id: str,
    wan_index: int,
    driver: Any,
    wait_seconds: float,
    action: Callable[[], Any],
    action_label: str,
) -> dict:
    """Kick off a drain in the background; error if one is already in
    flight for the same (device, wan) pair."""
    key = (dev_id, int(wan_index))
    existing = _active_drains.get(key)
    if existing and not existing.done():
        raise HTTPException(
            409,
            f"drain already in progress for {dev_id} wan{wan_index}; "
            f"DELETE /api/devices/{dev_id}/wan/{wan_index}/drain to cancel",
        )
    task = asyncio.create_task(
        _drain_and_run(
            dev_id, int(wan_index), driver,
            float(wait_seconds), action, action_label,
        )
    )
    _active_drains[key] = task
    return {
        "ok": True,
        "wan_index": int(wan_index),
        "wait_seconds": float(wait_seconds),
        "action": action_label,
    }


@app.post("/api/devices/{dev_id}/wan/{wan_index}/drain-and-disable")
async def device_wan_drain_and_disable(
    dev_id: str, wan_index: int, wait: float = 15.0,
):
    """Demote the WAN's priority, wait `wait` seconds, then disable it.
    Returns immediately with 202-style status; subscribe to the state
    stream for `<dev_id>.wan<n>.drain_*` keys to render the countdown."""
    driver = _device_drivers.get(dev_id)
    if driver is None:
        raise HTTPException(404, f"no running driver for device {dev_id!r}")
    if not hasattr(driver, "set_wan_enabled"):
        raise HTTPException(501, "driver does not support WAN toggling")

    async def _do_disable() -> None:
        await driver.set_wan_enabled(int(wan_index), False)

    return _start_drain(
        dev_id, int(wan_index), driver,
        float(wait), _do_disable, "disable",
    )


@app.delete("/api/devices/{dev_id}/wan/{wan_index}/drain")
async def device_wan_drain_cancel(dev_id: str, wan_index: int):
    """Cancel an in-flight drain. Skips the disruptive action but still
    restores the WAN's original priority."""
    key = (dev_id, int(wan_index))
    task = _active_drains.get(key)
    if task is None or task.done():
        raise HTTPException(404, f"no drain in progress for {dev_id} wan{wan_index}")
    task.cancel()
    return {"ok": True, "canceled": True}


class _DrainCarrierBody(BaseModel):
    carrier: str    # verizon / att / tmobile / auto
    wait: float = 15.0


@app.post("/api/devices/{dev_id}/wan/{wan_index}/drain-and-set-carrier")
async def device_wan_drain_and_set_carrier(
    dev_id: str, wan_index: int, body: _DrainCarrierBody,
):
    """Graceful carrier switch: demote priority, wait, then change the
    cellular carrier (the underlying RoamLink re-register drops active
    flows on this WAN, so draining first lets them migrate)."""
    driver = _device_drivers.get(dev_id)
    if driver is None:
        raise HTTPException(404, f"no running driver for device {dev_id!r}")
    if not hasattr(driver, "set_carrier"):
        raise HTTPException(501, "driver does not support carrier switching")

    async def _do_carrier() -> None:
        await driver.set_carrier(body.carrier)

    return _start_drain(
        dev_id, int(wan_index), driver,
        float(body.wait), _do_carrier, f"carrier:{body.carrier}",
    )


class _DrainRatBody(BaseModel):
    rat: str        # auto / LTE / LTE+3G / 3G / ...
    wait: float = 15.0


@app.post("/api/devices/{dev_id}/wan/{wan_index}/drain-and-set-rat")
async def device_wan_drain_and_set_rat(
    dev_id: str, wan_index: int, body: _DrainRatBody,
):
    """Graceful RAT (radio mode) switch: demote priority, wait, then
    lock the radio to the requested mode."""
    driver = _device_drivers.get(dev_id)
    if driver is None:
        raise HTTPException(404, f"no running driver for device {dev_id!r}")
    if not hasattr(driver, "set_rat"):
        raise HTTPException(501, "driver does not support RAT switching")

    async def _do_rat() -> None:
        await driver.set_rat(body.rat)

    return _start_drain(
        dev_id, int(wan_index), driver,
        float(body.wait), _do_rat, f"rat:{body.rat}",
    )


# RoamLink carrier PLMN codes (the three carriers Peplink's RoamLink has SIMs for)
ROAMLINK_CARRIERS = {
    "verizon": {"mcc": "311", "mnc": "480", "name": "Verizon"},
    "att":     {"mcc": "310", "mnc": "410", "name": "AT&T"},
    "tmobile": {"mcc": "310", "mnc": "260", "name": "T-Mobile"},
}


@app.post("/api/control/br1/sf/enable")
async def control_br1_sf_enable(body: SfEnableBody):
    """DEPRECATED. Use POST /api/devices/br1/control/sf_enable.

    Kept as an alias so the legacy JS frontend and older app builds
    keep working while the iOS app migrates to per-device endpoints."""
    logger.warning(
        "deprecated: /api/control/br1/sf/enable — use "
        "/api/devices/{id}/control/sf_enable"
    )
    return await device_control_sf_enable(
        "br1", _DeviceSfEnableBody(enabled=body.enable, profile_id=body.profile_id),
    )


@app.post("/api/control/br1/rat")
async def control_br1_rat(body: RatBody):
    """DEPRECATED. Use POST /api/devices/br1/control/rat."""
    logger.warning(
        "deprecated: /api/control/br1/rat — use "
        "/api/devices/{id}/control/rat"
    )
    return await device_control_rat(
        "br1", _DeviceRatBody(wan_index=2, rat=body.mode),
    )


@app.post("/api/control/br1/carrier")
async def control_br1_carrier(body: CarrierBody):
    """DEPRECATED. Use POST /api/devices/br1/control/carrier."""
    logger.warning(
        "deprecated: /api/control/br1/carrier — use "
        "/api/devices/{id}/control/carrier"
    )
    return await device_control_carrier(
        "br1", _DeviceCarrierBody(wan_index=2, carrier=body.carrier),
    )


# ---- Generic driver-backed control endpoints ----------------------------
#
# These parameterize on the device id so a Peplink router with any
# `devices.<id>` key (not just the legacy "br1") gets the same carrier /
# RAT / SpeedFusion controls via the dashboard. Each returns HTTP 501 if
# the device's driver kind doesn't support the operation (today: anything
# other than peplink_router).

class _DeviceCarrierBody(BaseModel):
    # wan_index currently ignored by the driver (RoamLink is always the
    # cellular WAN = WAN 2 on BR1 Pro 5G) but accepted so the API shape
    # scales to future multi-modem routers without a breaking change.
    wan_index: int = 2
    carrier:   str


class _DeviceRatBody(BaseModel):
    wan_index: int = 2
    rat:       str


class _DeviceSfEnableBody(BaseModel):
    enabled:    bool
    profile_id: int = 1


def _require_peplink_driver(dev_id: str):
    """Look up the running driver and confirm it's a peplink_router. Raises
    404 if no driver is registered, 501 if the kind is unsupported."""
    driver = _device_drivers.get(dev_id)
    if driver is None:
        raise HTTPException(404, f"no running driver for device {dev_id!r}")
    if getattr(driver, "kind", None) != "peplink_router":
        raise HTTPException(
            501,
            f"device kind {getattr(driver, 'kind', '?')!r} does not "
            "support cellular / SpeedFusion controls",
        )
    return driver


@app.post("/api/devices/{dev_id}/control/carrier")
async def device_control_carrier(dev_id: str, body: _DeviceCarrierBody):
    driver = _require_peplink_driver(dev_id)
    try:
        res = await driver.set_carrier(body.carrier)
    except ValueError as e:
        raise HTTPException(400, str(e))
    logger.info(
        f"driver {type(driver).__name__} device {dev_id!r} "
        f"carrier → {body.carrier}"
    )
    return {"ok": True, "carrier": body.carrier, "result": res}


@app.post("/api/devices/{dev_id}/control/rat")
async def device_control_rat(dev_id: str, body: _DeviceRatBody):
    driver = _require_peplink_driver(dev_id)
    try:
        res = await driver.set_rat(body.rat)
    except ValueError as e:
        raise HTTPException(400, str(e))
    logger.info(
        f"driver {type(driver).__name__} device {dev_id!r} "
        f"RAT → {body.rat}"
    )
    return {"ok": True, "rat": body.rat, "mode": body.rat, "result": res}


@app.post("/api/devices/{dev_id}/control/sf_enable")
async def device_control_sf_enable(dev_id: str, body: _DeviceSfEnableBody):
    driver = _require_peplink_driver(dev_id)
    res = await driver.set_sf_enable(body.enabled, profile_id=body.profile_id)
    logger.info(
        f"driver {type(driver).__name__} device {dev_id!r} "
        f"SF profile {body.profile_id} → {body.enabled}"
    )
    return {"ok": True, "enable": body.enabled, "enabled": body.enabled,
            "result": res}


@app.post("/api/control/udm/wan/{wan_id}/speedtest")
async def control_udm_speedtest(wan_id: int, body: SpeedtestBody):
    """Run a UDM speedtest tagged with a WAN id.

    Without force_standby, the UDM tests whichever uplink it's currently using
    and we just label the result with the wan_id the caller provided (normally
    the active one). With force_standby, the other WAN is disabled for the
    duration of the test so this one becomes active — disruptive but the only
    way to get a standby WAN's number without physically failing over."""
    ctrl = _get_udm_controller()
    result = await ctrl.run_speedtest(wan_id, force_standby=body.force_standby)
    # Publish the result into app state so all clients see it immediately,
    # not just the one that triggered the test.
    now = int(result.get("timestamp") or 0)
    updates = {
        f"udm.wan{wan_id}.speedtest.down_mbps": result["down_mbps"],
        f"udm.wan{wan_id}.speedtest.up_mbps":   result["up_mbps"],
        f"udm.wan{wan_id}.speedtest.latency_ms":result["latency_ms"],
        f"udm.wan{wan_id}.speedtest.timestamp": now,
        f"udm.wan{wan_id}.speedtest.mode":      result["mode"],
    }
    changed = state.update(updates)
    if changed:
        await ws_manager.broadcast(changed)
    return {"ok": True, "result": result}


@app.post("/api/control/{device}/wan/{wan_id}/priority")
async def control_wan_priority(device: str, wan_id: int, body: WanPriorityBody):
    if device == "udm":
        ctrl = _get_udm_controller()
        res = await ctrl.set_wan_priority(wan_id, body.priority)
        return {"ok": True, "result": res}
    ctrl = _get_controller(device)
    res = await ctrl.set_wan_priority(wan_id, body.priority)
    await ctrl.apply_config()
    return {"ok": True, "result": res}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Check token BEFORE accepting the connection. FastAPI exposes query_params
    # on the websocket object; no header trick required.
    if not await auth.verify_ws_token(ws):
        await ws.close(code=1008)     # 1008 = policy violation
        return
    await ws_manager.connect(ws)
    try:
        while True:
            # Keep connection alive, handle client messages if needed
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)
    except Exception:
        ws_manager.disconnect(ws)


# Alerts engine (singleton) + scheduler — created on startup.
_alerts: AlertsEngine | None = None
_scheduler: Scheduler | None = None


class AlertRuleUpdate(BaseModel):
    enabled: bool | None = None
    threshold: float | None = None


class ScheduleUpdate(BaseModel):
    enabled: bool | None = None
    hour: int | None = None
    minute: int | None = None


class SshPauseBody(BaseModel):
    seconds: int = 90
    client_label: str | None = None


@app.post("/api/ssh-pings/pause")
async def pause_ssh_pings(body: SshPauseBody):
    """Request a temporary pause of server-side SSH ping streams. Used by
    the iPhone app when it's on the BR1's LAN and polling directly —
    prevents redundant cellular-burning SSH pings from the server.

    Lease-based: the phone must call this again within `seconds` to stay
    paused. If the phone disconnects, the pause expires naturally (no
    stuck-paused bug)."""
    until = ssh_pause.request_pause(body.seconds, client_label=body.client_label or "")
    return {"ok": True, "paused_until": until,
            "seconds_remaining": ssh_pause.seconds_remaining()}


@app.post("/api/ssh-pings/resume")
async def resume_ssh_pings():
    ssh_pause.clear()
    return {"ok": True}


@app.get("/api/ssh-pings/state")
async def ssh_pings_state():
    return ssh_pause.snapshot()


class _PushRegisterBody(BaseModel):
    token: str
    platform: str = "ios"    # future-proof: "watchos"/"macos" can register separately


@app.post("/api/push/register")
async def register_device_token(body: _PushRegisterBody):
    """Register this device's APNs token for lock-screen pushes.
    iOS/watchOS clients call this once per launch. Idempotent — adding
    the same token twice is a no-op."""
    if not body.token or len(body.token) < 32:
        raise HTTPException(400, "invalid token")
    ok = push_tokens.register(body.token)
    return {"ok": ok, "registered_count": push_tokens.count()}


@app.post("/api/push/unregister")
async def unregister_device_token(body: _PushRegisterBody):
    """Remove a device token (e.g. after user toggles notifications off)."""
    push_tokens.unregister(body.token)
    return {"ok": True, "registered_count": push_tokens.count()}


@app.post("/api/push/test")
async def push_test(body: _PushRegisterBody):
    """Send a one-off test push to verify end-to-end wiring. Useful when
    debugging APNs config; hit it once from the app and check the push
    shows up on the lock screen."""
    if not apns.is_configured:
        raise HTTPException(503, "APNs not configured (check .env)")
    ok = await apns.send(
        body.token,
        title="NetMon test",
        body="If you see this, push is working.",
        severity="active",
    )
    return {"ok": ok}


@app.get("/api/bandwidth")
async def bandwidth_usage():
    """Breakdown of bytes consumed per NetMon subsystem since process start."""
    return bandwidth_meter.snapshot()


@app.get("/api/alerts/rules")
async def list_alert_rules():
    if _alerts is None: raise HTTPException(503, "alerts engine not ready")
    return {"rules": _alerts.catalog_view()}


@app.get("/api/alerts/rules/{rule_id}")
async def get_alert_rule(rule_id: str):
    if _alerts is None: raise HTTPException(503, "alerts engine not ready")
    view = _alerts.rule_view(rule_id)
    if view is None:
        raise HTTPException(404, f"Unknown rule id: {rule_id}")
    return view


@app.post("/api/alerts/rules")
async def create_alert_rule(body: dict):
    """Create a user-authored alert rule.

    Body shape (threshold-style):
      {id, name, severity, metric, comparison: '<'|'<='|'>'|'>=',
       threshold: number, unit?, min_duration_sec?, dedup_sec?}
    Or status-style:
      {id, name, severity, metric, bad_values: [str,...]}

    Built-in catalog rule ids are reserved; POST with one of them 400s.
    Writes alerts_config.json and hot-reloads the engine.
    """
    if _alerts is None: raise HTTPException(503, "alerts engine not ready")
    try:
        view = _alerts.create_rule(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return view


@app.put("/api/alerts/rules/{rule_id}")
async def replace_alert_rule(rule_id: str, body: dict):
    """Replace a custom rule in place.

    Built-in rules cannot be replaced via PUT — the server rejects with
    400 and directs the caller to POST /api/alerts/rules/{id} for the
    partial `enabled`/`threshold` override path. Writes config and
    hot-reloads.
    """
    if _alerts is None: raise HTTPException(503, "alerts engine not ready")
    try:
        view = _alerts.replace_rule(rule_id, body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if view is None:
        raise HTTPException(404, f"Unknown rule id: {rule_id}")
    return view


@app.delete("/api/alerts/rules/{rule_id}")
async def delete_alert_rule(rule_id: str):
    """Delete a custom rule. Built-in rules return 400."""
    if _alerts is None: raise HTTPException(503, "alerts engine not ready")
    if rule_id in _alerts._builtin_ids:
        raise HTTPException(
            400, f"cannot delete built-in rule {rule_id!r}"
        )
    ok = _alerts.delete_rule(rule_id)
    if not ok:
        raise HTTPException(404, f"Unknown rule id: {rule_id}")
    return {"ok": True, "id": rule_id}


@app.post("/api/alerts/rules/{rule_id}/test")
async def test_alert_rule(rule_id: str):
    """Dry-run a rule against current state — does NOT change firing
    state or emit a notification. Useful for the UI's "does this rule
    work?" button."""
    if _alerts is None: raise HTTPException(503, "alerts engine not ready")
    result = _alerts.test_rule(rule_id)
    if result is None:
        raise HTTPException(404, f"Unknown rule id: {rule_id}")
    return result


# Kept for backward compat: the old POST /api/alerts/rules/{id} endpoint
# was a PARTIAL updater for enabled/threshold overrides. It's ambiguous
# with the new POST /api/alerts/rules (create). We re-expose the partial
# updater under PATCH so clients that need it have a non-conflicting
# route.

@app.patch("/api/alerts/rules/{rule_id}")
async def patch_alert_rule(rule_id: str, body: AlertRuleUpdate):
    """Partial update: override `enabled` and/or `threshold` on any
    rule (built-in or custom)."""
    if _alerts is None: raise HTTPException(503, "alerts engine not ready")
    ok = _alerts.update_rule(
        rule_id, enabled=body.enabled, threshold=body.threshold
    )
    if not ok: raise HTTPException(404, f"Unknown rule id: {rule_id}")
    return {"ok": True}


# ---- Scheduler CRUD ----------------------------------------------------
#
# `/api/scheduler/tasks` is the full-CRUD variant. The legacy
# `/api/schedule` endpoints below are retained for clients that pinned
# to them — they still work but only expose partial updates.

@app.get("/api/scheduler/tasks")
async def list_scheduler_tasks():
    if _scheduler is None: raise HTTPException(503, "scheduler not ready")
    return {"tasks": _scheduler.list_schedules()}


@app.get("/api/scheduler/tasks/{task_id}")
async def get_scheduler_task(task_id: str):
    if _scheduler is None: raise HTTPException(503, "scheduler not ready")
    task = _scheduler.get_task(task_id)
    if task is None:
        raise HTTPException(404, f"Unknown task id: {task_id}")
    return task


class _SchedulerTaskBody(BaseModel):
    """Payload for POST/PUT. `id` is optional on POST (URL path or
    body). `config` is flat (wan_id, enabled, hour, minute)."""
    id: str | None = None
    wan_id: int | None = None
    enabled: bool | None = None
    hour: int | None = None
    minute: int | None = None


@app.post("/api/scheduler/tasks")
async def create_scheduler_task(body: _SchedulerTaskBody):
    if _scheduler is None: raise HTTPException(503, "scheduler not ready")
    task_id = (body.id or "").strip()
    if not task_id:
        raise HTTPException(400, "task 'id' is required")
    payload = body.model_dump(exclude_none=True)
    payload.pop("id", None)
    try:
        result = _scheduler.create_task(task_id, payload)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return result


@app.put("/api/scheduler/tasks/{task_id}")
async def update_scheduler_task(task_id: str, body: _SchedulerTaskBody):
    if _scheduler is None: raise HTTPException(503, "scheduler not ready")
    payload = body.model_dump(exclude_none=True)
    payload.pop("id", None)
    try:
        result = _scheduler.replace_task(task_id, payload)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if result is None:
        raise HTTPException(404, f"Unknown task id: {task_id}")
    return result


@app.delete("/api/scheduler/tasks/{task_id}")
async def delete_scheduler_task(task_id: str):
    if _scheduler is None: raise HTTPException(503, "scheduler not ready")
    ok = _scheduler.delete_task(task_id)
    if not ok:
        raise HTTPException(404, f"Unknown task id: {task_id}")
    return {"ok": True, "id": task_id}


@app.get("/api/schedule")
async def list_schedule():
    if _scheduler is None: raise HTTPException(503, "scheduler not ready")
    return {"schedules": _scheduler.list_schedules()}


@app.post("/api/schedule/{key}")
async def update_schedule(key: str, body: ScheduleUpdate):
    if _scheduler is None: raise HTTPException(503, "scheduler not ready")
    ok = _scheduler.update_schedule(
        key, enabled=body.enabled, hour=body.hour, minute=body.minute)
    if not ok: raise HTTPException(404, f"Unknown schedule key: {key}")
    return {"ok": True}


# ---- Server-level settings --------------------------------------------
#
# `/api/settings` exposes the non-per-device knobs from config.yaml:
# history, server.host/port, ping defaults. Fields that require a full
# restart (server.port / server.host) are marked in the PUT response
# under `requires_restart`; in-process state (history.max_points) is
# applied immediately.

_IMMEDIATE_SETTINGS = {
    # Keys the server can honor live. Everything else is deferred.
    "history.max_points",
}


def _settings_view() -> dict:
    """Project config.yaml down to the settings-surface the API exposes.

    Ping block: the top-level `ping:` field was migrated into the
    icmp_ping device at startup, so in the live config dict it's gone.
    We reconstitute it from the `ping_targets` device if present so the
    API view stays stable across restarts."""
    history = dict(config.get("history") or {})
    history.setdefault("max_points", state._max_history if hasattr(state, "_max_history") else 120)

    server_cfg = dict(config.get("server") or {})
    server_cfg.setdefault("host", "0.0.0.0")
    server_cfg.setdefault("port", 8077)

    # Ping: inspect the migrated icmp_ping device if it exists.
    ping_cfg: dict = {}
    pt = (config.get("devices") or {}).get("ping_targets")
    if isinstance(pt, dict):
        ping_cfg = {
            "interval": pt.get("interval", 5),
            "count":    pt.get("count", 1),
            "timeout":  pt.get("timeout", 2),
        }
    else:
        ping_cfg = {"interval": 5, "count": 1, "timeout": 2}

    return {
        "history": {"max_points": int(history.get("max_points", 120))},
        "server":  {
            "host": str(server_cfg.get("host", "0.0.0.0")),
            "port": int(server_cfg.get("port", 8077)),
        },
        "ping":    ping_cfg,
    }


@app.get("/api/settings")
async def get_settings():
    return _settings_view()


class _SettingsBody(BaseModel):
    """Partial settings update. Any block may be omitted; within a block,
    any key may be omitted. Unknown keys are rejected at 400."""
    history: dict | None = None
    server: dict | None = None
    ping: dict | None = None


@app.put("/api/settings")
async def update_settings(body: _SettingsBody):
    """Apply settings, persisting to config.yaml and returning a split
    view of what went into effect immediately vs what's deferred until
    the next restart."""
    applied: dict = {}
    deferred: dict = {}

    # ---- history block -------------------------------------------------
    if body.history is not None:
        for k, v in body.history.items():
            if k != "max_points":
                raise HTTPException(400, f"unknown history field: {k!r}")
            try:
                vv = int(v)
            except (TypeError, ValueError):
                raise HTTPException(400, "history.max_points must be int")
            if vv < 1 or vv > 100000:
                raise HTTPException(400, "history.max_points out of range")
            config.setdefault("history", {})["max_points"] = vv
            # Apply live: state._max_history is what AppState.update caps
            # rolling history by. Mutating it means future appends honor
            # the new cap; already-recorded points remain.
            try:
                state._max_history = vv
            except Exception:
                pass
            applied.setdefault("history", {})["max_points"] = vv

    # ---- server block --------------------------------------------------
    if body.server is not None:
        for k, v in body.server.items():
            if k not in ("host", "port"):
                raise HTTPException(400, f"unknown server field: {k!r}")
            if k == "port":
                try:
                    vv = int(v)
                except (TypeError, ValueError):
                    raise HTTPException(400, "server.port must be int")
                if vv < 1 or vv > 65535:
                    raise HTTPException(400, "server.port out of range")
                config.setdefault("server", {})["port"] = vv
                deferred.setdefault("server", {})["port"] = vv
            else:
                config.setdefault("server", {})["host"] = str(v)
                deferred.setdefault("server", {})["host"] = str(v)

    # ---- ping block ----------------------------------------------------
    if body.ping is not None:
        for k, v in body.ping.items():
            if k not in ("interval", "count", "timeout"):
                raise HTTPException(400, f"unknown ping field: {k!r}")
            try:
                vv = int(v)
            except (TypeError, ValueError):
                raise HTTPException(400, f"ping.{k} must be int")
            # Live-apply by mutating the icmp_ping device config if
            # present. New pollers pick it up on restart; existing ones
            # keep their constructor-captured value.
            devices = config.setdefault("devices", {})
            pt = devices.setdefault("ping_targets", {
                "kind": "icmp_ping", "targets": [],
            })
            if not isinstance(pt, dict):
                pt = {"kind": "icmp_ping", "targets": []}
                devices["ping_targets"] = pt
            pt[k] = vv
            # Interval/count/timeout don't retro-apply to running pollers,
            # so we call them out as deferred. (The config file is still
            # updated so a restart honors them.)
            deferred.setdefault("ping", {})[k] = vv

    _persist_config()

    return {
        "applied": applied,
        "requires_restart": deferred,
        "current": _settings_view(),
    }


# ---- Dashboard layout sync --------------------------------------------
#
# Persists the user's dashboard widget/device ordering + visibility in
# config.yaml under `dashboard.layout`. The iOS app was previously
# UserDefaults-only here, which meant a second device (or a reinstall)
# lost the user's arrangement. Treating it as server-owned config makes
# "everything on the dashboard is configurable" actually true across
# devices — and it's a tiny payload, so putting it in config.yaml next
# to the rest of the server config is justified.
#
# Shape:
#   {
#     "device_order":   ["br1", "udm", ...],    # ids, user-chosen order
#     "hidden":         ["ic2", ...],           # device ids to hide
#     "widget_order":   ["udm", "br1", ...],    # built-in WidgetID rawValues
#     "widget_hidden":  ["eventLog"],
#   }
#
# Missing or partially-missing blocks default to [] — which the app
# interprets as "use the built-in default order."


class _DashboardLayoutBody(BaseModel):
    device_order:  list[str] | None = None
    hidden:        list[str] | None = None
    widget_order:  list[str] | None = None
    widget_hidden: list[str] | None = None


def _dashboard_layout_view() -> dict:
    """Project config.yaml's dashboard.layout subtree down to the API
    shape. Always returns all four keys (with empty lists as defaults)
    so the iOS client can decode without special-casing first-run."""
    raw = (config.get("dashboard") or {}).get("layout") or {}
    def _as_list(v) -> list[str]:
        if isinstance(v, list):
            return [str(x) for x in v]
        return []
    return {
        "device_order":  _as_list(raw.get("device_order")),
        "hidden":        _as_list(raw.get("hidden")),
        "widget_order":  _as_list(raw.get("widget_order")),
        "widget_hidden": _as_list(raw.get("widget_hidden")),
    }


@app.get("/api/dashboard/layout")
async def get_dashboard_layout():
    return _dashboard_layout_view()


@app.put("/api/dashboard/layout")
async def update_dashboard_layout(body: _DashboardLayoutBody):
    """Full-replace PUT. Omitted keys are treated as "leave existing value
    alone" (not "clear"), so the app can send partial updates if it
    later wants to (e.g. only the device layer changed)."""
    dashboard = config.setdefault("dashboard", {})
    layout = dashboard.setdefault("layout", {})
    if body.device_order is not None:
        layout["device_order"] = [str(x) for x in body.device_order]
    if body.hidden is not None:
        layout["hidden"] = [str(x) for x in body.hidden]
    if body.widget_order is not None:
        layout["widget_order"] = [str(x) for x in body.widget_order]
    if body.widget_hidden is not None:
        layout["widget_hidden"] = [str(x) for x in body.widget_hidden]
    _persist_config()
    return _dashboard_layout_view()


# ---- UI preferences ---------------------------------------------------
#
# Non-device-specific UI knobs (theme, units, sparkline, alert-banner
# state). Persisted under `config.yaml` → `ui.*`. The iOS app + web
# client both GET these on load and PUT back whenever the user flips
# anything. They also ride along in `/api/state` snapshots under the
# `ui_prefs` key so clients don't need a separate fetch every delta.

_UI_DEFAULTS: dict = {
    "theme": "auto",
    "units": {
        "throughput":       "auto",
        "latency":          "ms",
        "bandwidth_prefix": "metric",
    },
    "timestamp_format": "relative",
    "sparkline": {
        "visible":       True,
        "window_points": 60,
        "height":        60,
    },
    "alert_banner": {
        "dismissed_ids": [],
        "dismissible":   True,
    },
    "dashboard_refresh": {
        "show_indicator": True,
        "format":         "relative",
    },
}


def _deep_merge_defaults(defaults: dict, override: Any) -> dict:
    """Return a dict that has every key from `defaults`, overlaid with
    whatever typed-compatible keys `override` carries. Missing keys come
    from defaults; unknown keys in override are dropped (so clients can't
    smuggle garbage into config.yaml).

    Special case: when `defaults` is an empty dict (or has no entry for
    a key that's also a dict in `override`), we treat it as an
    "unschematised" slot and pass the override dict through verbatim —
    that's how open maps like `rules: {rule_id: bool}` survive.
    """
    import copy
    out = copy.deepcopy(defaults)
    if not isinstance(override, dict):
        return out
    # Empty defaults dict = unschematised container; accept override as-is.
    if isinstance(defaults, dict) and not defaults and isinstance(override, dict):
        return copy.deepcopy(override)
    for k, dv in defaults.items():
        if k not in override:
            continue
        ov = override[k]
        if isinstance(dv, dict) and isinstance(ov, dict):
            out[k] = _deep_merge_defaults(dv, ov)
        elif isinstance(dv, list) and isinstance(ov, list):
            out[k] = list(ov)
        elif type(dv) is type(ov) or (isinstance(dv, bool) and isinstance(ov, bool)):
            out[k] = ov
        elif isinstance(dv, (int, float)) and isinstance(ov, (int, float)) and not isinstance(ov, bool):
            out[k] = ov
        elif isinstance(dv, str) and isinstance(ov, str):
            out[k] = ov
    return out


def _ui_prefs_view() -> dict:
    return _deep_merge_defaults(_UI_DEFAULTS, config.get("ui") or {})


def _validate_ui_prefs(body: dict) -> dict:
    """Raise 400 on obviously-invalid values; return a clean copy ready
    for persistence. Range clamps:
      sparkline.window_points: 20..240
      sparkline.height:        30..120
      theme:                   auto|dark|light
      units.throughput:        auto|bits|Mbps|Gbps
      units.latency:           ms|s
      units.bandwidth_prefix:  metric|binary
      timestamp_format:        relative|absolute|both
      dashboard_refresh.format: iso|relative|clock
    """
    if not isinstance(body, dict):
        raise HTTPException(400, "ui prefs must be an object")
    merged = _deep_merge_defaults(_UI_DEFAULTS, body)
    if merged["theme"] not in ("auto", "dark", "light"):
        raise HTTPException(400, f"invalid theme {merged['theme']!r}")
    if merged["units"]["throughput"] not in ("auto", "bits", "Mbps", "Gbps"):
        raise HTTPException(400, "invalid units.throughput")
    if merged["units"]["latency"] not in ("ms", "s"):
        raise HTTPException(400, "invalid units.latency")
    if merged["units"]["bandwidth_prefix"] not in ("metric", "binary"):
        raise HTTPException(400, "invalid units.bandwidth_prefix")
    if merged["timestamp_format"] not in ("relative", "absolute", "both"):
        raise HTTPException(400, "invalid timestamp_format")
    wp = int(merged["sparkline"]["window_points"])
    if wp < 20 or wp > 240:
        raise HTTPException(400, "sparkline.window_points out of range 20..240")
    merged["sparkline"]["window_points"] = wp
    h = int(merged["sparkline"]["height"])
    if h < 30 or h > 120:
        raise HTTPException(400, "sparkline.height out of range 30..120")
    merged["sparkline"]["height"] = h
    if merged["dashboard_refresh"]["format"] not in ("iso", "relative", "clock"):
        raise HTTPException(400, "invalid dashboard_refresh.format")
    merged["alert_banner"]["dismissed_ids"] = [
        str(x) for x in merged["alert_banner"].get("dismissed_ids") or []
    ]
    return merged


@app.get("/api/settings/ui")
async def get_ui_prefs():
    return _ui_prefs_view()


@app.put("/api/settings/ui")
async def put_ui_prefs(body: dict):
    clean = _validate_ui_prefs(body)
    config["ui"] = clean
    _persist_config()
    return _ui_prefs_view()


# ---- Per-card appearance ----------------------------------------------
#
# Keyed by driver kind. The defaults here match what the current frontend
# hardcodes, so a pre-existing deployment that's never PUT to this
# endpoint gets identical rendering on upgrade.

# Each kind lists EVERY metric key its card view emits, in canonical
# display order. The app's read-through iterates `metrics_order` and
# renders matching sections; anything missing here was being silently
# hidden after 17-rc1 wired strict filtering, which produced empty
# cards for every existing deployment that had never PUT to this
# endpoint. Keep these lists in sync with the *Card.swift view
# switches (BR1Card, UDMCard, PingCard).
_APPEARANCE_DEFAULTS: dict = {
    "peplink_router": {
        "metrics_visible": [
            "status", "uptime", "host", "wan_rows",
            "cellular", "speedfusion", "gps", "ping_targets",
        ],
        "metrics_order": [
            "status", "uptime", "host", "wan_rows",
            "cellular", "speedfusion", "gps", "ping_targets",
        ],
        "wan_row_metrics": ["latency", "jitter", "loss", "throughput", "signal"],
        "color_thresholds": {
            "latency_ms":   [100, 500],
            "loss_pct":     [1, 5],
            "jitter_ms":    [10, 50],
            "signal_rsrp":  [-100, -120],
        },
    },
    "unifi_network": {
        "metrics_visible": [
            "status", "uptime", "host", "cpu", "memory",
            "client_count", "wan_rows", "ping_targets",
        ],
        "metrics_order": [
            "status", "uptime", "host", "cpu", "memory",
            "client_count", "wan_rows", "ping_targets",
        ],
        "wan_row_metrics": ["latency", "throughput"],
        "color_thresholds": {
            "latency_ms":    [100, 500],
            "loss_pct":      [1, 5],
            "cpu_pct":       [70, 90],
            "memory_pct":    [70, 90],
        },
    },
    "icmp_ping": {
        "metrics_visible": [
            "status", "latency", "jitter", "loss", "sparkline",
        ],
        "metrics_order": [
            "status", "latency", "jitter", "loss", "sparkline",
        ],
        "wan_row_metrics": ["latency", "loss"],
        "color_thresholds": {
            "latency_ms": [100, 500],
            "loss_pct":   [1, 5],
            "jitter_ms":  [10, 50],
        },
    },
}


def _appearance_view() -> dict:
    raw = config.get("appearance") or {}
    out: dict = {}
    for kind, defaults in _APPEARANCE_DEFAULTS.items():
        out[kind] = _deep_merge_defaults(defaults, raw.get(kind) or {})
    return out


def _validate_appearance_block(kind: str, body: dict) -> dict:
    if kind not in _APPEARANCE_DEFAULTS:
        raise HTTPException(400, f"unknown card kind {kind!r}")
    merged = _deep_merge_defaults(_APPEARANCE_DEFAULTS[kind], body)
    merged["metrics_visible"] = [str(x) for x in merged["metrics_visible"]]
    merged["metrics_order"]   = [str(x) for x in merged["metrics_order"]]
    merged["wan_row_metrics"] = [str(x) for x in merged["wan_row_metrics"]]
    return merged


@app.get("/api/settings/appearance")
async def get_appearance():
    return _appearance_view()


@app.put("/api/settings/appearance")
async def put_appearance(body: dict):
    if not isinstance(body, dict):
        raise HTTPException(400, "appearance must be an object keyed by kind")
    # Validate the whole payload BEFORE mutating config so a bad block
    # doesn't leave the file half-updated.
    cleaned: dict = {}
    for kind, block in body.items():
        if not isinstance(block, dict):
            raise HTTPException(400, f"appearance[{kind!r}] must be an object")
        cleaned[kind] = _validate_appearance_block(kind, block)
    # Merge on top of any existing persisted appearance so PUTs can be
    # partial (only the card you're editing).
    existing = config.get("appearance") or {}
    merged = dict(existing)
    merged.update(cleaned)
    config["appearance"] = merged
    _persist_config()
    return _appearance_view()


# ---- Notifications prefs per-installation -----------------------------
#
# Per-APNs-token prefs: which rules, per-device mute list, quiet hours.
# Persisted alongside push_tokens.json as push_token_prefs.json so the
# existing token registry file stays a pure list (unchanged schema).

_PREFS_PATH = Path(__file__).parent / "secrets" / "push_token_prefs.json"
_NOTIF_DEFAULTS: dict = {
    "rules":       {},
    "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00"},
    "per_device":  {},
}


def _load_notif_prefs() -> dict:
    import json
    if not _PREFS_PATH.exists():
        return {}
    try:
        data = json.loads(_PREFS_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_notif_prefs(prefs: dict) -> None:
    import json
    _PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PREFS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(prefs))
    try:
        tmp.chmod(0o600)
    except Exception:
        pass
    tmp.replace(_PREFS_PATH)


def _token_prefs_view(token: str) -> dict:
    all_prefs = _load_notif_prefs()
    return _deep_merge_defaults(_NOTIF_DEFAULTS, all_prefs.get(token) or {})


def _validate_notif_prefs(body: dict) -> dict:
    if not isinstance(body, dict):
        raise HTTPException(400, "prefs must be an object")
    merged = _deep_merge_defaults(_NOTIF_DEFAULTS, body)
    # Coerce bool-valued rule/per_device maps.
    merged["rules"]      = {str(k): bool(v) for k, v in (body.get("rules") or {}).items()}
    merged["per_device"] = {str(k): bool(v) for k, v in (body.get("per_device") or {}).items()}
    qh = merged["quiet_hours"]
    qh["enabled"] = bool(qh.get("enabled", False))
    qh["start"] = str(qh.get("start", "22:00"))
    qh["end"]   = str(qh.get("end",   "07:00"))
    return merged


def _parse_hhmm(s: str) -> int:
    """Minutes-from-midnight for 'HH:MM'. Returns -1 on parse error."""
    try:
        hh, mm = s.split(":", 1)
        return int(hh) * 60 + int(mm)
    except Exception:
        return -1


def _in_quiet_hours(qh: dict, now_minutes: int) -> bool:
    if not qh.get("enabled"):
        return False
    start = _parse_hhmm(qh.get("start") or "")
    end   = _parse_hhmm(qh.get("end")   or "")
    if start < 0 or end < 0:
        return False
    if start == end:
        return False
    if start < end:
        return start <= now_minutes < end
    # Wraps midnight (e.g. 22:00 → 07:00).
    return now_minutes >= start or now_minutes < end


def _should_notify(token: str, alert: dict) -> bool:
    """True if the APNs loop should send `alert` to `token`. False when
    the user has muted the rule, muted the source device, or we're in
    quiet hours for a non-critical alert."""
    prefs = _token_prefs_view(token)
    rule_id = alert.get("rule_id") or alert.get("id") or ""
    # A rule explicitly toggled off mutes it.
    if prefs["rules"].get(str(rule_id)) is False:
        return False
    dev_id = alert.get("device_id") or alert.get("device") or ""
    if dev_id and prefs["per_device"].get(str(dev_id)) is False:
        return False
    severity = alert.get("severity", "active")
    if severity != "critical":
        import datetime
        now = datetime.datetime.now()
        if _in_quiet_hours(prefs["quiet_hours"], now.hour * 60 + now.minute):
            return False
    return True


@app.get("/api/push/tokens/{token}/prefs")
async def get_token_prefs(token: str):
    return _token_prefs_view(token)


@app.put("/api/push/tokens/{token}/prefs")
async def put_token_prefs(token: str, body: dict):
    clean = _validate_notif_prefs(body)
    all_prefs = _load_notif_prefs()
    all_prefs[token] = clean
    _save_notif_prefs(all_prefs)
    return clean


# ---- Event log + filter presets ---------------------------------------
#
# The alerts engine fires `alerts.fired` events per tick. We append each
# into an in-memory ring buffer so `/api/events` can paginate/filter over
# recent history without a separate persistence layer. Saved-filter
# presets are stored in config.yaml under `event_filters.presets`.

_EVENT_RING_MAX = 500
_event_ring: list[dict] = []


def _record_events(fired: list) -> None:
    """Append newly-fired alerts to the ring buffer. Called from the
    alerts loop. Each entry gets a monotonic `id` + ISO `ts` so the
    client can dedupe across reconnects."""
    import datetime
    if not fired:
        return
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    for a in fired:
        if not isinstance(a, dict):
            continue
        entry = {
            "id":        f"evt-{len(_event_ring) + 1}",
            "ts":        a.get("ts") or now_iso,
            "severity":  a.get("severity", "info"),
            "device_id": a.get("device_id") or a.get("device") or "",
            "rule_id":   a.get("rule_id") or a.get("id") or "",
            "title":     a.get("title", ""),
            "detail":    a.get("detail", ""),
        }
        _event_ring.append(entry)
    # Ring-buffer trim — drop oldest when over cap.
    if len(_event_ring) > _EVENT_RING_MAX:
        del _event_ring[: len(_event_ring) - _EVENT_RING_MAX]


def _filter_events(
    events: list[dict],
    severity: str | None = None,
    device_id: str | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    out = events
    if severity:
        out = [e for e in out if e.get("severity") == severity]
    if device_id:
        out = [e for e in out if e.get("device_id") == device_id]
    if since:
        out = [e for e in out if (e.get("ts") or "") >= since]
    if limit is not None:
        out = out[-int(limit):]
    return list(out)


@app.get("/api/events")
async def list_events(
    severity: str | None = None,
    device_id: str | None = None,
    since: str | None = None,
    limit: int | None = None,
):
    """Return recent alert events, newest last. Pass any combination of
    filter params; omit all of them to get the full ring buffer."""
    if severity and severity not in ("critical", "warning", "info"):
        raise HTTPException(400, f"invalid severity {severity!r}")
    events = _filter_events(_event_ring, severity, device_id, since, limit)
    return {"events": events, "total": len(_event_ring)}


def _event_filter_presets() -> list[dict]:
    ef = config.get("event_filters") or {}
    presets = ef.get("presets") or []
    return [p for p in presets if isinstance(p, dict)]


def _validate_event_preset(body: dict) -> dict:
    if not isinstance(body, dict):
        raise HTTPException(400, "preset must be an object")
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "preset.name is required")
    sev = body.get("severity")
    if sev is not None and sev not in ("critical", "warning", "info"):
        raise HTTPException(400, "preset.severity invalid")
    rel = body.get("since_relative_minutes")
    if rel is not None:
        try:
            rel = int(rel)
        except (TypeError, ValueError):
            raise HTTPException(400, "preset.since_relative_minutes must be int")
    out: dict = {"name": name}
    if sev is not None:
        out["severity"] = sev
    if body.get("device_id"):
        out["device_id"] = str(body["device_id"])
    if rel is not None:
        out["since_relative_minutes"] = rel
    return out


@app.get("/api/events/filters")
async def list_event_filters():
    return {"presets": _event_filter_presets()}


@app.post("/api/events/filters")
async def create_event_filter(body: dict):
    clean = _validate_event_preset(body)
    presets = list(_event_filter_presets())
    # Dense int id; not a UUID but stable for the small-N preset list.
    next_id = 1
    if presets:
        used = [int(p.get("id", 0) or 0) for p in presets]
        next_id = (max(used) if used else 0) + 1
    clean["id"] = str(next_id)
    presets.append(clean)
    config.setdefault("event_filters", {})["presets"] = presets
    _persist_config()
    return clean


@app.put("/api/events/filters/{preset_id}")
async def update_event_filter(preset_id: str, body: dict):
    clean = _validate_event_preset(body)
    presets = list(_event_filter_presets())
    for i, p in enumerate(presets):
        if str(p.get("id")) == str(preset_id):
            clean["id"] = str(preset_id)
            presets[i] = clean
            config.setdefault("event_filters", {})["presets"] = presets
            _persist_config()
            return clean
    raise HTTPException(404, f"no preset with id {preset_id!r}")


@app.delete("/api/events/filters/{preset_id}")
async def delete_event_filter(preset_id: str):
    presets = list(_event_filter_presets())
    new = [p for p in presets if str(p.get("id")) != str(preset_id)]
    if len(new) == len(presets):
        raise HTTPException(404, f"no preset with id {preset_id!r}")
    config.setdefault("event_filters", {})["presets"] = new
    _persist_config()
    return {"ok": True, "id": preset_id}


async def _alerts_loop():
    """Tick the alerts engine every ~5s. Publishes firing/resolved alerts
    through the same WebSocket machinery used for regular state updates,
    and fans out to APNs for any newly-firing alert so the user gets a
    lock-screen push even when the app is closed."""
    assert _alerts is not None
    while True:
        try:
            updates = _alerts.tick()
            if updates:
                changed = state.update(updates)
                if changed:
                    await ws_manager.broadcast(changed)
                # IMPORTANT: `alerts.fired` / `alerts.resolved` are EVENTS,
                # not persistent state. We put them through `state.update`
                # so the delta broadcast mechanism picks them up, then
                # immediately drop them from state. Otherwise they'd stick
                # around in `get_all()` and every reconnecting client
                # would receive them in the initial `full_state` frame —
                # which the client processes as "new delta" and fires a
                # lock-screen notification for an alert that's minutes or
                # hours old. That was the "notifications keep firing even
                # when I disabled the rule" bug.
                if "alerts.fired" in updates or "alerts.resolved" in updates:
                    state.delete("alerts.fired", "alerts.resolved")
                # Fan out newly-firing alerts to APNs. Silent no-op if
                # APNs isn't configured or no device tokens registered.
                fired = updates.get("alerts.fired")
                if fired:
                    # Always drop into the event ring, regardless of APNs
                    # state — the ring powers /api/events, not push.
                    _record_events(fired if isinstance(fired, list) else [fired])
                if fired and apns.is_configured and push_tokens.count() > 0:
                    all_tokens = push_tokens.all()
                    for alert in (fired if isinstance(fired, list) else [fired]):
                        # Filter by per-token prefs (rule mute, device mute,
                        # quiet hours). A token that's opted out of this
                        # rule simply isn't in the fanout list.
                        tokens = [t for t in all_tokens if _should_notify(t, alert)]
                        if not tokens:
                            continue
                        title = alert.get("title", "NetMon alert")
                        body = alert.get("detail", "")
                        severity = alert.get("severity", "active")
                        rule_id = alert.get("rule_id") or alert.get("id")
                        res = await apns.send_to_all(
                            tokens, title=title, body=body,
                            severity=severity, rule_id=rule_id,
                        )
                        logger.info(
                            f"APNs fanout rule={rule_id} "
                            f"sent={res['sent']}/{len(tokens)}"
                        )
        except Exception as e:
            logger.warning(f"alerts tick error: {e}")
        await asyncio.sleep(5)


# ---- InControl integration (cloud-only, not a device) ------------------

_INCONTROL_DEFAULTS = {
    "enabled":       False,
    "org_id":        "",
    "poll_interval": 60,
    "event_limit":   30,
}


def _incontrol_view() -> dict:
    raw = config.get("incontrol") or {}
    out = dict(_INCONTROL_DEFAULTS)
    for k, v in raw.items():
        if k in out:
            out[k] = v
    out["enabled"]       = bool(out["enabled"])
    out["org_id"]        = str(out["org_id"] or "")
    out["poll_interval"] = int(out["poll_interval"] or 60)
    out["event_limit"]   = int(out["event_limit"] or 30)
    return out


@app.get("/api/integrations/incontrol")
async def get_incontrol_integration():
    return _incontrol_view()


@app.put("/api/integrations/incontrol")
async def put_incontrol_integration(body: dict):
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be an object")
    merged = _incontrol_view()
    for k in ("enabled", "org_id", "poll_interval", "event_limit"):
        if k in body:
            merged[k] = body[k]
    merged["enabled"]       = bool(merged["enabled"])
    merged["org_id"]        = str(merged["org_id"] or "")
    try:
        merged["poll_interval"] = max(10, int(merged["poll_interval"]))
        merged["event_limit"]   = max(1, int(merged["event_limit"]))
    except (TypeError, ValueError):
        raise HTTPException(400, "poll_interval/event_limit must be integers")
    config["incontrol"] = merged
    _persist_config()
    return merged


@app.on_event("startup")
async def startup():
    logger.info("Starting NetMon pollers...")

    # Driver-based device pollers: any device entry with `kind:` runs
    # through the DeviceDriver registry. Devices without `kind:` fall
    # through to the legacy code path below so existing deployments keep
    # working without config edits.
    for dev_id, raw in config.get("devices", {}).items():
        if not isinstance(raw, dict) or "kind" not in raw:
            continue
        count, err = _start_driver_device(dev_id, raw)
        if err:
            logger.error(f"driver config error for device '{dev_id}': {err}")
            continue
        logger.info(
            f"driver {raw['kind']} built {count} poller(s) for '{dev_id}'"
        )

    # InControl 2 cloud integration. Top-level `incontrol:` block in
    # config.yaml; NOT a per-device driver. Credentials come from env
    # vars (NETMON_INCONTROL_CLIENT_ID / _CLIENT_SECRET).
    ic = config.get("incontrol") or {}
    if ic.get("enabled"):
        client_id = os.environ.get("NETMON_INCONTROL_CLIENT_ID", "")
        client_secret = os.environ.get("NETMON_INCONTROL_CLIENT_SECRET", "")
        if not client_id:
            logger.warning(
                "incontrol.enabled=true but NETMON_INCONTROL_CLIENT_ID is unset; skipping"
            )
        else:
            from pollers.incontrol import InControlPoller
            ic_cfg = {
                "client_id":     client_id,
                "client_secret": client_secret,
                "org_id":        ic.get("org_id", ""),
                "poll_interval": int(ic.get("poll_interval", 60)),
                "event_limit":   int(ic.get("event_limit", 30)),
            }
            ic_poller = InControlPoller(
                config=ic_cfg, state=state, ws_manager=ws_manager,
                bandwidth_meter=bandwidth_meter,
            )
            _registered_pollers.append(ic_poller)
            asyncio.create_task(ic_poller.run())
            logger.info("InControl integration started")

    # Alerts engine — evaluates rules against state every ~5s and publishes
    # firing/resolved alerts over the existing WebSocket.
    global _alerts, _scheduler
    alerts_cfg_path = Path(__file__).parent / "alerts_config.json"
    _alerts = AlertsEngine(state=state, ws_manager=ws_manager, config_path=alerts_cfg_path)
    asyncio.create_task(_alerts_loop())

    # Scheduler — daily per-WAN speedtests (off by default for WAN1, on for WAN2).
    sched_cfg_path = Path(__file__).parent / "scheduled_config.json"
    _scheduler = Scheduler(
        state=state, ws_manager=ws_manager,
        udm_controller_factory=_get_udm_controller,
        config_path=sched_cfg_path,
    )
    asyncio.create_task(_scheduler.run())

    logger.info("All pollers started (alerts + scheduler live)")
