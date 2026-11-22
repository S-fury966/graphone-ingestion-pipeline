"""Arbeitnow adapter — JSON API. created_at is a unix epoch (publication time)."""
from __future__ import annotations

import json
from typing import List

from .base import JobItem

SOURCE = "arbeitnow"


def parse(text: str) -> List[JobItem]:
    data = json.loads(text)
    out: List[JobItem] = []
    for j in data.get("data", []):
        url = (j.get("url") or "").strip()
        if not url:
            continue
        out.append(JobItem(
            source=SOURCE, url=url,
            company_raw=(j.get("company_name") or "").strip(),
            title=(j.get("title") or "").strip(),
            raw_date=str(j["created_at"]) if j.get("created_at") is not None else None,
            date_field="created_at",
            remote_flag=bool(j["remote"]) if "remote" in j else None,
            location=j.get("location") or "",
            tags=[str(t) for t in (j.get("tags") or [])],
            listing_type="chronological"))
    return out
