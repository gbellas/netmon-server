"""Tests for /api/events filtering + saved-preset CRUD."""

from __future__ import annotations

import pytest


@pytest.fixture
def api_client(monkeypatch):
    from fastapi.testclient import TestClient
    import server as server_mod

    monkeypatch.setattr(server_mod, "config", {})
    monkeypatch.setattr(server_mod, "_persist_config", lambda: None)
    # Reset the event ring buffer so tests don't bleed into each other.
    monkeypatch.setattr(server_mod, "_event_ring", [])
    import auth
    monkeypatch.setattr(auth, "_TOKEN", "t")
    return TestClient(server_mod.app), "t", server_mod


class TestEventQueryFilters:
    def _seed(self, server_mod):
        server_mod._record_events([
            {"severity": "info",     "device_id": "br1", "rule_id": "r1",
             "title": "a", "detail": "", "ts": "2026-04-19T10:00:00Z"},
            {"severity": "warning",  "device_id": "br1", "rule_id": "r2",
             "title": "b", "detail": "", "ts": "2026-04-19T11:00:00Z"},
            {"severity": "critical", "device_id": "udm", "rule_id": "r3",
             "title": "c", "detail": "", "ts": "2026-04-19T12:00:00Z"},
        ])

    def test_filter_by_severity(self, api_client):
        client, tok, server_mod = api_client
        self._seed(server_mod)
        r = client.get("/api/events?severity=warning",
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        events = r.json()["events"]
        assert len(events) == 1
        assert events[0]["rule_id"] == "r2"

    def test_filter_by_device(self, api_client):
        client, tok, server_mod = api_client
        self._seed(server_mod)
        r = client.get("/api/events?device_id=udm",
                       headers={"Authorization": f"Bearer {tok}"})
        events = r.json()["events"]
        assert [e["rule_id"] for e in events] == ["r3"]

    def test_filter_by_since(self, api_client):
        client, tok, server_mod = api_client
        self._seed(server_mod)
        r = client.get("/api/events?since=2026-04-19T10:30:00Z",
                       headers={"Authorization": f"Bearer {tok}"})
        events = r.json()["events"]
        assert [e["rule_id"] for e in events] == ["r2", "r3"]

    def test_limit_takes_most_recent(self, api_client):
        client, tok, server_mod = api_client
        self._seed(server_mod)
        r = client.get("/api/events?limit=2",
                       headers={"Authorization": f"Bearer {tok}"})
        events = r.json()["events"]
        assert [e["rule_id"] for e in events] == ["r2", "r3"]

    def test_invalid_severity_400(self, api_client):
        client, tok, _ = api_client
        r = client.get("/api/events?severity=bogus",
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 400


class TestPresetCRUD:
    def test_create_and_list(self, api_client):
        client, tok, _ = api_client
        r = client.post("/api/events/filters",
                        json={"name": "Critical only", "severity": "critical"},
                        headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        preset = r.json()
        assert preset["id"] == "1"
        assert preset["name"] == "Critical only"
        r2 = client.get("/api/events/filters",
                        headers={"Authorization": f"Bearer {tok}"})
        assert r2.json()["presets"][0]["id"] == "1"

    def test_update(self, api_client):
        client, tok, _ = api_client
        r = client.post("/api/events/filters",
                        json={"name": "p1", "severity": "info"},
                        headers={"Authorization": f"Bearer {tok}"})
        pid = r.json()["id"]
        r2 = client.put(f"/api/events/filters/{pid}",
                        json={"name": "p1 renamed", "severity": "warning"},
                        headers={"Authorization": f"Bearer {tok}"})
        assert r2.status_code == 200
        assert r2.json()["name"] == "p1 renamed"
        assert r2.json()["severity"] == "warning"

    def test_delete(self, api_client):
        client, tok, _ = api_client
        r = client.post("/api/events/filters", json={"name": "p1"},
                        headers={"Authorization": f"Bearer {tok}"})
        pid = r.json()["id"]
        r2 = client.delete(f"/api/events/filters/{pid}",
                           headers={"Authorization": f"Bearer {tok}"})
        assert r2.status_code == 200
        r3 = client.get("/api/events/filters",
                        headers={"Authorization": f"Bearer {tok}"})
        assert r3.json()["presets"] == []

    def test_delete_unknown_404(self, api_client):
        client, tok, _ = api_client
        r = client.delete("/api/events/filters/999",
                          headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 404

    def test_create_missing_name_400(self, api_client):
        client, tok, _ = api_client
        r = client.post("/api/events/filters", json={},
                        headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 400

    def test_create_with_since_relative_minutes(self, api_client):
        client, tok, _ = api_client
        r = client.post("/api/events/filters",
                        json={"name": "last hour",
                              "since_relative_minutes": 60},
                        headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        assert r.json()["since_relative_minutes"] == 60
