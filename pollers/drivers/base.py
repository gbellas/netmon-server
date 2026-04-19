"""DeviceDriver protocol and the generic device-config data class.

A `DeviceDriver` is a class whose instances translate a single entry in
config.yaml (with `kind: <driver-name>`) into a set of concrete pollers.
The driver owns poller construction but not their lifecycle — the
server's startup code collects the pollers from every driver and then
schedules them on the event loop.

Why this separation exists:
 - The old code hardcoded device IDs in `server.py` startup ("if udm_cfg
   exists, construct UniFiPoller"). Adding a new device meant editing
   startup code, the alerts engine, and the client dashboard.
 - Drivers invert that. `server.py` just walks `config["devices"]` and
   asks each driver "here's my config — give me pollers to run." New
   devices = one new driver file + one line in the registry.

Drivers are STATELESS by default: the pollers they create carry all
per-device state. A driver is allowed to hold state if it genuinely needs
cross-poller coordination (e.g. sharing a ping lock between REST and SSH
streams on the same router); that should be the exception, not the rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

# `BasePoller` isn't imported directly to avoid a circular import. Drivers
# return whatever subclass they've chosen; the server.py task runner only
# relies on the shape (has a `run()` coroutine, has a `name` attr). That's
# duck-typed all the way through.


@dataclass
class DeviceSpec:
    """Parsed, validated device entry from config.yaml.

    The raw YAML dict ends up here normalised — so drivers don't have to
    deal with "is this key present?" / "was it set to null?" questions.
    """

    id: str
    """Free-form identifier, e.g. "udm" or "truck". State keys are
    published as `<id>.<metric>`. Must be unique across the deployment
    and stable — renaming breaks historical sparkline data since the
    history buffer is keyed by state key."""

    kind: str
    """Driver kind. Must match a key in `drivers.registry.DRIVERS`. See
    the registry module for supported values."""

    display_name: str
    """Human-readable label for the UI. Defaults to `id` if missing."""

    host: str = ""
    """Primary management IP/hostname. Most drivers need this; those
    that don't (e.g. InControl cloud) can leave it blank."""

    username: str = ""
    password: str = ""
    """Auth for the primary management interface. Password is resolved
    from env var `NETMON_<ID-UPPERCASE>_PASSWORD` if empty here (server.py
    does that substitution before handing the spec to the driver)."""

    poll_interval: int = 10
    """Seconds between polls. Drivers can override per-poller."""

    verify_ssl: bool = False
    """Most home/LAN devices use self-signed certs. Default off."""

    is_mobile: bool = False
    """If true, the driver enables cellular-specific parsing (RAT, signal
    bars, bands). Used by peplink_router to distinguish BR1/MAX-family
    from Balance-family."""

    wan_carriers: dict[str, str] = field(default_factory=dict)
    """Per-WAN carrier labels for UI branding, e.g. {"1": "fiber",
    "2": "att"}. Only meaningful for gateways that can't detect the
    downstream ISP themselves (e.g. UniFi seeing an LTE modem as plain
    Ethernet)."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Driver-specific fields. Each driver documents what it reads here.
    E.g. peplink_router reads `extra["ssh"]` for the SSH ping streamer
    config; unifi_network reads `extra["site"]` (defaults to "default")."""

    @classmethod
    def from_config(cls, device_id: str, raw: dict) -> "DeviceSpec":
        """Parse a single entry from config.yaml's `devices:` map.

        Tolerant of legacy shape (old device configs lacked `kind:`).
        Callers should catch KeyError on missing required fields.
        """
        if "kind" not in raw:
            raise KeyError(f"device '{device_id}' missing required 'kind' field")
        known = {
            "kind", "name", "host", "username", "password",
            "poll_interval", "verify_ssl", "is_mobile", "wan_carriers",
        }
        extra = {k: v for k, v in raw.items() if k not in known}
        return cls(
            id=device_id,
            kind=raw["kind"],
            display_name=raw.get("name") or device_id,
            host=raw.get("host", ""),
            username=raw.get("username", ""),
            password=raw.get("password", ""),
            poll_interval=int(raw.get("poll_interval", 10)),
            verify_ssl=bool(raw.get("verify_ssl", False)),
            is_mobile=bool(raw.get("is_mobile", False)),
            wan_carriers={str(k): str(v)
                          for k, v in (raw.get("wan_carriers") or {}).items()},
            extra=extra,
        )


class DeviceDriver(Protocol):
    """Protocol all drivers must implement.

    Drivers don't inherit from a common base — protocol conformance is
    enough, which means third-party / out-of-tree drivers can plug in
    without subclassing. Add a driver by (1) writing a class that
    satisfies this protocol, (2) registering it in `drivers/registry.py`.
    """

    #: Unique kind identifier. Must match what users put in config.yaml.
    kind: str

    def __init__(self, spec: DeviceSpec) -> None:
        """Validate spec. Raise ValueError on missing / malformed fields.
        Don't do any I/O here — that happens inside the pollers."""

    def build_pollers(
        self,
        *,
        state: Any,
        ws_manager: Any,
        bandwidth_meter: Any = None,
        pause_state: Any = None,
    ) -> list[Any]:
        """Return a list of poller instances (already constructed but not
        started). Each poller must expose an async `run()` coroutine and
        a `name` string.

        The server appends these to its global poller registry and runs
        `asyncio.create_task(p.run())` for each one. Pollers created by
        one driver are allowed to share state with each other (e.g. the
        Peplink driver gives its REST and SSH pollers the same lock so
        they don't trip over the router's `support ping` CLI).
        """

    async def set_wan_enabled(self, wan_index: int, enabled: bool) -> dict:
        """Enable or disable a WAN interface.

        Drivers that can't implement this — because they don't own a
        local management API on the device, or the device has no notion
        of per-WAN enable/disable — raise NotImplementedError with a
        human-readable message. The server's
        POST /api/devices/{id}/wan/{n}/{enable|disable} endpoint
        translates NotImplementedError into a clean 501 so the iOS client
        can hide the toggle rather than showing a generic error.

        Implementations MUST be idempotent from the driver's view: calling
        enable on an already-enabled WAN should not error. Drivers that
        batch a config apply (Peplink) should apply before returning.

        Return value is passed through to the HTTP response as JSON, so
        return driver-level detail the client might surface. The server
        wraps it with {ok, wan_index, enabled} on the way out.
        """
        ...
