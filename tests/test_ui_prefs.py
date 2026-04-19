"""Tests for GET/PUT /api/settings/ui."""

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


class TestUIPrefs:
    def test_defaults(self, api_client):
        client, tok = api_client
        r = client.get("/api/settings/ui",
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        body = r.json()
        assert body["theme"] == "auto"
        assert body["units"]["throughput"] == "auto"
        assert body["sparkline"]["window_points"] == 60
        assert body["sparkline"]["height"] == 60
        assert body["alert_banner"]["dismissed_ids"] == []
        assert body["dashboard_refresh"]["format"] == "relative"

    def test_put_roundtrip(self, api_client):
        client, tok = api_client
        payload = {
            "theme": "dark",
            "units": {"throughput": "Mbps", "latency": "s",
                      "bandwidth_prefix": "binary"},
            "timestamp_format": "absolute",
            "sparkline": {"visible": False, "window_points": 120, "height": 90},
            "alert_banner": {"dismissed_ids": ["a", "b"], "dismissible": False},
            "dashboard_refresh": {"show_indicator": False, "format": "clock"},
        }
        r = client.put("/api/settings/ui", json=payload,
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        r2 = client.get("/api/settings/ui",
                        headers={"Authorization": f"Bearer {tok}"})
        body = r2.json()
        assert body["theme"] == "dark"
        assert body["sparkline"]["window_points"] == 120
        assert body["alert_banner"]["dismissed_ids"] == ["a", "b"]

    def test_partial_put_fills_defaults(self, api_client):
        client, tok = api_client
        r = client.put("/api/settings/ui", json={"theme": "light"},
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        body = r.json()
        assert body["theme"] == "light"
        # Everything else came from defaults.
        assert body["sparkline"]["window_points"] == 60

    def test_invalid_theme_400(self, api_client):
        client, tok = api_client
        r = client.put("/api/settings/ui", json={"theme": "neon"},
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 400

    def test_sparkline_window_points_out_of_range_400(self, api_client):
        client, tok = api_client
        r = client.put("/api/settings/ui",
                       json={"sparkline": {"window_points": 10, "height": 60}},
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 400

    def test_ui_prefs_ride_along_in_state(self, api_client):
        client, tok = api_client
        r = client.get("/api/state",
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        body = r.json()
        assert "ui_prefs" in body
        assert body["ui_prefs"]["theme"] == "auto"
