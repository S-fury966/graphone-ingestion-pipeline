"""Greenhouse adapter — per-company board API.

`first_published` is a real publication time (preferred). `updated_at` is a
MODIFIED date and is never used as freshness evidence; a job without
`first_published` is left undated (raw_date=None) for the snapshot heuristic.
Complete listing: the endpoint returns all currently-open jobs.
"""
from __future__ import annotations

import json
from typing import List

from .base import JobItem

SOURCE = "greenhouse"


def parse(text: str, company: str) -> List[JobItem]:
    data = json.loads(text)
    out: List[JobItem] = []
    for j in data.get("jobs", []):
        url = (j.get("absolute_url") or "").strip()
        if not url:
            continue
        first_pub = j.get("first_published")
        loc = (j.get("location") or {})
        departments = j.get("departments") or []
        dept = departments[0].get("name") if departments else None
        out.append(JobItem(
            source=SOURCE, url=url,
            company_raw=(j.get("company_name") or company or "").strip() or company,
            title=(j.get("title") or "").strip(),
            raw_date=first_pub,                 # None -> snapshot heuristic
            date_field="first_published" if first_pub else None,
            remote_flag=None,
            location=loc.get("name") or "",
            tags=[],
            department=dept,
            listing_type="complete",
            raw_modified=j.get("updated_at")))  # modified; logs only
    return out
