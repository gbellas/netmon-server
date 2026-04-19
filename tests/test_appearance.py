"""Tests for GET/PUT /api/settings/appearance."""

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


class TestAppearance:
    def test_defaults_expose_all_kinds(self, api_client):
        client, tok = api_client
        r = client.get("/api/settings/appearance",
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        body = r.json()
        for kind in ("peplink_router", "unifi_network", "peplink_derived",
                     "icmp_ping"):
            assert kind in body

    def test_peplink_router_defaults_match_dashboard(self, api_client):
        """Pre-existing deployments should look identical on upgrade."""
        client, tok = api_client
        r = client.get("/api/settings/appearance",
                       headers={"Authorization": f"Bearer {tok}"})
        pr = r.json()["peplink_router"]
        assert pr["metrics_visible"] == [
            "status", "uptime", "host", "wan_rows",
            "cellular", "speedfusion", "gps",
        ]
        assert pr["wan_row_metrics"] == [
            "latency", "jitter", "loss", "throughput", "signal",
        ]
        assert pr["color_thresholds"]["latency_ms"] == [100, 500]

    def test_defaults_match_authoritative_key_lists(self, api_client):
        """Each kind's metrics_order must enumerate every metric the
        matching card view emits. If you add a new metric to a card
        view, add it here too or upgraders will silently hide it."""
        client, tok = api_client
        r = client.get("/api/settings/appearance",
                       headers={"Authorization": f"Bearer {tok}"})
        body = r.json()

        authoritative = {
            "peplink_router": {
                "status", "uptime", "host", "wan_rows",
                "cellular", "speedfusion", "gps",
            },
            "unifi_network": {
                "status", "uptime", "host", "cpu", "memory",
                "client_count", "wan_rows",
            },
            "peplink_derived": {
                "status", "uptime", "host", "speedfusion",
                "bonded_throughput",
            },
            "icmp_ping": {
                "status", "latency", "jitter", "loss", "sparkline",
            },
        }
        for kind, keys in authoritative.items():
            order = body[kind]["metrics_order"]
            assert len(order) >= 5, f"{kind} order too short: {order}"
            assert set(order) == keys, f"{kind} order mismatch: {order}"
            assert set(body[kind]["metrics_visible"]) == keys

    def test_put_one_kind_preserves_others(self, api_client):
        client, tok = api_client
        r = client.put(
            "/api/settings/appearance",
            json={"peplink_router": {
                "metrics_visible": ["status"],
                "color_thresholds": {"latency_ms": [50, 200]},
            }},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["peplink_router"]["metrics_visible"] == ["status"]
        assert body["peplink_router"]["color_thresholds"]["latency_ms"] == [50, 200]
        # unifi_network defaults survived.
        assert body["unifi_network"]["metrics_visible"][0] == "status"

    def test_put_unknown_kind_400(self, api_client):
        client, tok = api_client
        r = client.put(
            "/api/settings/appearance",
            json={"bogus_kind": {"metrics_visible": []}},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 400
