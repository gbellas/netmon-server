"""Tests for `/api/version` and `/api/integrations/incontrol`.

InControl moved back from per-device driver to top-level integration; these
tests lock that shape in so nobody accidentally reintroduces the device kind.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def api_client(monkeypatch):
    from fastapi.testclient import TestClient
    import server as server_mod
    monkeypatch.setattr(server_mod, "config", {"devices": {}})
    monkeypatch.setattr(server_mod, "_persist_config", lambda: None)
    import auth
    monkeypatch.setattr(auth, "_TOKEN", "t")
    return TestClient(server_mod.app), "t"


class TestVersion:
    def test_version_endpoint_returns_metadata(self, api_client):
        client, tok = api_client
        r = client.get("/api/version",
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        body = r.json()
        assert "version" in body and body["version"]
        assert "git_sha" in body
        assert "build_date" in body
        assert "python_version" in body


class TestIncontrolIntegration:
    def test_defaults(self, api_client):
        client, tok = api_client
        r = client.get("/api/integrations/incontrol",
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False
        assert body["org_id"] == ""
        assert body["poll_interval"] == 60
        assert body["event_limit"] == 30

    def test_put_roundtrips(self, api_client):
        client, tok = api_client
        new = {"enabled": True, "org_id": "org-42",
               "poll_interval": 120, "event_limit": 50}
        r = client.put("/api/integrations/incontrol", json=new,
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        body = r.json()
        assert body == new
        # Read-back matches.
        r2 = client.get("/api/integrations/incontrol",
                        headers={"Authorization": f"Bearer {tok}"})
        assert r2.json() == new

    def test_put_rejects_non_int(self, api_client):
        client, tok = api_client
        r = client.put("/api/integrations/incontrol",
                       json={"poll_interval": "not-a-number"},
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 400
