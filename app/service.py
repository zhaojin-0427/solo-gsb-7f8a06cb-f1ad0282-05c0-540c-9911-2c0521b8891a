"""Core coordination logic.

All state transitions are:

* serialized per device via :func:`app.locks.device_lock`, and
* persisted inside a single SQLite ``IMMEDIATE`` transaction **before** the
  command/report response is returned.

The original (currently active) partition is never written: WRITE_PAGE
commands always target the inactive partition only.
"""
from __future__ import annotations

import base64
import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from .db import get_conn, transaction
from .locks import device_lock
from .models import CreateSessionIn, PageReportIn, RebootReportIn, decode_page_bytes

OPEN_STATUSES = ("WRITING", "AWAITING_SWITCH")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _page_dicts(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT page_index, digest, length(data) AS size FROM pages "
        "WHERE session_id = ? ORDER BY page_index",
        (session_id,),
    ).fetchall()
    return [
        {"index": r["page_index"], "sha256": r["digest"], "size": r["size"]}
        for r in rows
    ]


def _session_view(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    pages = _page_dicts(conn, row["id"])
    view = {
        "id": row["id"],
        "device_id": row["device_id"],
        "active_partition": row["active_partition"],
        "active_image_sha256": row["active_digest"],
        "target_partition": row["target_partition"],
        "target_image_sha256": row["target_digest"],
        "status": row["status"],
        "failure_reason": row["failure_reason"],
        "page_count": len(pages),
        "pages": pages,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if row["status"] == "AWAITING_SWITCH":
        sw = conn.execute(
            "SELECT id FROM commands WHERE session_id = ? AND type = 'SWITCH' "
            "AND status = 'PENDING' ORDER BY sequence DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        view["current_switch_command_id"] = sw["id"] if sw else None
    else:
        view["current_switch_command_id"] = None
    return view


def _get_session_or_404(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return row


def serialize_command(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    cmd: dict[str, Any] = {
        "id": row["id"],
        "session_id": row["session_id"],
        "device_id": row["device_id"],
        "type": row["type"],
        "target_partition": row["target_partition"],
        "page_index": row["page_index"],
        "sequence": row["sequence"],
        "status": row["status"],
        "result": row["result"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if row["type"] == "WRITE_PAGE" and row["page_index"] is not None:
        page = conn.execute(
            "SELECT data, digest FROM pages WHERE session_id = ? AND page_index = ?",
            (row["session_id"], row["page_index"]),
        ).fetchone()
        if page is not None:
            cmd["page_sha256"] = page["digest"]
            cmd["data_b64"] = base64.b64encode(page["data"]).decode("ascii")
    return cmd


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def create_session(payload: CreateSessionIn) -> dict[str, Any]:
    # ---- Input validation first (422), before any state change ------------
    decoded_pages: list[tuple[bytes, str]] = []
    for i, page in enumerate(payload.pages):
        try:
            raw = decode_page_bytes(page.data_b64)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"page {i}: {exc}"
            ) from exc
        actual = hashlib.sha256(raw).hexdigest()
        if actual != page.sha256:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"page {i} digest mismatch: claimed {page.sha256}, "
                    f"computed {actual}"
                ),
            )
        decoded_pages.append((raw, page.sha256))

    # Target image digest = SHA-256 of the page bytes concatenated in order.
    target_digest = hashlib.sha256(
        b"".join(raw for raw, _ in decoded_pages)
    ).hexdigest()

    target_partition = "B" if payload.active_partition == "A" else "A"
    session_id = _new_id()
    ts = _now()

    with device_lock(payload.device_id):
        try:
            with transaction() as conn:
                existing = conn.execute(
                    "SELECT id FROM sessions WHERE device_id = ? "
                    "AND status IN (?, ?)",
                    (payload.device_id, *OPEN_STATUSES),
                ).fetchone()
                if existing is not None:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"device {payload.device_id} already has an open "
                            f"session {existing['id']}"
                        ),
                    )
                conn.execute(
                    "INSERT INTO sessions (id, device_id, active_partition, "
                    "active_digest, target_partition, target_digest, status, "
                    "failure_reason, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'WRITING', NULL, ?, ?)",
                    (
                        session_id,
                        payload.device_id,
                        payload.active_partition,
                        payload.active_image_sha256,
                        target_partition,
                        target_digest,
                        ts,
                        ts,
                    ),
                )
                for i, (raw, digest) in enumerate(decoded_pages):
                    conn.execute(
                        "INSERT INTO pages (session_id, page_index, data, digest) "
                        "VALUES (?, ?, ?, ?)",
                        (session_id, i, raw, digest),
                    )
        except sqlite3.IntegrityError as exc:
            # Lost a race against the unique partial index uq_open_session_per_device.
            raise HTTPException(
                status_code=409,
                detail=f"device {payload.device_id} already has an open session",
            ) from exc

        with get_conn() as conn:
            row = _get_session_or_404(conn, session_id)
            return _session_view(conn, row)


def list_sessions(device_id: str | None = None) -> list[dict[str, Any]]:
    with get_conn() as conn:
        if device_id:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE device_id = ? ORDER BY created_at",
                (device_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY created_at"
            ).fetchall()
        return [_session_view(conn, r) for r in rows]


def get_session(session_id: str) -> dict[str, Any]:
    with get_conn() as conn:
        return _session_view(conn, _get_session_or_404(conn, session_id))


def get_open_session_for_device(device_id: str) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE device_id = ? AND status IN (?, ?)",
            (device_id, *OPEN_STATUSES),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"no open session for device {device_id}"
            )
        return _session_view(conn, row)


# ---------------------------------------------------------------------------
# Command pulling (device side)
# ---------------------------------------------------------------------------

def pull_command(device_id: str) -> dict[str, Any]:
    with device_lock(device_id):
        with transaction() as conn:
            session = conn.execute(
                "SELECT * FROM sessions WHERE device_id = ? AND status IN (?, ?)",
                (device_id, *OPEN_STATUSES),
            ).fetchone()
            if session is None:
                raise HTTPException(
                    status_code=404, detail=f"no open session for device {device_id}"
                )

            # Redelivery of an unacknowledged command keeps its ID and content.
            pending = conn.execute(
                "SELECT * FROM commands WHERE session_id = ? AND status = 'PENDING' "
                "ORDER BY sequence LIMIT 1",
                (session["id"],),
            ).fetchone()
            if pending is not None:
                redelivery = bool(pending["delivered"])
                if not redelivery:
                    conn.execute(
                        "UPDATE commands SET delivered = 1, updated_at = ? WHERE id = ?",
                        (_now(), pending["id"]),
                    )
                    pending = conn.execute(
                        "SELECT * FROM commands WHERE id = ?", (pending["id"],)
                    ).fetchone()
                return {"command": serialize_command(conn, pending),
                        "redelivery": redelivery}

            if session["status"] == "AWAITING_SWITCH":
                # Current switch attempt has no PENDING row only if it was
                # resolved; the session would then be terminal/reopened.
                raise HTTPException(status_code=409, detail="no pending command")

            # WRITING: lazily issue pages strictly in page order.
            issued = conn.execute(
                "SELECT COUNT(*) AS n FROM commands "
                "WHERE session_id = ? AND type = 'WRITE_PAGE'",
                (session["id"],),
            ).fetchone()["n"]
            page_count = conn.execute(
                "SELECT COUNT(*) AS n FROM pages WHERE session_id = ?",
                (session["id"],),
            ).fetchone()["n"]
            ts = _now()

            if issued < page_count:
                # Always targets the inactive partition, never the active one.
                cmd_id = _new_id()
                conn.execute(
                    "INSERT INTO commands (id, session_id, device_id, type, "
                    "target_partition, page_index, sequence, status, result, "
                    "delivered, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'WRITE_PAGE', ?, ?, ?, 'PENDING', NULL, 1, ?, ?)",
                    (
                        cmd_id,
                        session["id"],
                        device_id,
                        session["target_partition"],
                        issued,
                        issued,
                        ts,
                        ts,
                    ),
                )
            else:
                # Every page acknowledged: only now is the switch allowed.
                cmd_id = _new_id()
                conn.execute(
                    "INSERT INTO commands (id, session_id, device_id, type, "
                    "target_partition, page_index, sequence, status, result, "
                    "delivered, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'SWITCH', ?, NULL, ?, 'PENDING', NULL, 1, ?, ?)",
                    (
                        cmd_id,
                        session["id"],
                        device_id,
                        session["target_partition"],
                        issued,
                        ts,
                        ts,
                    ),
                )
                conn.execute(
                    "UPDATE sessions SET status = 'AWAITING_SWITCH', updated_at = ? "
                    "WHERE id = ?",
                    (ts, session["id"]),
                )

            row = conn.execute(
                "SELECT * FROM commands WHERE id = ?", (cmd_id,)
            ).fetchone()
            return {"command": serialize_command(conn, row), "redelivery": False}


# ---------------------------------------------------------------------------
# Page write reports
# ---------------------------------------------------------------------------

def report_page_write(session_id: str, payload: PageReportIn) -> dict[str, Any]:
    # Cheap lookup outside the device lock to find the owning device.
    with get_conn() as conn:
        cmd_row = conn.execute(
            "SELECT * FROM commands WHERE id = ?", (payload.command_id,)
        ).fetchone()
        if cmd_row is None:
            raise HTTPException(status_code=404, detail="command not found")
        owner_device = cmd_row["device_id"]

    with device_lock(owner_device):
        with transaction() as conn:
            session = _get_session_or_404(conn, session_id)
            cmd = conn.execute(
                "SELECT * FROM commands WHERE id = ?", (payload.command_id,)
            ).fetchone()

            if cmd["session_id"] != session_id:
                raise HTTPException(status_code=409, detail="command belongs to another session")
            if cmd["type"] != "WRITE_PAGE":
                raise HTTPException(status_code=409, detail="command is not a WRITE_PAGE command")
            if cmd["status"] in ("FAILED", "RESOLVED") or session["status"] != "WRITING":
                # Expired instruction: session failed/completed, or a switch
                # has already been issued -> no write reports accepted anymore.
                raise HTTPException(status_code=409, detail="command or session no longer accepts page reports")

            if cmd["status"] == "ACKED":
                # Same command may be re-reported while still writing: it is
                # idempotent for the same value, conflicting for a new one.
                page = conn.execute(
                    "SELECT digest FROM pages WHERE session_id = ? AND page_index = ?",
                    (session_id, cmd["page_index"]),
                ).fetchone()
                if page is not None and page["digest"] == payload.page_sha256:
                    return {
                        "result": "ACKED",
                        "idempotent": True,
                        "session": _session_view(conn, session),
                    }
                raise HTTPException(status_code=409, detail="same command id reported with a different digest")

            page = conn.execute(
                "SELECT digest FROM pages WHERE session_id = ? AND page_index = ?",
                (session_id, cmd["page_index"]),
            ).fetchone()
            ts = _now()

            if page is not None and page["digest"] == payload.page_sha256:
                conn.execute(
                    "UPDATE commands SET status = 'ACKED', result = 'MATCH', "
                    "updated_at = ? WHERE id = ?",
                    (ts, cmd["id"]),
                )
                conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (ts, session_id),
                )
                result = "ACKED"
            else:
                # Digest mismatch -> command and session enter the failed state.
                conn.execute(
                    "UPDATE commands SET status = 'FAILED', result = 'MISMATCH', "
                    "updated_at = ? WHERE id = ?",
                    (ts, cmd["id"]),
                )
                conn.execute(
                    "UPDATE sessions SET status = 'FAILED', "
                    "failure_reason = ?, updated_at = ? WHERE id = ?",
                    (
                        f"page {cmd['page_index']} post-write digest "
                        f"{payload.page_sha256} did not match expected "
                        f"{page['digest'] if page else '?'}",
                        ts,
                        session_id,
                    ),
                )
                result = "FAILED"

            session = _get_session_or_404(conn, session_id)
            return {
                "result": result,
                "idempotent": False,
                "session": _session_view(conn, session),
            }


# ---------------------------------------------------------------------------
# Reboot reports (switch adjudication)
# ---------------------------------------------------------------------------

def report_reboot(session_id: str, payload: RebootReportIn) -> dict[str, Any]:
    with get_conn() as conn:
        cmd_row = conn.execute(
            "SELECT * FROM commands WHERE id = ?", (payload.switch_command_id,)
        ).fetchone()
        if cmd_row is None:
            raise HTTPException(status_code=404, detail="switch command not found")
        owner_device = cmd_row["device_id"]

    with device_lock(owner_device):
        with transaction() as conn:
            session = _get_session_or_404(conn, session_id)
            cmd = conn.execute(
                "SELECT * FROM commands WHERE id = ?", (payload.switch_command_id,)
            ).fetchone()

            if cmd["session_id"] != session_id:
                raise HTTPException(status_code=409, detail="command belongs to another session")
            if cmd["type"] != "SWITCH":
                raise HTTPException(status_code=409, detail="command is not a SWITCH command")

            # Idempotency keyed by the unique reboot report id.
            existing = conn.execute(
                "SELECT * FROM reboot_reports WHERE id = ?", (payload.report_id,)
            ).fetchone()
            if existing is not None:
                same = (
                    existing["switch_command_id"] == payload.switch_command_id
                    and existing["active_partition"] == payload.active_partition
                    and existing["active_digest"] == payload.active_image_sha256
                )
                if same:
                    return {
                        "result": "DUPLICATE",
                        "idempotent": True,
                        "session": _session_view(conn, session),
                    }
                raise HTTPException(status_code=409, detail="report id already used with different values")

            if cmd["status"] != "PENDING":
                # Report for an expired (already adjudicated / superseded) attempt.
                raise HTTPException(status_code=409, detail="switch attempt is no longer pending")

            ts = _now()
            conn.execute(
                "INSERT INTO reboot_reports (id, session_id, switch_command_id, "
                "active_partition, active_digest, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    payload.report_id,
                    session_id,
                    cmd["id"],
                    payload.active_partition,
                    payload.active_image_sha256,
                    ts,
                ),
            )

            result: str
            new_switch_id: str | None = None

            if (
                payload.active_partition == session["target_partition"]
                and payload.active_image_sha256 == session["target_digest"]
            ):
                # Target partition booted with the target image: done.
                conn.execute(
                    "UPDATE commands SET status = 'RESOLVED', result = 'SWITCHED', "
                    "updated_at = ? WHERE id = ?",
                    (ts, cmd["id"]),
                )
                conn.execute(
                    "UPDATE sessions SET status = 'COMPLETED', updated_at = ? "
                    "WHERE id = ?",
                    (ts, session_id),
                )
                result = "SWITCHED"

            elif (
                payload.active_partition == session["active_partition"]
                and payload.active_image_sha256 == session["active_digest"]
            ):
                # Still on the original partition/image: rollback observed.
                # Resolve this attempt and open a fresh one; no switch is
                # re-issued with the same id.
                conn.execute(
                    "UPDATE commands SET status = 'RESOLVED', result = 'ROLLED_BACK', "
                    "updated_at = ? WHERE id = ?",
                    (ts, cmd["id"]),
                )
                new_switch_id = _new_id()
                conn.execute(
                    "INSERT INTO commands (id, session_id, device_id, type, "
                    "target_partition, page_index, sequence, status, result, "
                    "delivered, created_at, updated_at) "
                    "SELECT ?, s.id, s.device_id, 'SWITCH', s.target_partition, "
                    "NULL, c.sequence + 1, 'PENDING', NULL, 0, ?, ? "
                    "FROM commands c JOIN sessions s ON s.id = c.session_id "
                    "WHERE c.id = ?",
                    (new_switch_id, ts, ts, cmd["id"]),
                )
                conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (ts, session_id),
                )
                result = "ROLLED_BACK"

            else:
                # Booted into an unexpected partition/image: failure.
                conn.execute(
                    "UPDATE commands SET status = 'RESOLVED', result = 'FAILED', "
                    "updated_at = ? WHERE id = ?",
                    (ts, cmd["id"]),
                )
                conn.execute(
                    "UPDATE sessions SET status = 'FAILED', failure_reason = ?, "
                    "updated_at = ? WHERE id = ?",
                    (
                        "reboot report did not match either the target or the "
                        "original partition/image",
                        ts,
                        session_id,
                    ),
                )
                result = "FAILED"

            session = _get_session_or_404(conn, session_id)
            return {
                "result": result,
                "idempotent": False,
                "new_switch_command_id": new_switch_id,
                "session": _session_view(conn, session),
            }


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def list_commands(session_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        _get_session_or_404(conn, session_id)
        rows = conn.execute(
            "SELECT * FROM commands WHERE session_id = ? ORDER BY sequence, created_at",
            (session_id,),
        ).fetchall()
        return [serialize_command(conn, r) for r in rows]


def list_reboot_reports(session_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        _get_session_or_404(conn, session_id)
        rows = conn.execute(
            "SELECT id, switch_command_id, active_partition, active_digest, created_at "
            "FROM reboot_reports WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]
