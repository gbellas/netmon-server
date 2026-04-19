"""NetMon - Network Monitoring Dashboard."""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

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
# Balance310DerivedPoller, BR1SshPingPoller, InControlPoller) are no
# longer imported here — they're constructed by driver classes under
# pollers/drivers/*. server.py only knows the registry.
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
    # `balance310` deliberately maps to `peplink_derived`, NOT
    # `peplink_router`. The author's Balance 310 is InControl-managed
    # with no reachable local REST API; the `Balance310DerivedPoller`
    # that synthesises its state from ping + BR1 peer info is wrapped by
    # the `peplink_derived` driver. Mapping it to `peplink_router` would
    # produce permanent REST auth errors. See pollers/drivers/peplink_derived.py.
    _legacy_kind_map = {
        "udm":        {"kind": "unifi_network"},
        "br1":        {"kind": "peplink_router", "is_mobile": True},
        "balance310": {"kind": "peplink_derived"},
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

    # 3) Top-level incontrol block → synthesized incontrol device.
    ic = cfg.get("incontrol")
    if isinstance(ic, dict) and ic.get("enabled"):
        if "incontrol" not in devices:
            devices["incontrol"] = {
                "kind":          "incontrol",
                "name":          "InControl 2",
                "enabled":       True,
                "org_id":        ic.get("org_id", ""),
                "poll_interval": int(ic.get("poll_interval", 60)),
                "event_limit":   int(ic.get("event_limit", 30)),
            }
    cfg.pop("incontrol", None)

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
        # For peplink_derived (InControl-managed Balance routers with no
        # local REST), we derive the tunnel peer from the sibling
        # peplink_router flagged is_mobile=true and inject it into
        # `spec.extra` under reserved underscore keys. The driver uses
        # those to compute its `tunnel_ping_key` and `br1_name` so its
        # state matches what the legacy Balance310DerivedPoller produced.
        if spec.kind == "peplink_derived":
            peer_id, peer_host = _find_mobile_peplink_peer()
            if peer_host:
                spec.extra["_peer_host"] = peer_host
            if peer_id:
                spec.extra["_peer_id"] = peer_id
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


def _find_mobile_peplink_peer() -> tuple[str, str]:
    """Return `(id, host)` of the first peplink_router device flagged
    `is_mobile: true`, or `("", "")` if none exists. Used to wire the
    peplink_derived driver's tunnel-peer reference.

    Picks the first in insertion order (YAML preserves it in pyyaml ≥5).
    In practice there's only one mobile router in the author's deployment;
    if a future config has several, the choice is deterministic.
    """
    for dev_id, raw in (config.get("devices") or {}).items():
        if not isinstance(raw, dict):
            continue
        if raw.get("kind") == "peplink_router" and raw.get("is_mobile"):
            return dev_id, raw.get("host", "")
    return "", ""


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
    """Return `incoming` with any empty-string secret falling back to the
    value from `previous`.

    Rationale: GET redacts passwords to "" so they never traverse the
    wire. The editor PUTs the form back verbatim. Without this merge
    every round-trip would wipe the password. Operators then either
    have to re-type every secret on every edit (friction) or the
    client has to track which fields are "sentinel redacted" vs
    actually-empty (complexity the server is better placed to solve).

    Only empty *strings* trigger the fallback — explicit `null`
    survives, which is how a caller deliberately clears a password.
    Recurses into nested dicts (catches `ssh.password`) but NOT into
    lists; our secrets don't live in list elements.
    """
    import copy
    if not isinstance(incoming, dict) or not isinstance(previous, dict):
        return copy.deepcopy(incoming)

    merged: dict = {}
    for k, v in incoming.items():
        if isinstance(v, dict):
            merged[k] = _merge_preserving_secrets(previous.get(k) or {}, v)
        elif k in _SECRET_KEYS and v == "":
            # Preserve whatever was there before, including if it was
            # also empty (nothing to preserve = still empty).
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
    return JSONResponse({
        "data": state.get_all(),
        "history": state.get_history(),
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
        if kind == "incontrol":
            capabilities.append("cloud")
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
    elif kind == "incontrol":
        clean.setdefault("enabled",       False)
        clean.setdefault("org_id",        "")
        clean.setdefault("poll_interval", 60)
        clean.setdefault("event_limit",   30)

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


# RoamLink carrier PLMN codes (the three carriers Peplink's RoamLink has SIMs for)
ROAMLINK_CARRIERS = {
    "verizon": {"mcc": "311", "mnc": "480", "name": "Verizon"},
    "att":     {"mcc": "310", "mnc": "410", "name": "AT&T"},
    "tmobile": {"mcc": "310", "mnc": "260", "name": "T-Mobile"},
}


@app.post("/api/control/br1/sf/enable")
async def control_br1_sf_enable(body: SfEnableBody):
    """Toggle the BR1's SpeedFusion profile. Disabled = tunnel down, traffic
    goes direct via WANs (subject to outbound policy)."""
    ctrl = _get_controller("br1")
    res = await ctrl.set_sf_profile_enable(body.profile_id, body.enable)
    await ctrl.apply_config()
    return {"ok": True, "enable": body.enable, "result": res}


@app.post("/api/control/br1/rat")
async def control_br1_rat(body: RatBody):
    """Lock BR1 cellular modem to a specific RAT (LTE-only, 3G-only, Auto, etc.).
    Triggers an immediate reconnect so the change takes effect."""
    valid = {"auto", "LTE", "LTE+3G", "3G+2G", "3G", "2G", "3G_2G", "2G_3G"}
    if body.mode not in valid:
        raise HTTPException(400, f"Invalid mode '{body.mode}'. Valid: {', '.join(sorted(valid))}")
    ctrl = _get_controller("br1")
    res = await ctrl.set_cellular_rat_and_reconnect(body.mode)
    return {"ok": True, "mode": body.mode, "result": res}


@app.post("/api/control/br1/carrier")
async def control_br1_carrier(body: CarrierBody):
    """Switch RoamLink eSIM carrier AND force the modem to re-register immediately.

    Without the forced reconnect the modem would keep its current connection and
    only honor the new preference on the next natural reconnect cycle (could be
    hours or never), making the UI look broken. We disable/enable the cellular
    WAN right after saving the preference, which triggers an immediate re-scan."""
    ctrl = _get_controller("br1")
    key = body.carrier.lower().strip()
    if key == "auto":
        res = await ctrl.set_roamlink_auto_and_reconnect()
    elif key in ROAMLINK_CARRIERS:
        c = ROAMLINK_CARRIERS[key]
        res = await ctrl.set_roamlink_carrier_and_reconnect(c["mcc"], c["mnc"], c["name"])
    else:
        raise HTTPException(400, f"Unknown carrier '{body.carrier}'. Use: verizon, att, tmobile, auto")
    return {"ok": True, "carrier": body.carrier, "result": res}


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


@app.post("/api/alerts/rules/{rule_id}")
async def update_alert_rule(rule_id: str, body: AlertRuleUpdate):
    if _alerts is None: raise HTTPException(503, "alerts engine not ready")
    ok = _alerts.update_rule(rule_id, enabled=body.enabled, threshold=body.threshold)
    if not ok: raise HTTPException(404, f"Unknown rule id: {rule_id}")
    return {"ok": True}


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
                if fired and apns.is_configured and push_tokens.count() > 0:
                    tokens = push_tokens.all()
                    for alert in fired:
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

    # All pollers — including the previously-legacy udm / br1 /
    # balance310 / ping_targets / incontrol entries — are now driven
    # from the devices: map via `_migrate_legacy_config` + the driver
    # registry. There is no second code path.

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
