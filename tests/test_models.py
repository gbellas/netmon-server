"""AppState tests — the in-memory store + rolling history."""

from __future__ import annotations

from models import AppState


class TestAppStateUpdate:
    def test_update_returns_only_changed_keys(self) -> None:
        s = AppState()
        first = s.update({"a": 1, "b": 2})
        assert first == {"a": 1, "b": 2}
        # Re-submit the same values — nothing changed.
        again = s.update({"a": 1, "b": 2})
        assert again == {}
        # Change one, leave the other.
        partial = s.update({"a": 1, "b": 3})
        assert partial == {"b": 3}

    def test_update_none_vs_missing(self) -> None:
        # Storing None is allowed and considered a change (e.g. clearing
        # a WAN's latency to indicate "not measured").
        s = AppState()
        s.update({"k": 1})
        delta = s.update({"k": None})
        assert delta == {"k": None}
        # Re-setting to same None should be a no-op.
        assert s.update({"k": None}) == {}

    def test_get_all_returns_copy(self) -> None:
        # get_all() must not expose the internal dict by reference —
        # callers that mutate their result shouldn't corrupt state.
        s = AppState()
        s.update({"a": 1})
        snapshot = s.get_all()
        snapshot["a"] = 999
        assert s.get("a") == 1


class TestAppStateHistory:
    def test_numeric_values_tracked(self) -> None:
        s = AppState(max_history=5)
        for i in range(10):
            s.update({"latency_ms": float(i)})
        hist = s.get_history_for("latency_ms")
        # Deque max length is 5 — expect most recent 5 entries.
        assert len(hist) == 5
        values = [h["v"] for h in hist]
        assert values == [5.0, 6.0, 7.0, 8.0, 9.0]
        # Each entry has a timestamp.
        assert all("t" in h for h in hist)

    def test_non_numeric_not_tracked(self) -> None:
        # Only int/float values get sparkline history. String/status
        # values would clutter the buffer without being plottable.
        s = AppState()
        s.update({"status": "online"})
        s.update({"status": "offline"})
        assert s.get_history_for("status") == []

    def test_get_history_returns_copy(self) -> None:
        s = AppState()
        s.update({"v": 1.0})
        h1 = s.get_history()
        h1["v"] = []
        # Original history untouched.
        assert len(s.get_history()["v"]) == 1


class TestAppStateDelete:
    def test_delete_removes_data_and_history(self) -> None:
        s = AppState()
        s.update({"ephemeral": 1.0, "keep": "x"})
        s.delete("ephemeral")
        all_data = s.get_all()
        assert "ephemeral" not in all_data
        assert all_data.get("keep") == "x"
        # History for the deleted key is also cleared — prevents stale
        # sparkline data from being replayed to a reconnecting client.
        assert s.get_history_for("ephemeral") == []

    def test_delete_nonexistent_is_noop(self) -> None:
        s = AppState()
        s.update({"a": 1})
        s.delete("never_set")        # must not raise
        assert s.get("a") == 1

    def test_delete_multiple(self) -> None:
        s = AppState()
        s.update({"a": 1, "b": 2, "c": 3})
        s.delete("a", "b")
        assert set(s.get_all().keys()) == {"c"}
