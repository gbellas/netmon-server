"""Tests for the per-device `direct` mode block.

Covers the DeviceSpec.extra round-trip and the legacy top-level
direct_host/direct_port migration onto the br1 device.
"""

from __future__ import annotations

import copy


from server import _migrate_legacy_config
from pollers.drivers import DeviceSpec


class TestLegacyDirectMigration:
    def test_top_level_direct_host_port_folds_into_br1(self) -> None:
        cfg = {
            "direct_host": "10.0.0.10",
            "direct_port": 8080,
            "devices": {
                "br1": {
                    "kind": "peplink_router",
                    "host": "1.2.3.4", "username": "u", "password": "p",
                },
            },
        }
        out = _migrate_legacy_config(copy.deepcopy(cfg))
        assert "direct_host" not in out
        assert "direct_port" not in out
        direct = out["devices"]["br1"]["direct"]
        assert direct["enabled"] is True
        assert direct["host"] == "10.0.0.10"
        assert direct["port"] == 8080

    def test_no_br1_is_noop(self) -> None:
        cfg = {"direct_host": "x", "devices": {}}
        out = _migrate_legacy_config(copy.deepcopy(cfg))
        # Legacy top-level keys are popped regardless; without a br1 to
        # land them on we just drop them (no silent resurrection).
        assert "direct_host" not in out


class TestDeviceSpecDirectRoundTrip:
    def test_direct_passes_through_extra(self) -> None:
        raw = {
            "kind": "peplink_router",
            "host": "1.2.3.4", "username": "u", "password": "p",
            "direct": {
                "enabled":    True,
                "host":       "192.168.1.1",
                "port":       8080,
                "auth_mode":  "api_key",
                "auth_token": "secret",
                "timeout_ms": 1500,
            },
        }
        spec = DeviceSpec.from_config("br1", raw)
        assert spec.extra["direct"]["enabled"] is True
        assert spec.extra["direct"]["host"] == "192.168.1.1"
        assert spec.extra["direct"]["auth_mode"] == "api_key"

    def test_direct_enabled_defaulted_to_false(self) -> None:
        raw = {
            "kind": "peplink_router",
            "host": "1.2.3.4", "username": "u", "password": "p",
            "direct": {"host": "10.0.0.1"},
        }
        spec = DeviceSpec.from_config("br1", raw)
        assert spec.extra["direct"]["enabled"] is False
