"""Phase V anti-bot tests. No real internet except a LOCAL aiohttp server."""
from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from src.crawler.browser import BrowserFetcher, _Challenge, _NavOutcome, _Transient
from src.crawler.fetcher import AsyncFetcher, FetchResult, detect_block
from src.crawler.ladder import (BLOCKED_BY_CHALLENGE, HTTP_ERROR, JS_ONLY, OK,
                                RENDERED_OK, ROBOTS_DISALLOWED, STILL_JS_ONLY,
                                LadderPolicy, fetch_with_ladder)

SHORT_HTML = "<html><body><h1>Hi</h1><p>short</p></body></html>"           # js_only
LONG_HTML = "<html><body><p>" + ("word " * 120) + "</p></body></html>"     # plenty of text
CF_HTML = ("<html><head><title>Just a moment...</title></head><body>"
           "<div class='cf-chl'>Checking your browser</div></body></html>")


# ======================= challenge detection on fixtures =======================
@pytest.mark.parametrize("marker", [
    "<title>Just a moment...</title>", "cf-chl-bypass", "please enable javascript and cookies",
    "DataDome", "Please complete the captcha", "attention required! | cloudflare",
])
def test_detect_block_markers(marker):
    assert detect_block(200, f"<html>{marker}</html>") is True


def test_detect_block_status_403_503():
    assert detect_block(403, "normal page") is True
    assert detect_block(503, "normal page") is True


def test_detect_block_clean_page_is_false():
    assert detect_block(200, "<html><body>A normal AI startup landing page</body></html>") is False


# ======================= ladder logic (fake fetcher + browser) =================
class _FakeHTTP:
    def __init__(self, result: FetchResult):
        self.result = result
        self.calls = 0

    async def fetch_text(self, url, **kw):
        self.calls += 1
        return self.result


class _FakeBrowser:
    def __init__(self, result: FetchResult):
        self.result = result
        self.calls = 0

    async def fetch(self, url, **kw):
        self.calls += 1
        return self.result


def _ok(text):
    return FetchResult(url="u", status=200, text=text, final_url="u")


async def test_ladder_l0_enough_text_is_ok():
    http = _FakeHTTP(_ok(LONG_HTML))
    r = await fetch_with_ladder("u", LadderPolicy(render="auto"), http)
    assert r.status == OK and r.final_level == "L0" and r.levels_tried == ["L0"]


async def test_ladder_escalates_js_only_to_browser():
    http = _FakeHTTP(_ok(SHORT_HTML))
    browser = _FakeBrowser(FetchResult(url="u", status=200, text=LONG_HTML, rendered=True))
    r = await fetch_with_ladder("u", LadderPolicy(render="auto"), http, browser)
    assert r.status == RENDERED_OK and r.final_level == "L1"
    assert r.levels_tried == ["L0", "L1"] and r.rendered
    assert r.text_len_l0 < 300 and r.text_len_l1 >= 300
    assert browser.calls == 1


async def test_ladder_render_never_does_not_render():
    http = _FakeHTTP(_ok(SHORT_HTML))
    browser = _FakeBrowser(_ok(LONG_HTML))
    r = await fetch_with_ladder("u", LadderPolicy(render="never"), http, browser)
    assert r.status == JS_ONLY and r.final_level == "L0"
    assert browser.calls == 0                      # never rendered


async def test_ladder_stops_on_challenge_at_l0_no_browser_call():
    http = _FakeHTTP(FetchResult(url="u", status=403, text=CF_HTML, blocked=True))
    browser = _FakeBrowser(_ok(LONG_HTML))
    r = await fetch_with_ladder("u", LadderPolicy(render="auto"), http, browser)
    assert r.status == BLOCKED_BY_CHALLENGE and r.final_level == "L2"
    assert r.blocked and browser.calls == 0        # no escalation, no retry


async def test_ladder_stops_on_challenge_at_l1():
    http = _FakeHTTP(_ok(SHORT_HTML))
    browser = _FakeBrowser(FetchResult(url="u", status=200, text=CF_HTML,
                                       blocked=True, rendered=True))
    r = await fetch_with_ladder("u", LadderPolicy(render="auto"), http, browser)
    assert r.status == BLOCKED_BY_CHALLENGE and r.final_level == "L2"


async def test_ladder_respects_robots():
    http = _FakeHTTP(FetchResult(url="u", error="disallowed by robots.txt"))
    r = await fetch_with_ladder("u", LadderPolicy(render="auto"), http)
    assert r.status == ROBOTS_DISALLOWED


async def test_ladder_render_required_but_no_browser():
    http = _FakeHTTP(_ok(SHORT_HTML))
    r = await fetch_with_ladder("u", LadderPolicy(render="auto"), http, None)
    assert r.status == JS_ONLY and "no browser" in r.reason


# ======== BrowserFetcher retry-vs-challenge (manual state, no real browser) ====
def _bare_browser(**kw):
    """A BrowserFetcher with just enough internal state to call fetch(), without
    launching a real Chromium (we monkeypatch _navigate)."""
    b = BrowserFetcher(respect_robots=False, **kw)
    b._global_sem = asyncio.Semaphore(b.global_concurrency)
    b._ctx_queue = asyncio.Queue()
    for _ in range(b.context_pool_size):
        b._ctx_queue.put_nowait(object())      # placeholder context, never used
    return b


async def test_browser_retries_transient_then_succeeds(monkeypatch):
    b = _bare_browser(max_attempts=3, base_delay=0.0)
    calls = {"n": 0}

    async def fake_nav(ctx, url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Transient("nav timeout")
        return _NavOutcome(html=LONG_HTML, status=200, final_url=url)

    monkeypatch.setattr(b, "_navigate", fake_nav)
    r = await b.fetch("http://x", respect_robots=False)
    assert r.rendered and r.status == 200 and r.attempts == 2
    assert calls["n"] == 2                          # retried once


async def test_browser_no_retry_on_challenge(monkeypatch):
    b = _bare_browser(max_attempts=3, base_delay=0.0)
    calls = {"n": 0}

    async def fake_nav(ctx, url):
        calls["n"] += 1
        raise _Challenge(403, url, CF_HTML)

    monkeypatch.setattr(b, "_navigate", fake_nav)
    r = await b.fetch("http://x", respect_robots=False)
    assert r.blocked and r.attempts == 1
    assert calls["n"] == 1                          # challenge => NO retry


async def test_browser_honours_concurrency_limit(monkeypatch):
    b = _bare_browser(global_concurrency=2, context_pool_size=4, base_delay=0.0)
    state = {"cur": 0, "max": 0}

    async def fake_nav(ctx, url):
        state["cur"] += 1
        state["max"] = max(state["max"], state["cur"])
        await asyncio.sleep(0.02)
        state["cur"] -= 1
        return _NavOutcome(html=LONG_HTML, status=200, final_url=url)

    monkeypatch.setattr(b, "_navigate", fake_nav)
    await asyncio.gather(*(b.fetch(f"http://x/{i}", respect_robots=False) for i in range(8)))
    assert state["max"] <= 2                         # never exceeded the limit


# ============================ LOCAL SERVER (real browser) ======================
@pytest.fixture
async def server():
    requested: list[str] = []

    async def robots(_):
        return web.Response(text="User-agent: *\nAllow: /\n")

    async def static_page(_):
        return web.Response(text="<html><body><p>" + ("content " * 80) + "</p></body></html>",
                            content_type="text/html")

    async def js_page(_):
        # Almost no server text; a script injects the real content after load.
        body = ("<html><body><div id='app'></div>"
                "<script>document.getElementById('app').innerHTML="
                "'<h1>Rendered</h1><p>" + ("rendered content " * 60) + "</p>';"
                "</script></body></html>")
        return web.Response(text=body, content_type="text/html")

    async def challenge_page(_):
        return web.Response(text=CF_HTML, content_type="text/html", status=503)

    async def with_img(_):
        return web.Response(
            text="<html><body><p>" + ("text " * 80) + "</p>"
                 "<img src='/img.png'></body></html>", content_type="text/html")

    async def img(_):
        requested.append("/img.png")
        return web.Response(body=b"\x89PNG\r\n", content_type="image/png")

    app = web.Application()
    app.router.add_get("/robots.txt", robots)
    app.router.add_get("/static", static_page)
    app.router.add_get("/js", js_page)
    app.router.add_get("/challenge", challenge_page)
    app.router.add_get("/withimg", with_img)
    app.router.add_get("/img.png", img)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = list(runner.addresses)[0][1]
    base = f"http://127.0.0.1:{port}"
    yield base, requested
    await runner.cleanup()


async def test_ladder_real_browser_renders_js_page(server):
    base, _ = server
    async with AsyncFetcher(respect_robots=False) as http, \
            BrowserFetcher(global_concurrency=1, context_pool_size=1,
                           respect_robots=False, robots_fetcher=http) as br:
        r = await fetch_with_ladder(f"{base}/js", LadderPolicy(render="auto",
                                    respect_robots=False), http, br)
    assert r.status == RENDERED_OK and r.final_level == "L1"
    assert r.text_len_l0 < 300 < r.text_len_l1
    assert "rendered content" in r.text


async def test_real_browser_detects_challenge_and_stops(server):
    base, _ = server
    async with BrowserFetcher(global_concurrency=1, context_pool_size=1,
                              respect_robots=False) as br:
        r = await br.fetch(f"{base}/challenge", respect_robots=False)
    assert r.blocked and r.attempts == 1            # detected, no retry


async def test_real_browser_blocks_images(server):
    base, requested = server
    async with BrowserFetcher(global_concurrency=1, context_pool_size=1,
                              respect_robots=False, block_resources=True) as br:
        r = await br.fetch(f"{base}/withimg", respect_robots=False)
    assert r.rendered
    assert "/img.png" not in requested              # image request was aborted
