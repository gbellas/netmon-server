"""SSH pause-lease tests.

The pause state is what the iPhone sets when it takes over BR1 SSH
pinging locally (on-LAN). It's a lease that expires naturally so a
missed heartbeat doesn't silence the server forever.
"""

from __future__ import annotations

import time

import pytest

from ssh_pause import SshPauseState


def test_initial_state_not_paused() -> None:
    p = SshPauseState()
    assert not p.is_paused()
    assert p.seconds_remaining() == 0.0
    snap = p.snapshot()
    assert snap["paused"] is False
    assert snap["paused_until"] is None
    assert snap["seconds_remaining"] == 0.0


def test_request_pause_sets_until() -> None:
    p = SshPauseState()
    until = p.request_pause(60.0, client_label="iphone")
    assert p.is_paused()
    assert abs(until - (time.time() + 60.0)) < 1.0
    snap = p.snapshot()
    assert snap["paused"] is True
    assert snap["paused_by"] == "iphone"


def test_max_lease_enforced() -> None:
    p = SshPauseState()
    # Client asks for an hour — server caps at MAX_LEASE_SECONDS.
    p.request_pause(3600.0)
    remaining = p.seconds_remaining()
    assert remaining <= SshPauseState.MAX_LEASE_SECONDS
    # Also can't be negative if they ask for -1.
    p.request_pause(-1.0)
    assert p.seconds_remaining() == 0.0
    assert not p.is_paused()


def test_clear_stops_pause() -> None:
    p = SshPauseState()
    p.request_pause(60.0)
    assert p.is_paused()
    p.clear()
    assert not p.is_paused()
    assert p.snapshot()["paused"] is False


def test_heartbeat_extends_lease() -> None:
    p = SshPauseState()
    p.request_pause(30.0, client_label="iphone")
    first = p.snapshot()["paused_until"]
    assert first is not None
    time.sleep(0.05)
    p.request_pause(30.0, client_label="iphone")
    second = p.snapshot()["paused_until"]
    # Heartbeat resets the 30s window rather than stacking — we always
    # take a full 30s from NOW, so the second should be strictly later.
    assert second > first


def test_snapshot_shape() -> None:
    p = SshPauseState()
    snap = p.snapshot()
    # The API clients (iPhone, web UI, watchdog) rely on these keys.
    # Lock them down so accidental renames break this test instead of
    # silently breaking consumers.
    assert set(snap.keys()) >= {
        "paused", "paused_until", "seconds_remaining",
        "paused_by", "last_request_at",
    }
