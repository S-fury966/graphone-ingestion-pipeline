"""Discover + normalize GitHub repo URLs from paper text (and HF as fallback)."""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from ..common.logging import get_logger
from .fetcher import AsyncFetcher

log = get_logger("crawler.github_links")

# owner: alnum + hyphen (not leading/trailing hyphen); repo: alnum . _ -
_GH_RE = re.compile(
    r"https?://(?:www\.)?github\.com/"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}[A-Za-z0-9])?)/"
    r"([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)

# First-segment values that are GitHub site routes, not user/org repos.
_RESERVED_OWNERS = {
    "features", "about", "pricing", "topics", "collections", "sponsors",
    "marketplace", "settings", "notifications", "explore", "orgs", "users",
    "login", "join", "site", "security", "contact", "enterprise", "team",
    "apps", "readme", "search", "new", "organizations", "account", "pulls",
    "issues", "watching", "stars", "dashboard", "sponsors-explorer",
}
# Second-segment values that are never real repos.
_RESERVED_REPOS = {"", ".", "..", "tree", "blob", "raw", "releases", "issues",
                   "pulls", "wiki", "settings", "actions"}


def normalize_repo_url(url: str) -> Optional[str]:
    """Normalize to https://github.com/{owner}/{repo}, or None if not a repo."""
    m = _GH_RE.match(url.strip())
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    # Strip common trailing artifacts and suffixes.
    repo = repo.split("#")[0].split("?")[0]
    if repo.endswith(".git"):
        repo = repo[:-4]
    repo = repo.rstrip(".,);:'\"]}>")  # markdown/sentence punctuation
    if not repo or owner.lower() in _RESERVED_OWNERS or repo.lower() in _RESERVED_REPOS:
        return None
    return f"https://github.com/{owner}/{repo}"


def extract_repo_urls(text: str) -> List[str]:
    """All unique, normalized repo URLs found in free text (order preserved)."""
    out: List[str] = []
    seen = set()
    for m in _GH_RE.finditer(text or ""):
        norm = normalize_repo_url(m.group(0))
        if norm and norm.lower() not in seen:
            seen.add(norm.lower())
            out.append(norm)
    return out


def split_repo(url: str) -> Tuple[str, str]:
    """('https://github.com/owner/repo') -> ('owner', 'repo')."""
    owner, repo = url.rstrip("/").rsplit("/", 2)[-2:]
    return owner, repo


async def from_huggingface(fetcher: AsyncFetcher, arxiv_id: str) -> List[str]:
    """Best-effort fallback: scrape the HF papers page for GitHub links.

    PwC's REST API is dead (paperswithcode.com now redirects to HF). The HF
    papers page occasionally lists repo links; results are noisy, so this is
    treated as low-confidence and every link is still verified via the GitHub
    API. Failures are normal and never fatal.
    """
    url = f"https://huggingface.co/papers/{arxiv_id}"
    try:
        res = await fetcher.fetch_text(url, respect_robots=False)
        if not res.ok or not res.text:
            return []
        return extract_repo_urls(res.text)
    except Exception as exc:  # noqa: BLE001
        log.warning("HF fallback failed", extra={"extra": {
            "arxiv_id": arxiv_id, "reason": str(exc)}})
        return []
