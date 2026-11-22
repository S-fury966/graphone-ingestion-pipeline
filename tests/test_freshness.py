"""Freshness decision tests — pure, deterministic, fixed `now`."""
from datetime import datetime, timedelta, timezone

from src.common.dates import parse_date
from src.common.freshness import (FRESHNESS_UNPROVABLE, INFERRED_FRESH, STALE,
                                  UNDATED_BASELINE, VERIFIED_FRESH, Snapshot,
                                  content_hash, decide, snapshot_continuity,
                                  url_hash)
from src.schemas.enums import DateConfidence

NOW = datetime(2026, 11, 21, 21, 0, 0, tzinfo=timezone.utc)


def _snap(hours_ago=1.0, hashes=("u1", "u2")):
    return Snapshot(last_snapshot_at=NOW - timedelta(hours=hours_ago),
                    url_hashes=set(hashes))


def _decide(candidate=None, prev=None, continuity_ok=True,
            seen_url=False, seen_content=False, first_seen=NOW):
    return decide(candidate=candidate, url_hash_="uX", content_hash_="cX",
                  seen_url_before=seen_url, seen_content_before=seen_content,
                  first_seen_at=first_seen, prev_snapshot=prev or _snap(),
                  continuity_ok=continuity_ok, now=NOW, window_hours=24)


# ---- (a) trusted timestamp ----
def test_verified_fresh_explicit():
    cand = parse_date("2026-11-21T10:00:00Z", now=NOW)
    cand.source = "feed_published"
    d = _decide(candidate=cand)
    assert d.status == VERIFIED_FRESH and d.accepted
    assert d.date_confidence == DateConfidence.EXPLICIT
    assert d.published_date == cand.dt and d.date_source == "feed_published"


def test_stale_explicit():
    cand = parse_date("2026-11-18T10:00:00Z", now=NOW)
    d = _decide(candidate=cand)
    assert d.status == STALE and not d.accepted


def test_relative_verified_fresh():
    cand = parse_date("3 hours ago", now=NOW)
    cand.source = "relative_text"
    d = _decide(candidate=cand)
    assert d.status == VERIFIED_FRESH
    assert d.date_confidence == DateConfidence.RELATIVE


def test_coarse_relative_routes_to_snapshot():
    # "yesterday" is unverifiable -> ignored as a timestamp -> snapshot path.
    cand = parse_date("yesterday", now=NOW)
    d = _decide(candidate=cand)        # fresh snapshot, continuity, unseen
    assert d.status == INFERRED_FRESH


def test_future_candidate_ignored_then_snapshot():
    cand = parse_date("2026-11-30T00:00:00Z", now=NOW)  # status FUTURE_DATE
    d = _decide(candidate=cand)
    assert d.status == INFERRED_FRESH  # fell through to snapshot path


# ---- (b) snapshot heuristic ----
def test_inferred_fresh_all_conditions():
    d = _decide(candidate=None)
    assert d.status == INFERRED_FRESH and d.accepted
    assert d.date_confidence == DateConfidence.HEURISTIC
    assert d.date_source == "snapshot" and d.published_date == NOW
    assert "continuity" in d.evidence


def test_first_run_is_baseline():
    d = _decide(candidate=None, prev=Snapshot(last_snapshot_at=None))
    assert d.status == UNDATED_BASELINE and not d.accepted


def test_snapshot_too_old():
    d = _decide(candidate=None, prev=_snap(hours_ago=30))
    assert d.status == FRESHNESS_UNPROVABLE
    assert "older than window" in d.evidence


def test_no_continuity():
    d = _decide(candidate=None, continuity_ok=False)
    assert d.status == FRESHNESS_UNPROVABLE
    assert "continuity" in d.evidence


def test_already_seen():
    d = _decide(candidate=None, seen_url=True)
    assert d.status == FRESHNESS_UNPROVABLE
    assert "already seen" in d.evidence
    d2 = _decide(candidate=None, seen_content=True)
    assert d2.status == FRESHNESS_UNPROVABLE


# ---- helpers ----
def test_continuity_helper():
    prev = Snapshot(last_snapshot_at=NOW, url_hashes={"a", "b"})
    assert snapshot_continuity(prev, {"b", "c"}) is True
    assert snapshot_continuity(prev, {"c", "d"}) is False
    assert snapshot_continuity(Snapshot(None, set()), {"a"}) is False


def test_hash_helpers_stable():
    assert url_hash("https://x.com/a") == url_hash(" https://x.com/a ")
    assert content_hash("abc") != content_hash("abd")
