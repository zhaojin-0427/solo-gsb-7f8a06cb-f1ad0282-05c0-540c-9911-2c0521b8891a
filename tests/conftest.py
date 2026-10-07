"""Shared pytest fixtures: isolated temp DB per test and a TestClient."""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "firmware.db"
    monkeypatch.setenv("FIRMWARE_DB_PATH", str(db_file))

    import app.config
    import app.db
    import app.locks
    import app.main
    import app.models
    import app.service
    import app.routers

    for mod in (
        app.config,
        app.db,
        app.locks,
        app.models,
        app.service,
        app.routers,
        app.main,
    ):
        importlib.reload(mod)

    with TestClient(app.main.app) as c:
        yield c


def make_pages(*payloads: bytes) -> list[dict]:
    import base64
    import hashlib

    return [
        {
            "data_b64": base64.b64encode(p).decode(),
            "sha256": hashlib.sha256(p).hexdigest(),
        }
        for p in payloads
    ]


def target_digest(pages: list[dict]) -> str:
    import base64
    import hashlib

    blob = b"".join(base64.b64decode(p["data_b64"]) for p in pages)
    return hashlib.sha256(blob).hexdigest()


def sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()
