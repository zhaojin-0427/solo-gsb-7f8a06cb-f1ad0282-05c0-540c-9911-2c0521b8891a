"""FastAPI application entrypoint."""
from __future__ import annotations

from fastapi import FastAPI

from .db import init_db
from .routers import router


def create_app() -> FastAPI:
    init_db()
    app = FastAPI(
        title="Dual-partition firmware flashing coordinator",
        version="1.0.0",
        description=(
            "Coordinates firmware flashing on dual-partition devices. "
            "Write-page commands only ever target the inactive partition; "
            "a partition switch is issued only after every page digest "
            "matches, and reboot reports adjudicate each switch attempt."
        ),
    )
    app.include_router(router)
    return app


app = create_app()
