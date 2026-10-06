"""Per-device advisory locks.

Every state-mutating or adjudicating operation for a device is serialized on
that device's lock. SQLite ``BEGIN IMMEDIATE`` additionally serializes writers
across processes (e.g. multiple uvicorn workers), so concurrent pulls and
reports for the same device are always adjudicated atomically.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager


class DeviceLocks:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    @contextmanager
    def device(self, device_id: str):
        with self._guard:
            lock = self._locks.setdefault(device_id, threading.RLock())
        with lock:
            yield


device_locks = DeviceLocks()
