"""In-memory application state and data models."""

import time
from collections import deque
from threading import Lock


class AppState:
    """Thread-safe in-memory state store with rolling history."""

    def __init__(self, max_history: int = 120):
        self._data: dict[str, any] = {}
        self._history: dict[str, deque] = {}
        self._max_history = max_history
        self._lock = Lock()

    def get_all(self) -> dict:
        with self._lock:
            return dict(self._data)

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def update(self, updates: dict) -> dict:
        """Update state and return only the keys that actually changed."""
        changed = {}
        now = time.time()
        with self._lock:
            for key, value in updates.items():
                old = self._data.get(key)
                if old != value:
                    self._data[key] = value
                    changed[key] = value
                    # Track numeric values in history for sparklines
                    if isinstance(value, (int, float)):
                        if key not in self._history:
                            self._history[key] = deque(maxlen=self._max_history)
                        self._history[key].append({"t": now, "v": value})
        return changed

    def delete(self, *keys: str) -> None:
        """Drop one or more keys from both the data and history dicts.
        Used for ephemeral "event" keys that shouldn't stick around and
        accidentally be replayed on reconnect (e.g. `alerts.fired`)."""
        with self._lock:
            for k in keys:
                self._data.pop(k, None)
                self._history.pop(k, None)

    def get_history(self) -> dict:
        with self._lock:
            return {k: list(v) for k, v in self._history.items()}

    def get_history_for(self, key: str) -> list:
        with self._lock:
            if key in self._history:
                return list(self._history[key])
            return []
