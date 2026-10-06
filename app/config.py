"""Runtime configuration for the firmware flasher coordinator."""
from __future__ import annotations

import os

DB_PATH = os.environ.get("FLASHCOORD_DB_PATH", "/data/flashcoord.db")
