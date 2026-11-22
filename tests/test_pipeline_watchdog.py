"""The products pipeline progress watchdog (no network).

If no startup finishes within --watchdog-timeout-s, the run must defer the
pending startups and exit cleanly instead of hanging forever.
"""
import argparse
import asyncio

import pytest

import src.pipeline.products as P


class _FakeDB:
    """Minimal stand-in: the watchdog path never reaches real DB work."""
    def __init__(self, *a, **k):
        pass

    def count_products(self):
        return 0

    def set_crawl_status(self, *a, **k):
        pass

    def close(self):
        pass


def _args(**over):
    base = dict(db=":memory:", limit=5, concurrency=2, max_llm_calls=None,
                retry_failed=False, reset_products=False, dry_run=True,
                use_browser=False, browser_concurrency=2,
                watchdog_timeout_s=1.0)
    base.update(over)
    return argparse.Namespace(**base)


async def test_watchdog_breaks_a_stalled_run(monkeypatch, caplog):
    rows = [{"slug": f"s{i}", "website": f"https://s{i}.example",
             "entity_name": f"S{i}", "one_liner": "x"} for i in range(4)]

    monkeypatch.setattr(P, "Database", _FakeDB)
    monkeypatch.setattr(P, "_select_startups", lambda *a, **k: rows)
    monkeypatch.setattr(P, "load_plan_tiers", lambda *a, **k: set())

    async def hanging_crawl(*a, **k):
        await asyncio.Event().wait()   # never returns -> a stalled startup

    monkeypatch.setattr(P, "crawl_startup", hanging_crawl)

    # With a 1s watchdog, run() must return quickly. Without the watchdog this
    # would hang forever and wait_for would raise TimeoutError.
    with caplog.at_level("WARNING"):
        await asyncio.wait_for(P.run(_args()), timeout=15)

    text = caplog.text
    assert "WATCHDOG tripped" in text
    assert "deferring pending startups" in text or "pending startups deferred" in text


async def test_watchdog_disabled_with_zero(monkeypatch):
    """watchdog_timeout_s=0 disables the watchdog (it returns immediately)."""
    rows = [{"slug": "s0", "website": "https://s0.example",
             "entity_name": "S0", "one_liner": "x"}]
    monkeypatch.setattr(P, "Database", _FakeDB)
    monkeypatch.setattr(P, "_select_startups", lambda *a, **k: rows)
    monkeypatch.setattr(P, "load_plan_tiers", lambda *a, **k: set())

    finished = asyncio.Event()

    async def quick_crawl(*a, **k):
        finished.set()
        return P.CrawlResult(slug="s0", status="http_error", pages=[],
                             pages_fetched=0, error="x")

    monkeypatch.setattr(P, "crawl_startup", quick_crawl)
    # No stall here, so even with the watchdog off the run completes normally.
    await asyncio.wait_for(P.run(_args(watchdog_timeout_s=0.0)), timeout=15)
    assert finished.is_set()
