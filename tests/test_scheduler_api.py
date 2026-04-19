"""Tests for scheduler CRUD + /api/scheduler/tasks REST surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scheduled_tasks import Scheduler


class _FakeState:
    def get_all(self):
        return {}
    def update(self, u):
        return u


class _NoopWS:
    async def broadcast(self, d):
        pass


def _udm_factory():
    raise RuntimeError("no UDM in tests")


@pytest.fixture
def scheduler(tmp_path):
    return Scheduler(
        state=_FakeState(), ws_manager=_NoopWS(),
        udm_controller_factory=_udm_factory,
        config_path=tmp_path / "scheduled_config.json",
    )


class TestCRUD:
    def test_defaults_created_on_first_run(self, scheduler) -> None:
        # Loader seeds wan1/wan2 defaults if absent.
        keys = {s["key"] for s in scheduler.list_schedules()}
        assert {"speedtest_wan1", "speedtest_wan2"}.issubset(keys)

    def test_create_task(self, scheduler, tmp_path) -> None:
        result = scheduler.create_task("speedtest_wan3", {
            "wan_id": 3, "enabled": True, "hour": 7, "minute": 15,
        })
        assert result["wan_id"] == 3
        # Persisted.
        on_disk = json.loads((tmp_path / "scheduled_config.json").read_text())
        assert "speedtest_wan3" in on_disk

    def test_create_rejects_duplicate(self, scheduler) -> None:
        with pytest.raises(ValueError, match="already exists"):
            scheduler.create_task("speedtest_wan1",
                                  {"wan_id": 1, "hour": 1, "minute": 0})

    def test_create_validates_hour(self, scheduler) -> None:
        with pytest.raises(ValueError, match="hour"):
            scheduler.create_task("bad", {"wan_id": 1, "hour": 99})

    def test_create_requires_wan_id(self, scheduler) -> None:
        with pytest.raises(ValueError, match="wan_id"):
            scheduler.create_task("bad", {"hour": 1})

    def test_replace_task(self, scheduler) -> None:
        result = scheduler.replace_task("speedtest_wan2", {
            "wan_id": 2, "enabled": False, "hour": 3, "minute": 30,
        })
        assert result is not None
        assert result["enabled"] is False
        assert result["hour"] == 3

    def test_replace_unknown_returns_none(self, scheduler) -> None:
        assert scheduler.replace_task("nope", {"wan_id": 1}) is None

    def test_delete(self, scheduler, tmp_path) -> None:
        assert scheduler.delete_task("speedtest_wan1") is True
        assert scheduler.get_task("speedtest_wan1") is None
        on_disk = json.loads((tmp_path / "scheduled_config.json").read_text())
        assert "speedtest_wan1" not in on_disk

    def test_delete_unknown_returns_false(self, scheduler) -> None:
        assert scheduler.delete_task("nope") is False


class TestHotReload:
    def test_create_visible_immediately_in_list(self, scheduler) -> None:
        scheduler.create_task("new_one", {"wan_id": 4, "hour": 2, "minute": 0})
        keys = {s["key"] for s in scheduler.list_schedules()}
        assert "new_one" in keys

    def test_reload_picks_up_external_edits(self, scheduler, tmp_path) -> None:
        # External writer bumps the hour on wan2.
        cfg_path = tmp_path / "scheduled_config.json"
        data = json.loads(cfg_path.read_text())
        data["speedtest_wan2"]["hour"] = 20
        cfg_path.write_text(json.dumps(data))
        scheduler.reload()
        task = scheduler.get_task("speedtest_wan2")
        assert task["hour"] == 20


# ---- HTTP ---------------------------------------------------------------

@pytest.fixture
def api_client(scheduler, monkeypatch):
    from fastapi.testclient import TestClient
    import server as server_mod
    monkeypatch.setattr(server_mod, "_scheduler", scheduler)
    import auth
    monkeypatch.setattr(auth, "_TOKEN", "t")
    return TestClient(server_mod.app), "t"


class TestSchedulerAPI:
    def test_list(self, api_client) -> None:
        client, tok = api_client
        r = client.get("/api/scheduler/tasks",
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        assert isinstance(r.json()["tasks"], list)

    def test_create(self, api_client) -> None:
        client, tok = api_client
        r = client.post("/api/scheduler/tasks", json={
            "id": "speedtest_wan5",
            "wan_id": 5, "enabled": True, "hour": 6, "minute": 0,
        }, headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, r.text
        assert r.json()["wan_id"] == 5

    def test_create_bad_body_400(self, api_client) -> None:
        client, tok = api_client
        r = client.post("/api/scheduler/tasks", json={
            "id": "x", "wan_id": 9999,
        }, headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 400

    def test_put_404_for_unknown(self, api_client) -> None:
        client, tok = api_client
        r = client.put("/api/scheduler/tasks/nope", json={
            "wan_id": 1, "hour": 1, "minute": 0,
        }, headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 404

    def test_delete_then_404(self, api_client) -> None:
        client, tok = api_client
        h = {"Authorization": f"Bearer {tok}"}
        d = client.delete("/api/scheduler/tasks/speedtest_wan1", headers=h)
        assert d.status_code == 200
        g = client.get("/api/scheduler/tasks/speedtest_wan1", headers=h)
        assert g.status_code == 404
