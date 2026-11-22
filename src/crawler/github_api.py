"""GitHub repo verification + star counts, with rate-limit handling + cache."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

from ..common.logging import get_logger

log = get_logger("crawler.github_api")

API_URL = "https://api.github.com/repos/{owner}/{repo}"


@dataclass
class RepoInfo:
    owner: str
    repo: str
    exists: bool
    stars: Optional[int] = None
    html_url: Optional[str] = None  # canonical (handles renames/redirects)


class GitHubClient:
    """Verifies repos through an AsyncFetcher; caches results in the DB.

    `fetcher` must expose an async fetch_json(url, **opts) -> FetchResult.
    `cache` must expose get_repo_cache(key) and set_repo_cache(key, exists,
    stars, html_url). Both are duck-typed for easy testing.
    """

    def __init__(self, fetcher: Any, token: Optional[str], cache: Any,
                 min_remaining: int = 2, max_attempts: int = 4) -> None:
        self.fetcher = fetcher
        self.token = token
        self.cache = cache
        self.min_remaining = min_remaining
        self.max_attempts = max_attempts
        self._sleep = asyncio.sleep  # injectable for tests
        self._time = time.time

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:  # value never logged
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _wait_until_reset(self, reset: Any) -> None:
        try:
            delta = max(0, int(float(reset)) - int(self._time())) + 1
        except (TypeError, ValueError):
            delta = 60
        log.warning("github rate limit; sleeping until reset",
                    extra={"extra": {"sleep_s": delta}})
        await self._sleep(delta)

    async def get_repo(self, owner: str, repo: str, force: bool = False) -> RepoInfo:
        key = f"{owner.lower()}/{repo.lower()}"
        if not force and self.cache is not None:
            cached = self.cache.get_repo_cache(key)
            if cached is not None:
                return RepoInfo(owner, repo, bool(cached["exists"]),
                                cached["stars"], cached["html_url"])

        url = API_URL.format(owner=owner, repo=repo)
        for attempt in range(1, self.max_attempts + 1):
            res = await self.fetcher.fetch_json(
                url, headers=self._headers(), respect_robots=False, max_attempts=1)
            remaining = res.headers.get("X-RateLimit-Remaining")
            reset = res.headers.get("X-RateLimit-Reset")

            if res.status == 404:
                if self.cache is not None:
                    self.cache.set_repo_cache(key, False, None, None)
                return RepoInfo(owner, repo, False, None, None)

            if res.status == 200 and res.json:
                stars = res.json.get("stargazers_count")
                html = res.json.get("html_url")
                if self.cache is not None:
                    self.cache.set_repo_cache(key, True, stars, html)
                await self._maybe_wait(remaining, reset)
                return RepoInfo(owner, repo, True, stars, html)

            if res.status in (403, 429):
                # Primary rate limit: remaining 0 -> wait for reset.
                if str(remaining) == "0" and reset:
                    await self._wait_until_reset(reset)
                    continue
                # Secondary/abuse limit: honor Retry-After or back off.
                retry_after = res.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else min(60.0, 2.0 ** attempt)
                log.warning("github secondary rate limit; backing off",
                            extra={"extra": {"attempt": attempt, "wait_s": wait}})
                await self._sleep(wait)
                continue

            # Other transient error: back off and retry.
            log.warning("github unexpected status",
                        extra={"extra": {"status": res.status, "attempt": attempt}})
            await self._sleep(min(30.0, 2.0 ** attempt))

        # Exhausted attempts -> treat as unverified, do NOT cache.
        return RepoInfo(owner, repo, False, None, None)

    async def _maybe_wait(self, remaining: Any, reset: Any) -> None:
        if remaining is None:
            return
        try:
            if int(remaining) <= self.min_remaining and reset:
                await self._wait_until_reset(reset)
        except (TypeError, ValueError):
            return
