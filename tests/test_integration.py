"""End-to-end integration test: boots the real server against a
minimal mock device config, exercises the HTTP API, asserts responses.

Slow-ish (~3s) so it's tagged separately; CI runs it unconditionally
but local dev can skip with `-m "not integration"`.

What's NOT exercised here:
 - Actual Peplink / UniFi polling (those need real devices or VCR-style
   recorded fixtures; TODO Phase 5d)
 - WebSocket deltas (pollers don't have a mock driver yet)
 - APNs real sends (covered by unit tests + stub sends)

What IS exercised:
 - Token bootstrap + bearer enforcement
 - /api/health shape
 - /api/devices + /api/driver-kinds  (new Phase 1 endpoints)
 - /api/config/export scrubs secrets
 - /api/config/import validates kinds
 - /api/ssh-pings/pause + resume lease semantics
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))


def _free_port() -> int:
    """Grab a free TCP port by binding to :0 and immediately closing."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextmanager
def booted_server(monkeypatch, tmp_path):
    """Start the server in a background thread bound to a free port.

    Yields (base_url, token). Tears down the thread on exit.
    """
    # Isolate everything that would otherwise touch real files / state:
    #  - config file → tmp_path/config.yaml with one legacy + one driver device
    #  - secrets dir → tmp_path/secrets (APNs can be inert)
    #  - env vars scoped via monkeypatch
    import yaml

    sample_cfg = {
        "server": {"host": "127.0.0.1", "port": _free_port()},
        "devices": {
            # Driver-based device (new shape). icmp_ping needs no auth.
            "pings": {
                "kind": "icmp_ping",
                "name": "Test Pings",
                "targets": [{"host": "127.0.0.1", "name": "loopback"}],
                "interval": 60,
            },
            # Legacy-shaped device (no kind:). Server should list it with
            # a legacy_* synthetic kind.
            "udm": {
                "name": "Fake UDM",
                "host": "127.0.0.1",
                "username": "fake",
                "password": "supersecret-redact-me",
            },
        },
        "ping": {"interval": 60, "count": 1, "timeout": 2},
        "history": {"max_points": 60},
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(sample_cfg))

    # Neutralize APNs + disable InControl cloud.
    monkeypatch.delenv("APNS_KEY_PATH", raising=False)
    monkeypatch.delenv("NETMON_INCONTROL_CLIENT_ID", raising=False)
    # Use a predictable token so we don't have to parse it from logs.
    monkeypatch.setenv("NETMON_API_TOKEN", "test-token-e2e")

    # Point server.py at our tmp config + run from tmp_path so
    # alerts_config.json / scheduled_config.json land there.
    monkeypatch.chdir(tmp_path)
    # Symlink config.local.yaml so server.py's local-preferred loader
    # picks up our test config from inside the source tree.
    (_ROOT / "config.local.yaml.bak").unlink(missing_ok=True)
    test_local = _ROOT / "config.local.yaml"
    original = None
    if test_local.exists():
        # Save + restore the operator's real config.local.yaml.
        original = test_local.read_text()
    test_local.write_text(yaml.safe_dump(sample_cfg))

    # Now we can import server.py — its module-level side effects run
    # against the env vars + config.local.yaml we just set up.
    import importlib
    import server as server_mod
    importlib.reload(server_mod)

    port = sample_cfg["server"]["port"]
    import uvicorn
    uv_config = uvicorn.Config(
        server_mod.app, host="127.0.0.1", port=port, log_level="error",
    )
    uv_server = uvicorn.Server(uv_config)

    loop = asyncio.new_event_loop()

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(uv_server.serve())

    thread = Thread(target=run, daemon=True)
    thread.start()
    # Wait for the HTTP socket to accept connections — up to 5s.
    deadline = time.time() + 5
    import http.client
    while time.time() < deadline:
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
            c.request("GET", "/api/health")
            r = c.getresponse()
            if r.status in (200, 401):
                r.read()
                break
            r.read()
        except Exception:
            pass
        time.sleep(0.05)
    else:
        raise RuntimeError("server didn't start in 5s")

    try:
        yield f"http://127.0.0.1:{port}", "test-token-e2e"
    finally:
        uv_server.should_exit = True
        thread.join(timeout=5)
        # Restore the operator's real config.local.yaml (or delete ours).
        if original is not None:
            test_local.write_text(original)
        else:
            test_local.unlink(missing_ok=True)


@pytest.fixture
def server(monkeypatch, tmp_path):
    with booted_server(monkeypatch, tmp_path) as s:
        yield s


def _get(url: str, token: str | None = None):
    import http.client, urllib.parse
    u = urllib.parse.urlparse(url)
    c = http.client.HTTPConnection(u.netloc, timeout=5)
    h = {"Authorization": f"Bearer {token}"} if token else {}
    c.request("GET", u.path + (f"?{u.query}" if u.query else ""), headers=h)
    r = c.getresponse()
    body = r.read().decode()
    return r.status, body


def _post(url: str, token: str, payload: dict):
    import http.client, urllib.parse
    u = urllib.parse.urlparse(url)
    c = http.client.HTTPConnection(u.netloc, timeout=5)
    body = json.dumps(payload)
    c.request("POST", u.path, body=body, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    r = c.getresponse()
    rb = r.read().decode()
    return r.status, rb


class TestE2E:
    def test_health_open(self, server):
        base, _ = server
        status, body = _get(f"{base}/api/health")
        assert status == 200
        doc = json.loads(body)
        assert doc["ok"] is True
        # The pollers registered from our config should show up.
        names = [p["name"] for p in doc["pollers"]]
        assert "pings" in names

    def test_devices_requires_auth(self, server):
        base, tok = server
        status, _ = _get(f"{base}/api/devices")
        assert status == 401
        status, _ = _get(f"{base}/api/devices", token=tok)
        assert status == 200

    def test_devices_lists_both_shapes(self, server):
        base, tok = server
        _, body = _get(f"{base}/api/devices", token=tok)
        doc = json.loads(body)
        ids = [d["id"] for d in doc["devices"]]
        assert set(ids) == {"pings", "udm"}
        # Post-migration the legacy `udm` entry (no kind: in the fixture)
        # is rewritten to kind=unifi_network before the list endpoint
        # runs, so both devices come back with a real driver kind.
        by_id = {d["id"]: d for d in doc["devices"]}
        assert by_id["pings"]["kind"] == "icmp_ping"
        assert by_id["udm"]["kind"] == "unifi_network"

    def test_driver_kinds_lists_registry(self, server):
        base, tok = server
        _, body = _get(f"{base}/api/driver-kinds", token=tok)
        doc = json.loads(body)
        assert set(doc["kinds"]) == {
            "peplink_router", "peplink_derived",
            "unifi_network", "icmp_ping",
        }

    def test_config_export_scrubs_passwords(self, server):
        base, tok = server
        _, body = _get(f"{base}/api/config/export", token=tok)
        doc = json.loads(body)
        # The legacy UDM config had a very-secret password.
        udm = doc["devices"]["udm"]
        assert udm["password"] == "", "password must be redacted"
        assert "supersecret-redact-me" not in body, \
            "redaction should be thorough, not just key-specific"

    def test_config_import_validates_kinds(self, server):
        base, tok = server
        # An unknown kind must be rejected with 400 + a helpful message.
        bad = {"config": {"devices": {"x": {"kind": "totally_fake"}}}}
        status, body = _post(f"{base}/api/config/import", tok, bad)
        assert status == 400
        assert "totally_fake" in body
        # A valid kind should be accepted.
        ok = {"config": {"devices": {"y": {
            "kind": "icmp_ping",
            "targets": [{"host": "1.1.1.1"}],
        }}}}
        status, body = _post(f"{base}/api/config/import", tok, ok)
        assert status == 200

    def test_device_crud_roundtrip(self, server):
        base, tok = server

        # POST a new icmp_ping device
        new_body = {
            "id": "extra",
            "config": {
                "kind": "icmp_ping",
                "name": "Extra pings",
                "targets": [{"host": "1.1.1.1", "name": "cf"}],
            },
        }
        status, body = _post(f"{base}/api/devices", tok, new_body)
        assert status == 200, body
        doc = json.loads(body)
        assert doc["id"] == "extra"
        assert doc["pollers_started"] == 1

        # GET /api/devices should now include it
        _, body = _get(f"{base}/api/devices", token=tok)
        ids = {d["id"] for d in json.loads(body)["devices"]}
        assert "extra" in ids

        # PUT with an updated config (different display name)
        updated = dict(new_body)
        updated["config"] = {**updated["config"], "name": "Renamed"}
        status, body = _post(f"{base}/api/devices/extra", tok, updated)
        # fastapi treats the extra path segment as PUT only; POST
        # to the nested path would 405. We're reusing the test helper
        # which always POSTs, so do a raw PUT via http.client below.
        import http.client, urllib.parse
        u = urllib.parse.urlparse(f"{base}/api/devices/extra")
        c = http.client.HTTPConnection(u.netloc, timeout=5)
        c.request("PUT", u.path,
                  body=json.dumps(updated),
                  headers={"Authorization": f"Bearer {tok}",
                           "Content-Type": "application/json"})
        r = c.getresponse(); rb = r.read().decode()
        assert r.status == 200, rb

        _, body = _get(f"{base}/api/devices", token=tok)
        renamed = next(d for d in json.loads(body)["devices"]
                       if d["id"] == "extra")
        assert renamed["display_name"] == "Renamed"

        # DELETE
        c = http.client.HTTPConnection(u.netloc, timeout=5)
        c.request("DELETE", u.path,
                  headers={"Authorization": f"Bearer {tok}"})
        r = c.getresponse(); rb = r.read().decode()
        assert r.status == 200, rb

        _, body = _get(f"{base}/api/devices", token=tok)
        ids = {d["id"] for d in json.loads(body)["devices"]}
        assert "extra" not in ids

    def test_device_post_rejects_bad_id(self, server):
        base, tok = server
        # Uppercase + reserved chars → 400
        for bad in ["UPPER", "with space", "-startsdash", "a" * 33, ""]:
            status, _ = _post(
                f"{base}/api/devices", tok,
                {"id": bad,
                 "config": {"kind": "icmp_ping",
                            "targets": [{"host": "1.1.1.1"}]}},
            )
            assert status == 400, f"expected 400 for id={bad!r}"

    def test_device_post_rejects_duplicate_id(self, server):
        base, tok = server
        # "pings" exists from the fixture; posting again → 409.
        status, body = _post(
            f"{base}/api/devices", tok,
            {"id": "pings",
             "config": {"kind": "icmp_ping",
                        "targets": [{"host": "1.1.1.1"}]}},
        )
        assert status == 409, body

    def test_device_put_requires_existing(self, server):
        base, tok = server
        import http.client, urllib.parse
        u = urllib.parse.urlparse(f"{base}/api/devices/nonexistent")
        c = http.client.HTTPConnection(u.netloc, timeout=5)
        c.request("PUT", u.path,
                  body=json.dumps({"config": {"kind": "icmp_ping",
                      "targets": [{"host": "1.1.1.1"}]}}),
                  headers={"Authorization": f"Bearer {tok}",
                           "Content-Type": "application/json"})
        r = c.getresponse(); r.read()
        assert r.status == 404

    def test_ssh_pause_lease(self, server):
        base, tok = server
        # Initially not paused.
        _, body = _get(f"{base}/api/ssh-pings/state", token=tok)
        assert json.loads(body)["paused"] is False
        # Request a 30s pause.
        status, _ = _post(
            f"{base}/api/ssh-pings/pause", tok,
            {"seconds": 30, "client_label": "test"},
        )
        assert status == 200
        _, body = _get(f"{base}/api/ssh-pings/state", token=tok)
        assert json.loads(body)["paused"] is True
        # Clear it.
        _post(f"{base}/api/ssh-pings/resume", tok, {})
        _, body = _get(f"{base}/api/ssh-pings/state", token=tok)
        assert json.loads(body)["paused"] is False
