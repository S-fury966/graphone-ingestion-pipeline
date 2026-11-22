"""NEWS pipeline tests — fully offline (fake fetcher, temp DB, fixed `now`)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.common.config import SourceConfig
from src.common.freshness import (INFERRED_FRESH, UNDATED_BASELINE,
                                  VERIFIED_FRESH)
from src.crawler.fetcher import FetchResult
from src.llm.config import LLMConfig, TierConfig
from src.llm.orchestrator import Orchestrator
from src.llm.providers import MockProvider
from src.pipeline import news as N
from src.storage.db import Database

NOW = datetime(2026, 11, 21, 21, 0, 0, tzinfo=timezone.utc)


class FakeFetcher:
    """Serves canned responses by URL; records nothing to the network."""

    def __init__(self, routes: dict):
        self.routes = routes  # url -> dict(text=, status=, headers=, blocked=)

    async def fetch_text(self, url, **_):
        r = self.routes.get(url)
        if r is None:
            return FetchResult(url=url, status=404, error="HTTP 404")
        return FetchResult(url=url, status=r.get("status", 200), text=r.get("text"),
                           headers=r.get("headers", {"Date": "Sat, 21 Nov 2026 21:00:00 +0000"}),
                           blocked=r.get("blocked", False),
                           error=None if r.get("status", 200) < 400 else "err")


def _src(name, **kw):
    base = dict(type="rss", base_url="https://feed.example.com/f.xml",
                record_types=["NEWS"], rate_limit_per_sec=1.0, max_concurrency=1,
                enabled=True)
    base.update(kw)
    return SourceConfig(name=name, **base)


def _rss(items):
    body = "".join(
        f"<item><title>{t}</title><link>{l}</link>"
        f"{('<pubDate>' + d + '</pubDate>') if d else ''}"
        f"<content:encoded>{b}</content:encoded></item>"
        for (t, l, d, b) in items)
    return ('<rss xmlns:content="http://purl.org/rss/1.0/modules/content/">'
            f"<channel>{body}</channel></rss>")


async def _process(db, fetcher, src, now=NOW, window=24, **kw):
    stats = N.Stats()
    await N.process_source(db, fetcher, src, stats, now, window,
                           {"done": False}, **kw)
    return stats


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


# ---- VERIFIED_FRESH from the feed's own date ----
async def test_feed_date_verified_fresh(db):
    src = _src("tc", base_url="https://feed.example.com/f.xml",
              full_text_in_feed=True, article_crawl=False)
    feed = _rss([
        ("Fresh AI story", "https://x.com/a",
         "Sat, 21 Nov 2026 18:00:00 +0000", "A long body about models " * 10),
        ("Old story", "https://x.com/b",
         "Mon, 17 Nov 2026 10:00:00 +0000", "stale body " * 10),
    ])
    f = FakeFetcher({"https://feed.example.com/f.xml": {"text": feed}})
    stats = await _process(db, f, src)
    assert stats.stored == 1
    assert stats.decisions[VERIFIED_FRESH] == 1
    row = list(db.iter_news())[0]
    assert row["freshness_status"] == VERIFIED_FRESH
    assert row["date_source"] == "feed_published"
    assert row["date_confidence"] == "EXPLICIT"
    assert row["published_date"] == "2026-11-21T18:00:00+00:00"


# ---- date from article JSON-LD when the feed has none ----
async def test_article_jsonld_backfill(db):
    src = _src("blog", article_crawl=True, full_text_in_feed=False)
    feed = _rss([("Undated feed item", "https://p.com/post", None, "")])
    article = """<html><head>
      <script type="application/ld+json">
      {"@type":"Article","datePublished":"2026-11-21T12:00:00Z",
       "dateModified":"2026-11-21T20:00:00Z"}</script></head>
      <body><article>Real full article text here about AI.</article></body></html>"""
    f = FakeFetcher({"https://feed.example.com/f.xml": {"text": feed},
                     "https://p.com/post": {"text": article}})
    stats = await _process(db, f, src)
    assert stats.stored == 1
    row = list(db.iter_news())[0]
    assert row["date_source"] == "jsonld"
    assert row["published_date"] == "2026-11-21T12:00:00+00:00"
    assert "Real full article text" in row["full_text"]


# ---- snapshot heuristic across two runs ----
async def test_snapshot_baseline_then_inferred(db):
    src = _src("undated", full_text_in_feed=True, article_crawl=False)
    run1 = _rss([("Item A", "https://u.com/a", None, "body a " * 10),
                 ("Item B", "https://u.com/b", None, "body b " * 10)])
    f1 = FakeFetcher({"https://feed.example.com/f.xml": {"text": run1}})
    s1 = await _process(db, f1, src, now=NOW)
    assert s1.stored == 0
    assert s1.decisions[UNDATED_BASELINE] == 2
    assert db.get_source_state("undated") is not None  # baseline snapshot saved

    # Run 2 one hour later: B carries over (continuity), C is new.
    now2 = NOW + timedelta(hours=1)
    run2 = _rss([("Item B", "https://u.com/b", None, "body b " * 10),
                 ("Item C", "https://u.com/c", None, "body c " * 10)])
    f2 = FakeFetcher({"https://feed.example.com/f.xml": {"text": run2}})
    s2 = await _process(db, f2, src, now=now2)
    assert s2.decisions[INFERRED_FRESH] == 1       # only C
    assert s2.stored == 1
    row = list(db.iter_news())[0]
    assert row["date_source"] == "snapshot"
    assert row["date_confidence"] == "HEURISTIC"
    assert row["url"] == "https://u.com/c"


async def test_snapshot_no_continuity_unprovable(db):
    src = _src("undated2", full_text_in_feed=True, article_crawl=False)
    # Seed a stale, non-overlapping snapshot.
    db.save_source_snapshot("undated2", NOW.isoformat(), ["old1", "old2"])
    run = _rss([("New only", "https://z.com/new", None, "text " * 10)])
    f = FakeFetcher({"https://feed.example.com/f.xml": {"text": run}})
    s = await _process(db, f, src, now=NOW)
    # No overlap with {old1,old2} -> continuity fails -> not stored.
    assert s.stored == 0
    assert s.decisions["FRESHNESS_UNPROVABLE"] == 1


# ---- AI keyword filter ----
async def test_ai_filter_drops_non_ai(db):
    src = _src("ars", ai_filter=True, full_text_in_feed=True, article_crawl=False)
    feed = _rss([
        ("New AI model released", "https://a.com/ai",
         "Sat, 21 Nov 2026 18:00:00 +0000", "about machine learning " * 10),
        ("Gardening tips", "https://a.com/garden",
         "Sat, 21 Nov 2026 18:00:00 +0000", "about tomatoes " * 10),
    ])
    f = FakeFetcher({"https://feed.example.com/f.xml": {"text": feed}})
    stats = await _process(db, f, src)
    assert stats.ai_filtered == 1 and stats.stored == 1
    assert list(db.iter_news())[0]["url"] == "https://a.com/ai"


# ---- the pipeline is LLM-free: dates/freshness are purely deterministic ----
async def test_pipeline_makes_no_llm_calls(db):
    # A MockProvider wired into an Orchestrator must receive ZERO calls: the news
    # pipeline never consults an LLM for dates, freshness, or text.
    cfg = LLMConfig(tiers=[TierConfig(name="mock", provider="mock", model="m",
                                      enabled=True, max_input_tokens=4000,
                                      max_output_tokens=512, rpm_limit=1000)])
    mock = MockProvider(script=[{"summary": "should never be used"}])
    orch = Orchestrator(cfg, providers={"mock": mock}, max_calls=10)
    src = _src("tc2", full_text_in_feed=True, article_crawl=False)
    feed = _rss([("Fresh AI", "https://x.com/s",
                  "Sat, 21 Nov 2026 18:00:00 +0000", "ai body " * 20)])
    f = FakeFetcher({"https://feed.example.com/f.xml": {"text": feed}})
    stats = await _process(db, f, src)   # no orchestrator passed in
    assert stats.stored == 1
    row = list(db.iter_news())[0]
    assert row["published_date"] == "2026-11-21T18:00:00+00:00"
    assert row["date_confidence"] == "EXPLICIT"
    assert row["date_source"] == "feed_published"
    assert row["summary"] is None
    assert orch.calls_made == 0 and mock.calls == 0


# ---- failed fetch must not overwrite the last good snapshot ----
async def test_failed_fetch_keeps_snapshot(db):
    src = _src("flaky", full_text_in_feed=True, article_crawl=False)
    db.save_source_snapshot("flaky", NOW.isoformat(), ["keep1", "keep2"])
    f = FakeFetcher({"https://feed.example.com/f.xml": {"text": None, "status": 500}})
    stats = await _process(db, f, src)
    assert stats.fetch_errors == 1
    state = db.get_source_state("flaky")
    assert state["url_hashes"] == ["keep1", "keep2"]  # unchanged


# ---- export writes the two extra columns ----
async def test_export_has_extra_columns(db, tmp_path, monkeypatch):
    src = _src("tc3", full_text_in_feed=True, article_crawl=False)
    feed = _rss([("Fresh AI", "https://x.com/e",
                  "Sat, 21 Nov 2026 18:00:00 +0000", "ai body " * 20)])
    f = FakeFetcher({"https://feed.example.com/f.xml": {"text": feed}})
    await _process(db, f, src)
    monkeypatch.setattr(N, "CSV_PATH", str(tmp_path / "news.csv"))
    monkeypatch.setattr(N, "JSONL_PATH", str(tmp_path / "news.jsonl"))
    N.export(db)
    csv_head = Path(tmp_path / "news.csv").read_text().splitlines()[0]
    assert "freshness_status" in csv_head and "date_source" in csv_head
    line = Path(tmp_path / "news.jsonl").read_text().splitlines()[0]
    obj = json.loads(line)
    assert obj["freshness_status"] == VERIFIED_FRESH
    assert obj["date_source"] == "feed_published"
    assert obj["content"]["published_date"].startswith("2026-11-21T18:00:00")
