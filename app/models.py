"""Request validation models.

Invalid base64 page data, wrong-length/non-hex digests, wrong page count or
duplicate pages all surface as FastAPI 422 responses.
"""
from __future__ import annotations

import base64
import binascii
import re

from pydantic import BaseModel, Field, field_validator

_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _validate_sha256_hex(value: str, field: str) -> str:
    if not isinstance(value, str) or not _HEX_SHA256.match(value):
        raise ValueError(f"{field} must be 64 lowercase hex characters (SHA-256)")
    return value


def decode_page_bytes(value: str) -> bytes:
    """Strict standard-alphabet base64 decode (padding required)."""
    if not isinstance(value, str) or not value:
        raise ValueError("page data must be non-empty base64 text")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("page data is not valid base64") from exc


class PageIn(BaseModel):
    data_b64: str = Field(..., description="Standard base64 encoded page bytes")
    sha256: str = Field(..., description="Expected SHA-256 of the decoded bytes")

    @field_validator("sha256")
    @classmethod
    def _sha(cls, v: str) -> str:
        return _validate_sha256_hex(v, "sha256")


class CreateSessionIn(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=128)
    active_partition: str = Field(..., description="Currently booted partition")
    active_image_sha256: str
    pages: list[PageIn] = Field(..., min_length=1, max_length=8)

    @field_validator("active_partition")
    @classmethod
    def _partition(cls, v: str) -> str:
        if v not in ("A", "B"):
            raise ValueError("active_partition must be 'A' or 'B'")
        return v

    @field_validator("active_image_sha256")
    @classmethod
    def _active_digest(cls, v: str) -> str:
        return _validate_sha256_hex(v, "active_image_sha256")


class PageReportIn(BaseModel):
    command_id: str = Field(..., min_length=1)
    page_sha256: str

    @field_validator("page_sha256")
    @classmethod
    def _sha(cls, v: str) -> str:
        return _validate_sha256_hex(v, "page_sha256")


class RebootReportIn(BaseModel):
    report_id: str = Field(..., min_length=1, max_length=128)
    switch_command_id: str = Field(..., min_length=1)
    active_partition: str
    active_image_sha256: str

    @field_validator("active_partition")
    @classmethod
    def _partition(cls, v: str) -> str:
        if v not in ("A", "B"):
            raise ValueError("active_partition must be 'A' or 'B'")
        return v

    @field_validator("active_image_sha256")
    @classmethod
    def _digest(cls, v: str) -> str:
        return _validate_sha256_hex(v, "active_image_sha256")
