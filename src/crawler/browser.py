"""Async Playwright browser fetcher for JS-rendered pages (Phase V).

Used only when a plain HTTP fetch yields too little text (js_only) and the
source policy allows rendering. Honest and polite by design:

  * one shared Chromium, a small pool of reusable contexts, a global concurrency
    limit and a per-domain token-bucket rate limit (same idea as AsyncFetcher);
  * a realistic, CONSISTENT desktop-Chromium User-Agent, standard viewport,
    configurable locale/timezone — NO stealth plugins, NO fingerprint spoofing,
    NO captcha solving, NO attempt to defeat a challenge;
  * images/fonts/media/known trackers are blocked to save bandwidth;
  * on a detected challenge (Cloudflare "Just a moment", cf-chl, Datadome,
    captcha) we do NOT wait it out or retry — we close the page and return
    blocked=True so the caller can fall back to an alternative source;
  * transient navigation errors (timeouts) get a short backoff retry; a
    challenge never does.

API keys / secrets are never logged.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from ..common.logging import get_logger
from .fetcher import AsyncFetcher, FetchResult, _TokenBucket, detect_block

log = get_logger("crawler.browser")

# A single, descriptive, consistent desktop-Chromium UA (no rotation inside a
# session: a real browser keeps one UA). Honest about being automated-friendly.
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/159.0.0.0 Safari/537.36 GraphOneBot/1.0 (+contact in README)"
)
DEFAULT_VIEWPORT = {"width": 1366, "height": 768}

# Resource types we never need for text extraction.
_BLOCK_RESOURCE_TYPES = {"image", "media", "font"}
# A few well-known analytics/ad hosts to drop (bandwidth + politeness).
_BLOCK_HOST_FRAGMENTS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "facebook.net", "hotjar.com", "segment.io", "mixpanel.com",
    "fullstory.com", "amplitude.com", "intercom.io",
)


@dataclass
class _NavOutcome:
    html: str
    status: int
    final_url: str


class BrowserFetcher:
    """Async context manager wrapping one Chromium + a reusable context pool."""

    def __init__(
        self,
        *,
        global_concurrency: int = 2,
        context_pool_size: int = 2,
        rate_limit_per_sec: float = 0.5,
        respect_robots: bool = True,
        nav_timeout_s: float = 20.0,
        stability_timeout_s: float = 6.0,
        locale: str = "en-US",
        timezone_id: str = "UTC",
        user_agent: str = DEFAULT_UA,
        viewport: Optional[dict] = None,
        block_resources: bool = True,
        max_attempts: int = 2,
        base_delay: float = 0.5,
        robots_fetcher: Optional[AsyncFetcher] = None,
    ) -> None:
        self.global_concurrency = global_concurrency
        self.context_pool_size = max(1, context_pool_size)
        self.rate_limit_per_sec = rate_limit_per_sec
        self.respect_robots = respect_robots
        self.nav_timeout_s = nav_timeout_s
        self.stability_timeout_s = stability_timeout_s
        self.locale = locale
        self.timezone_id = timezone_id
        self.user_agent = user_agent
        self.viewport = viewport or DEFAULT_VIEWPORT
        self.block_resources = block_resources
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        # Robots: share AsyncFetcher's cache when given one; else make a private
        # fetcher just for robots (same code path, same TTL cache).
        self._robots_fetcher = robots_fetcher
        self._owns_robots_fetcher = robots_fetcher is None

        self._pw = None
        self._browser = None
        self._contexts: list = []
        self._ctx_queue: Optional[asyncio.Queue] = None
        self._global_sem: Optional[asyncio.Semaphore] = None
        self._buckets: dict[str, _TokenBucket] = {}
        self._state_lock = asyncio.Lock()
        self._nav_count = 0

    # ---- lifecycle ----
    async def __aenter__(self) -> "BrowserFetcher":
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._global_sem = asyncio.Semaphore(self.global_concurrency)
        self._ctx_queue = asyncio.Queue()
        for _ in range(self.context_pool_size):
            ctx = await self._new_context()
            self._contexts.append(ctx)
            self._ctx_queue.put_nowait(ctx)
        if self._owns_robots_fetcher:
            self._robots_fetcher = AsyncFetcher(respect_robots=True)
            await self._robots_fetcher.__aenter__()
        log.info("browser started", extra={"extra": {
            "contexts": self.context_pool_size,
            "global_concurrency": self.global_concurrency,
            "block_resources": self.block_resources}})
        return self

    async def __aexit__(self, *exc) -> None:
        for ctx in self._contexts:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()
        if self._owns_robots_fetcher and self._robots_fetcher is not None:
            await self._robots_fetcher.close()
        log.info("browser stopped", extra={"extra": {"navigations": self._nav_count}})

    async def _new_context(self):
        ctx = await self._browser.new_context(
            user_agent=self.user_agent, viewport=self.viewport,
            locale=self.locale, timezone_id=self.timezone_id,
            ignore_https_errors=False,
        )
        if self.block_resources:
            await ctx.route("**/*", self._route_handler)
        return ctx

    async def _route_handler(self, route):
        req = route.request
        host = urlsplit(req.url).netloc.lower()
        if (req.resource_type in _BLOCK_RESOURCE_TYPES
                or any(frag in host for frag in _BLOCK_HOST_FRAGMENTS)):
            await route.abort()
        else:
            await route.continue_()

    # ---- rate limiting ----
    async def _bucket(self, domain: str) -> _TokenBucket:
        async with self._state_lock:
            b = self._buckets.get(domain)
            if b is None:
                b = _TokenBucket(self.rate_limit_per_sec, capacity=1.0)
                self._buckets[domain] = b
            return b

    # ---- robots ----
    async def _robots_allowed(self, url: str) -> bool:
        if self._robots_fetcher is None:
            return True
        return await self._robots_fetcher._robots_allowed(url)

    # ---- public API ----
    async def fetch(self, url: str, *, respect_robots: Optional[bool] = None,
                    locale: Optional[str] = None,
                    timezone_id: Optional[str] = None) -> FetchResult:
        started = time.monotonic()
        respect = self.respect_robots if respect_robots is None else respect_robots
        if respect and not await self._robots_allowed(url):
            log.warning("skipped by robots.txt (browser)", extra={"extra": {"url": url}})
            return FetchResult(url=url, error="disallowed by robots.txt", rendered=True,
                               elapsed_ms=(time.monotonic() - started) * 1000)

        domain = urlsplit(url).netloc.lower()
        bucket = await self._bucket(domain)
        last_error: Optional[str] = None

        async with self._global_sem:
            for attempt in range(1, self.max_attempts + 1):
                await bucket.acquire()
                ctx = await self._ctx_queue.get()
                try:
                    outcome = await self._navigate(ctx, url)
                except _Challenge as ch:
                    # Challenge: never retry, never wait it out.
                    log.warning("challenge detected; stopping (no retry)", extra={"extra": {
                        "url": url, "status": ch.status}})
                    return FetchResult(
                        url=url, final_url=ch.final_url, status=ch.status,
                        text=ch.html, blocked=True, rendered=True, attempts=attempt,
                        elapsed_ms=(time.monotonic() - started) * 1000,
                        error=f"blocked ({ch.status})")
                except _Transient as tr:
                    last_error = tr.reason
                    if attempt < self.max_attempts:
                        wait = random.uniform(0, self.base_delay * (2 ** (attempt - 1)))
                        log.warning("transient nav error; retrying", extra={"extra": {
                            "url": url, "reason": tr.reason, "attempt": attempt,
                            "wait_s": round(wait, 3)}})
                        await asyncio.sleep(wait)
                        continue
                    return FetchResult(url=url, error=tr.reason, rendered=True,
                                       attempts=attempt,
                                       elapsed_ms=(time.monotonic() - started) * 1000)
                finally:
                    self._ctx_queue.put_nowait(ctx)

                # success
                self._nav_count += 1
                elapsed = (time.monotonic() - started) * 1000
                blocked = detect_block(outcome.status, outcome.html[:4096])
                res = FetchResult(
                    url=url, final_url=outcome.final_url, status=outcome.status,
                    text=outcome.html, content_type="text/html", rendered=True,
                    blocked=blocked, attempts=attempt, elapsed_ms=elapsed,
                    error=None if outcome.status < 400 else f"HTTP {outcome.status}")
                log.info("rendered", extra={"extra": {
                    "url": url, "status": outcome.status, "attempts": attempt,
                    "elapsed_ms": round(elapsed, 1), "blocked": blocked,
                    "html_chars": len(outcome.html)}})
                return res

        return FetchResult(url=url, error=last_error or "unknown", rendered=True)  # pragma: no cover

    async def _navigate(self, ctx, url: str) -> _NavOutcome:
        from playwright.async_api import Error as PWError, TimeoutError as PWTimeout
        page = await ctx.new_page()
        page.set_default_navigation_timeout(self.nav_timeout_s * 1000)
        try:
            try:
                resp = await page.goto(url, wait_until="domcontentloaded",
                                       timeout=self.nav_timeout_s * 1000)
            except PWTimeout as exc:
                raise _Transient(f"nav timeout: {type(exc).__name__}")
            except PWError as exc:
                raise _Transient(f"nav error: {type(exc).__name__}")
            status = resp.status if resp is not None else 0
            final_url = page.url

            # Early challenge check on the first paint (before spending the
            # stability budget on a page we will reject anyway).
            head = (await page.content())[:4096]
            if detect_block(status, head):
                raise _Challenge(status, page.url, await page.content())

            # Wait for network to settle, else fall back to text stability.
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=self.stability_timeout_s * 1000)
            except PWTimeout:
                await self._wait_text_stable(page)

            html = await page.content()
            if detect_block(status, html[:4096]):
                raise _Challenge(status, page.url, html)
            return _NavOutcome(html=html, status=status, final_url=final_url)
        finally:
            await page.close()

    async def _wait_text_stable(self, page, rounds: int = 3, interval: float = 0.4) -> None:
        """Poll document text length until it stops growing (bounded)."""
        last = -1
        for _ in range(rounds):
            try:
                n = await page.evaluate("document.body ? document.body.innerText.length : 0")
            except Exception:  # noqa: BLE001
                return
            if n == last:
                return
            last = n
            await asyncio.sleep(interval)


# ---- control-flow signals (internal) ----
class _Challenge(Exception):
    def __init__(self, status: int, final_url: str, html: str):
        super().__init__("challenge")
        self.status = status
        self.final_url = final_url
        self.html = html


class _Transient(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
