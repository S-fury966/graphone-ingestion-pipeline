"""RESEARCH_PAPER record schema."""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from .base import BaseRecord, _ensure_utc


class ResearchPaperContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    authors: List[str]
    paper_url: HttpUrl
    published_date: datetime
    arxiv_id: Optional[str] = None  # added: paper uniqueness key
    github_url: Optional[HttpUrl] = None
    github_stars: Optional[int] = Field(default=None, ge=0)

    @field_validator("title")
    @classmethod
    def _title_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("content.title must not be empty")
        return v.strip()

    @field_validator("published_date")
    @classmethod
    def _published_utc(cls, v: datetime) -> datetime:
        return _ensure_utc(v, "content.published_date")

    @model_validator(mode="after")
    def _stars_require_url(self) -> "ResearchPaperContent":
        if self.github_stars is not None and self.github_url is None:
            raise ValueError("github_stars set without github_url")
        return self


class ResearchPaper(BaseRecord):
    recordType: Literal["RESEARCH_PAPER"] = "RESEARCH_PAPER"
    content: ResearchPaperContent
