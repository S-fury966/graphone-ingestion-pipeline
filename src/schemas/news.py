"""NEWS record schema."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator

from .base import BaseRecord, _ensure_utc
from .enums import DateConfidence


class NewsContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    published_date: datetime
    full_text: str
    date_confidence: DateConfidence
    author: Optional[str] = None
    summary: Optional[str] = None

    @field_validator("title", "full_text")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("news text fields must not be empty")
        return v.strip()

    @field_validator("published_date")
    @classmethod
    def _published_utc(cls, v: datetime) -> datetime:
        return _ensure_utc(v, "content.published_date")


class News(BaseRecord):
    recordType: Literal["NEWS"] = "NEWS"
    content: NewsContent
