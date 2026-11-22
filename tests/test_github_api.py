"""GitHubClient tests with a fake fetcher (no real internet)."""
import time

from src.crawler.fetcher import FetchResult
from src.crawler.github_api import GitHubClient


class FakeFetcher:
    """Returns queued FetchResults in order; records call count."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def fetch_json(self, url, **opts):
        self.calls += 1
        return self.responses.pop(0)


class FakeCache:
    def __init__(self):
        self.store = {}

    def get_repo_cache(self, key):
        return self.store.get(key)

    def set_repo_cache(self, key, exists, stars, html_url):
        self.store[key] = {"exists": 1 if exists else 0, "stars": stars, "html_url": html_url}


def _resp(status, json=None, headers=None):
    return FetchResult(url="u", status=status, json=json,
                       headers=headers or {}, error=None if 200 <= status < 300 else f"HTTP {status}")


async def _no_sleep(_s):
    return None


async def test_200_stars_and_canonical_html_url():
    f = FakeFetcher([_resp(200, {"stargazers_count": 42,
                                 "html_url": "https://github.com/new/name"})])
    client = GitHubClient(f, token="x", cache=FakeCache())
    client._sleep = _no_sleep
    info = await client.get_repo("old", "name")
    assert info.exists and info.stars == 42
    assert info.html_url == "https://github.com/new/name"  # rename followed


async def test_404_dropped():
    cache = FakeCache()
    f = FakeFetcher([_resp(404)])
    client = GitHubClient(f, token=None, cache=cache)
    info = await client.get_repo("foo", "ghost")
    assert info.exists is False
    assert cache.store["foo/ghost"]["exists"] == 0  # cached as missing


async def test_cache_hit_avoids_second_call():
    cache = FakeCache()
    f = FakeFetcher([_resp(200, {"stargazers_count": 1, "html_url": "https://github.com/a/b"})])
    client = GitHubClient(f, token=None, cache=cache)
    client._sleep = _no_sleep
    await client.get_repo("a", "b")
    again = await client.get_repo("a", "b")
    assert again.stars == 1
    assert f.calls == 1  # second resolved from cache


async def test_primary_rate_limit_sleeps_until_reset():
    waits = []

    async def record(s):
        waits.append(s)

    reset = str(int(time.time()) + 3)
    f = FakeFetcher([
        _resp(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset}),
        _resp(200, {"stargazers_count": 7, "html_url": "https://github.com/a/b"}),
    ])
    client = GitHubClient(f, token="x", cache=FakeCache())
    client._sleep = record
    info = await client.get_repo("a", "b")
    assert info.exists and info.stars == 7
    assert any(w >= 2 for w in waits)  # slept ~3s until reset


async def test_secondary_rate_limit_backoff():
    waits = []

    async def record(s):
        waits.append(s)

    f = FakeFetcher([
        _resp(403, headers={"X-RateLimit-Remaining": "59", "Retry-After": "1"}),
        _resp(200, {"stargazers_count": 3, "html_url": "https://github.com/a/b"}),
    ])
    client = GitHubClient(f, token="x", cache=FakeCache())
    client._sleep = record
    info = await client.get_repo("a", "b")
    assert info.exists and info.stars == 3
    assert 1 in waits
