"""Config-shape migration + device-edit round-trip tests.

`_migrate_legacy_config` is the load-time step that rewrites the
pre-driver config shape (legacy device names without `kind:`, top-level
`ping_targets`, top-level `incontrol`) into the generic
`devices: {id: {kind: ..., ...}}` shape. After migration every
monitored thing — ping targets, InControl cloud, UDM, BR1, Balance 310
— is a driver-backed entry in one uniform list. These tests lock that
contract down so a future refactor can't silently regress back to
parallel code paths.
"""

from __future__ import annotations

import copy

import pytest
import yaml

from server import (
    _migrate_legacy_config,
    _device_edit_view,
    _merge_preserving_secrets,
)
from pollers.drivers import DeviceSpec, get_driver


# ------ migration: legacy named devices -----------------------------------


class TestLegacyDeviceKindInference:
    def test_udm_becomes_unifi_network(self) -> None:
        cfg = {
            "devices": {
                "udm": {
                    "host": "192.168.1.1",
                    "username": "netmon",
                    "password": "x",
                    "wan_carriers": {1: "fiber"},
                },
            },
        }
        out = _migrate_legacy_config(copy.deepcopy(cfg))
        assert out["devices"]["udm"]["kind"] == "unifi_network"
        # Other fields preserved verbatim
        assert out["devices"]["udm"]["host"] == "192.168.1.1"
        assert out["devices"]["udm"]["username"] == "netmon"
        assert out["devices"]["udm"]["wan_carriers"] == {1: "fiber"}

    def test_br1_becomes_peplink_router_mobile(self) -> None:
        cfg = {
            "devices": {
                "br1": {"host": "192.168.50.1", "username": "admin"},
            },
        }
        out = _migrate_legacy_config(copy.deepcopy(cfg))
        assert out["devices"]["br1"]["kind"] == "peplink_router"
        # BR1 is always mobile (cellular) — the migration fills in
        # is_mobile so the driver enables RAT parsing.
        assert out["devices"]["br1"]["is_mobile"] is True

    def test_balance310_becomes_peplink_router_wired(self) -> None:
        cfg = {
            "devices": {
                "balance310": {"host": "192.168.2.1", "username": "admin"},
            },
        }
        out = _migrate_legacy_config(copy.deepcopy(cfg))
        assert out["devices"]["balance310"]["kind"] == "peplink_router"
        # Balance is wired — no is_mobile fill-in.
        assert out["devices"]["balance310"].get("is_mobile", False) is False

    def test_explicit_kind_untouched(self) -> None:
        # An operator who already set `kind:` shouldn't have their value
        # clobbered by the legacy-name inference.
        cfg = {
            "devices": {
                "br1": {"kind": "peplink_router", "host": "1.1.1.1",
                        "username": "admin", "is_mobile": False},
            },
        }
        out = _migrate_legacy_config(copy.deepcopy(cfg))
        # The explicit is_mobile=false must survive — don't overwrite.
        assert out["devices"]["br1"]["is_mobile"] is False

    def test_unknown_device_without_kind_left_alone(self) -> None:
        # A device with an unrecognized legacy name gets no `kind:`
        # inference. Startup will skip it (can't start a driver for a
        # kind-less entry) but the operator's data survives so they
        # can fix the config.
        cfg = {
            "devices": {
                "mystery": {"host": "10.0.0.1"},
            },
        }
        out = _migrate_legacy_config(copy.deepcopy(cfg))
        assert "kind" not in out["devices"]["mystery"]

    def test_idempotent(self) -> None:
        cfg = {
            "devices": {
                "udm": {"host": "1.1.1.1", "username": "u"},
                "br1": {"host": "2.2.2.2", "username": "u"},
            },
        }
        once = _migrate_legacy_config(copy.deepcopy(cfg))
        twice = _migrate_legacy_config(copy.deepcopy(once))
        assert once == twice


# ------ migration: top-level ping_targets ---------------------------------


class TestPingTargetsMigration:
    def test_synthesizes_icmp_ping_device(self) -> None:
        cfg = {
            "devices": {},
            "ping_targets": [
                {"name": "Gateway", "host": "192.168.1.1"},
                {"name": "Cloudflare", "host": "1.1.1.1"},
            ],
            "ping": {"count": 2, "timeout": 3, "interval": 7},
        }
        out = _migrate_legacy_config(copy.deepcopy(cfg))
        # Legacy keys popped.
        assert "ping_targets" not in out
        assert "ping" not in out
        # New synthetic device.
        d = out["devices"]["ping_targets"]
        assert d["kind"] == "icmp_ping"
        assert d["name"] == "Ping targets"
        assert d["targets"] == cfg["ping_targets"]
        assert d["count"] == 2
        assert d["timeout"] == 3
        assert d["interval"] == 7

    def test_defaults_when_ping_block_missing(self) -> None:
        cfg = {
            "devices": {},
            "ping_targets": [{"host": "1.1.1.1"}],
        }
        out = _migrate_legacy_config(copy.deepcopy(cfg))
        d = out["devices"]["ping_targets"]
        assert d["count"] == 1
        assert d["timeout"] == 2
        assert d["interval"] == 5

    def test_empty_ping_targets_not_synthesized(self) -> None:
        cfg = {"devices": {}, "ping_targets": []}
        out = _migrate_legacy_config(copy.deepcopy(cfg))
        assert "ping_targets" not in out["devices"]
        # ping_targets + ping keys still popped so startup never sees them.
        assert "ping_targets" not in out

    def test_preserves_user_authored_ping_targets_device(self) -> None:
        # If the operator already defined their own `ping_targets` device
        # entry, don't overwrite it.
        cfg = {
            "devices": {
                "ping_targets": {"kind": "icmp_ping", "name": "custom",
                                 "targets": [{"host": "9.9.9.9"}]},
            },
            "ping_targets": [{"host": "1.1.1.1"}],
        }
        out = _migrate_legacy_config(copy.deepcopy(cfg))
        assert out["devices"]["ping_targets"]["name"] == "custom"
        assert out["devices"]["ping_targets"]["targets"] == [{"host": "9.9.9.9"}]


# ------ migration: top-level incontrol ------------------------------------


class TestInControlMigration:
    def test_enabled_becomes_device(self) -> None:
        cfg = {
            "devices": {},
            "incontrol": {
                "enabled": True, "org_id": "org-abc",
                "poll_interval": 120, "event_limit": 50,
            },
        }
        out = _migrate_legacy_config(copy.deepcopy(cfg))
        assert "incontrol" not in out
        d = out["devices"]["incontrol"]
        assert d["kind"] == "incontrol"
        assert d["enabled"] is True
        assert d["org_id"] == "org-abc"
        assert d["poll_interval"] == 120
        assert d["event_limit"] == 50

    def test_disabled_not_synthesized(self) -> None:
        cfg = {"devices": {}, "incontrol": {"enabled": False, "org_id": "x"}}
        out = _migrate_legacy_config(copy.deepcopy(cfg))
        # Disabled integrations don't need a device entry — but the
        # key is still popped so startup's legacy branch never runs.
        assert "incontrol" not in out["devices"]
        assert "incontrol" not in out

    def test_missing_incontrol_block_is_noop(self) -> None:
        cfg = {"devices": {"gateway": {"kind": "unifi_network",
                                       "host": "1.1.1.1", "username": "a"}}}
        out = _migrate_legacy_config(copy.deepcopy(cfg))
        assert "incontrol" not in out["devices"]


# ------ real-config fixture (the committed example config.yaml) -----------


class TestRealConfigFixture:
    def test_every_entry_has_kind_after_migration(self) -> None:
        # Matches the shape of the real config.yaml that ships with
        # the repo. After migration every entry in devices: must have
        # a `kind:` so the startup loop can dispatch to a driver.
        import pathlib
        repo_root = pathlib.Path(__file__).parent.parent
        raw = yaml.safe_load((repo_root / "config.yaml").read_text())
        migrated = _migrate_legacy_config(raw)
        for dev_id, dev in migrated.get("devices", {}).items():
            assert "kind" in dev, f"device {dev_id!r} still missing kind after migration"
            # Kind must be one the registry knows about — otherwise
            # startup would log an error and skip the device.
            from pollers.drivers import DRIVERS
            assert dev["kind"] in DRIVERS


# ------ edit-view round-trip (GET → PUT) ----------------------------------


class TestEditViewRoundTrip:
    def test_peplink_defaults_filled_in(self) -> None:
        raw = {"kind": "peplink_router", "host": "1.1.1.1",
               "username": "admin", "password": "secret"}
        view = _device_edit_view("br1", raw)
        # Every field a driver reads is present.
        assert view["poll_interval"] == 10
        assert view["verify_ssl"] is False
        assert view["is_mobile"] is False
        assert view["wan_carriers"] == {}
        assert view["ssh"]["enabled"] is False
        assert view["ssh"]["port"] == 22
        assert view["ssh"]["count"] == 5
        # Secrets redacted.
        assert view["password"] == ""
        assert view["ssh"]["password"] == ""

    def test_icmp_ping_defaults(self) -> None:
        raw = {"kind": "icmp_ping", "targets": [{"host": "1.1.1.1"}]}
        view = _device_edit_view("lan", raw)
        assert view["count"] == 1
        assert view["timeout"] == 2
        assert view["interval"] == 5

    def test_incontrol_defaults(self) -> None:
        raw = {"kind": "incontrol", "enabled": True, "org_id": "abc"}
        view = _device_edit_view("incontrol", raw)
        assert view["poll_interval"] == 60
        assert view["event_limit"] == 30

    def test_put_empty_password_preserves_previous(self) -> None:
        previous = {"kind": "peplink_router", "host": "1.1.1.1",
                    "username": "admin", "password": "real-secret",
                    "ssh": {"enabled": True, "password": "ssh-secret"}}
        # GET returns redacted view; editor PUTs it back unchanged.
        redacted = _device_edit_view("br1", previous)
        assert redacted["password"] == ""
        merged = _merge_preserving_secrets(previous, redacted)
        assert merged["password"] == "real-secret"
        assert merged["ssh"]["password"] == "ssh-secret"

    def test_put_new_password_overrides(self) -> None:
        previous = {"kind": "peplink_router", "host": "1.1.1.1",
                    "username": "admin", "password": "old"}
        incoming = {"kind": "peplink_router", "host": "1.1.1.1",
                    "username": "admin", "password": "new"}
        merged = _merge_preserving_secrets(previous, incoming)
        assert merged["password"] == "new"

    def test_migrate_then_serialize_then_put(self) -> None:
        # Full round-trip: a pre-driver config is migrated, one
        # migrated device is serialized for the editor, the editor
        # PUTs it back (with redacted password), and the server's
        # merge-preserving-secrets step leaves the password intact.
        cfg = {
            "devices": {
                "udm": {"host": "192.168.1.1", "username": "netmon",
                        "password": "udm-pw", "wan_carriers": {1: "fiber"}},
            },
        }
        migrated = _migrate_legacy_config(copy.deepcopy(cfg))
        assert migrated["devices"]["udm"]["kind"] == "unifi_network"

        # Serialize for the editor.
        view = _device_edit_view("udm", migrated["devices"]["udm"])
        assert view["kind"] == "unifi_network"
        assert view["password"] == ""

        # PUT it back (simulated: client sends the view verbatim).
        merged = _merge_preserving_secrets(migrated["devices"]["udm"], view)
        assert merged["kind"] == "unifi_network"
        assert merged["password"] == "udm-pw"
        # And it dumps back to valid YAML.
        dumped = yaml.safe_dump({"devices": {"udm": merged}})
        reloaded = yaml.safe_load(dumped)
        assert reloaded["devices"]["udm"]["kind"] == "unifi_network"
        assert reloaded["devices"]["udm"]["password"] == "udm-pw"
        # And the DeviceSpec parse succeeds (driver would start cleanly).
        spec = DeviceSpec.from_config("udm", merged)
        get_driver(spec.kind)(spec)


# ------ `Any` import sanity ------------------------------------------------


def test_migration_returns_input_when_not_a_dict() -> None:
    # Guard rail for a malformed YAML load. Shouldn't explode.
    assert _migrate_legacy_config("not-a-dict") == "not-a-dict"  # type: ignore[arg-type]
    assert _migrate_legacy_config(None) is None  # type: ignore[arg-type]
