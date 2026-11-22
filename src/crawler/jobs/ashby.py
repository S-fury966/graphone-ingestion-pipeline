"""Ashby adapter — per-company board API. publishedAt is publication time.

Complete listing: the endpoint returns all currently-open jobs.
"""
from __future__ import annotations

import json
from typing import List

from .base import JobItem

SOURCE = "ashby"


def parse(text: str, company: str) -> List[JobItem]:
    data = json.loads(text)
    out: List[JobItem] = []
    for j in data.get("jobs", []):
        url = (j.get("jobUrl") or j.get("applyUrl") or "").strip()
        if not url:
            continue
        workplace = (j.get("workplaceType") or "")
        out.append(JobItem(
            source=SOURCE, url=url,
            company_raw=company,
            title=(j.get("title") or "").strip(),
            raw_date=j.get("publishedAt"),
            date_field="publishedAt",
            remote_flag=bool(j["isRemote"]) if "isRemote" in j else None,
            location=" ".join(x for x in [j.get("location") or "", workplace] if x),
            tags=[],
            department=j.get("department") or j.get("team"),
            listing_type="complete"))
    return out
