"""Shared JOB types + deterministic field classifiers (is_remote, role_family)."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

import yaml

BOARDS_PATH = "config/job_boards.yaml"
ROLE_FAMILIES_PATH = "config/role_families.yaml"

REMOTE_WORDS = ("remote", "anywhere", "work from home", "work-from-home", "wfh",
                "distributed", "fully remote", "remote-first", "remote first")
ONSITE_WORDS = ("on-site", "on site", "onsite", "hybrid", "in office",
                "in-office", "in person", "in-person")


@dataclass
class JobItem:
    """One raw job posting from an adapter, pre-classification."""
    source: str
    url: str
    company_raw: str
    title: str
    # Publication date: the raw value + the field name it came from. raw_date is
    # None when the source gives no publication time (-> snapshot heuristic).
    raw_date: Optional[str]
    date_field: Optional[str]
    # remote signals
    remote_flag: Optional[bool] = None
    location: str = ""
    tags: List[str] = field(default_factory=list)
    # role signal
    department: Optional[str] = None
    # provenance / extras
    listing_type: str = "chronological"
    comment_text: Optional[str] = None     # HN "Who is hiring" free text
    raw_modified: Optional[str] = None      # e.g. Greenhouse updated_at (for logs)


# ---------------------------------------------------------------------------
# is_remote
# ---------------------------------------------------------------------------
def decide_remote(remote_flag: Optional[bool], location: str, title: str,
                  tags: Optional[List[str]]) -> Tuple[bool, str]:
    """Return (is_remote, remote_basis).

    basis in {api_flag, text_match, location_only, default_false}.
    """
    if remote_flag is True:
        return True, "api_flag"
    text = " ".join([location or "", title or "", " ".join(tags or [])]).lower()
    if any(w in text for w in REMOTE_WORDS):
        return True, "text_match"
    # Explicit on-site/hybrid wording, or a physical location, with no remote
    # wording -> not remote (a physical arrangement is stated).
    if any(w in text for w in ONSITE_WORDS) or (location or "").strip():
        return False, "location_only"
    return False, "default_false"


# ---------------------------------------------------------------------------
# role_family
# ---------------------------------------------------------------------------
@lru_cache(maxsize=4)
def load_role_table(path: str = ROLE_FAMILIES_PATH) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    fams = data.get("families", {}) or {}
    return tuple((fam, tuple(str(k).lower() for k in kws))
                 for fam, kws in fams.items())


def classify_role(department: Optional[str], title: Optional[str],
                  table: Optional[Tuple] = None) -> Tuple[str, str]:
    """Return (role_family, role_basis).

    basis in {department, title_match, default_other}. The ATS department/team
    name is tried first, then the job title; falls back to "Other".
    """
    table = table if table is not None else load_role_table()
    for value, basis in ((department, "department"), (title, "title_match")):
        if value and value.strip():
            low = value.lower()
            for fam, kws in table:
                if any(kw in low for kw in kws):
                    return fam, basis
    return "Other", "default_other"


# ---------------------------------------------------------------------------
# board slugs
# ---------------------------------------------------------------------------
@lru_cache(maxsize=4)
def load_board_slugs(path: str = BOARDS_PATH) -> dict:
    """{'greenhouse': [{'slug','company'}...], 'ashby': [...]}"""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {"greenhouse": data.get("greenhouse", []) or [],
            "ashby": data.get("ashby", []) or []}
