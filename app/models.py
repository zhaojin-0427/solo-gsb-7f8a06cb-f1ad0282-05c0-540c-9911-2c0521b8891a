"""Pydantic request/response models and input validation."""
from __future__ import annotations

import base64
import binascii
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

Partition = Literal["a", "b"]

_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def normalize_digest(value: str, field: str) -> str:
    """Accept 64 hex chars (case-insensitive); return lowercase."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip().lower()
    if not _HEX_SHA256.match(normalized):
        raise ValueError(f"{field} must be a 64 character hex SHA-256 digest")
    return normalized


def decode_page_bytes(data_b64: str) -> bytes:
    """Strict base64 decode.

    Rejects embedded whitespace (after stripping outer padding space),
    non-base64 characters, and incorrect padding -- such inputs yield 422.
    """
    if not isinstance(data_b64, str) or not data_b64:
        raise ValueError("page data must be non-empty base64 text")
    raw = data_b64.strip()
    if re.search(r"\s", raw):
        raise ValueError("page data must not contain whitespace inside base64 text")
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("page data is not valid base64") from exc


class PageInput(BaseModel):
    data: str = Field(description="Raw page bytes encoded as base64.")
    sha256: str = Field(description="SHA-256 of the decoded page bytes.")

    @field_validator("sha256")
    @classmethod
    def _digest(cls, v: str) -> str:
        return normalize_digest(v, "page sha256")


class CreateSessionRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    active_partition: Partition
    active_image_digest: str
    pages: list[PageInput] = Field(min_length=1, max_length=8)

    @field_validator("device_id")
    @classmethod
    def _device(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("device_id must not be empty")
        return v

    @field_validator("active_image_digest")
    @classmethod
    def _active_digest(cls, v: str) -> str:
        return normalize_digest(v, "active_image_digest")


class PageWriteReportRequest(BaseModel):
    sha256: str = Field(description="Post-write page digest measured by the device.")

    @field_validator("sha256")
    @classmethod
    def _digest(cls, v: str) -> str:
        return normalize_digest(v, "sha256")


class RebootReportRequest(BaseModel):
    report_id: str = Field(min_length=1, max_length=128)
    switch_instruction_id: str = Field(min_length=1, max_length=64)
    active_partition: Partition
    active_image_digest: str

    @field_validator("report_id", "switch_instruction_id")
    @classmethod
    def _ids(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("id must not be empty")
        return v

    @field_validator("active_image_digest")
    @classmethod
    def _digest(cls, v: str) -> str:
        return normalize_digest(v, "active_image_digest")


class PageState(BaseModel):
    page_index: int
    expected_digest: str
    reported_digest: Optional[str] = None
    acknowledged: bool
    instruction_id: Optional[str] = None


class SwitchAttemptOut(BaseModel):
    attempt: int
    instruction_id: str
    status: str
    created_at: str


class SessionOut(BaseModel):
    id: str
    device_id: str
    status: str
    active_partition: str
    inactive_partition: str
    active_image_digest: str
    target_image_digest: str
    page_count: int
    current_instruction_id: Optional[str] = None
    switch_attempt: int
    failure_reason: Optional[str] = None
    created_at: str
    updated_at: str
    pages: list[PageState] = []
    switch_attempts: list[SwitchAttemptOut] = []


class InstructionOut(BaseModel):
    id: str
    sequence: int
    kind: str
    status: str
    page_index: Optional[int] = None
    switch_attempt: Optional[int] = None
    payload: dict
    created_at: str
    reported_digest: Optional[str] = None
    acknowledged_at: Optional[str] = None


class PageAckOut(BaseModel):
    instruction_id: str
    page_index: int
    expected_digest: str
    reported_digest: str
    match: bool
    idempotent: bool
    session_status: str
    failure_reason: Optional[str] = None


class RebootAckOut(BaseModel):
    report_id: str
    instruction_id: str
    outcome: Literal["completed", "rollback", "failure"]
    idempotent: bool
    session_status: str
    new_switch_instruction_id: Optional[str] = None
    failure_reason: Optional[str] = None
