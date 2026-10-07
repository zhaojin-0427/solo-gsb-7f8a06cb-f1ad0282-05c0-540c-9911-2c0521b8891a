"""HTTP routes: session management, device command channel, reports, history."""
from __future__ import annotations

from fastapi import APIRouter, Query

from .models import CreateSessionIn, PageReportIn, RebootReportIn
from . import service

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@router.post("/sessions", status_code=201)
def create_session(payload: CreateSessionIn) -> dict:
    return service.create_session(payload)


@router.get("/sessions")
def list_sessions(device_id: str | None = Query(default=None)) -> list[dict]:
    return service.list_sessions(device_id)


@router.get("/devices/{device_id}/open-session")
def device_open_session(device_id: str) -> dict:
    return service.get_open_session_for_device(device_id)


@router.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    return service.get_session(session_id)


@router.get("/sessions/{session_id}/commands")
def session_commands(session_id: str) -> list[dict]:
    return service.list_commands(session_id)


@router.get("/sessions/{session_id}/reboot-reports")
def session_reboot_reports(session_id: str) -> list[dict]:
    return service.list_reboot_reports(session_id)


# ---------------------------------------------------------------------------
# Device command channel
# ---------------------------------------------------------------------------

@router.post("/devices/{device_id}/commands:pull")
def pull_command(device_id: str) -> dict:
    return service.pull_command(device_id)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@router.post("/sessions/{session_id}/page-reports")
def page_report(session_id: str, payload: PageReportIn) -> dict:
    return service.report_page_write(session_id, payload)


@router.post("/sessions/{session_id}/reboot-reports")
def reboot_report(session_id: str, payload: RebootReportIn) -> dict:
    return service.report_reboot(session_id, payload)
