import base64
import hashlib
import os
import tempfile

import pytest

# Point the service at a throwaway database before importing the app.
_tmpdir = tempfile.mkdtemp(prefix="flashcoord-test-")
os.environ["FLASHCOORD_DB_PATH"] = os.path.join(_tmpdir, "test.db")

from app.db import init_db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    if os.path.exists(os.environ["FLASHCOORD_DB_PATH"]):
        os.remove(os.environ["FLASHCOORD_DB_PATH"])
    for suffix in ("-wal", "-shm"):
        p = os.environ["FLASHCOORD_DB_PATH"] + suffix
        if os.path.exists(p):
            os.remove(p)
    init_db()
    yield


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_pages(blobs: list[bytes]):
    return [{"data": b64(b), "sha256": sha(b)} for b in blobs]


def target_digest(blobs: list[bytes]) -> str:
    return hashlib.sha256(b"".join(blobs)).hexdigest()


def hdr(device_id: str) -> dict:
    return {"X-Device-Id": device_id}
