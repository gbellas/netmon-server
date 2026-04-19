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
