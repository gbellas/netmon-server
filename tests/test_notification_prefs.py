"""Tests for per-device-token notification prefs + _should_notify filter."""

from __future__ import annotations

import pytest


@pytest.fixture
def api_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import server as server_mod

    # Redirect the prefs file to a throwaway path so the real
    # secrets/push_token_prefs.json isn't touched.
    monkeypatch.setattr(server_mod, "_PREFS_PATH",
                        tmp_path / "push_token_prefs.json")
    import auth
    monkeypatch.setattr(auth, "_TOKEN", "t")
    return TestClient(server_mod.app), "t", server_mod


class TestTokenPrefsCRUD:
    def test_get_defaults(self, api_client):
        client, tok, _ = api_client
        r = client.get("/api/push/tokens/abc/prefs",
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        body = r.json()
        assert body["rules"] == {}
        assert body["per_device"] == {}
        assert body["quiet_hours"]["enabled"] is False

    def test_put_roundtrip(self, api_client):
        client, tok, _ = api_client
        payload = {
            "rules": {"ping_high_latency": False, "wan_down": True},
            "per_device": {"br1": False},
            "quiet_hours": {"enabled": True, "start": "22:00", "end": "06:30"},
        }
        r = client.put("/api/push/tokens/abc/prefs", json=payload,
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        r2 = client.get("/api/push/tokens/abc/prefs",
                        headers={"Authorization": f"Bearer {tok}"})
        body = r2.json()
        assert body["rules"]["ping_high_latency"] is False
        assert body["per_device"]["br1"] is False
        assert body["quiet_hours"]["enabled"] is True


class TestShouldNotifyFilter:
    def test_rule_mute_blocks(self, api_client):
        _, _, server_mod = api_client
        # Seed prefs for a token.
        server_mod._save_notif_prefs({"tok": {
            "rules": {"ping_high_latency": False},
        }})
        assert server_mod._should_notify(
            "tok", {"rule_id": "ping_high_latency", "severity": "warning"},
        ) is False

    def test_rule_not_listed_passes(self, api_client):
        _, _, server_mod = api_client
        server_mod._save_notif_prefs({"tok": {"rules": {}}})
        assert server_mod._should_notify(
            "tok", {"rule_id": "other", "severity": "warning"},
        ) is True

    def test_per_device_mute_blocks(self, api_client):
        _, _, server_mod = api_client
        server_mod._save_notif_prefs({"tok": {"per_device": {"br1": False}}})
        assert server_mod._should_notify(
            "tok", {"rule_id": "x", "device_id": "br1", "severity": "warning"},
        ) is False

    def test_quiet_hours_suppresses_non_critical(self, api_client):
        _, _, server_mod = api_client
        # Quiet hours that definitely include "now" (00:00..23:59).
        server_mod._save_notif_prefs({"tok": {
            "quiet_hours": {"enabled": True, "start": "00:00", "end": "23:59"},
        }})
        assert server_mod._should_notify(
            "tok", {"rule_id": "x", "severity": "warning"},
        ) is False

    def test_quiet_hours_bypassed_by_critical(self, api_client):
        _, _, server_mod = api_client
        server_mod._save_notif_prefs({"tok": {
            "quiet_hours": {"enabled": True, "start": "00:00", "end": "23:59"},
        }})
        assert server_mod._should_notify(
            "tok", {"rule_id": "x", "severity": "critical"},
        ) is True


class TestQuietHoursWrap:
    def test_wrap_midnight(self, api_client):
        _, _, server_mod = api_client
        qh = {"enabled": True, "start": "22:00", "end": "07:00"}
        assert server_mod._in_quiet_hours(qh, 23 * 60) is True   # 23:00
        assert server_mod._in_quiet_hours(qh, 3 * 60) is True    # 03:00
        assert server_mod._in_quiet_hours(qh, 12 * 60) is False  # noon

    def test_same_day_window(self, api_client):
        _, _, server_mod = api_client
        qh = {"enabled": True, "start": "09:00", "end": "17:00"}
        assert server_mod._in_quiet_hours(qh, 12 * 60) is True
        assert server_mod._in_quiet_hours(qh, 20 * 60) is False
