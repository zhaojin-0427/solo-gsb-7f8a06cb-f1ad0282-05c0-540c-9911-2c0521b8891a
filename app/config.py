"""Runtime configuration, overridable through environment variables."""
from __future__ import annotations

import os
from pathlib import Path


DB_PATH = Path(os.getenv("FIRMWARE_DB_PATH", "/data/firmware.db"))
HOST = os.getenv("FIRMWARE_HOST", "0.0.0.0")
PORT = int(os.getenv("FIRMWARE_PORT", "8080"))
