"""Peplink InControl 2 cloud driver.

Wraps the `InControlPoller` in the DeviceDriver protocol so InControl 2
cloud integration lives inside the devices: map like every other poller
instead of being a special top-level config stanza.

Config shape:

    devices:
      incontrol:
        kind: incontrol
        name: "InControl 2"
        enabled: true
        org_id: "abc123"
        poll_interval: 60
        event_limit: 30

Credentials are read from environment variables:
  - NETMON_INCONTROL_CLIENT_ID
  - NETMON_INCONTROL_CLIENT_SECRET

They are NEVER stored in config.yaml — the OAuth client credentials
grant admin-ish access to the operator's Peplink organization, so
keeping them out of the config file keeps them out of config exports
/ shared backups. If either env var is unset the driver builds no
pollers and logs a warning (same behavior as the legacy path).

If `enabled` is false, `build_pollers` returns [] so the operator can
leave the device entry in place and flip it on later without editing
anything else.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .base import DeviceSpec
from ..incontrol import InControlPoller


class InControlDriver:
    kind = "incontrol"

    def __init__(self, spec: DeviceSpec) -> None:
        self.spec = spec
        # No up-front validation beyond "it's a dict". `enabled=false` is
        # fine (builds zero pollers); `enabled=true` with no org_id will
        # produce API errors at poll time which surface via /api/health.

    def build_pollers(
        self,
        *,
        state: Any,
        ws_manager: Any,
        bandwidth_meter: Any = None,
        pause_state: Any = None,
    ) -> list[Any]:
        spec = self.spec
        enabled = bool(spec.extra.get("enabled", False))
        if not enabled:
            return []

        client_id = os.environ.get("NETMON_INCONTROL_CLIENT_ID", "")
        client_secret = os.environ.get("NETMON_INCONTROL_CLIENT_SECRET", "")
        if not client_id:
            logging.getLogger(f"netmon.{spec.id}").warning(
                "incontrol driver enabled but NETMON_INCONTROL_CLIENT_ID "
                "is unset; skipping"
            )
            return []

        cfg = {
            "client_id":     client_id,
            "client_secret": client_secret,
            "org_id":        spec.extra.get("org_id", ""),
            "poll_interval": int(spec.extra.get("poll_interval", 60)),
            "event_limit":   int(spec.extra.get("event_limit", 30)),
        }
        poller = InControlPoller(
            config=cfg,
            state=state,
            ws_manager=ws_manager,
            bandwidth_meter=bandwidth_meter,
        )
        # InControlPoller hardcodes its name to "ic2"; if the operator
        # named the device something else, patch the name so state keys
        # land under their chosen id. Default deployments keep "ic2" for
        # continuity with the pre-driver shape.
        if spec.id != "ic2":
            poller.name = spec.id
            poller.logger = logging.getLogger(f"netmon.{spec.id}")
        return [poller]

    async def set_wan_enabled(self, wan_index: int, enabled: bool) -> dict:
        """InControl 2 is a cloud integration, not a router.

        Even though InControl CAN toggle WANs on managed devices, doing
        that from here would mean picking which managed device the caller
        meant — and the caller has specifically addressed the `incontrol`
        device entry, not a downstream router. Raise 501 so callers
        address the actual router entry instead.
        """
        raise NotImplementedError(
            "incontrol is a cloud integration, not a routed device. "
            "Target the specific router's device entry to toggle its WAN."
        )
