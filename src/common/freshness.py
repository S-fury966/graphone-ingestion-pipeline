"""Deterministic freshness decision — pure Python, no LLM, no network.

For every item we decide one of:

  * ``VERIFIED_FRESH``   — a trusted publication timestamp inside the window.
  * ``INFERRED_FRESH``   — no usable timestamp, but the snapshot heuristic
                           ("new since last complete run") proves it is new.
  * ``STALE``            — a trusted timestamp, but outside the window.
  * ``FRESHNESS_UNPROVABLE`` — undated and the snapshot heuristic cannot bound it.
  * ``UNDATED_BASELINE`` — first run for the source; we only record a snapshot
                           and emit no undated items.

The LLM is never consulted. Dates come only from :mod:`src.common.dates` /
:mod:`src.crawler.article_dates`; this module just compares them to ``now`` and
applies the snapshot rules.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Set

from ..schemas.enums import DateConfidence
from .dates import ParsedDate, is_fresh, utc_now

VERIFIED_FRESH = "VERIFIED_FRESH"
INFERRED_FRESH = "INFERRED_FRESH"
STALE = "STALE"
FRESHNESS_UNPROVABLE = "FRESHNESS_UNPROVABLE"
UNDATED_BASELINE = "UNDATED_BASELINE"

_ACCEPTED = {VERIFIED_FRESH, INFERRED_FRESH}


def url_hash(url: str) -> str:
    return hashlib.sha256((url or "").strip().encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


@dataclass
class Snapshot:
    """The last COMPLETE listing we saw for a source."""
    last_snapshot_at: Optional[datetime]
    url_hashes: Set[str] = field(default_factory=set)

    @property
    def exists(self) -> bool:
        return self.last_snapshot_at is not None and bool(self.url_hashes)


def snapshot_continuity(prev: Snapshot, current_url_hashes: Set[str]) -> bool:
    """At least one URL from the previous snapshot still appears in the current
    listing. If none overlap, the feed may have rolled past items we never saw,
    so the 'new since last run' bound is not trustworthy."""
    if not prev.exists:
        return False
    return bool(prev.url_hashes & current_url_hashes)


@dataclass
class FreshnessDecision:
    status: str
    # Populated for accepted (fresh) items:
    freshness_status: Optional[str] = None       # VERIFIED_FRESH | INFERRED_FRESH
    published_date: Optional[datetime] = None
    date_confidence: Optional[DateConfidence] = None
    date_source: Optional[str] = None
    evidence: Optional[str] = None

    @property
    def accepted(self) -> bool:
        return self.status in _ACCEPTED


def decide(
    *,
    candidate: Optional[ParsedDate],
    url_hash_: str,
    content_hash_: str,
    seen_url_before: bool,
    seen_content_before: bool,
    first_seen_at: datetime,
    prev_snapshot: Snapshot,
    continuity_ok: bool,
    now: Optional[datetime] = None,
    window_hours: int = 24,
    allow_snapshot: bool = True,
) -> FreshnessDecision:
    """Decide freshness for a single item. See module docstring for the states.

    ``candidate`` is the best publication timestamp already parsed by the caller
    (feed published field, or one from ``article_dates``, or a relative string),
    or ``None``. A coarse/unverifiable relative date (``verifiable is False``)
    is treated as *no usable timestamp* and routed to the snapshot heuristic.
    """
    now = now or utc_now()

    # (a) Trusted publication timestamp path.
    usable = candidate is not None and candidate.ok and candidate.verifiable
    if usable:
        if is_fresh(candidate, now, window_hours):
            return FreshnessDecision(
                status=VERIFIED_FRESH,
                freshness_status=VERIFIED_FRESH,
                published_date=candidate.dt,
                date_confidence=candidate.confidence,
                date_source=candidate.source or "feed_published",
                evidence=f"published {candidate.dt.isoformat()} "
                         f"({candidate.source or 'feed_published'}) within "
                         f"{window_hours}h of {now.isoformat()}",
            )
        return FreshnessDecision(
            status=STALE,
            date_source=candidate.source or "feed_published",
            evidence=f"published {candidate.dt.isoformat()} older than {window_hours}h",
        )

    # (b) No usable timestamp. For sources whose publication date must come from
    # the article itself (date_authority: article), the snapshot heuristic is
    # disallowed — appearing in the feed proves submission, not publication.
    if not allow_snapshot:
        return FreshnessDecision(
            status=FRESHNESS_UNPROVABLE,
            evidence="no article publication date; snapshot inference disabled "
                     "for this source (feed date is not a publication date)",
        )

    # Otherwise fall back to the snapshot heuristic. The first run for a source
    # has no prior complete snapshot: record a baseline and emit nothing.
    if not prev_snapshot.exists:
        return FreshnessDecision(
            status=UNDATED_BASELINE,
            evidence="first run for source; recording baseline snapshot only",
        )

    reasons = []
    if not (now - prev_snapshot.last_snapshot_at <= timedelta(hours=window_hours)):
        reasons.append("previous snapshot older than window")
    if not continuity_ok:
        reasons.append("no feed continuity with previous snapshot")
    if seen_url_before or seen_content_before:
        reasons.append("url/content already seen")
    if reasons:
        return FreshnessDecision(
            status=FRESHNESS_UNPROVABLE,
            evidence="; ".join(reasons),
        )

    return FreshnessDecision(
        status=INFERRED_FRESH,
        freshness_status=INFERRED_FRESH,
        published_date=first_seen_at,
        date_confidence=DateConfidence.HEURISTIC,
        date_source="snapshot",
        evidence=f"new since snapshot at {prev_snapshot.last_snapshot_at.isoformat()} "
                 f"(continuity confirmed); first seen {first_seen_at.isoformat()}",
    )
