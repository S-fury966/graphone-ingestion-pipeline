"""Exporter hooks for canonicalization (Step 13).

These are provided for later wiring into the CSV/JSONL/Sheets exporters. They
are intentionally NOT called on live data in this step: the database keeps the
raw values untouched, and existing exports are unchanged. When an exporter does
adopt them, the canonical name replaces the raw field and the raw value is kept
as an extra `*_raw` column.
"""
from __future__ import annotations

from typing import Optional

from .resolver import PRODUCT, STARTUP, Resolution, Resolver


def canonicalize_job_company(resolver: Resolver, company_raw: str) -> tuple[str, str]:
    """-> (content.company canonical, company_raw kept as extra column)."""
    res = resolver.resolve(company_raw, STARTUP)
    return res.canonical, company_raw


def canonicalize_startup_name(resolver: Resolver, entity_name_raw: str) -> tuple[str, str]:
    """-> (content.entityName canonical, raw_name kept as extra column)."""
    res = resolver.resolve(entity_name_raw, STARTUP)
    return res.canonical, entity_name_raw


def canonicalize_product_startup(resolver: Resolver, startup_name_raw: str) -> tuple[str, str]:
    """-> (content.startupName canonical, raw_name kept as extra column)."""
    res = resolver.resolve(startup_name_raw, STARTUP)
    return res.canonical, startup_name_raw


def canonicalize_product_name(resolver: Resolver, product_raw: str,
                              startup: Optional[str] = None) -> tuple[str, str]:
    """-> (canonical product name scoped to its startup, raw kept as extra)."""
    res: Resolution = resolver.resolve(product_raw, PRODUCT, startup=startup)
    return res.canonical, product_raw
