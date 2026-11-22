"""Shared base models and helpers for all record schemas."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from pydantic import BaseModel, ConfigDict, HttpUrl, field_validator


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime, field_name: str) -> datetime:
    """Reject naive datetimes; normalize aware ones to UTC."""
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware (ISO-8601 with offset)")
    return value.astimezone(timezone.utc)


def flatten_dict(data: Dict[str, Any], parent_key: str = "", sep: str = ".") -> Dict[str, Any]:
    """Flatten a nested dict into dotted keys, suitable for a CSV row.

    Lists are joined with ';' so each record maps to a single flat row.
    """
    items: Dict[str, Any] = {}
    for key, value in data.items():
        new_key = f"{parent_key}{sep}{key}" if parent_key else key
        if isinstance(value, dict):
            items.update(flatten_dict(value, new_key, sep))
        elif isinstance(value, list):
            items[new_key] = ";".join("" if v is None else str(v) for v in value)
        else:
            items[new_key] = value
    return items


class SourceInfo(BaseModel):
    """Provenance for every record — name plus a valid URL."""
    model_config = ConfigDict(extra="forbid")

    name: str
    url: HttpUrl

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("source.name must not be empty")
        return v.strip()


class BaseRecord(BaseModel):
    """Common fields shared by every record type.

    Every record is traceable: it carries source.name, source.url and a
    timezone-aware collectedAt timestamp (stored as UTC).
    """
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    schemaVersion: str = "1.0"
    source: SourceInfo
    collectedAt: datetime

    @field_validator("collectedAt")
    @classmethod
    def _collected_utc(cls, v: datetime) -> datetime:
        return _ensure_utc(v, "collectedAt")

    def to_flat_dict(self) -> Dict[str, Any]:
        """Dump to a flat dict with dotted keys (for CSV writers)."""
        return flatten_dict(self.model_dump(mode="json"))

    def to_json(self) -> str:
        """Dump to a JSON string (for JSONL writers)."""
        return self.model_dump_json()
