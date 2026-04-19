"""Tests for the alerts engine CRUD + the /api/alerts/rules REST surface.

Engine-level tests drive AlertsEngine directly (fast, no HTTP). The API
tests use FastAPI's TestClient with a boot-time-monkey-patched config to
avoid touching real devices.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alerts import AlertsEngine


class _FakeState:
    def __init__(self, data: dict | None = None) -> None:
        self._d = dict(data or {})

    def get_all(self) -> dict:
        return dict(self._d)

    def get(self, k, default=None):
        return self._d.get(k, default)

    def update(self, updates):
        self._d.update(updates)
        return updates


class _NoopWS:
    async def broadcast(self, delta):
        pass


@pytest.fixture
def engine(tmp_path):
    """Fresh engine pointed at a tmp config file."""
    return AlertsEngine(
        state=_FakeState(),
        ws_manager=_NoopWS(),
        config_path=tmp_path / "alerts_config.json",
    )


class TestCRUD:
    def test_list_contains_builtins(self, engine) -> None:
        view = engine.catalog_view()
        ids = [r["id"] for r in view]
        # A handful of known catalog ids must be present.
        assert "udm_high_cpu" in ids
        assert "wan1_down" in ids

    def test_create_threshold_rule_persists_and_reloads(self, engine, tmp_path) -> None:
        spec = {
            "id": "my_custom",
            "name": "My custom rule",
            "severity": "warning",
            "metric": "some.metric",
            "comparison": ">",
            "threshold": 42.0,
            "unit": "ms",
        }
        created = engine.create_rule(spec)
        assert created["id"] == "my_custom"
        assert created["custom"] is True
        # Must hit the file so a new engine loading the same path sees it.
        raw = json.loads((tmp_path / "alerts_config.json").read_text())
        assert "_custom" in raw
        assert any(r["id"] == "my_custom" for r in raw["_custom"])

        # Fresh engine from disk → rule survives.
        engine2 = AlertsEngine(
            state=_FakeState(),
            ws_manager=_NoopWS(),
            config_path=tmp_path / "alerts_config.json",
        )
        ids2 = [r["id"] for r in engine2.catalog_view()]
        assert "my_custom" in ids2

    def test_create_status_rule(self, engine) -> None:
        created = engine.create_rule({
            "id": "custom_status",
            "severity": "info",
            "metric": "some.status",
            "bad_values": ["down", "OFFLINE"],
        })
        assert created["bad_values"] == ["down", "OFFLINE"]

    def test_create_rejects_builtin_shadow(self, engine) -> None:
        with pytest.raises(ValueError, match="built-in"):
            engine.create_rule({
                "id": "udm_high_cpu",  # built-in
                "severity": "warning",
                "metric": "x",
                "comparison": ">", "threshold": 1.0,
            })

    def test_create_rejects_duplicate(self, engine) -> None:
        engine.create_rule({
            "id": "dup", "severity": "info", "metric": "x",
            "comparison": ">", "threshold": 1.0,
        })
        with pytest.raises(ValueError, match="already exists"):
            engine.create_rule({
                "id": "dup", "severity": "info", "metric": "x",
                "comparison": ">", "threshold": 2.0,
            })

    def test_create_rejects_bad_severity(self, engine) -> None:
        with pytest.raises(ValueError, match="severity"):
            engine.create_rule({
                "id": "bad_sev", "severity": "super-critical",
                "metric": "x", "comparison": ">", "threshold": 1.0,
            })

    def test_create_rejects_bad_comparison(self, engine) -> None:
        with pytest.raises(ValueError, match="comparison"):
            engine.create_rule({
                "id": "bad_cmp", "severity": "info",
                "metric": "x", "comparison": "~=", "threshold": 1.0,
            })

    def test_replace_custom_rule(self, engine) -> None:
        engine.create_rule({
            "id": "r1", "severity": "info", "metric": "x",
            "comparison": ">", "threshold": 10.0,
        })
        updated = engine.replace_rule("r1", {
            "severity": "critical", "metric": "y",
            "comparison": "<", "threshold": 5.0,
        })
        assert updated["severity"] == "critical"
        assert updated["metric"] == "y"
        assert updated["default_threshold"] == 5.0

    def test_replace_builtin_raises(self, engine) -> None:
        with pytest.raises(ValueError, match="built-in"):
            engine.replace_rule("udm_high_cpu", {
                "severity": "info", "metric": "x",
                "comparison": ">", "threshold": 1.0,
            })

    def test_delete_custom(self, engine) -> None:
        engine.create_rule({
            "id": "gone", "severity": "info", "metric": "x",
            "comparison": ">", "threshold": 1.0,
        })
        assert engine.delete_rule("gone") is True
        assert engine.rule_view("gone") is None

    def test_delete_builtin_returns_false(self, engine) -> None:
        assert engine.delete_rule("udm_high_cpu") is False


class TestHotReload:
    def test_reload_rules_picks_up_new_spec(self, engine) -> None:
        assert engine.rule_view("hotloaded") is None
        engine.create_rule({
            "id": "hotloaded", "severity": "info", "metric": "m",
            "comparison": ">", "threshold": 1.0,
        })
        # Creating already hot-reloads; confirm the rule is live.
        assert engine.rule_view("hotloaded") is not None
        # And evaluation works without a restart.
        engine.state = _FakeState({"m": 5.0})  # type: ignore[assignment]
        res = engine.test_rule("hotloaded")
        assert res["fires"] is True
        assert res["alert"]["value"] == 5.0


class TestTestRule:
    def test_fires_when_condition_true(self, engine) -> None:
        engine.state = _FakeState({"udm.cpu": 99.5})  # type: ignore[assignment]
        res = engine.test_rule("udm_high_cpu")
        assert res is not None
        assert res["fires"] is True
        assert res["alert"]["severity"] == "warning"
        # Does NOT persist state: engine._currently_firing is empty.
        assert "udm_high_cpu" not in engine._currently_firing

    def test_does_not_fire_when_condition_false(self, engine) -> None:
        engine.state = _FakeState({"udm.cpu": 10.0})  # type: ignore[assignment]
        res = engine.test_rule("udm_high_cpu")
        assert res["fires"] is False
        assert res["alert"] is None

    def test_unknown_rule_returns_none(self, engine) -> None:
        assert engine.test_rule("nope") is None


# ---- HTTP-level tests via FastAPI TestClient ---------------------------
#
# We monkey-patch server.py's module-level `_alerts` singleton to our
# engine so the HTTP handlers see a controllable engine, without
# booting any pollers.

@pytest.fixture
def api_client(engine, monkeypatch):
    from fastapi.testclient import TestClient
    import server as server_mod
    monkeypatch.setattr(server_mod, "_alerts", engine)
    # Route past the auth middleware.
    import auth
    monkeypatch.setattr(auth, "_TOKEN", "t")
    client = TestClient(server_mod.app)
    return client, "t"


class TestAlertsAPI:
    def test_list(self, api_client) -> None:
        client, tok = api_client
        r = client.get("/api/alerts/rules",
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        assert "rules" in r.json()

    def test_create_and_get(self, api_client) -> None:
        client, tok = api_client
        body = {
            "id": "api_custom", "severity": "warning",
            "metric": "x.y", "comparison": ">", "threshold": 3.0,
        }
        r = client.post("/api/alerts/rules", json=body,
                        headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, r.text
        assert r.json()["id"] == "api_custom"
        r2 = client.get("/api/alerts/rules/api_custom",
                        headers={"Authorization": f"Bearer {tok}"})
        assert r2.status_code == 200
        assert r2.json()["custom"] is True

    def test_create_validation_error_400(self, api_client) -> None:
        client, tok = api_client
        r = client.post("/api/alerts/rules", json={"id": ""},
                        headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 400

    def test_put_then_delete(self, api_client) -> None:
        client, tok = api_client
        h = {"Authorization": f"Bearer {tok}"}
        client.post("/api/alerts/rules", json={
            "id": "cr", "severity": "info", "metric": "m",
            "comparison": ">", "threshold": 1.0,
        }, headers=h)
        r = client.put("/api/alerts/rules/cr", json={
            "severity": "critical", "metric": "m2",
            "comparison": "<", "threshold": 9.0,
        }, headers=h)
        assert r.status_code == 200
        assert r.json()["severity"] == "critical"
        d = client.delete("/api/alerts/rules/cr", headers=h)
        assert d.status_code == 200
        # Gone now.
        g = client.get("/api/alerts/rules/cr", headers=h)
        assert g.status_code == 404

    def test_cannot_delete_builtin(self, api_client) -> None:
        client, tok = api_client
        r = client.delete("/api/alerts/rules/udm_high_cpu",
                          headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 400

    def test_test_rule_endpoint(self, api_client, engine) -> None:
        client, tok = api_client
        engine.state = _FakeState({"udm.cpu": 99.0})  # type: ignore[assignment]
        r = client.post("/api/alerts/rules/udm_high_cpu/test",
                        headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        assert r.json()["fires"] is True

    def test_patch_partial_enabled(self, api_client) -> None:
        client, tok = api_client
        r = client.patch(
            "/api/alerts/rules/udm_high_cpu",
            json={"enabled": False, "threshold": 80.0},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200
