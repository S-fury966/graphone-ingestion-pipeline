"""RemoteOK adapter — JSON API. `date` (ISO) is publication time; all remote."""
from __future__ import annotations

import json
from typing import List

from .base import JobItem

SOURCE = "remoteok"


def parse(text: str) -> List[JobItem]:
    data = json.loads(text)
    if not isinstance(data, list):
        return []
    out: List[JobItem] = []
    for j in data:
        if not isinstance(j, dict) or not j.get("id"):
            continue  # first element is a legal/terms notice
        url = (j.get("url") or j.get("apply_url") or "").strip()
        if not url:
            continue
        out.append(JobItem(
            source=SOURCE, url=url,
            company_raw=(j.get("company") or "").strip(),
            title=(j.get("position") or "").strip(),
            raw_date=j.get("date") or (str(j["epoch"]) if j.get("epoch") else None),
            date_field="date",
            remote_flag=True,  # RemoteOK jobs are remote by nature
            location=j.get("location") or "",
            tags=[str(t) for t in (j.get("tags") or [])],
            listing_type="chronological"))
    return out
