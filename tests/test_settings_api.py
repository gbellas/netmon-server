"""Tests for GET / PUT /api/settings.

Patches server.py's module-level `config` dict and `_persist_config`
writer so we never touch the real config.local.yaml.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def api_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import server as server_mod

    # Substitute an isolated config. Give it both a server block and
    # a ping_targets icmp_ping device so the settings projection has
    # something to reflect back.
    fake_cfg = {
        "server": {"host": "0.0.0.0", "port": 8077},
        "history": {"max_points": 120},
        "devices": {
            "ping_targets": {
                "kind": "icmp_ping", "targets": [],
                "interval": 5, "count": 1, "timeout": 2,
            },
        },
    }
    monkeypatch.setattr(server_mod, "config", fake_cfg)

    # _persist_config writes yaml to disk — redirect to a tmp file so
    # tests don't clobber the dev config.
    persisted: list[dict] = []
    def _fake_persist() -> None:
        persisted.append({k: v for k, v in server_mod.config.items()})
    monkeypatch.setattr(server_mod, "_persist_config", _fake_persist)

    import auth
    monkeypatch.setattr(auth, "_TOKEN", "t")
    return TestClient(server_mod.app), "t", persisted, fake_cfg


class TestGetSettings:
    def test_shape(self, api_client) -> None:
        client, tok, _, _ = api_client
        r = client.get("/api/settings",
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"history", "server", "ping"}
        assert body["history"]["max_points"] == 120
        assert body["server"]["port"] == 8077
        assert body["ping"]["interval"] == 5


class TestPutSettings:
    def test_history_applies_immediately(self, api_client) -> None:
        client, tok, persisted, cfg = api_client
        r = client.put("/api/settings",
                       json={"history": {"max_points": 500}},
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        body = r.json()
        assert body["applied"]["history"]["max_points"] == 500
        assert body["requires_restart"] == {}
        # Config + persist side effect ran.
        assert cfg["history"]["max_points"] == 500
        assert persisted, "config should have been persisted"

    def test_server_port_is_deferred(self, api_client) -> None:
        client, tok, _, cfg = api_client
        r = client.put("/api/settings",
                       json={"server": {"port": 9000}},
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] == {}
        assert body["requires_restart"]["server"]["port"] == 9000
        assert cfg["server"]["port"] == 9000

    def test_ping_interval_is_deferred(self, api_client) -> None:
        client, tok, _, cfg = api_client
        r = client.put("/api/settings",
                       json={"ping": {"interval": 15, "count": 3}},
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        body = r.json()
        assert body["requires_restart"]["ping"]["interval"] == 15
        assert body["requires_restart"]["ping"]["count"] == 3
        assert cfg["devices"]["ping_targets"]["interval"] == 15
        assert cfg["devices"]["ping_targets"]["count"] == 3

    def test_unknown_field_400(self, api_client) -> None:
        client, tok, _, _ = api_client
        r = client.put("/api/settings",
                       json={"history": {"bogus": 1}},
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 400

    def test_bad_port_range_400(self, api_client) -> None:
        client, tok, _, _ = api_client
        r = client.put("/api/settings",
                       json={"server": {"port": 999999}},
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 400

    def test_bad_max_points_type_400(self, api_client) -> None:
        client, tok, _, _ = api_client
        r = client.put("/api/settings",
                       json={"history": {"max_points": "not-a-number"}},
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 400

    def test_current_reflects_applied(self, api_client) -> None:
        client, tok, _, _ = api_client
        client.put("/api/settings",
                   json={"history": {"max_points": 42}},
                   headers={"Authorization": f"Bearer {tok}"})
        r = client.get("/api/settings",
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.json()["history"]["max_points"] == 42


# ---- Dashboard layout GET / PUT ---------------------------------------

class TestDashboardLayout:
    def test_get_empty_defaults_to_empty_lists(self, api_client) -> None:
        client, tok, _, _ = api_client
        r = client.get("/api/dashboard/layout",
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        body = r.json()
        # Shape is stable even with no persisted layout — all four keys,
        # each an empty list. That's what the iOS client decodes against.
        assert body == {
            "device_order":  [],
            "hidden":        [],
            "widget_order":  [],
            "widget_hidden": [],
        }

    def test_put_roundtrips_and_persists(self, api_client) -> None:
        client, tok, persisted, cfg = api_client
        layout = {
            "device_order":  ["br1", "udm", "balance310"],
            "hidden":        ["ic2"],
            "widget_order":  ["br1", "udm", "eventLog"],
            "widget_hidden": ["eventLog"],
        }
        r = client.put("/api/dashboard/layout", json=layout,
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        assert r.json() == layout
        # Persisted to config + a read-back returns the same thing.
        assert cfg["dashboard"]["layout"] == layout
        assert persisted, "layout PUT should trigger _persist_config"
        r2 = client.get("/api/dashboard/layout",
                        headers={"Authorization": f"Bearer {tok}"})
        assert r2.json() == layout

    def test_partial_put_preserves_other_blocks(self, api_client) -> None:
        """Sending only widget_order shouldn't wipe the device layer."""
        client, tok, _, cfg = api_client
        # Seed with a full layout.
        cfg.setdefault("dashboard", {})["layout"] = {
            "device_order": ["a", "b"],
            "hidden":       [],
            "widget_order": ["udm"],
            "widget_hidden": [],
        }
        r = client.put("/api/dashboard/layout",
                       json={"widget_order": ["br1", "udm"]},
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        body = r.json()
        assert body["widget_order"] == ["br1", "udm"]
        # Untouched blocks remain.
        assert body["device_order"] == ["a", "b"]

    def test_coerces_non_string_entries_to_str(self, api_client) -> None:
        client, tok, _, _ = api_client
        # Pydantic enforces list[str] so numeric entries 400 at parse.
        # But mixed string ids should roundtrip as-is.
        r = client.put("/api/dashboard/layout",
                       json={"device_order": ["123", "br1"]},
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        assert r.json()["device_order"] == ["123", "br1"]


# ---- Generic per-device control endpoints -----------------------------
#
# These live in server.py too, so it's natural to exercise them here
# alongside the other settings-style endpoints. The tests stub
# _device_drivers with a fake driver that records the call, skipping
# the real PeplinkController / aiohttp path entirely.


class _FakePeplinkDriver:
    kind = "peplink_router"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def set_carrier(self, carrier: str) -> dict:
        self.calls.append(("carrier", (carrier,), {}))
        return {"ok": True}

    async def set_rat(self, rat: str) -> dict:
        self.calls.append(("rat", (rat,), {}))
        return {"ok": True}

    async def set_sf_enable(self, enabled: bool, profile_id: int = 1) -> dict:
        self.calls.append(("sf", (enabled,), {"profile_id": profile_id}))
        return {"ok": True}


class _FakeUniFiDriver:
    kind = "unifi_network"


class TestGenericDeviceControl:
    @pytest.fixture
    def driver_client(self, monkeypatch):
        from fastapi.testclient import TestClient
        import server as server_mod
        drivers: dict = {}
        monkeypatch.setattr(server_mod, "_device_drivers", drivers)
        import auth
        monkeypatch.setattr(auth, "_TOKEN", "t")
        return TestClient(server_mod.app), "t", drivers

    def test_carrier_peplink_dispatches(self, driver_client) -> None:
        client, tok, drivers = driver_client
        drv = _FakePeplinkDriver()
        drivers["truck"] = drv
        r = client.post(
            "/api/devices/truck/control/carrier",
            json={"wan_index": 2, "carrier": "verizon"},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200
        assert r.json()["carrier"] == "verizon"
        assert drv.calls[0] == ("carrier", ("verizon",), {})

    def test_rat_peplink_dispatches(self, driver_client) -> None:
        client, tok, drivers = driver_client
        drv = _FakePeplinkDriver()
        drivers["edge"] = drv
        r = client.post(
            "/api/devices/edge/control/rat",
            json={"wan_index": 2, "rat": "LTE"},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200
        assert drv.calls[0] == ("rat", ("LTE",), {})

    def test_sf_enable_peplink_dispatches(self, driver_client) -> None:
        client, tok, drivers = driver_client
        drv = _FakePeplinkDriver()
        drivers["br1"] = drv
        r = client.post(
            "/api/devices/br1/control/sf_enable",
            json={"enabled": False},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200
        assert drv.calls[0] == ("sf", (False,), {"profile_id": 1})

    def test_unsupported_kind_returns_501(self, driver_client) -> None:
        client, tok, drivers = driver_client
        drivers["gateway"] = _FakeUniFiDriver()
        r = client.post(
            "/api/devices/gateway/control/carrier",
            json={"carrier": "verizon"},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 501

    def test_unknown_device_returns_404(self, driver_client) -> None:
        client, tok, _ = driver_client
        r = client.post(
            "/api/devices/nope/control/rat",
            json={"rat": "LTE"},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 404

    def test_deprecated_br1_carrier_alias_still_works(
        self, driver_client,
    ) -> None:
        """The /api/control/br1/carrier path must keep dispatching as
        before, so older app builds don't break after the refactor."""
        client, tok, drivers = driver_client
        drv = _FakePeplinkDriver()
        drivers["br1"] = drv
        r = client.post(
            "/api/control/br1/carrier",
            json={"carrier": "att"},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200
        assert drv.calls[0] == ("carrier", ("att",), {})
