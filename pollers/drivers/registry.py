"""Kind → driver-class map.

Adding a new driver:
 1. Write a class implementing the `DeviceDriver` protocol from base.py.
    Put it in `pollers/drivers/<kind>.py`.
 2. Import it below and add an entry to `DRIVERS`.

This file is intentionally the only place that imports every driver. That
means the rest of the codebase can get a driver by string name without
circular import pain.

If a user puts an unknown `kind:` in config.yaml, `get_driver()` raises
a descriptive error at startup rather than silently skipping the device
(which would show up as "my device isn't appearing in the UI" with no log
explanation).
"""

from __future__ import annotations

from .base import DeviceDriver
from .peplink_router import PeplinkRouterDriver
from .unifi_network import UniFiNetworkDriver
from .icmp_ping import IcmpPingDriver
from .incontrol import InControlDriver


DRIVERS: dict[str, type[DeviceDriver]] = {
    PeplinkRouterDriver.kind:  PeplinkRouterDriver,
    UniFiNetworkDriver.kind:   UniFiNetworkDriver,
    IcmpPingDriver.kind:       IcmpPingDriver,
    InControlDriver.kind:      InControlDriver,
}


def get_driver(kind: str) -> type[DeviceDriver]:
    """Look up a driver class by kind. Raises KeyError with a listing of
    known kinds on miss, so misconfigured devices surface at boot."""
    if kind not in DRIVERS:
        known = ", ".join(sorted(DRIVERS.keys()))
        raise KeyError(
            f"unknown device kind {kind!r}. Known kinds: {known}"
        )
    return DRIVERS[kind]
