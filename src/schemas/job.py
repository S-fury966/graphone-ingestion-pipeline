"""JOB record schema."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from .base import BaseRecord, _ensure_utc


class JobContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: str  # canonical
    title: str    # the job title as published
    date: datetime
    is_remote: bool
    role_family: str  # e.g. "Engineering"

    @field_validator("company", "title", "role_family")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("job text fields must not be empty")
        return v.strip()

    @field_validator("date")
    @classmethod
    def _date_utc(cls, v: datetime) -> datetime:
        return _ensure_utc(v, "content.date")


class Job(BaseRecord):
    recordType: Literal["JOB"] = "JOB"
    content: JobContent
