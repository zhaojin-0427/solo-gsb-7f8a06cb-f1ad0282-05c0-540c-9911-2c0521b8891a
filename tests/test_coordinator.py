"""End-to-end behavioral tests for the flashing coordinator."""
from __future__ import annotations

import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor

from conftest import make_pages, sha, target_digest


P0 = b"page-zero-bytes!!"
P1 = b"\x00\x01\x02page-one\xff\xfe"
P2 = b"third-page"


def _session_body(device="dev-1", active="A", active_digest=sha(b"old-fw"), pages=None):
    return {
        "device_id": device,
        "active_partition": active,
        "active_image_sha256": active_digest,
        "pages": pages if pages is not None else make_pages(P0, P1, P2),
    }


def _create(client, **kw):
    r = client.post("/sessions", json=_session_body(**kw))
    assert r.status_code == 201, r.text
    return r.json()


def _ack_page(client, session_id, cmd, expected_digest):
    r = client.post(
        f"/sessions/{session_id}/page-reports",
        json={"command_id": cmd["id"], "page_sha256": expected_digest},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_create_session_validates_and_computes_target(client):
    pages = make_pages(P0, P1)
    body = _session_body(pages=pages)
    r = client.post("/sessions", json=body)
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["status"] == "WRITING"
    assert s["active_partition"] == "A"
    assert s["target_partition"] == "B"  # never the active partition
    assert s["target_image_sha256"] == target_digest(pages)
    assert s["page_count"] == 2


def test_invalid_base64_returns_422(client):
    pages = make_pages(P0)
    pages[0]["data_b64"] = "###not-base64###"
    r = client.post("/sessions", json=_session_body(pages=pages))
    assert r.status_code == 422


def test_page_digest_mismatch_returns_422(client):
    pages = make_pages(P0)
    pages[0]["sha256"] = sha(b"something-else")
    r = client.post("/sessions", json=_session_body(pages=pages))
    assert r.status_code == 422
    assert "digest mismatch" in r.json()["detail"]


def test_bad_page_count_returns_422(client):
    r = client.post("/sessions", json=_session_body(pages=[]))
    assert r.status_code == 422

    many = make_pages(*[b"x"] * 9)
    r = client.post("/sessions", json=_session_body(pages=many))
    assert r.status_code == 422


def test_full_success_flow_writes_only_inactive_partition(client):
    s = _create(client, pages=make_pages(P0, P1, P2))
    sid, device = s["id"], s["device_id"]
    expected = [P0, P1, P2]

    for i, payload in enumerate(expected):
        pulled = client.post(f"/devices/{device}/commands:pull").json()
        cmd = pulled["command"]
        assert pulled["redelivery"] is False
        assert cmd["type"] == "WRITE_PAGE"
        assert cmd["target_partition"] == "B"  # active A must never be written
        assert cmd["page_index"] == i
        assert base64.b64decode(cmd["data_b64"]) == payload

        ack = _ack_page(client, sid, cmd, sha(payload))
        assert ack["result"] == "ACKED"

    # All pages matched -> switch is now issued.
    sw = client.post(f"/devices/{device}/commands:pull").json()
    assert sw["command"]["type"] == "SWITCH"
    assert sw["command"]["target_partition"] == "B"

    s2 = client.get(f"/sessions/{sid}").json()
    assert s2["status"] == "AWAITING_SWITCH"
    assert s2["current_switch_command_id"] == sw["command"]["id"]

    # No further write-page command can be pulled after the switch.
    again = client.post(f"/devices/{device}/commands:pull").json()
    assert again["command"]["id"] == sw["command"]["id"]
    assert again["command"]["type"] == "SWITCH"

    # Device reboots into the target partition/image.
    r = client.post(
        f"/sessions/{sid}/reboot-reports",
        json={
            "report_id": "rep-1",
            "switch_command_id": sw["command"]["id"],
            "active_partition": "B",
            "active_image_sha256": s["target_image_sha256"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"] == "SWITCHED"
    assert body["session"]["status"] == "COMPLETED"

    history = client.get(f"/sessions/{sid}/commands").json()
    assert [c["type"] for c in history] == [
        "WRITE_PAGE", "WRITE_PAGE", "WRITE_PAGE", "SWITCH"
    ]
    assert all(c["target_partition"] == "B" for c in history)


def test_unacknowledged_write_is_redelivered_with_same_id_and_content(client):
    pages = make_pages(P0, P1)
    s = _create(client, pages=pages)
    first = client.post(f"/devices/{s['device_id']}/commands:pull").json()
    second = client.post(f"/devices/{s['device_id']}/commands:pull").json()
    assert second["redelivery"] is True
    assert second["command"]["id"] == first["command"]["id"]
    assert second["command"]["data_b64"] == first["command"]["data_b64"]
    assert second["command"]["page_index"] == 0


def test_page_digest_mismatch_enters_failed_state(client):
    s = _create(client, pages=make_pages(P0, P1))
    cmd = client.post(f"/devices/{s['device_id']}/commands:pull").json()["command"]

    r = client.post(
        f"/sessions/{s['id']}/page-reports",
        json={"command_id": cmd["id"], "page_sha256": sha(b"corrupt!")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "FAILED"
    assert body["session"]["status"] == "FAILED"

    # No more commands once failed.
    r2 = client.post(f"/devices/{s['device_id']}/commands:pull")
    assert r2.status_code == 404

    # A new session for the device is allowed after termination.
    s_new = _create(client, device=s["device_id"], pages=make_pages(P2))
    assert s_new["status"] == "WRITING"


def test_same_id_different_page_digest_is_409(client):
    s = _create(client, pages=make_pages(P0))
    cmd = client.post(f"/devices/{s['device_id']}/commands:pull").json()["command"]
    _ack_page(client, s["id"], cmd, sha(P0))
    r = client.post(
        f"/sessions/{s['id']}/page-reports",
        json={"command_id": cmd["id"], "page_sha256": sha(b"other")},
    )
    assert r.status_code == 409


def test_write_page_after_switch_is_409(client):
    s = _create(client, pages=make_pages(P0))
    cmd = client.post(f"/devices/{s['device_id']}/commands:pull").json()["command"]
    _ack_page(client, s["id"], cmd, sha(P0))
    sw = client.post(f"/devices/{s['device_id']}/commands:pull").json()["command"]

    # A late write report for the old command after switching is expired.
    r = client.post(
        f"/sessions/{s['id']}/page-reports",
        json={"command_id": cmd["id"], "page_sha256": sha(P0)},
    )
    assert r.status_code == 409

    # A page report referencing the switch command id is invalid.
    r = client.post(
        f"/sessions/{s['id']}/page-reports",
        json={"command_id": sw["id"], "page_sha256": sha(P0)},
    )
    assert r.status_code == 409


def test_rollback_creates_new_switch_attempt(client):
    old_digest = sha(b"old-fw")
    s = _create(client, active_digest=old_digest, pages=make_pages(P0))
    cmd = client.post(f"/devices/{s['device_id']}/commands:pull").json()["command"]
    _ack_page(client, s["id"], cmd, sha(P0))
    sw1 = client.post(f"/devices/{s['device_id']}/commands:pull").json()["command"]

    # Reboot report says we are still on the original partition/image.
    r = client.post(
        f"/sessions/{s['id']}/reboot-reports",
        json={
            "report_id": "rep-rollback",
            "switch_command_id": sw1["id"],
            "active_partition": "A",
            "active_image_sha256": old_digest,
        },
    )
    body = r.json()
    assert r.status_code == 200
    assert body["result"] == "ROLLED_BACK"
    assert body["new_switch_command_id"] != sw1["id"]
    assert body["session"]["status"] == "AWAITING_SWITCH"
    assert body["session"]["current_switch_command_id"] == body["new_switch_command_id"]

    # Old attempt cannot be re-adjudicated: expired instruction -> 409.
    r = client.post(
        f"/sessions/{s['id']}/reboot-reports",
        json={
            "report_id": "rep-late",
            "switch_command_id": sw1["id"],
            "active_partition": "A",
            "active_image_sha256": old_digest,
        },
    )
    assert r.status_code == 409

    # Next pull delivers the fresh switch attempt (new id, not a duplicate).
    sw2 = client.post(f"/devices/{s['device_id']}/commands:pull").json()
    assert sw2["command"]["id"] == body["new_switch_command_id"]
    assert sw2["redelivery"] is False

    # New attempt succeeds -> complete.
    r = client.post(
        f"/sessions/{s['id']}/reboot-reports",
        json={
            "report_id": "rep-success",
            "switch_command_id": sw2["command"]["id"],
            "active_partition": "B",
            "active_image_sha256": s["target_image_sha256"],
        },
    )
    assert r.json()["result"] == "SWITCHED"
    assert r.json()["session"]["status"] == "COMPLETED"


def test_unexpected_reboot_state_fails(client):
    s = _create(client, pages=make_pages(P0))
    cmd = client.post(f"/devices/{s['device_id']}/commands:pull").json()["command"]
    _ack_page(client, s["id"], cmd, sha(P0))
    sw = client.post(f"/devices/{s['device_id']}/commands:pull").json()["command"]

    r = client.post(
        f"/sessions/{s['id']}/reboot-reports",
        json={
            "report_id": "rep-weird",
            "switch_command_id": sw["id"],
            "active_partition": "A",  # original partition...
            "active_image_sha256": sha(b"mystery-image"),  # ...unknown image
        },
    )
    body = r.json()
    assert body["result"] == "FAILED"
    assert body["session"]["status"] == "FAILED"


def test_duplicate_reboot_report_is_idempotent(client):
    s = _create(client, pages=make_pages(P0))
    cmd = client.post(f"/devices/{s['device_id']}/commands:pull").json()["command"]
    _ack_page(client, s["id"], cmd, sha(P0))
    sw = client.post(f"/devices/{s['device_id']}/commands:pull").json()["command"]

    payload = {
        "report_id": "rep-dup",
        "switch_command_id": sw["id"],
        "active_partition": "B",
        "active_image_sha256": s["target_image_sha256"],
    }
    first = client.post(f"/sessions/{s['id']}/reboot-reports", json=payload).json()
    assert first["result"] == "SWITCHED"

    second = client.post(f"/sessions/{s['id']}/reboot-reports", json=payload).json()
    assert second["result"] == "DUPLICATE"
    assert second["idempotent"] is True
    assert second["session"]["status"] == "COMPLETED"

    # Same report id reused with different values -> 409, state untouched.
    payload["active_partition"] = "A"
    r = client.post(f"/sessions/{s['id']}/reboot-reports", json=payload)
    assert r.status_code == 409


def test_concurrent_open_session_creation_only_one_wins(client):
    body = _session_body(device="race-dev")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _: client.post("/sessions", json=body), range(8)
        ))
    codes = sorted(r.status_code for r in results)
    assert codes.count(201) == 1
    assert codes.count(409) == 7


def test_concurrent_pulls_and_reports_complete_coherently(client):
    s = _create(client, device="conc-dev", pages=make_pages(P0, P1, P2))
    sid, dev = s["id"], s["device_id"]
    digests = {0: sha(P0), 1: sha(P1), 2: sha(P2)}

    pulled_ids: list[str] = []

    def pull_once(_):
        r = client.post(f"/devices/{dev}/commands:pull")
        return r.json() if r.status_code == 200 else None

    # 3 pages; pullers and ackers run interleaved repeatedly.
    with ThreadPoolExecutor(max_workers=6) as pool:
        for round_no in range(6):
            pulls = [p for p in pool.map(pull_once, range(6)) if p]
            writes = [p["command"] for p in pulls
                      if p["command"]["type"] == "WRITE_PAGE"]
            pulled_ids.extend(c["id"] for c in writes)
            acks = list(pool.map(
                lambda c: client.post(
                    f"/sessions/{sid}/page-reports",
                    json={"command_id": c["id"],
                          "page_sha256": digests[c["page_index"]]},
                ),
                writes,
            ))
            assert all(r.status_code in (200, 409) for r in acks)
            if round_no >= 3:
                break

    history = client.get(f"/sessions/{sid}/commands").json()
    write_cmds = [c for c in history if c["type"] == "WRITE_PAGE"]
    assert len(write_cmds) == 3
    assert {c["page_index"] for c in write_cmds} == {0, 1, 2}
    assert all(c["status"] == "ACKED" for c in write_cmds)

    sw = client.post(f"/devices/{dev}/commands:pull").json()["command"]
    assert sw["type"] == "SWITCH"
    done = client.post(
        f"/sessions/{sid}/reboot-reports",
        json={
            "report_id": "rep-final",
            "switch_command_id": sw["id"],
            "active_partition": "B",
            "active_image_sha256": s["target_image_sha256"],
        },
    ).json()
    assert done["session"]["status"] == "COMPLETED"


def test_state_and_history_endpoints(client):
    s = _create(client, pages=make_pages(P0))
    listing = client.get("/sessions", params={"device_id": s["device_id"]}).json()
    assert any(x["id"] == s["id"] for x in listing)

    open_s = client.get(f"/devices/{s['device_id']}/open-session").json()
    assert open_s["id"] == s["id"]

    reports = client.get(f"/sessions/{s['id']}/reboot-reports").json()
    assert reports == []

    assert client.get("/sessions/unknown").status_code == 404
    assert client.post("/devices/unknown/commands:pull").status_code == 404


def test_session_survives_process_restart(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("FIRMWARE_DB_PATH", str(tmp_path / "persist.db"))

    import app.config
    import app.db
    import app.main
    import app.locks
    import app.service
    import app.routers
    import app.models

    for mod in (app.config, app.db, app.locks, app.models, app.service,
                app.routers, app.main):
        importlib.reload(mod)

    from fastapi.testclient import TestClient

    with TestClient(app.main.app) as c1:
        s = _create(c1, device="restart-dev", pages=make_pages(P0, P1))
        cmd0 = c1.post(f"/devices/{s['device_id']}/commands:pull").json()["command"]
        _ack_page(c1, s["id"], cmd0, sha(P0))
        in_flight = c1.post(f"/devices/{s['device_id']}/commands:pull").json()
        assert in_flight["command"]["page_index"] == 1
        sid = s["id"]
        in_flight_id = in_flight["command"]["id"]

    # Simulate a fresh process: reload every module (locks and pools are reset),
    # pointing at the same database file.
    for mod in (app.config, app.db, app.locks, app.models, app.service,
                app.routers, app.main):
        importlib.reload(mod)

    with TestClient(app.main.app) as c2:
        s2 = c2.get(f"/sessions/{sid}").json()
        assert s2["status"] == "WRITING"
        redelivered = c2.post(
            f"/devices/restart-dev/commands:pull"
        ).json()
        # Unacknowledged write keeps the SAME id after restart.
        assert redelivered["command"]["id"] == in_flight_id
        assert redelivered["command"]["page_index"] == 1

        _ack_page(c2, sid, redelivered["command"], sha(P1))
        sw = c2.post("/devices/restart-dev/commands:pull").json()["command"]
        assert sw["type"] == "SWITCH"
        done = c2.post(
            f"/sessions/{sid}/reboot-reports",
            json={
                "report_id": "rep-after-restart",
                "switch_command_id": sw["id"],
                "active_partition": "B",
                "active_image_sha256": s2["target_image_sha256"],
            },
        ).json()
        assert done["session"]["status"] == "COMPLETED"
