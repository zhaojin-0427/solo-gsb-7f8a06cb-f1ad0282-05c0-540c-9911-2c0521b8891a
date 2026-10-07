"""Per-device serialization.

Every state-changing interaction of one device (pull, page report, reboot
report, session creation) takes that device's lock, so conflicting requests
are adjudicated one at a time. SQLite ``IMMEDIATE`` transactions then extend
the guarantee to the durable state.
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_device_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()


@contextmanager
def device_lock(device_id: str) -> Iterator[None]:
    with _registry_lock:
        lock = _device_locks.setdefault(device_id, threading.Lock())
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
