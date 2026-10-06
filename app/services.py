"""Core adjudication logic: sessions, instructions and device reports.

The state machine
-----------------
::

    writing ──all pages acked──▶ switch_pending ──target boot──▶ completed
       │                             │
       │ digest mismatch             ├─ original boot (rollback) ─▶ switch_pending (new attempt)
       ▼                             │
    failed ◀─────────────────────────┘ (any other boot result)

Invariants enforced here:

* Inputs are fully validated (base64 + per-page digest + concatenated target
  digest) before any instruction is produced.
* ``write_page`` instructions only ever target the *inactive* partition, in
  page order, one unacknowledged write at a time.
* Every instruction is committed to SQLite before it is returned, so a
  re-pull returns the same instruction id and content.
* After the switch is issued the session never produces another page write,
  and while a switch result is unknown it is never re-issued (re-pulls return
  the same pending switch instruction).
* Only a reboot report bound to the *current* switch attempt adjudicates;
  equal re-submissions are idempotent, stale instructions or same-id /
  different-value reports get 409 without changing state.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException

from .db import connect, transaction
from .locking import device_locks
from .models import decode_page_bytes

OPEN_STATUSES = ("writing", "switch_pending")
OTHER_PARTITION = {"a": "b", "b": "a"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #

def _instruction_payload(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["payload"])


def _instruction_out(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    reported_digest: Optional[str] = None
    acknowledged_at: Optional[str] = None
    if row["kind"] == "write_page" and row["page_index"] is not None:
        page = conn.execute(
            "SELECT reported_digest, acknowledged_at FROM pages "
            "WHERE session_id = ? AND page_index = ?",
            (row["session_id"], row["page_index"]),
        ).fetchone()
        if page:
            reported_digest = page["reported_digest"]
            acknowledged_at = page["acknowledged_at"]
    return {
        "id": row["id"],
        "sequence": row["sequence"],
        "kind": row["kind"],
        "status": row["status"],
        "page_index": row["page_index"],
        "switch_attempt": row["switch_attempt"],
        "payload": _instruction_payload(row),
        "created_at": row["created_at"],
        "reported_digest": reported_digest,
        "acknowledged_at": acknowledged_at,
    }


def _session_out(conn: sqlite3.Connection, srow: sqlite3.Row) -> dict[str, Any]:
    pages = [
        {
            "page_index": r["page_index"],
            "expected_digest": r["expected_digest"],
            "reported_digest": r["reported_digest"],
            "acknowledged": r["acknowledged_at"] is not None,
            "instruction_id": r["instruction_id"],
        }
        for r in conn.execute(
            "SELECT page_index, expected_digest, reported_digest, acknowledged_at, instruction_id "
            "FROM pages WHERE session_id = ? ORDER BY page_index",
            (srow["id"],),
        ).fetchall()
    ]
    attempts = [
        {
            "attempt": r["switch_attempt"],
            "instruction_id": r["id"],
            "status": r["status"],
            "created_at": r["created_at"],
        }
        for r in conn.execute(
            "SELECT switch_attempt, id, status, created_at FROM instructions "
            "WHERE session_id = ? AND kind = 'switch' ORDER BY switch_attempt",
            (srow["id"],),
        ).fetchall()
    ]
    return {
        "id": srow["id"],
        "device_id": srow["device_id"],
        "status": srow["status"],
        "active_partition": srow["active_partition"],
        "inactive_partition": srow["inactive_partition"],
        "active_image_digest": srow["active_image_digest"],
        "target_image_digest": srow["target_image_digest"],
        "page_count": srow["page_count"],
        "current_instruction_id": srow["current_instruction_id"],
        "switch_attempt": srow["switch_attempt"],
        "failure_reason": srow["failure_reason"],
        "created_at": srow["created_at"],
        "updated_at": srow["updated_at"],
        "pages": pages,
        "switch_attempts": attempts,
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _get_open_session(conn: sqlite3.Connection, device_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sessions WHERE device_id = ? AND status IN (?, ?) "
        "ORDER BY created_at DESC LIMIT 1",
        (device_id, *OPEN_STATUSES),
    ).fetchone()


def _fail_session(conn: sqlite3.Connection, session_id: str, reason: str) -> None:
    conn.execute(
        "UPDATE sessions SET status = 'failed', failure_reason = ?, "
        "current_instruction_id = NULL, updated_at = ? WHERE id = ?",
        (reason, _now(), session_id),
    )
    conn.execute(
        "UPDATE instructions SET status = 'rejected' "
        "WHERE session_id = ? AND status = 'pending'",
        (session_id,),
    )


def _next_sequence(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) AS m FROM instructions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row["m"]) + 1


def _insert_instruction(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    device_id: str,
    kind: str,
    payload: dict[str, Any],
    page_index: Optional[int] = None,
    switch_attempt: Optional[int] = None,
) -> sqlite3.Row:
    inst_id = _new_id()
    seq = _next_sequence(conn, session_id)
    now = _now()
    conn.execute(
        "INSERT INTO instructions (id, session_id, device_id, sequence, kind, "
        "page_index, payload, switch_attempt, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
        (inst_id, session_id, device_id, seq, kind, page_index,
         json.dumps(payload, separators=(",", ":")), switch_attempt, now),
    )
    return conn.execute("SELECT * FROM instructions WHERE id = ?", (inst_id,)).fetchone()


def _create_switch_instruction(conn: sqlite3.Connection, srow: sqlite3.Row,
                               attempt: int) -> sqlite3.Row:
    """Persist a switch (swap) instruction for the given attempt."""
    payload = {
        "type": "switch",
        "attempt": attempt,
        "swap": {"from": srow["inactive_partition"], "to": srow["active_partition"]},
    }
    return _insert_instruction(
        conn,
        session_id=srow["id"],
        device_id=srow["device_id"],
        kind="switch",
        payload=payload,
        switch_attempt=attempt,
    )


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #

def create_session(req) -> dict[str, Any]:
    # 1) Validate encoding/per-page digests and derive the concatenated target
    #    digest *before* opening any state-changing transaction.
    chunks: list[bytes] = []
    for idx, page in enumerate(req.pages):
        try:
            blob = decode_page_bytes(page.data)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={
                "error": "invalid_page_encoding",
                "page_index": idx,
                "message": str(exc),
            })
        actual = hashlib.sha256(blob).hexdigest()
        if actual != page.sha256:
            raise HTTPException(status_code=422, detail={
                "error": "page_digest_mismatch",
                "page_index": idx,
                "submitted_sha256": page.sha256,
                "computed_sha256": actual,
            })
        chunks.append(blob)
    target_digest = hashlib.sha256(b"".join(chunks)).hexdigest()

    inactive = OTHER_PARTITION[req.active_partition]
    with device_locks.device(req.device_id):
        conn = connect()
        try:
            with transaction(conn):
                existing = _get_open_session(conn, req.device_id)
                if existing is not None:
                    raise HTTPException(status_code=409, detail={
                        "error": "open_session_exists",
                        "session_id": existing["id"],
                        "status": existing["status"],
                        "message": "device already has an unfinished session",
                    })

                session_id = _new_id()
                now = _now()
                conn.execute(
                    "INSERT INTO sessions (id, device_id, status, active_partition, "
                    "inactive_partition, active_image_digest, target_image_digest, "
                    "page_count, current_instruction_id, switch_attempt, "
                    "failure_reason, created_at, updated_at) "
                    "VALUES (?, ?, 'writing', ?, ?, ?, ?, ?, NULL, 0, NULL, ?, ?)",
                    (session_id, req.device_id, req.active_partition, inactive,
                     req.active_image_digest, target_digest, len(req.pages), now, now),
                )
                for idx, page in enumerate(req.pages):
                    conn.execute(
                        "INSERT INTO pages (session_id, page_index, data_b64, "
                        "expected_digest) VALUES (?, ?, ?, ?)",
                        (session_id, idx, page.data.strip(), page.sha256),
                    )
            srow = conn.execute("SELECT * FROM sessions WHERE id = ?",
                                (session_id,)).fetchone()
            return _session_out(conn, srow)
        finally:
            conn.close()


def pull_next_instruction(device_id: str) -> dict[str, Any]:
    with device_locks.device(device_id):
        conn = connect()
        try:
            with transaction(conn):
                session = _get_open_session(conn, device_id)
                if session is None:
                    raise HTTPException(status_code=404, detail={
                        "error": "no_open_session",
                        "message": "no unfinished session for this device",
                    })

                if session["status"] == "writing":
                    # Unacknowledged write in flight: hand back the SAME
                    # instruction (same id and content).
                    pending = conn.execute(
                        "SELECT * FROM instructions WHERE session_id = ? "
                        "AND kind = 'write_page' AND status = 'pending' "
                        "ORDER BY sequence LIMIT 1",
                        (session["id"],),
                    ).fetchone()
                    if pending is None:
                        # Issue the next not-yet-acknowledged page, in order.
                        page = conn.execute(
                            "SELECT * FROM pages WHERE session_id = ? "
                            "AND acknowledged_at IS NULL ORDER BY page_index LIMIT 1",
                            (session["id"],),
                        ).fetchone()
                        if page is None:
                            # All pages already verified (e.g. process
                            # restarted between acknowledgement and status
                            # transition): move to the first switch attempt.
                            inst = _create_switch_instruction(conn, session, attempt=1)
                            conn.execute(
                                "UPDATE sessions SET status = 'switch_pending', "
                                "switch_attempt = 1, current_instruction_id = ?, "
                                "updated_at = ? WHERE id = ?",
                                (inst["id"], _now(), session["id"]),
                            )
                            return _instruction_out(conn, inst)
                        payload = {
                            "type": "write_page",
                            "partition": session["inactive_partition"],
                            "page_index": page["page_index"],
                            "data": page["data_b64"],
                            "expected_sha256": page["expected_digest"],
                        }
                        inst = _insert_instruction(
                            conn,
                            session_id=session["id"],
                            device_id=device_id,
                            kind="write_page",
                            payload=payload,
                            page_index=page["page_index"],
                        )
                        conn.execute(
                            "UPDATE pages SET instruction_id = ? "
                            "WHERE session_id = ? AND page_index = ?",
                            (inst["id"], session["id"], page["page_index"]),
                        )
                        conn.execute(
                            "UPDATE sessions SET current_instruction_id = ?, "
                            "updated_at = ? WHERE id = ?",
                            (inst["id"], _now(), session["id"]),
                        )
                        pending = conn.execute(
                            "SELECT * FROM instructions WHERE id = ?",
                            (inst["id"],)).fetchone()
                    return _instruction_out(conn, pending)

                # switch_pending: never create a second switch while the
                # outcome of the current one is unknown; return the pending
                # switch instruction of the current attempt.
                attempt = session["switch_attempt"]
                sw = conn.execute(
                    "SELECT * FROM instructions WHERE session_id = ? AND kind = 'switch' "
                    "AND switch_attempt = ?",
                    (session["id"], attempt),
                ).fetchone()
                if sw is None or sw["status"] != "pending":
                    raise HTTPException(status_code=409, detail={
                        "error": "switch_outcome_pending_reboot",
                        "message": "switch result unknown; cannot re-issue switch",
                    })
                return _instruction_out(conn, sw)
        finally:
            conn.close()


def report_page_write(device_id: str, instruction_id: str, reported_digest: str) -> dict[str, Any]:
    with device_locks.device(device_id):
        conn = connect()
        try:
            with transaction(conn):
                inst = conn.execute(
                    "SELECT * FROM instructions WHERE id = ?", (instruction_id,),
                ).fetchone()
                if inst is None or inst["device_id"] != device_id:
                    raise HTTPException(status_code=404, detail={
                        "error": "instruction_not_found",
                    })
                if inst["kind"] != "write_page":
                    raise HTTPException(status_code=409, detail={
                        "error": "not_a_write_instruction",
                        "message": "instruction id refers to a switch instruction",
                    })

                session = conn.execute(
                    "SELECT * FROM sessions WHERE id = ?", (inst["session_id"],),
                ).fetchone()
                page = conn.execute(
                    "SELECT * FROM pages WHERE session_id = ? AND page_index = ?",
                    (session["id"], inst["page_index"]),
                ).fetchone()

                # ----- Replay / conflict handling for adjudicated reports -----
                if session["status"] == "failed" and inst["status"] == "rejected":
                    # The digest mismatch already failed the session. Repeating
                    # the SAME report is idempotent; a different value is 409.
                    if page["reported_digest"] == reported_digest:
                        return {
                            "instruction_id": inst["id"],
                            "page_index": inst["page_index"],
                            "expected_digest": page["expected_digest"],
                            "reported_digest": reported_digest,
                            "match": False,
                            "idempotent": True,
                            "session_status": "failed",
                            "failure_reason": session["failure_reason"],
                        }
                    raise HTTPException(status_code=409, detail={
                        "error": "conflicting_report",
                        "message": "instruction already failed with a different digest",
                    })

                if inst["status"] == "acknowledged":
                    if page and page["reported_digest"] == reported_digest:
                        return {
                            "instruction_id": inst["id"],
                            "page_index": inst["page_index"],
                            "expected_digest": page["expected_digest"],
                            "reported_digest": reported_digest,
                            "match": True,
                            "idempotent": True,
                            "session_status": session["status"],
                            "failure_reason": session["failure_reason"],
                        }
                    raise HTTPException(status_code=409, detail={
                        "error": "conflicting_report",
                        "message": "instruction already acknowledged with a different digest",
                    })

                # Pending instruction but the session has left the writing
                # phase: no fresh page adjudication is possible anymore.
                if session["status"] != "writing":
                    raise HTTPException(status_code=409, detail={
                        "error": "stale_instruction",
                        "session_status": session["status"],
                        "message": "no page writes accepted after switch or failure",
                    })

                match = reported_digest == page["expected_digest"]
                if not match:
                    conn.execute(
                        "UPDATE pages SET reported_digest = ? WHERE id = ?",
                        (reported_digest, page["id"]),
                    )
                    reason = (
                        f"page {page['page_index']} digest mismatch: "
                        f"expected {page['expected_digest']}, got {reported_digest}"
                    )
                    _fail_session(conn, session["id"], reason)
                    return {
                        "instruction_id": inst["id"],
                        "page_index": inst["page_index"],
                        "expected_digest": page["expected_digest"],
                        "reported_digest": reported_digest,
                        "match": False,
                        "idempotent": False,
                        "session_status": "failed",
                        "failure_reason": reason,
                    }

                # Match: acknowledge the page and its instruction.
                now = _now()
                conn.execute(
                    "UPDATE pages SET reported_digest = ?, acknowledged_at = ? "
                    "WHERE id = ?",
                    (reported_digest, now, page["id"]),
                )
                conn.execute(
                    "UPDATE instructions SET status = 'acknowledged' WHERE id = ?",
                    (inst["id"],),
                )

                remaining = conn.execute(
                    "SELECT COUNT(*) AS c FROM pages WHERE session_id = ? "
                    "AND acknowledged_at IS NULL",
                    (session["id"],),
                ).fetchone()["c"]

                if remaining == 0:
                    # All pages verified on the inactive partition: issue the
                    # first switch attempt.
                    inst_sw = _create_switch_instruction(conn, session, attempt=1)
                    conn.execute(
                        "UPDATE sessions SET status = 'switch_pending', "
                        "switch_attempt = 1, current_instruction_id = ?, "
                        "failure_reason = NULL, updated_at = ? WHERE id = ?",
                        (inst_sw["id"], now, session["id"]),
                    )
                    new_status = "switch_pending"
                else:
                    conn.execute(
                        "UPDATE sessions SET current_instruction_id = NULL, "
                        "updated_at = ? WHERE id = ?",
                        (now, session["id"]),
                    )
                    new_status = "writing"

                return {
                    "instruction_id": inst["id"],
                    "page_index": inst["page_index"],
                    "expected_digest": page["expected_digest"],
                    "reported_digest": reported_digest,
                    "match": True,
                    "idempotent": False,
                    "session_status": new_status,
                    "failure_reason": None,
                }
        finally:
            conn.close()


def report_reboot(req, device_id: str) -> dict[str, Any]:
    with device_locks.device(device_id):
        conn = connect()
        try:
            with transaction(conn):
                inst = conn.execute(
                    "SELECT * FROM instructions WHERE id = ?",
                    (req.switch_instruction_id,),
                ).fetchone()
                if inst is None or inst["device_id"] != device_id:
                    raise HTTPException(status_code=404, detail={
                        "error": "instruction_not_found",
                    })
                if inst["kind"] != "switch":
                    raise HTTPException(status_code=409, detail={
                        "error": "not_a_switch_instruction",
                    })

                session = conn.execute(
                    "SELECT * FROM sessions WHERE id = ?", (inst["session_id"],),
                ).fetchone()

                def _reboot_row_matches(r: sqlite3.Row) -> bool:
                    return (
                        r["device_id"] == device_id
                        and r["instruction_id"] == inst["id"]
                        and r["active_partition"] == req.active_partition
                        and r["active_digest"] == req.active_image_digest
                    )

                # Same report id already seen on THIS device?
                prior = conn.execute(
                    "SELECT * FROM reports WHERE device_id = ? AND id = ?",
                    (device_id, req.report_id),
                ).fetchone()
                if prior is not None:
                    if _reboot_row_matches(prior):
                        # For an idempotent rollback report, return the
                        # follow-up switch attempt that was created when the
                        # report was first adjudicated.
                        follow = conn.execute(
                            "SELECT i.id FROM instructions i WHERE i.session_id = ? "
                            "AND i.kind = 'switch' AND i.switch_attempt = "
                            "(SELECT switch_attempt FROM instructions WHERE id = ?) + 1",
                            (session["id"], inst["id"]),
                        ).fetchone()
                        new_id = follow["id"] if follow else None
                        return {
                            "report_id": prior["id"],
                            "instruction_id": inst["id"],
                            "outcome": prior["outcome"],
                            "idempotent": True,
                            "session_status": session["status"],
                            "new_switch_instruction_id": new_id,
                            "failure_reason": session["failure_reason"],
                        }
                    raise HTTPException(status_code=409, detail={
                        "error": "conflicting_report",
                        "message": "report id already used with different values",
                    })

                # This instruction already adjudicated by another report id.
                other = conn.execute(
                    "SELECT * FROM reports WHERE instruction_id = ? LIMIT 1",
                    (inst["id"],),
                ).fetchone()
                if other is not None:
                    raise HTTPException(status_code=409, detail={
                        "error": "instruction_already_reported",
                    })

                # Only the report bound to the CURRENT switch attempt may
                # adjudicate an open session.
                current = _get_open_session(conn, device_id)
                if (
                    current is None
                    or current["id"] != session["id"]
                    or session["status"] != "switch_pending"
                    or inst["status"] != "pending"
                    or inst["switch_attempt"] != session["switch_attempt"]
                ):
                    raise HTTPException(status_code=409, detail={
                        "error": "stale_instruction",
                        "message": "reboot report does not target the current switch attempt",
                    })

                now = _now()
                booted_target = (
                    req.active_partition == session["inactive_partition"]
                    and req.active_image_digest == session["target_image_digest"]
                )
                booted_original = (
                    req.active_partition == session["active_partition"]
                    and req.active_image_digest == session["active_image_digest"]
                )

                if booted_target:
                    outcome = "completed"
                    conn.execute(
                        "UPDATE instructions SET status = 'consumed' WHERE id = ?",
                        (inst["id"],),
                    )
                    conn.execute(
                        "UPDATE sessions SET status = 'completed', "
                        "current_instruction_id = NULL, failure_reason = NULL, "
                        "updated_at = ? WHERE id = ?",
                        (now, session["id"]),
                    )
                    new_switch_id: Optional[str] = None
                    new_status = "completed"
                    reason: Optional[str] = None
                elif booted_original:
                    # Rollback: device swapped back to the old firmware. Start
                    # a NEW switch attempt without rewriting any page.
                    outcome = "rollback"
                    next_attempt = session["switch_attempt"] + 1
                    conn.execute(
                        "UPDATE instructions SET status = 'consumed' WHERE id = ?",
                        (inst["id"],),
                    )
                    new_inst = _create_switch_instruction(
                        conn, session, attempt=next_attempt)
                    conn.execute(
                        "UPDATE sessions SET status = 'switch_pending', "
                        "switch_attempt = ?, current_instruction_id = ?, "
                        "updated_at = ? WHERE id = ?",
                        (next_attempt, new_inst["id"], now, session["id"]),
                    )
                    new_switch_id = new_inst["id"]
                    new_status = "switch_pending"
                    reason = None
                else:
                    outcome = "failure"
                    reason = (
                        f"unexpected boot state: partition {req.active_partition} "
                        f"digest {req.active_image_digest}"
                    )
                    conn.execute(
                        "UPDATE instructions SET status = 'rejected' WHERE id = ?",
                        (inst["id"],),
                    )
                    _fail_session(conn, session["id"], reason)
                    new_switch_id = None
                    new_status = "failed"

                conn.execute(
                    "INSERT INTO reports (id, instruction_id, session_id, device_id, "
                    "active_partition, active_digest, outcome, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (req.report_id, inst["id"], session["id"], device_id,
                     req.active_partition, req.active_image_digest, outcome, now),
                )

                return {
                    "report_id": req.report_id,
                    "instruction_id": inst["id"],
                    "outcome": outcome,
                    "idempotent": False,
                    "session_status": new_status,
                    "new_switch_instruction_id": new_switch_id,
                    "failure_reason": reason,
                }
        finally:
            conn.close()


def get_session(device_id: str, session_id: str) -> dict[str, Any]:
    with device_locks.device(device_id):
        conn = connect()
        try:
            srow = conn.execute(
                "SELECT * FROM sessions WHERE id = ? AND device_id = ?",
                (session_id, device_id),
            ).fetchone()
            if srow is None:
                raise HTTPException(status_code=404, detail={
                    "error": "session_not_found",
                })
            return _session_out(conn, srow)
        finally:
            conn.close()


def get_open_session(device_id: str) -> dict[str, Any]:
    with device_locks.device(device_id):
        conn = connect()
        try:
            srow = _get_open_session(conn, device_id)
            if srow is None:
                raise HTTPException(status_code=404, detail={
                    "error": "no_open_session",
                })
            return _session_out(conn, srow)
        finally:
            conn.close()


def list_sessions(device_id: str, limit: int) -> list[dict[str, Any]]:
    with device_locks.device(device_id):
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE device_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (device_id, limit),
            ).fetchall()
            return [_session_out(conn, r) for r in rows]
        finally:
            conn.close()


def list_instructions(device_id: str, session_id: str) -> list[dict[str, Any]]:
    with device_locks.device(device_id):
        conn = connect()
        try:
            session = conn.execute(
                "SELECT * FROM sessions WHERE id = ? AND device_id = ?",
                (session_id, device_id),
            ).fetchone()
            if session is None:
                raise HTTPException(status_code=404, detail={
                    "error": "session_not_found",
                })
            rows = conn.execute(
                "SELECT * FROM instructions WHERE session_id = ? ORDER BY sequence",
                (session_id,),
            ).fetchall()
            return [_instruction_out(conn, r) for r in rows]
        finally:
            conn.close()
