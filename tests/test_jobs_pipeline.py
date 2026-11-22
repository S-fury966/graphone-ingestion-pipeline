"""JOBS pipeline tests — offline (FakeFetcher, temp DB, MockProvider, fixed now)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.common.config import SourceConfig
from src.common.freshness import (INFERRED_FRESH, UNDATED_BASELINE,
                                  VERIFIED_FRESH)
from src.crawler.fetcher import FetchResult
from src.crawler.jobs import load_role_table
from src.llm.config import LLMConfig, TierConfig
from src.llm.orchestrator import Orchestrator
from src.llm.providers import MockProvider
from src.pipeline import jobs as J
from src.storage.db import Database

NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
FIX = Path(__file__).parent / "fixtures" / "jobs"
DATE_HDR = {"Date": "Sun, 22 Nov 2026 12:00:00 +0000"}


def fx(name):
    return (FIX / name).read_text(encoding="utf-8")


class FakeFetcher:
    def __init__(self, routes):
        self.routes = routes

    async def fetch_text(self, url, **_):
        r = self.routes.get(url)
        if r is None:
            return FetchResult(url=url, status=404, error="HTTP 404", headers=DATE_HDR)
        return FetchResult(url=url, status=r.get("status", 200), text=r.get("text"),
                           headers=r.get("headers", DATE_HDR), blocked=r.get("blocked", False),
                           error=None if r.get("status", 200) < 400 else "err")


def src(name, **kw):
    base = dict(type="api", base_url="https://api.example.com/x", record_types=["JOB"],
                rate_limit_per_sec=1.0, max_concurrency=1, enabled=True)
    base.update(kw)
    return SourceConfig(name=name, **base)


async def _ingest(db, fetcher, source, now=NOW, window=24, orch=None, max_items=None,
                  dry_run=False):
    stats, rejects = J.Stats(), []
    await J.ingest_source(db, fetcher, source, now, window, stats, rejects, orch,
                          load_role_table(), max_items, {"done": False, "skew": None},
                          dry_run)
    return stats, rejects


def _rows(db):
    return list(db.iter_jobs())


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def _mock_orch(script):
    cfg = LLMConfig(tiers=[TierConfig(name="mock", provider="mock", model="m",
                                      enabled=True, max_input_tokens=4000,
                                      max_output_tokens=512, rpm_limit=1000)])
    return Orchestrator(cfg, providers={"mock": MockProvider(script=script)}, max_calls=30)


# ---- Arbeitnow: epoch date, AI filter, remote api_flag ----
async def test_arbeitnow(db):
    s = src("arbeitnow", base_url="https://www.arbeitnow.com/api/job-board-api",
            ai_filter=True, listing_type="chronological")
    f = FakeFetcher({"https://www.arbeitnow.com/api/job-board-api": {"text": fx("arbeitnow.json")}})
    stats, rejects = await _ingest(db, f, s)
    assert stats.stored == 1                       # ML Engineer only
    assert stats.verified["arbeitnow"] == 1
    reasons = {r.reason for r in rejects}
    assert "NON_AI" in reasons and "STALE" in reasons
    row = _rows(db)[0]
    assert row["title"] == "Machine Learning Engineer"
    assert row["date_source"] == "created_at" and row["freshness_status"] == VERIFIED_FRESH
    assert row["is_remote"] == 1 and row["remote_basis"] == "api_flag"


# ---- RemoteOK: ISO date, all-remote ----
async def test_remoteok(db):
    s = src("remoteok", base_url="https://remoteok.com/api", ai_filter=True)
    f = FakeFetcher({"https://remoteok.com/api": {"text": fx("remoteok.json")}})
    stats, _ = await _ingest(db, f, s)
    assert stats.stored == 1  # Data Scientist fresh; NLP old -> stale
    row = _rows(db)[0]
    assert row["company_raw"] == "DataCorp" and row["role_family"] == "Data"
    assert row["date_source"] == "date"


# ---- Ashby: complete listing, publishedAt, department role, api_flag ----
async def test_ashby(db, monkeypatch):
    monkeypatch.setattr(J, "load_board_slugs",
                        lambda *a, **k: {"ashby": [{"slug": "cohere", "company": "Cohere"}],
                                         "greenhouse": []})
    s = src("ashby", base_url="https://api.ashbyhq.com", listing_type="complete",
            ai_filter=False)
    f = FakeFetcher({"https://api.ashbyhq.com/posting-api/job-board/cohere":
                     {"text": fx("ashby.json")}})
    stats, _ = await _ingest(db, f, s)
    assert stats.stored == 1
    row = _rows(db)[0]
    assert row["role_family"] == "Research" and row["role_basis"] == "department"
    assert row["date_source"] == "publishedAt" and row["remote_basis"] == "api_flag"


# ---- Greenhouse: first_published is used (not updated_at) ----
async def test_greenhouse_first_published(db, monkeypatch):
    monkeypatch.setattr(J, "load_board_slugs",
                        lambda *a, **k: {"greenhouse": [{"slug": "anthropic", "company": "Anthropic"}],
                                         "ashby": []})
    s = src("greenhouse", base_url="https://boards-api.greenhouse.io",
            listing_type="complete", ai_filter=False)
    f = FakeFetcher({"https://boards-api.greenhouse.io/v1/boards/anthropic/jobs":
                     {"text": fx("greenhouse_firstpub.json")}})
    stats, _ = await _ingest(db, f, s)
    assert stats.stored == 1  # SWE fresh; Recruiter old -> stale
    row = _rows(db)[0]
    assert row["title"] == "Software Engineer"
    assert row["date_source"] == "first_published"
    assert row["published_date"] == "2026-11-22T06:00:00+00:00"  # NOT updated_at
    assert row["remote_basis"] == "location_only"  # San Francisco, no remote wording


# ---- Greenhouse updated_at-only: must NOT be verified (undated -> snapshot) ----
async def test_greenhouse_updated_only_not_verified(db, monkeypatch):
    monkeypatch.setattr(J, "load_board_slugs",
                        lambda *a, **k: {"greenhouse": [{"slug": "scaleai", "company": "Scale AI"}],
                                         "ashby": []})
    s = src("greenhouse", base_url="https://boards-api.greenhouse.io",
            listing_type="complete", ai_filter=False)
    f = FakeFetcher({"https://boards-api.greenhouse.io/v1/boards/scaleai/jobs":
                     {"text": fx("greenhouse_updated_only.json")}})
    stats, rejects = await _ingest(db, f, s)
    # First run, undated -> baseline only, nothing verified or stored.
    assert stats.stored == 0 and not stats.verified
    assert any(r.reason == UNDATED_BASELINE for r in rejects)


# ---- complete-listing snapshot logic across runs ----
async def test_complete_listing_snapshot(db, monkeypatch):
    monkeypatch.setattr(J, "load_board_slugs",
                        lambda *a, **k: {"greenhouse": [{"slug": "scaleai", "company": "Scale AI"}],
                                         "ashby": []})
    s = src("greenhouse", base_url="https://boards-api.greenhouse.io",
            listing_type="complete", ai_filter=False)
    url = "https://boards-api.greenhouse.io/v1/boards/scaleai/jobs"
    base = json.loads(fx("greenhouse_updated_only.json"))
    # Run 1: one undated job -> baseline snapshot only.
    f1 = FakeFetcher({url: {"text": json.dumps(base)}})
    s1, _ = await _ingest(db, f1, s, now=NOW)
    assert s1.stored == 0

    # Run 2 (+1h): old job stays, a NEW undated job appears.
    now2 = NOW + timedelta(hours=1)
    two = {"jobs": base["jobs"] + [{"absolute_url": "https://boards.greenhouse.io/scaleai/2",
            "company_name": "Scale AI", "title": "ML Researcher",
            "updated_at": now2.isoformat(), "location": {"name": "Remote"},
            "departments": [{"name": "Research"}]}]}
    f2 = FakeFetcher({url: {"text": json.dumps(two)}})
    s2, rej2 = await _ingest(db, f2, s, now=now2)
    assert s2.stored == 1 and s2.inferred["greenhouse"] == 1  # only the new job
    row = [r for r in _rows(db)][0]
    assert row["freshness_status"] == INFERRED_FRESH and row["date_source"] == "snapshot"
    # the carried-over old job is "already seen" -> unprovable, not re-stored
    assert any(r.reason == "FRESHNESS_UNPROVABLE" for r in rej2)


async def test_snapshot_older_than_24h_rejected(db, monkeypatch):
    monkeypatch.setattr(J, "load_board_slugs",
                        lambda *a, **k: {"greenhouse": [{"slug": "scaleai", "company": "Scale AI"}],
                                         "ashby": []})
    s = src("greenhouse", base_url="https://boards-api.greenhouse.io",
            listing_type="complete", ai_filter=False)
    # Seed a 30h-old snapshot for this board.
    db.save_source_snapshot("greenhouse:scaleai", (NOW - timedelta(hours=30)).isoformat(),
                            ["someoldhash"])
    url = "https://boards-api.greenhouse.io/v1/boards/scaleai/jobs"
    f = FakeFetcher({url: {"text": fx("greenhouse_updated_only.json")}})
    stats, rejects = await _ingest(db, f, s, now=NOW)
    assert stats.stored == 0
    assert any(r.reason == "FRESHNESS_UNPROVABLE" for r in rejects)


async def test_partial_fetch_keeps_snapshot(db, monkeypatch):
    monkeypatch.setattr(J, "load_board_slugs",
                        lambda *a, **k: {"greenhouse": [{"slug": "scaleai", "company": "Scale AI"}],
                                         "ashby": []})
    s = src("greenhouse", base_url="https://boards-api.greenhouse.io",
            listing_type="complete", ai_filter=False)
    db.save_source_snapshot("greenhouse:scaleai", NOW.isoformat(), ["keep"])
    url = "https://boards-api.greenhouse.io/v1/boards/scaleai/jobs"
    f = FakeFetcher({url: {"text": None, "status": 500}})
    stats, rejects = await _ingest(db, f, s, now=NOW)
    assert db.get_source_state("greenhouse:scaleai")["url_hashes"] == ["keep"]
    assert any(r.reason in ("HTTP_ERROR", "ROBOTS") for r in rejects)


# ---- HN grounded extraction via MockProvider ----
async def test_hn_grounded_extraction(db):
    orch = _mock_orch(script=[
        {"company": "Acme AI", "title": "Senior ML Engineer", "is_remote": True,
         "role_family": "Engineering",
         "_evidence": {"company": "Acme AI", "title": "Senior ML Engineer",
                       "is_remote": "Remote (US)", "role_family": "ML Engineer"}},
        {"company": None, "title": None,  # ungrounded / not a job
         "_evidence": {}},
    ])
    s = src("hn-hiring", base_url="https://hn.algolia.com/api/v1/search_by_date",
            uses_llm=True, ai_filter=False)
    from src.crawler.jobs import hn_hiring
    routes = {hn_hiring.THREAD_SEARCH: {"text": fx("hn_thread.json")},
              hn_hiring.comments_url("999", hits=100): {"text": fx("hn_comments.json")}}
    f = FakeFetcher(routes)
    stats, rejects = await _ingest(db, f, s, orch=orch, max_items=100)
    assert stats.stored == 1                      # comment1 kept, comment2 rejected
    assert any(r.reason == "UNGROUNDED" for r in rejects)
    row = _rows(db)[0]
    assert row["company_raw"] == "Acme AI"   # LLM-extracted company kept as raw
    assert row["title"] == "Senior ML Engineer"
    # LLM extracted fields, but the DATE/FRESHNESS are deterministic (created_at):
    assert row["date_source"] == "comment_created_at"
    assert row["freshness_status"] == VERIFIED_FRESH
    assert row["date_confidence"] == "EXPLICIT"
    assert row["is_remote"] == 1


async def test_llm_cannot_change_freshness(db):
    # Even if the LLM 'claims' a company/title, freshness comes only from created_at.
    orch = _mock_orch(script=[
        {"company": "X Corp", "title": "Engineer",
         "_evidence": {"company": "X Corp", "title": "Engineer"}}])
    s = src("hn-hiring", base_url="https://hn.algolia.com/api/v1/search_by_date",
            uses_llm=True)
    from src.crawler.jobs import hn_hiring
    # single top-level comment whose created_at is 40h ago -> must be STALE, not fresh
    old = (NOW - timedelta(hours=40)).isoformat().replace("+00:00", "Z")
    comments = {"hits": [{"objectID": "5", "parent_id": 999, "created_at": old,
                          "comment_text": "X Corp | Engineer | come work"}]}
    routes = {hn_hiring.THREAD_SEARCH: {"text": fx("hn_thread.json")},
              hn_hiring.comments_url("999", hits=100): {"text": json.dumps(comments)}}
    f = FakeFetcher(routes)
    stats, rejects = await _ingest(db, f, s, orch=orch, max_items=100)
    assert stats.stored == 0
    assert any(r.reason == "STALE" for r in rejects)


# ---- dedup across runs ----
async def test_dedup_across_runs(db):
    s = src("arbeitnow", base_url="https://www.arbeitnow.com/api/job-board-api",
            ai_filter=True)
    f = FakeFetcher({"https://www.arbeitnow.com/api/job-board-api": {"text": fx("arbeitnow.json")}})
    s1, _ = await _ingest(db, f, s)
    assert s1.stored == 1
    s2, rej2 = await _ingest(db, f, s)   # same data again
    assert s2.stored == 0
    assert any(r.reason == "DUPLICATE" for r in rej2)


# ---- per-source item cap ----
async def test_max_items_cap(db):
    s = src("arbeitnow", base_url="https://www.arbeitnow.com/api/job-board-api",
            ai_filter=False)  # filter off so all 3 are candidates
    f = FakeFetcher({"https://www.arbeitnow.com/api/job-board-api": {"text": fx("arbeitnow.json")}})
    stats, _ = await _ingest(db, f, s, max_items=1)
    assert stats.seen["arbeitnow"] == 1   # only the first item processed


# ---- future / bad dates ----
async def test_future_and_bad_dates(db):
    s = src("arbeitnow", base_url="https://www.arbeitnow.com/api/job-board-api", ai_filter=False)
    future = int((NOW + timedelta(days=3)).timestamp())
    payload = {"data": [
        {"url": "https://a/f", "company_name": "F", "title": "Eng", "created_at": future,
         "remote": True, "location": "", "tags": []},
        {"url": "https://a/b", "company_name": "B", "title": "Eng", "created_at": "not-a-date",
         "remote": True, "location": "", "tags": []}]}
    f = FakeFetcher({"https://www.arbeitnow.com/api/job-board-api": {"text": json.dumps(payload)}})
    stats, rejects = await _ingest(db, f, s)
    reasons = {r.reason for r in rejects}
    assert "FUTURE_DATE" in reasons and "BAD_DATE" in reasons
    assert stats.stored == 0


# ---- export recomputes freshness + has extra columns ----
async def test_export_recomputes_freshness(db, tmp_path, monkeypatch):
    now_iso = NOW.isoformat()
    # one currently-fresh, one that was fresh when stored but is now >24h old
    db.insert_job({"url_hash": "h1", "url": "https://x/1", "source_name": "ashby",
                   "company_raw": "Cohere", "title": "Eng", "is_remote": 1,
                   "remote_basis": "api_flag", "role_family": "Engineering",
                   "role_basis": "department", "published_date": (NOW - timedelta(hours=2)).isoformat(),
                   "date_confidence": "EXPLICIT", "freshness_status": VERIFIED_FRESH,
                   "date_source": "publishedAt", "content_hash": "c1", "llm_tier": None,
                   "collected_at": now_iso, "first_seen_at": now_iso})
    db.insert_job({"url_hash": "h2", "url": "https://x/2", "source_name": "ashby",
                   "company_raw": "Cohere", "title": "Old", "is_remote": 0,
                   "remote_basis": "location_only", "role_family": "Sales",
                   "role_basis": "department", "published_date": (NOW - timedelta(hours=30)).isoformat(),
                   "date_confidence": "EXPLICIT", "freshness_status": VERIFIED_FRESH,
                   "date_source": "publishedAt", "content_hash": "c2", "llm_tier": None,
                   "collected_at": now_iso, "first_seen_at": now_iso})
    monkeypatch.setattr(J, "CSV_PATH", str(tmp_path / "jobs.csv"))
    monkeypatch.setattr(J, "JSONL_PATH", str(tmp_path / "jobs.jsonl"))
    n = J.export(db, window_hours=24, now=NOW)
    assert n == 1  # the 30h-old one is dropped at export time
    head = Path(tmp_path / "jobs.csv").read_text().splitlines()[0]
    for col in ("freshness_status", "date_source", "remote_basis", "role_basis", "company_raw"):
        assert col in head
    obj = json.loads(Path(tmp_path / "jobs.jsonl").read_text().splitlines()[0])
    assert obj["content"]["title"] == "Eng" and obj["remote_basis"] == "api_flag"
