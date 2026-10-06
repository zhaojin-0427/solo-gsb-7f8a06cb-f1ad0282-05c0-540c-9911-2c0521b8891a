"""SQLite schema and connection helpers."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                      TEXT PRIMARY KEY,
    device_id               TEXT NOT NULL,
    status                  TEXT NOT NULL,  -- writing | switch_pending | completed | failed
    active_partition        TEXT NOT NULL,  -- partition active when the session was created
    inactive_partition      TEXT NOT NULL,  -- the ONLY partition we ever write
    active_image_digest     TEXT NOT NULL,
    target_image_digest     TEXT NOT NULL,
    page_count              INTEGER NOT NULL,
    current_instruction_id  TEXT,
    switch_attempt          INTEGER NOT NULL DEFAULT 0,
    failure_reason          TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_device_created
    ON sessions(device_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_open_per_device
    ON sessions(device_id) WHERE status IN ('writing', 'switch_pending');

CREATE TABLE IF NOT EXISTS pages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT NOT NULL REFERENCES sessions(id),
    page_index          INTEGER NOT NULL,
    data_b64            TEXT NOT NULL,
    expected_digest     TEXT NOT NULL,
    reported_digest     TEXT,
    instruction_id      TEXT,
    acknowledged_at     TEXT,
    UNIQUE(session_id, page_index)
);

CREATE TABLE IF NOT EXISTS instructions (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    device_id       TEXT NOT NULL,
    sequence        INTEGER NOT NULL,
    kind            TEXT NOT NULL,        -- write_page | switch
    page_index      INTEGER,
    payload         TEXT NOT NULL,        -- JSON instruction sent to the device
    switch_attempt  INTEGER,              -- set for switch instructions
    status          TEXT NOT NULL,        -- pending | acknowledged | rejected | consumed
    created_at      TEXT NOT NULL,
    UNIQUE(session_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_instructions_session ON instructions(session_id, sequence);

CREATE TABLE IF NOT EXISTS reports (
    id                  TEXT NOT NULL,          -- device-provided unique report id
    device_id           TEXT NOT NULL,          -- uniqueness is scoped per device
    instruction_id      TEXT NOT NULL REFERENCES instructions(id),
    session_id          TEXT NOT NULL REFERENCES sessions(id),
    active_partition    TEXT NOT NULL,
    active_digest       TEXT NOT NULL,
    outcome             TEXT NOT NULL,           -- completed | rollback | failure
    created_at          TEXT NOT NULL,
    PRIMARY KEY (device_id, id)
);
CREATE INDEX IF NOT EXISTS idx_reports_instruction ON reports(instruction_id);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        # WAL + busy_timeout give us reader/writer concurrency with clean
        # serialization under BEGIN IMMEDIATE.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A write transaction that grabs the write lock immediately.

    Combined with the per-device threading lock this guarantees that
    concurrent pull/report calls for one device cannot interleave their
    read-modify-write adjudication, even across processes.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
