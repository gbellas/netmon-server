"""NetMon device drivers.

A *driver* is the glue between a device entry in config.yaml and the
poller tasks that run on behalf of that device. Each driver:

- Knows its `kind` string (e.g. "peplink_router", "unifi_network"); the
  registry maps those strings to driver classes.
- Validates its device config (host, credentials, optional per-kind params).
- Builds one or more `BasePoller` subclasses and registers them with the
  server's task runner.

Drivers are thin — they don't reimplement polling logic. They adapt
existing pollers in `pollers/*.py` to the new generic device-config
shape. That lets us add a new device type (e.g. OPNsense, MikroTik)
without touching any poller internals: write a new driver module, import
the appropriate poller class, done.

See `pollers.drivers.registry` for the kind → class map and
`pollers.drivers.base` for the `DeviceDriver` protocol.
"""

from .base import DeviceDriver, DeviceSpec  # noqa: F401
from .registry import DRIVERS, get_driver   # noqa: F401
