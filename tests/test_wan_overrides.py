"""Tests for per-WAN overrides:
 - legacy wan_carriers migration into wan_overrides[idx].carrier_override
 - device GET/PUT round-trips both fields
 - PUT with wan_overrides wipes the legacy wan_carriers dict
"""

from __future__ import annotations

import copy

import pytest


from server import _migrate_legacy_config


class TestWanCarriersMigration:
    def test_legacy_wan_carriers_become_wan_overrides(self) -> None:
        cfg = {
            "devices": {
                "br1": {
                    "kind": "peplink_router",
                    "host": "1.2.3.4",
                    "username": "u",
                    "password": "p",
                    "wan_carriers": {"1": "fiber", "2": "verizon"},
                },
            },
        }
        out = _migrate_legacy_config(copy.deepcopy(cfg))
        wo = out["devices"]["br1"]["wan_overrides"]
        assert wo["1"] == {"carrier_override": "fiber"}
        assert wo["2"] == {"carrier_override": "verizon"}

    def test_migration_skipped_when_wan_overrides_already_set(self) -> None:
        cfg = {
            "devices": {
                "br1": {
                    "kind": "peplink_router",
                    "host": "1.2.3.4", "username": "u", "password": "p",
                    "wan_carriers":  {"1": "fiber"},
                    "wan_overrides": {"1": {"label": "ISP-A"}},
                },
            },
        }
        out = _migrate_legacy_config(copy.deepcopy(cfg))
        # Pre-existing wan_overrides wins; legacy dict is not merged in.
        assert out["devices"]["br1"]["wan_overrides"] == {"1": {"label": "ISP-A"}}


class TestWanOverridesAPI:
    @pytest.fixture
    def api_client(self, monkeypatch):
        from fastapi.testclient import TestClient
        import server as server_mod

        cfg = {
            "devices": {
                "br1": {
                    "kind": "peplink_router",
                    "host": "1.2.3.4", "username": "u", "password": "p",
                    "wan_carriers": {"1": "fiber"},
                },
            },
        }
        monkeypatch.setattr(server_mod, "config", cfg)
        monkeypatch.setattr(server_mod, "_persist_config", lambda: None)
        monkeypatch.setattr(server_mod, "_validate_device_config",
                            lambda dev_id, raw: None)
        monkeypatch.setattr(server_mod, "_stop_driver_device", lambda dev_id: 0)
        monkeypatch.setattr(server_mod, "_start_driver_device",
                            lambda dev_id, raw: (1, ""))
        import auth
        monkeypatch.setattr(auth, "_TOKEN", "t")
        return TestClient(server_mod.app), "t", cfg

    def test_get_exposes_both_fields(self, api_client):
        client, tok, _ = api_client
        r = client.get("/api/devices/br1",
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        body = r.json()["config"]
        assert "wan_carriers" in body
        assert "wan_overrides" in body

    def test_put_wan_overrides_wipes_wan_carriers(self, api_client):
        client, tok, cfg = api_client
        r = client.put(
            "/api/devices/br1",
            json={"config": {
                "kind": "peplink_router",
                "host": "1.2.3.4", "username": "u", "password": "p",
                "wan_overrides": {
                    "1": {"label": "Home fiber", "carrier_override": "fiber"},
                    "2": {"label": "Cellular"},
                },
            }},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200, r.text
        assert "wan_carriers" not in cfg["devices"]["br1"]
        assert cfg["devices"]["br1"]["wan_overrides"]["1"]["label"] == "Home fiber"
