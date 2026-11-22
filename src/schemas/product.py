"""PRODUCT record schema."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator

from .base import BaseRecord
from .enums import PricingModel


class ProductContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    startupName: str  # canonical
    name: str         # product name (added: needed for startup+product uniqueness)
    # Optional: null when there is no pricing evidence (never guessed).
    pricingModel: Optional[PricingModel] = None

    @field_validator("startupName", "name")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("product name fields must not be empty")
        return v.strip()


class Product(BaseRecord):
    recordType: Literal["PRODUCT"] = "PRODUCT"
    content: ProductContent
