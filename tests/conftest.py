"""Shared pytest fixtures.

Keeps `sys.path` munging and stub modules in one place so individual
test files stay readable.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# Ensure the repo root is on sys.path so `import server`, `import alerts`
# etc. work from a pytest invocation rooted anywhere.
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))


class FakeState:
    """Minimal AppState stand-in for tests that don't care about history.

    Matches the subset of `models.AppState` that pollers/drivers touch:
    `get_all()`, `get()`, `update(updates)`. Returns what actually
    changed, just like the real one does.
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def get_all(self) -> dict:
        return dict(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def update(self, updates: dict) -> dict:
        changed: dict = {}
        for k, v in updates.items():
            if self._data.get(k) != v:
                self._data[k] = v
                changed[k] = v
        return changed


class FakeWS:
    """Stand-in for ws_manager. Collects broadcasts into a list so tests
    can assert what was published."""

    def __init__(self) -> None:
        self.broadcasts: list[dict] = []
        self._has_clients = True

    def has_clients(self) -> bool:
        return self._has_clients

    def is_idle(self, threshold_sec: float = 60.0) -> bool:
        return not self._has_clients

    async def broadcast(self, delta: dict) -> None:
        self.broadcasts.append(dict(delta))


@pytest.fixture
def state() -> FakeState:
    return FakeState()


@pytest.fixture
def ws() -> FakeWS:
    return FakeWS()
