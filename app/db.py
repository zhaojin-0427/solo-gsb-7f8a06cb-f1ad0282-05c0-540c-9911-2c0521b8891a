"""SQLite access layer.

A single database file is shared by every worker. Each state-changing
service operation runs inside an ``IMMEDIATE`` transaction, which acquires a
write lock up front; together with the per-device locks in
:mod:`app.locks` this makes pull/report adjudication atomic per device.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                 TEXT PRIMARY KEY,
    device_id          TEXT NOT NULL,
    active_partition   TEXT NOT NULL CHECK (active_partition IN ('A', 'B')),
    active_digest      TEXT NOT NULL,
    target_partition   TEXT NOT NULL CHECK (target_partition IN ('A', 'B')),
    target_digest      TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN (
                           'WRITING', 'AWAITING_SWITCH', 'COMPLETED', 'FAILED')),
    failure_reason     TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_device
    ON sessions (device_id, status);

-- SQLite enforces the "one open session per device" invariant even when two
-- requests race past the application-level check.
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_session_per_device
    ON sessions (device_id)
    WHERE status IN ('WRITING', 'AWAITING_SWITCH');

CREATE TABLE IF NOT EXISTS pages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    page_index  INTEGER NOT NULL,
    data        BLOB NOT NULL,
    digest      TEXT NOT NULL,
    UNIQUE (session_id, page_index)
);

CREATE TABLE IF NOT EXISTS commands (
    id               TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL REFERENCES sessions(id),
    device_id        TEXT NOT NULL,
    type             TEXT NOT NULL CHECK (type IN ('WRITE_PAGE', 'SWITCH')),
    target_partition TEXT NOT NULL,
    page_index       INTEGER,
    sequence         INTEGER NOT NULL,
    status           TEXT NOT NULL
                         CHECK (status IN ('PENDING', 'ACKED', 'RESOLVED', 'FAILED')),
    result           TEXT CHECK (result IN (
                         'MATCH', 'MISMATCH',
                         'SWITCHED', 'ROLLED_BACK', 'FAILED')),
    delivered        INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_commands_session
    ON commands (session_id, sequence);
CREATE INDEX IF NOT EXISTS idx_commands_device_pending
    ON commands (device_id, status);

CREATE TABLE IF NOT EXISTS reboot_reports (
    id                       TEXT PRIMARY KEY,
    session_id               TEXT NOT NULL REFERENCES sessions(id),
    switch_command_id        TEXT NOT NULL REFERENCES commands(id),
    active_partition         TEXT NOT NULL
                                 CHECK (active_partition IN ('A', 'B')),
    active_digest            TEXT NOT NULL,
    created_at               TEXT NOT NULL,
    UNIQUE (id)
);

CREATE INDEX IF NOT EXISTS idx_reboot_switch
    ON reboot_reports (switch_command_id);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        DB_PATH,
        isolation_level=None,  # transactions are managed explicitly below
        check_same_thread=False,
        timeout=30.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Read/write connection; commit/rollback is the caller's responsibility
    only when an explicit transaction was started."""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Immediate write transaction, committed on success, rolled back on error."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
