"""Shared async HTTP fetcher reused by every scraper.

One AsyncFetcher wraps a single aiohttp.ClientSession and provides:
  - global + per-domain concurrency limits
  - per-domain token-bucket rate limiting
  - robots.txt fetching/caching (switchable per source)
  - retries with exponential backoff + jitter (honoring Retry-After)
  - rotating realistic User-Agents
  - connect/total timeouts + a hard response-size cap
  - optional on-disk cache (off by default)
  - block detection (Cloudflare/Datadome/captcha markers) -> result.blocked

It never raises on a normal HTTP failure; it returns a FetchResult with
`error` set. It only raises on programmer errors.
"""
from __future__ import annotations

import asyncio
import hashlib
import json as jsonlib
import random
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import aiohttp

from ..common.logging import get_logger

log = get_logger("crawler.fetcher")

# ---------------------------------------------------------------------------
# Defaults / constants
# ---------------------------------------------------------------------------
DEFAULT_MAX_BYTES = 5 * 1024 * 1024          # 5 MB
DEFAULT_GLOBAL_CONCURRENCY = 50
DEFAULT_DOMAIN_CONCURRENCY = 4
DEFAULT_RATE_PER_SEC = 2.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 30.0
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_TOTAL_TIMEOUT = 30.0
DEFAULT_ROBOTS_TTL = 3600.0
DEFAULT_CACHE_TTL = 3600.0

ROBOTS_AGENT = "GraphOneBot"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

USER_AGENTS: Sequence[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
)

# Lowercased challenge markers used to flag a blocked/challenge page.
BLOCK_MARKERS: Sequence[str] = (
    "just a moment",
    "cf-chl",
    "cf-browser-verification",
    "attention required! | cloudflare",
    "checking your browser",
    "datadome",
    "captcha",
    "please enable javascript and cookies",
)


def detect_block(status: int, body_head: str) -> bool:
    """Shared challenge/block detection (used by AsyncFetcher and BrowserFetcher).

    A hard 403/503, or any known Cloudflare/Datadome/captcha marker in the first
    few KB of the body, counts as a challenge. We never try to solve it.
    """
    if status in (403, 503):
        return True
    low = (body_head or "").lower()
    return any(marker in low for marker in BLOCK_MARKERS)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass
class FetchResult:
    url: str
    final_url: Optional[str] = None
    status: int = 0
    text: Optional[str] = None
    content_type: Optional[str] = None
    headers: Mapping[str, str] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    attempts: int = 0
    from_cache: bool = False
    blocked: bool = False
    error: Optional[str] = None
    # True when the HTML came from a rendered browser page (BrowserFetcher).
    rendered: bool = False
    # Deviation (documented): extra convenience field populated by fetch_json().
    json: Any = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300


# ---------------------------------------------------------------------------
# Token bucket (per-domain rate limiting)
# ---------------------------------------------------------------------------
class _TokenBucket:
    """Async token bucket. capacity=1 => strict spacing of 1/rate seconds."""

    def __init__(
        self,
        rate: float,
        capacity: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate = max(rate, 1e-6)
        self.capacity = max(capacity, 1.0)
        self.tokens = self.capacity
        self._updated = time_func()
        self._sleep = sleep
        self._time = time_func
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._time()
            self.tokens = min(self.capacity, self.tokens + (now - self._updated) * self.rate)
            self._updated = now
            if self.tokens < 1.0:
                wait = (1.0 - self.tokens) / self.rate
                await self._sleep(wait)
                self.tokens = 0.0
                self._updated = self._time()
            else:
                self.tokens -= 1.0


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------
class AsyncFetcher:
    def __init__(
        self,
        *,
        global_concurrency: int = DEFAULT_GLOBAL_CONCURRENCY,
        domain_concurrency: int = DEFAULT_DOMAIN_CONCURRENCY,
        rate_limit_per_sec: float = DEFAULT_RATE_PER_SEC,
        respect_robots: bool = True,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_delay: float = DEFAULT_BASE_DELAY,
        max_delay: float = DEFAULT_MAX_DELAY,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        rate_capacity: float = 1.0,
        robots_ttl: float = DEFAULT_ROBOTS_TTL,
        cache_dir: str | Path = "data/cache",
        cache_ttl: float = DEFAULT_CACHE_TTL,
        user_agents: Sequence[str] = USER_AGENTS,
        robots_agent: str = ROBOTS_AGENT,
    ) -> None:
        self.global_concurrency = global_concurrency
        self.domain_concurrency = domain_concurrency
        self.rate_limit_per_sec = rate_limit_per_sec
        self.respect_robots = respect_robots
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.connect_timeout = connect_timeout
        self.total_timeout = total_timeout
        self.max_bytes = max_bytes
        self.rate_capacity = rate_capacity
        self.robots_ttl = robots_ttl
        self.cache_dir = Path(cache_dir)
        self.cache_ttl = cache_ttl
        self.user_agents = list(user_agents)
        self.robots_agent = robots_agent

        # Injectable sleeps so tests run fast / can assert wait times.
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep       # retry backoff
        self._rate_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep  # rate limiter

        self._session: Optional[aiohttp.ClientSession] = None
        self._global_sem: Optional[asyncio.Semaphore] = None
        self._domain_sems: dict[str, asyncio.Semaphore] = {}
        self._buckets: dict[str, _TokenBucket] = {}
        self._robots: dict[str, tuple[Optional[RobotFileParser], float]] = {}
        self._state_lock = asyncio.Lock()

    # ---- lifecycle ----
    async def __aenter__(self) -> "AsyncFetcher":
        self._session = aiohttp.ClientSession()
        self._global_sem = asyncio.Semaphore(self.global_concurrency)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ---- internal helpers ----
    @staticmethod
    def _domain(url: str) -> str:
        return urlsplit(url).netloc.lower()

    async def _domain_sem(self, domain: str) -> asyncio.Semaphore:
        async with self._state_lock:
            sem = self._domain_sems.get(domain)
            if sem is None:
                sem = asyncio.Semaphore(self.domain_concurrency)
                self._domain_sems[domain] = sem
            return sem

    async def _bucket(self, domain: str) -> _TokenBucket:
        async with self._state_lock:
            bucket = self._buckets.get(domain)
            if bucket is None:
                bucket = _TokenBucket(
                    self.rate_limit_per_sec,
                    capacity=self.rate_capacity,
                    sleep=self._rate_sleep,
                )
                self._buckets[domain] = bucket
            return bucket

    def _headers(self, overrides: Optional[Mapping[str, str]]) -> dict[str, str]:
        headers = {
            "User-Agent": random.choice(self.user_agents),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if overrides:
            headers.update(overrides)
        return headers

    # ---- robots ----
    async def _robots_allowed(self, url: str) -> bool:
        domain = self._domain(url)
        now = time.monotonic()
        cached = self._robots.get(domain)
        if cached is None or cached[1] < now:
            parser = await self._fetch_robots(url, domain)
            self._robots[domain] = (parser, now + self.robots_ttl)
        else:
            parser = cached[0]
        if parser is None:  # unreachable robots.txt -> allow (already warned)
            return True
        return parser.can_fetch(self.robots_agent, url)

    async def _fetch_robots(self, url: str, domain: str) -> Optional[RobotFileParser]:
        parts = urlsplit(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        try:
            assert self._session is not None
            timeout = aiohttp.ClientTimeout(total=self.total_timeout, connect=self.connect_timeout)
            async with self._session.get(
                robots_url, timeout=timeout, headers=self._headers(None)
            ) as resp:
                body = await resp.read()
            parser = RobotFileParser()
            parser.parse(body.decode("utf-8", errors="replace").splitlines())
            return parser
        except Exception as exc:  # noqa: BLE001 - robots failure must not break fetching
            log.warning(
                "robots.txt unreachable; allowing by default",
                extra={"extra": {"domain": domain, "reason": str(exc)}},
            )
            return None

    # ---- cache ----
    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _cache_read(self, url: str, ttl: float) -> Optional[FetchResult]:
        path = self._cache_path(url)
        if not path.exists():
            return None
        try:
            data = jsonlib.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        if time.time() - data.get("ts", 0) > ttl:
            return None
        return FetchResult(
            url=url,
            final_url=data.get("final_url"),
            status=data.get("status", 0),
            text=data.get("text"),
            content_type=data.get("content_type"),
            headers=data.get("headers", {}),
            from_cache=True,
            attempts=0,
            blocked=data.get("blocked", False),
        )

    def _cache_write(self, result: FetchResult) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "ts": time.time(),
                "final_url": result.final_url,
                "status": result.status,
                "text": result.text,
                "content_type": result.content_type,
                "headers": dict(result.headers),
                "blocked": result.blocked,
            }
            self._cache_path(result.url).write_text(
                jsonlib.dumps(payload), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("cache write failed", extra={"extra": {"reason": str(exc)}})

    # ---- backoff ----
    @staticmethod
    def _parse_retry_after(value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            pass
        try:  # HTTP-date form
            dt = parsedate_to_datetime(value)
            return max(0.0, dt.timestamp() - time.time())
        except Exception:  # noqa: BLE001
            return None

    def _backoff(self, attempt: int, retry_after: Optional[float]) -> float:
        capped = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        wait = random.uniform(0, capped)  # full jitter
        if retry_after is not None:
            wait = max(wait, retry_after)
        return wait

    @staticmethod
    def _is_blocked(status: int, body_head: str) -> bool:
        return detect_block(status, body_head)

    # ---- core request ----
    async def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Optional[Mapping[str, str]] = None,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Any] = None,
        respect_robots: Optional[bool] = None,
        max_attempts: Optional[int] = None,
        total_timeout: Optional[float] = None,
        connect_timeout: Optional[float] = None,
        max_bytes: Optional[int] = None,
        use_cache: bool = False,
        cache_ttl: Optional[float] = None,
    ) -> FetchResult:
        if self._session is None or self._global_sem is None:
            raise RuntimeError("AsyncFetcher must be used as an async context manager")

        respect = self.respect_robots if respect_robots is None else respect_robots
        attempts_cap = max_attempts or self.max_attempts
        total_to = total_timeout or self.total_timeout
        connect_to = connect_timeout or self.connect_timeout
        size_cap = max_bytes or self.max_bytes
        ttl = self.cache_ttl if cache_ttl is None else cache_ttl
        domain = self._domain(url)
        started = time.monotonic()

        # Disk cache (read)
        if use_cache:
            cached = self._cache_read(url, ttl)
            if cached is not None:
                log.info("request", extra={"extra": {
                    "method": method, "url": url, "status": cached.status,
                    "attempts": 0, "elapsed_ms": 0.0, "from_cache": True}})
                return cached

        # robots.txt
        if respect and not await self._robots_allowed(url):
            log.warning("skipped by robots.txt", extra={"extra": {"url": url}})
            return FetchResult(url=url, error="disallowed by robots.txt",
                               elapsed_ms=(time.monotonic() - started) * 1000)

        sem = await self._domain_sem(domain)
        bucket = await self._bucket(domain)
        result = FetchResult(url=url)

        async with self._global_sem, sem:
            for attempt in range(1, attempts_cap + 1):
                result.attempts = attempt
                await bucket.acquire()  # rate limit each attempt
                timeout = aiohttp.ClientTimeout(total=total_to, connect=connect_to)
                try:
                    assert self._session is not None
                    async with self._session.request(
                        method, url, headers=self._headers(headers),
                        params=params, json=json_body, timeout=timeout,
                        allow_redirects=True,
                    ) as resp:
                        chunks: list[bytes] = []
                        total = 0
                        truncated = False
                        async for chunk in resp.content.iter_chunked(65536):
                            chunks.append(chunk)
                            total += len(chunk)
                            if total > size_cap:
                                truncated = True
                                break
                        raw = b"".join(chunks)
                        if truncated:
                            raw = raw[:size_cap]
                            log.warning("response truncated to size cap", extra={"extra": {
                                "url": url, "max_bytes": size_cap}})
                        charset = resp.charset or "utf-8"
                        text = raw.decode(charset, errors="replace")

                        result.status = resp.status
                        result.final_url = str(resp.url)
                        result.content_type = resp.headers.get("Content-Type")
                        result.headers = dict(resp.headers)
                        result.text = text
                        result.blocked = self._is_blocked(resp.status, text[:4096])

                    # Retry on retryable status
                    if resp.status in RETRYABLE_STATUS and attempt < attempts_cap:
                        retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
                        wait = self._backoff(attempt, retry_after)
                        log.warning("retrying", extra={"extra": {
                            "url": url, "reason": f"HTTP {resp.status}",
                            "attempt": attempt, "wait_s": round(wait, 3)}})
                        await self._sleep(wait)
                        continue

                    if resp.status >= 400:
                        result.error = f"HTTP {resp.status}"
                    result.elapsed_ms = (time.monotonic() - started) * 1000
                    log.info("request", extra={"extra": {
                        "method": method, "url": url, "status": result.status,
                        "attempts": attempt, "elapsed_ms": round(result.elapsed_ms, 1),
                        "from_cache": False, "blocked": result.blocked}})
                    if use_cache and result.ok:
                        self._cache_write(result)
                    return result

                except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as exc:
                    reason = type(exc).__name__
                    if attempt < attempts_cap:
                        wait = self._backoff(attempt, None)
                        log.warning("retrying", extra={"extra": {
                            "url": url, "reason": reason, "attempt": attempt,
                            "wait_s": round(wait, 3)}})
                        await self._sleep(wait)
                        continue
                    result.error = reason
                    result.elapsed_ms = (time.monotonic() - started) * 1000
                    log.warning("request failed", extra={"extra": {
                        "method": method, "url": url, "reason": reason,
                        "attempts": attempt, "elapsed_ms": round(result.elapsed_ms, 1)}})
                    return result

        return result  # pragma: no cover

    # ---- public API ----
    async def fetch_text(self, url: str, **opts: Any) -> FetchResult:
        return await self._request(url, **opts)

    async def fetch_json(self, url: str, **opts: Any) -> FetchResult:
        opts.setdefault("headers", {})
        result = await self._request(url, **opts)
        if result.ok and result.text is not None:
            try:
                result.json = jsonlib.loads(result.text)
            except (ValueError, TypeError) as exc:
                result.error = f"invalid JSON: {exc}"
        return result

    async def fetch_many(self, urls: Iterable[str], **opts: Any) -> list[FetchResult]:
        url_list = list(urls)
        coros = [self.fetch_text(u, **opts) for u in url_list]
        raw = await asyncio.gather(*coros, return_exceptions=True)
        results: list[FetchResult] = []
        for u, item in zip(url_list, raw):
            if isinstance(item, BaseException):
                log.warning("fetch_many item raised", extra={"extra": {
                    "url": u, "reason": repr(item)}})
                results.append(FetchResult(url=u, error=repr(item)))
            else:
                results.append(item)
        return results


# ---------------------------------------------------------------------------
# Factory: build a fetcher from a SourceConfig (config-driven limits)
# ---------------------------------------------------------------------------
def fetcher_from_source(source: "Any", **overrides: Any) -> AsyncFetcher:
    """Build an AsyncFetcher using a SourceConfig's rate/concurrency settings.

    `source` is a src.common.config.SourceConfig (duck-typed to avoid a hard
    import cycle). Any keyword override wins over the config value.
    """
    params: dict[str, Any] = {
        "domain_concurrency": getattr(source, "max_concurrency", DEFAULT_DOMAIN_CONCURRENCY),
        "rate_limit_per_sec": getattr(source, "rate_limit_per_sec", DEFAULT_RATE_PER_SEC),
    }
    params.update(overrides)
    return AsyncFetcher(**params)
