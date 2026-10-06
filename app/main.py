"""FastAPI application: firmware flash coordination HTTP API."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status

from . import services
from .db import init_db
from .models import (
    CreateSessionRequest,
    InstructionOut,
    PageAckOut,
    PageWriteReportRequest,
    RebootAckOut,
    RebootReportRequest,
    SessionOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Dual-Partition Firmware Flash Coordinator",
    version="1.0.0",
    description=(
        "Coordinates page-wise firmware writes to the inactive partition of "
        "dual-partition devices, verifies per-page digests, and adjudicates "
        "partition-switch reboots."
    ),
    lifespan=lifespan,
)


def device_header(request: Request) -> str:
    value = request.headers.get("X-Device-Id", "").strip()
    if not value:
        raise HTTPException(status_code=422, detail={
            "error": "missing_device_header",
            "message": "X-Device-Id header is required",
        })
    return value


@app.post(
    "/sessions",
    response_model=SessionOut,
    status_code=status.HTTP_201_CREATED,
    tags=["sessions"],
)
def create_session(req: CreateSessionRequest) -> Any:
    """Create a flashing session. Validates base64 encoding and digests first."""
    return services.create_session(req)


@app.post(
    "/devices/me/instructions/pull",
    response_model=InstructionOut,
    tags=["device"],
)
def pull(device_id: str = Depends(device_header)) -> Any:
    """Long-poll style fetch of the next pending instruction.

    Repeated calls return the same unacknowledged instruction (same id and
    content). After a switch is issued no further page writes are produced.
    """
    return services.pull_next_instruction(device_id)


@app.post(
    "/devices/me/instructions/{instruction_id}/page-report",
    response_model=PageAckOut,
    tags=["device"],
)
def page_report(
    instruction_id: str,
    req: PageWriteReportRequest,
    device_id: str = Depends(device_header),
) -> Any:
    """Device reports the post-write digest measured for one page."""
    return services.report_page_write(device_id, instruction_id, req.sha256)


@app.post(
    "/devices/me/reboot-reports",
    response_model=RebootAckOut,
    tags=["device"],
)
def reboot_report(
    req: RebootReportRequest,
    device_id: str = Depends(device_header),
) -> Any:
    """Device reports what partition/digest it booted into after a switch."""
    return services.report_reboot(req, device_id)


@app.get("/devices/me/sessions/current", response_model=SessionOut, tags=["query"])
def current_session(device_id: str = Depends(device_header)) -> Any:
    return services.get_open_session(device_id)


@app.get("/devices/me/sessions", response_model=list[SessionOut], tags=["query"])
def list_sessions(
    device_id: str = Depends(device_header),
    limit: int = Query(50, ge=1, le=200),
) -> Any:
    return services.list_sessions(device_id, limit)


@app.get(
    "/devices/me/sessions/{session_id}",
    response_model=SessionOut,
    tags=["query"],
)
def get_session(session_id: str, device_id: str = Depends(device_header)) -> Any:
    return services.get_session(device_id, session_id)


@app.get(
    "/devices/me/sessions/{session_id}/instructions",
    response_model=list[InstructionOut],
    tags=["query"],
)
def get_instructions(session_id: str, device_id: str = Depends(device_header)) -> Any:
    """Instruction history (including payloads), in emission order."""
    return services.list_instructions(device_id, session_id)


@app.get("/healthz", tags=["ops"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}
