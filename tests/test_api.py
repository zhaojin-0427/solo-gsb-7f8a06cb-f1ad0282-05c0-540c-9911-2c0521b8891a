import base64

from conftest import b64, hdr, make_pages, sha, target_digest


def create_session(client, device="dev-1", active="a", active_digest="a" * 64,
                   blobs=None):
    blobs = blobs if blobs is not None else [b"page-0-bytes", b"page-1-bytes"]
    body = {
        "device_id": device,
        "active_partition": active,
        "active_image_digest": active_digest,
        "pages": make_pages(blobs),
    }
    r = client.post("/sessions", json=body)
    return r, blobs


def full_flash(client, device="dev-1", active="a", active_digest="a" * 64,
               blobs=None):
    r, blobs = create_session(client, device, active, active_digest, blobs)
    assert r.status_code == 201, r.text
    session = r.json()
    assert session["status"] == "writing"
    assert session["inactive_partition"] == ("b" if active == "a" else "a")
    assert session["target_image_digest"] == target_digest(blobs)

    # Pull + ack every page in order.
    last = None
    for idx, blob in enumerate(blobs):
        p = client.post("/devices/me/instructions/pull", headers=hdr(device))
        assert p.status_code == 200, p.text
        inst = p.json()
        assert inst["kind"] == "write_page"
        assert inst["page_index"] == idx
        assert inst["payload"]["partition"] == session["inactive_partition"]
        assert base64.b64decode(inst["payload"]["data"]) == blob
        rep = client.post(
            f"/devices/me/instructions/{inst['id']}/page-report",
            headers=hdr(device),
            json={"sha256": sha(blob)},
        )
        assert rep.status_code == 200, rep.text
        last = rep.json()
    return session, blobs, last


def _flash_to_switch(client, device, active="a", active_digest="a" * 64,
                     blobs=None):
    """Run the write phase and return (session, blobs, switch instruction)."""
    session, blobs, _ = full_flash(client, device, active, active_digest, blobs)
    sw = client.post("/devices/me/instructions/pull",
                     headers=hdr(device)).json()
    assert sw["kind"] == "switch"
    return session, blobs, sw


def test_create_session_validates_base64(client):
    body = {
        "device_id": "dev-x",
        "active_partition": "a",
        "active_image_digest": "a" * 64,
        "pages": [{"data": "not!!!base64@@@", "sha256": "a" * 64}],
    }
    r = client.post("/sessions", json=body)
    assert r.status_code == 422


def test_create_session_rejects_bad_page_digest(client):
    body = {
        "device_id": "dev-x",
        "active_partition": "a",
        "active_image_digest": "a" * 64,
        "pages": [{"data": b64(b"hello"), "sha256": "b" * 64}],
    }
    r = client.post("/sessions", json=body)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "page_digest_mismatch"


def test_page_count_bounds(client):
    for n, code in ((0, 422), (9, 422)):
        body = {
            "device_id": "dev-x",
            "active_partition": "a",
            "active_image_digest": "a" * 64,
            "pages": make_pages([b"x"] * n),
        }
        assert client.post("/sessions", json=body).status_code == code


def test_full_happy_path_switch_and_target_boot(client):
    session, blobs, last = full_flash(client)
    # After the final ack the session awaits the first switch reboot.
    assert last["session_status"] == "switch_pending"
    pull = client.post("/devices/me/instructions/pull", headers=hdr("dev-1"))
    assert pull.status_code == 200
    sw = pull.json()
    assert sw["kind"] == "switch"
    assert sw["switch_attempt"] == 1
    # Switch swaps inactive -> active target.
    assert sw["payload"]["swap"] == {"from": "b", "to": "a"}

    # Device boots into the target partition with target digest -> complete.
    rr = client.post("/devices/me/reboot-reports", headers=hdr("dev-1"), json={
        "report_id": "rep-1",
        "switch_instruction_id": sw["id"],
        "active_partition": "b",
        "active_image_digest": target_digest(blobs),
    })
    assert rr.status_code == 200, rr.text
    body = rr.json()
    assert body["outcome"] == "completed"
    assert body["session_status"] == "completed"

    # No more instructions after completion.
    assert client.post("/devices/me/instructions/pull",
                       headers=hdr("dev-1")).status_code == 404


def test_repeated_pull_returns_same_write_instruction(client):
    r, blobs = create_session(client, device="dev-dup")
    sid = r.json()["id"]
    p1 = client.post("/devices/me/instructions/pull", headers=hdr("dev-dup"))
    p2 = client.post("/devices/me/instructions/pull", headers=hdr("dev-dup"))
    p3 = client.post("/devices/me/instructions/pull", headers=hdr("dev-dup"))
    ids = {p1.json()["id"], p2.json()["id"], p3.json()["id"]}
    assert ids == {p1.json()["id"]}
    assert p1.json()["payload"] == p3.json()["payload"]

    # History shows exactly one write instruction.
    hist = client.get(
        f"/devices/me/sessions/{sid}/instructions", headers=hdr("dev-dup"))
    assert hist.status_code == 200
    kinds = [i["kind"] for i in hist.json()]
    assert kinds == ["write_page"]


def test_page_digest_mismatch_fails_session(client):
    r, blobs = create_session(client, device="dev-fail")
    p = client.post("/devices/me/instructions/pull", headers=hdr("dev-fail"))
    inst = p.json()
    rep = client.post(
        f"/devices/me/instructions/{inst['id']}/page-report",
        headers=hdr("dev-fail"),
        json={"sha256": "c" * 64},
    )
    assert rep.status_code == 200
    assert rep.json()["match"] is False
    assert rep.json()["session_status"] == "failed"

    # Repeating the SAME mismatching report is idempotent (same adjudication).
    again = client.post(
        f"/devices/me/instructions/{inst['id']}/page-report",
        headers=hdr("dev-fail"),
        json={"sha256": "c" * 64},
    )
    assert again.status_code == 200
    body = again.json()
    assert body["idempotent"] is True
    assert body["match"] is False
    assert body["session_status"] == "failed"

    # A different digest against the failed instruction -> 409, unchanged.
    conflict = client.post(
        f"/devices/me/instructions/{inst['id']}/page-report",
        headers=hdr("dev-fail"),
        json={"sha256": "0" * 64},
    )
    assert conflict.status_code == 409

    # No new instructions after failure.
    assert client.post("/devices/me/instructions/pull",
                       headers=hdr("dev-fail")).status_code == 404


def test_equal_page_report_is_idempotent_conflicting_is_409(client):
    r, blobs = create_session(client, device="dev-idem")
    inst = client.post("/devices/me/instructions/pull",
                       headers=hdr("dev-idem")).json()
    good = sha(blobs[0])
    rep = client.post(
        f"/devices/me/instructions/{inst['id']}/page-report",
        headers=hdr("dev-idem"), json={"sha256": good})
    assert rep.status_code == 200 and rep.json()["idempotent"] is False

    again = client.post(
        f"/devices/me/instructions/{inst['id']}/page-report",
        headers=hdr("dev-idem"), json={"sha256": good})
    assert again.status_code == 200 and again.json()["idempotent"] is True

    bad = client.post(
        f"/devices/me/instructions/{inst['id']}/page-report",
        headers=hdr("dev-idem"), json={"sha256": "d" * 64})
    assert bad.status_code == 409


def test_one_open_session_per_device(client):
    r1, _ = create_session(client, device="dev-one")
    assert r1.status_code == 201
    r2, _ = create_session(client, device="dev-one")
    assert r2.status_code == 409
    assert r2.json()["detail"]["error"] == "open_session_exists"

    # A different device is fine.
    r3, _ = create_session(client, device="dev-two")
    assert r3.status_code == 201


def test_no_writes_after_switch(client):
    session, blobs, _ = full_flash(client, device="dev-sw")
    sw = client.post("/devices/me/instructions/pull",
                     headers=hdr("dev-sw")).json()
    # Only the switch can come back, even on repeated pulls.
    for _ in range(3):
        p = client.post("/devices/me/instructions/pull",
                        headers=hdr("dev-sw")).json()
        assert p["id"] == sw["id"]
        assert p["kind"] == "switch"

    # A page report against a write instruction is 409 after switch phase.
    # (The acknowledged instruction already exists; report a different digest.)
    hist = client.get(
        f"/devices/me/sessions/{session['id']}/instructions",
        headers=hdr("dev-sw")).json()
    write_id = [i for i in hist if i["kind"] == "write_page"][0]["id"]
    r = client.post(
        f"/devices/me/instructions/{write_id}/page-report",
        headers=hdr("dev-sw"), json={"sha256": "e" * 64})
    assert r.status_code == 409


def test_rollback_generates_new_switch_attempt(client):
    session, blobs, _ = full_flash(client, device="dev-rb", active="a")
    sw1 = client.post("/devices/me/instructions/pull",
                      headers=hdr("dev-rb")).json()

    # Device booted back into the ORIGINAL partition with original digest.
    rb = client.post("/devices/me/reboot-reports", headers=hdr("dev-rb"), json={
        "report_id": "rep-rb",
        "switch_instruction_id": sw1["id"],
        "active_partition": "a",
        "active_image_digest": "a" * 64,
    })
    assert rb.status_code == 200
    body = rb.json()
    assert body["outcome"] == "rollback"
    assert body["session_status"] == "switch_pending"
    assert body["new_switch_instruction_id"]
    assert body["new_switch_instruction_id"] != sw1["id"]

    # Pull returns the new attempt; no page write is reissued.
    sw2 = client.post("/devices/me/instructions/pull",
                      headers=hdr("dev-rb")).json()
    assert sw2["id"] == body["new_switch_instruction_id"]
    assert sw2["switch_attempt"] == 2

    # Original attempt cannot be reported again: it is already adjudicated
    # (409, state unchanged).
    stale = client.post("/devices/me/reboot-reports", headers=hdr("dev-rb"), json={
        "report_id": "rep-late",
        "switch_instruction_id": sw1["id"],
        "active_partition": "a",
        "active_image_digest": "a" * 64,
    })
    assert stale.status_code == 409
    cur = client.get("/devices/me/sessions/current",
                     headers=hdr("dev-rb")).json()
    assert cur["status"] == "switch_pending"
    assert cur["switch_attempt"] == 2

    # Second attempt succeeds: boot into target.
    ok = client.post("/devices/me/reboot-reports", headers=hdr("dev-rb"), json={
        "report_id": "rep-ok",
        "switch_instruction_id": sw2["id"],
        "active_partition": "b",
        "active_image_digest": target_digest(blobs),
    })
    assert ok.json()["outcome"] == "completed"


def test_reboot_unexpected_state_fails(client):
    session, blobs, _ = full_flash(client, device="dev-weird")
    sw = client.post("/devices/me/instructions/pull",
                     headers=hdr("dev-weird")).json()
    # Booted into target partition but with wrong digest.
    weird = client.post("/devices/me/reboot-reports",
                        headers=hdr("dev-weird"), json={
        "report_id": "rep-weird",
        "switch_instruction_id": sw["id"],
        "active_partition": "b",
        "active_image_digest": "f" * 64,
    })
    assert weird.status_code == 200
    assert weird.json()["outcome"] == "failure"
    assert weird.json()["session_status"] == "failed"


def test_reboot_report_idempotent_and_conflicting(client):
    session, blobs, _ = full_flash(client, device="dev-rep")
    sw = client.post("/devices/me/instructions/pull",
                     headers=hdr("dev-rep")).json()
    payload = {
        "report_id": "rep-same",
        "switch_instruction_id": sw["id"],
        "active_partition": "b",
        "active_image_digest": target_digest(blobs),
    }
    first = client.post("/devices/me/reboot-reports",
                        headers=hdr("dev-rep"), json=payload)
    assert first.json()["idempotent"] is False
    again = client.post("/devices/me/reboot-reports",
                        headers=hdr("dev-rep"), json=payload)
    assert again.status_code == 200
    assert again.json()["idempotent"] is True
    assert again.json()["outcome"] == "completed"

    # Same report id, different values -> 409, no state change.
    conflicting = dict(payload)
    conflicting["active_partition"] = "a"
    r = client.post("/devices/me/reboot-reports",
                    headers=hdr("dev-rep"), json=conflicting)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "conflicting_report"

    # New report id against the already-consumed instruction -> 409.
    payload2 = dict(payload, report_id="rep-other")
    r2 = client.post("/devices/me/reboot-reports",
                     headers=hdr("dev-rep"), json=payload2)
    assert r2.status_code == 409


def test_rollback_report_idempotent_returns_same_followup(client):
    session, blobs, _ = full_flash(client, device="dev-rb2")
    sw = client.post("/devices/me/instructions/pull",
                     headers=hdr("dev-rb2")).json()
    payload = {
        "report_id": "rep-rb2",
        "switch_instruction_id": sw["id"],
        "active_partition": "a",
        "active_image_digest": "a" * 64,
    }
    first = client.post("/devices/me/reboot-reports",
                        headers=hdr("dev-rb2"), json=payload).json()
    second = client.post("/devices/me/reboot-reports",
                         headers=hdr("dev-rb2"), json=payload).json()
    assert second["idempotent"] is True
    assert second["new_switch_instruction_id"] == first["new_switch_instruction_id"]
    # Repeated pulls still return the same (single) follow-up switch.
    pull = client.post("/devices/me/instructions/pull",
                       headers=hdr("dev-rb2")).json()
    assert pull["id"] == first["new_switch_instruction_id"]
    assert pull["switch_attempt"] == 2


def test_report_id_unique_scoped_per_device(client):
    # Two devices may each use the same report id without mutual conflict.
    _, blobs1, sw1 = _flash_to_switch(client, "dev-share-1", active="a",
                                      active_digest="1" * 64)
    _, blobs2, sw2 = _flash_to_switch(client, "dev-share-2", active="a",
                                      active_digest="2" * 64)
    for device, sw, blobs in (
        ("dev-share-1", sw1, blobs1),
        ("dev-share-2", sw2, blobs2),
    ):
        r = client.post("/devices/me/reboot-reports", headers=hdr(device), json={
            "report_id": "shared-report-id",
            "switch_instruction_id": sw["id"],
            "active_partition": "b",
            "active_image_digest": target_digest(blobs),
        })
        assert r.status_code == 200, r.text
        assert r.json()["outcome"] == "completed"


def test_history_and_status_queries(client):
    session, blobs, _ = full_flash(client, device="dev-q")
    sid = session["id"]
    cur = client.get("/devices/me/sessions/current", headers=hdr("dev-q"))
    assert cur.status_code == 200
    assert cur.json()["status"] == "switch_pending"

    got = client.get(f"/devices/me/sessions/{sid}", headers=hdr("dev-q"))
    assert got.status_code == 200
    assert len(got.json()["pages"]) == len(blobs)

    hist = client.get(f"/devices/me/sessions/{sid}/instructions",
                      headers=hdr("dev-q")).json()
    assert [i["kind"] for i in hist][:len(blobs)] == ["write_page"] * len(blobs)
    assert hist[-1]["kind"] == "switch"

    lst = client.get("/devices/me/sessions", headers=hdr("dev-q")).json()
    assert any(s["id"] == sid for s in lst)

    # Other devices can't see the session.
    other = client.get(f"/devices/me/sessions/{sid}", headers=hdr("intruder"))
    assert other.status_code == 404


def test_writes_only_ever_target_inactive_partition(client):
    # Start on b: the inactive target is a.
    session, blobs, _ = full_flash(client, device="dev-b", active="b",
                                   active_digest="9" * 64)
    hist = client.get(
        f"/devices/me/sessions/{session['id']}/instructions",
        headers=hdr("dev-b")).json()
    for inst in hist:
        if inst["kind"] == "write_page":
            assert inst["payload"]["partition"] == "a"
        else:
            assert inst["payload"]["swap"] == {"from": "a", "to": "b"}


def test_missing_device_header(client):
    r = client.post("/devices/me/instructions/pull")
    assert r.status_code == 422
