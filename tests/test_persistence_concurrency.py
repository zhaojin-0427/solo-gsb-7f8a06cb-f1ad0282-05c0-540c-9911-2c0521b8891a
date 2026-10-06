"""Persistence across process restart and concurrent per-device adjudication."""
from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from conftest import b64, hdr, make_pages, sha, target_digest


def _new_client():
    from app.main import app

    return TestClient(app)


def _body(device="dev-restart", active="a", blobs=None):
    blobs = blobs or [b"p0", b"p1", b"p2"]
    return {
        "device_id": device,
        "active_partition": active,
        "active_image_digest": "a" * 64,
        "pages": make_pages(blobs),
    }, blobs


def test_session_survives_process_restart_with_pending_write(client):
    body, blobs = _body()
    r = client.post("/sessions", json=body)
    assert r.status_code == 201
    sid = r.json()["id"]

    first = client.post("/devices/me/instructions/pull",
                        headers=hdr("dev-restart")).json()

    # Simulate process restart: brand-new client/app state, same DB file.
    client2 = _new_client()
    pull = client2.post("/devices/me/instructions/pull",
                        headers=hdr("dev-restart")).json()
    assert pull["id"] == first["id"]  # same instruction id and content
    assert pull["payload"] == first["payload"]

    status = client2.get("/devices/me/sessions/current",
                         headers=hdr("dev-restart")).json()
    assert status["id"] == sid
    assert status["status"] == "writing"

    # Complete the flash after the restart.
    for idx, blob in enumerate(blobs):
        inst = client2.post("/devices/me/instructions/pull",
                            headers=hdr("dev-restart")).json()
        assert inst["page_index"] == idx
        rep = client2.post(
            f"/devices/me/instructions/{inst['id']}/page-report",
            headers=hdr("dev-restart"), json={"sha256": sha(blob)})
        assert rep.status_code == 200

    sw = client2.post("/devices/me/instructions/pull",
                      headers=hdr("dev-restart")).json()
    assert sw["kind"] == "switch"

    # Restart again while switch outcome unknown: same switch, never a new one.
    client3 = _new_client()
    sw2 = client3.post("/devices/me/instructions/pull",
                       headers=hdr("dev-restart")).json()
    assert sw2["id"] == sw["id"]
    assert sw2["switch_attempt"] == 1

    done = client3.post("/devices/me/reboot-reports",
                        headers=hdr("dev-restart"), json={
        "report_id": "rep-after-restart",
        "switch_instruction_id": sw["id"],
        "active_partition": "b",
        "active_image_digest": target_digest(blobs),
    })
    assert done.json()["outcome"] == "completed"


def test_concurrent_pulls_get_single_instruction(client):
    body, blobs = _body(device="dev-conc-pull")
    assert client.post("/sessions", json=body).status_code == 201

    results: list[dict] = []
    errors: list[Exception] = []

    def worker():
        try:
            local = _new_client()
            r = local.post("/devices/me/instructions/pull",
                           headers=hdr("dev-conc-pull"))
            results.append(r.json())
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len({i["id"] for i in results}) == 1
    assert results[0]["kind"] == "write_page"
    assert len({json_dump(i["payload"]) for i in results}) == 1

    # Exactly one instruction row was persisted.
    cur = client.get("/devices/me/sessions/current",
                     headers=hdr("dev-conc-pull")).json()
    hist = client.get(
        f"/devices/me/sessions/{cur['id']}/instructions",
        headers=hdr("dev-conc-pull")).json()
    assert len(hist) == 1


def json_dump(value):
    import json
    return json.dumps(value, sort_keys=True)


def test_concurrent_identical_reboot_reports_adjudicated_once(client):
    body, blobs = _body(device="dev-conc-reb")
    client.post("/sessions", json=body)
    blobs_shas = [sha(b) for b in blobs]
    for idx, digest in enumerate(blobs_shas):
        inst = client.post("/devices/me/instructions/pull",
                           headers=hdr("dev-conc-reb")).json()
        client.post(
            f"/devices/me/instructions/{inst['id']}/page-report",
            headers=hdr("dev-conc-reb"), json={"sha256": digest})
    sw = client.post("/devices/me/instructions/pull",
                     headers=hdr("dev-conc-reb")).json()

    outcomes: list[tuple[int, object]] = []
    lock = threading.Lock()

    def worker(n):
        local = _new_client()
        r = local.post("/devices/me/reboot-reports",
                       headers=hdr("dev-conc-reb"), json={
            "report_id": f"rep-{n}",
            "switch_instruction_id": sw["id"],
            "active_partition": "b",
            "active_image_digest": target_digest(blobs),
        })
        with lock:
            outcomes.append((r.status_code, r.json()))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = sorted(c for c, _ in outcomes)
    # Exactly one report wins and completes the attempt; the others are 409.
    assert codes[0] == 200
    assert all(c == 409 for c in codes[1:])
    winner = [b for c, b in outcomes if c == 200][0]
    assert winner["outcome"] == "completed"

    cur = client.get("/devices/me/sessions/current",
                     headers=hdr("dev-conc-reb"))
    assert cur.status_code == 404  # session finished


def test_concurrent_rollback_reports_create_single_followup(client):
    body, blobs = _body(device="dev-conc-rb")
    client.post("/sessions", json=body)
    for blob in blobs:
        inst = client.post("/devices/me/instructions/pull",
                           headers=hdr("dev-conc-rb")).json()
        client.post(
            f"/devices/me/instructions/{inst['id']}/page-report",
            headers=hdr("dev-conc-rb"), json={"sha256": sha(blob)})
    sw = client.post("/devices/me/instructions/pull",
                     headers=hdr("dev-conc-rb")).json()

    followups: list[str] = []
    statuses: list[int] = []
    lock = threading.Lock()

    def worker(n):
        local = _new_client()
        r = local.post("/devices/me/reboot-reports",
                       headers=hdr("dev-conc-rb"), json={
            "report_id": f"rb-{n}",
            "switch_instruction_id": sw["id"],
            "active_partition": "a",
            "active_image_digest": "a" * 64,
        })
        with lock:
            statuses.append(r.status_code)
            if r.status_code == 200:
                followups.append(r.json()["new_switch_instruction_id"])

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(statuses)[0] == 200
    assert all(s == 409 for s in sorted(statuses)[1:])
    assert len(followups) == 1  # exactly one new switch attempt

    pulls = set()
    for _ in range(5):
        p = client.post("/devices/me/instructions/pull",
                        headers=hdr("dev-conc-rb")).json()
        pulls.add(p["id"])
    assert pulls == set(followups)


def test_concurrent_session_creation_single_winner(client):
    bodies = [_body(device="dev-conc-create")[0] for _ in range(8)]
    statuses: list[int] = []
    lock = threading.Lock()

    def worker(body):
        local = _new_client()
        r = local.post("/sessions", json=body)
        with lock:
            statuses.append(r.status_code)

    threads = [threading.Thread(target=worker, args=(b,)) for b in bodies]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert statuses.count(201) == 1
    assert statuses.count(409) == 7
