"""Hacker News "Who is hiring?" adapter (Algolia API).

Two steps (the pipeline does the fetching):
  1. find the current monthly "Ask HN: Who is hiring?" thread,
  2. read its TOP-LEVEL comments (parent_id == story_id); each comment's
     ``created_at`` is the job's publication time.

The comment text is free prose — company/title/is_remote/role_family are left
empty here and extracted by the grounded LLM step in the pipeline. The LLM never
sets the date or freshness.
"""
from __future__ import annotations

import json
import re
from typing import List, Optional

from bs4 import BeautifulSoup

from .base import JobItem

SOURCE = "hn-hiring"

THREAD_SEARCH = ("https://hn.algolia.com/api/v1/search_by_date"
                 "?tags=story,author_whoishiring&hitsPerPage=1")


def comments_url(story_id: str, hits: int = 100) -> str:
    return ("https://hn.algolia.com/api/v1/search_by_date"
            f"?tags=comment,story_{story_id}&hitsPerPage={hits}")


def parse_thread(text: str) -> Optional[str]:
    """Return the objectID of the most recent 'Who is hiring?' story."""
    data = json.loads(text)
    for h in data.get("hits", []):
        title = (h.get("title") or "").lower()
        if "who is hiring" in title:
            return h.get("objectID")
    hits = data.get("hits", [])
    return hits[0].get("objectID") if hits else None


def _clean(html: str) -> str:
    """HN comment HTML -> plain text: unescape entities, drop tags, keep line
    breaks. HN uses <p> as a paragraph separator and <br> as a line break; both
    become newlines so the pipe-delimited header and the body stay on their own
    lines for the grounded LLM step."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for p in soup.find_all("p"):
        p.insert_before("\n")               # <p> marks a paragraph boundary
    text = soup.get_text()                   # also unescapes HTML entities
    lines = [ln.strip() for ln in text.splitlines()]
    text = re.sub(r"\n{2,}", "\n", "\n".join(lines)).strip()
    return text


def parse_comments(text: str, story_id: str) -> List[JobItem]:
    data = json.loads(text)
    out: List[JobItem] = []
    for c in data.get("hits", []):
        # top-level comments only (direct children of the thread)
        if str(c.get("parent_id")) != str(story_id):
            continue
        oid = c.get("objectID")
        body = _clean(c.get("comment_text") or "")
        if not oid or not body:
            continue
        out.append(JobItem(
            source=SOURCE,
            url=f"https://news.ycombinator.com/item?id={oid}",
            company_raw="", title="",     # filled by the grounded LLM step
            raw_date=c.get("created_at"),
            date_field="comment_created_at",
            comment_text=body,
            listing_type="chronological"))
    return out
