"""AsyncFetcher tests against a local aiohttp server (no real internet)."""
from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio
from aiohttp import web

from src.crawler.fetcher import AsyncFetcher


# ---------------------------------------------------------------------------
# Local test server
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def server(tmp_path):
    state = {
        "c429": 0, "c500": 0, "secret_hits": 0,
        "inflight": 0, "max_inflight": 0, "rate_times": [],
    }

    async def ok(_req):
        return web.Response(text="ok")

    async def flaky429(_req):
        state["c429"] += 1
        if state["c429"] == 1:
            return web.Response(status=429, headers={"Retry-After": "7"}, text="slow down")
        return web.Response(text="recovered")

    async def flaky500(_req):
        state["c500"] += 1
        if state["c500"] <= 2:
            return web.Response(status=500, text="boom")
        return web.Response(text="recovered")

    async def slow(_req):
        await asyncio.sleep(1.0)
        return web.Response(text="late")

    async def robots(_req):
        return web.Response(text="User-agent: *\nDisallow: /secret\n")

    async def secret(_req):
        state["secret_hits"] += 1
        return web.Response(text="top secret")

    async def concurrency(_req):
        state["inflight"] += 1
        state["max_inflight"] = max(state["max_inflight"], state["inflight"])
        await asyncio.sleep(0.1)
        state["inflight"] -= 1
        return web.Response(text="ok")

    async def rate(_req):
        state["rate_times"].append(time.monotonic())
        return web.Response(text="ok")

    async def cloudflare(_req):
        return web.Response(
            status=200,
            text="<html><head><title>Just a moment...</title></head>"
                 "<body>cf-chl-bypass checking your browser</body></html>",
        )

    async def big(_req):
        return web.Response(body=b"x" * (200 * 1024))  # 200 KB

    async def as_json(_req):
        return web.json_response({"hello": "world", "n": 3})

    app = web.Application()
    app.add_routes([
        web.get("/ok", ok),
        web.get("/flaky429", flaky429),
        web.get("/flaky500", flaky500),
        web.get("/slow", slow),
        web.get("/robots.txt", robots),
        web.get("/secret", secret),
        web.get("/concurrency", concurrency),
        web.get("/rate", rate),
        web.get("/cloudflare", cloudflare),
        web.get("/big", big),
        web.get("/json", as_json),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    base = f"http://127.0.0.1:{port}"
    try:
        yield base, state
    finally:
        await runner.cleanup()


def make_fetcher(tmp_path, **kw):
    kw.setdefault("rate_limit_per_sec", 1000)  # effectively no rate wait
    kw.setdefault("cache_dir", tmp_path / "cache")
    return AsyncFetcher(**kw)


async def fast_sleep(_seconds):  # patched-in: records nothing, returns instantly
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
async def test_429_retry_after_then_200(server, tmp_path):
    base, _ = server
    waits = []

    async def record(seconds):
        waits.append(seconds)

    async with make_fetcher(tmp_path) as f:
        f._sleep = record
        res = await f.fetch_text(f"{base}/flaky429")

    assert res.status == 200
    assert res.text == "recovered"
    assert res.attempts == 2
    assert any(w >= 7 for w in waits)  # Retry-After honored


async def test_500_twice_then_200(server, tmp_path):
    base, _ = server
    async with make_fetcher(tmp_path, max_attempts=3) as f:
        f._sleep = fast_sleep
        res = await f.fetch_text(f"{base}/flaky500")
    assert res.status == 200
    assert res.attempts == 3


async def test_timeout_returns_error(server, tmp_path):
    base, _ = server
    async with make_fetcher(tmp_path, max_attempts=2) as f:
        f._sleep = fast_sleep
        res = await f.fetch_text(f"{base}/slow", total_timeout=0.2)
    assert res.error is not None
    assert res.status == 0
    assert res.attempts == 2


async def test_robots_disallow_is_skipped(server, tmp_path):
    base, state = server
    async with make_fetcher(tmp_path, respect_robots=True) as f:
        res = await f.fetch_text(f"{base}/secret")
    assert res.error == "disallowed by robots.txt"
    assert state["secret_hits"] == 0  # server never hit


async def test_domain_concurrency_limit(server, tmp_path):
    base, state = server
    async with make_fetcher(tmp_path, domain_concurrency=2) as f:
        await f.fetch_many([f"{base}/concurrency"] * 6)
    assert state["max_inflight"] <= 2


async def test_rate_limit_spacing(server, tmp_path):
    base, state = server
    # Real sleeps here: 5 req/s => >= ~0.2s apart.
    async with make_fetcher(tmp_path, rate_limit_per_sec=5, domain_concurrency=5) as f:
        await f.fetch_many([f"{base}/rate"] * 4)
    times = sorted(state["rate_times"])
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps, "expected multiple requests"
    assert min(gaps) >= 0.15  # allow small tolerance under 0.2


async def test_cloudflare_challenge_flagged_blocked(server, tmp_path):
    base, _ = server
    async with make_fetcher(tmp_path) as f:
        res = await f.fetch_text(f"{base}/cloudflare")
    assert res.blocked is True
    assert res.status == 200  # we do not bypass, only flag


async def test_oversized_response_truncated(server, tmp_path):
    base, _ = server
    async with make_fetcher(tmp_path) as f:
        res = await f.fetch_text(f"{base}/big", max_bytes=1024)
    assert res.error is None
    assert res.text is not None
    assert len(res.text) <= 1024  # truncated, did not crash


async def test_fetch_many_one_failure_others_survive(server, tmp_path):
    base, _ = server
    async with make_fetcher(tmp_path, max_attempts=1) as f:
        f._sleep = fast_sleep
        results = await f.fetch_many([
            f"{base}/ok",
            "http://127.0.0.1:1/nope",  # connection refused
            f"{base}/ok",
        ])
    assert len(results) == 3
    assert results[0].status == 200
    assert results[1].error is not None
    assert results[2].status == 200


async def test_fetch_json(server, tmp_path):
    base, _ = server
    async with make_fetcher(tmp_path) as f:
        res = await f.fetch_json(f"{base}/json")
    assert res.ok
    assert res.json == {"hello": "world", "n": 3}


async def test_disk_cache_hit(server, tmp_path):
    base, _ = server
    async with make_fetcher(tmp_path) as f:
        first = await f.fetch_text(f"{base}/ok", use_cache=True)
        second = await f.fetch_text(f"{base}/ok", use_cache=True)
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.text == "ok"
