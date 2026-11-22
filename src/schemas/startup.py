"""STARTUP record schema."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .base import BaseRecord


class StartupData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employeeCount: Optional[int] = Field(default=None, ge=0)


class StartupContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entityName: str  # canonical
    data: StartupData = Field(default_factory=StartupData)

    @field_validator("entityName")
    @classmethod
    def _entity_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("content.entityName must not be empty")
        return v.strip()


class Startup(BaseRecord):
    recordType: Literal["STARTUP"] = "STARTUP"
    content: StartupContent
