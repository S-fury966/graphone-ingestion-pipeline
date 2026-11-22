"""Escalation ladder for fetching a page (Phase V).

fetch_with_ladder() tries, in order, and records the level used:

  L0  plain AsyncFetcher request (polite headers, caching, conditional requests).
  L1  BrowserFetcher render — only when the L0 page is js_only (visible text
      under ~300 chars) AND the source policy allows rendering (or render:always).
  L2  blocked: a challenge (Cloudflare / Datadome / captcha) detected at ANY
      level STOPS the ladder. We record status=blocked_by_challenge, make no
      retries and no tricks, and tell the caller to use an alternative source
      (official API, RSS, partner feed, or archive).

Policy is per-source (config/sources.yaml): render never|auto|always, plus
respect_robots and max_rate. A source that disallows automated access or whose
terms forbid AI collection is configured render: never and is never rendered.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..common.logging import get_logger
from ..llm.chunker import html_to_text
from .browser import BrowserFetcher
from .fetcher import AsyncFetcher, FetchResult

log = get_logger("crawler.ladder")

JS_ONLY_MIN_CHARS = 300

# ladder status values
OK = "ok"                                 # L0 had enough text
JS_ONLY = "js_only"                       # L0 too little text, no render attempted
RENDERED_OK = "rendered_ok"               # L1 render produced enough text
STILL_JS_ONLY = "still_js_only"           # L1 render still too little / failed
BLOCKED_BY_CHALLENGE = "blocked_by_challenge"
HTTP_ERROR = "http_error"
ROBOTS_DISALLOWED = "robots_disallowed"


@dataclass(frozen=True)
class LadderPolicy:
    render: str = "auto"                  # never | auto | always
    respect_robots: bool = True
    max_rate: Optional[float] = None

    @classmethod
    def from_source(cls, source) -> "LadderPolicy":
        return cls(
            render=getattr(source, "render", "auto"),
            respect_robots=getattr(source, "respect_robots", True),
            max_rate=getattr(source, "rate_limit_per_sec", None),
        )


@dataclass
class LadderResult:
    url: str
    status: str
    final_level: str                      # L0 | L1 | L2
    levels_tried: List[str] = field(default_factory=list)
    text: Optional[str] = None
    html: Optional[str] = None
    rendered: bool = False
    blocked: bool = False
    reason: Optional[str] = None
    text_len_l0: int = 0
    text_len_l1: Optional[int] = None
    final_url: Optional[str] = None


def visible_text(html: Optional[str]) -> str:
    if not html:
        return ""
    header, blocks = html_to_text(html)
    return "\n".join([header] + [b.text for b in blocks]).strip()


def _blocked(url: str, detected_level: str, levels: List[str],
             res: FetchResult) -> LadderResult:
    return LadderResult(
        url=url, status=BLOCKED_BY_CHALLENGE, final_level="L2",
        levels_tried=levels, rendered=res.rendered, blocked=True,
        final_url=res.final_url,
        reason=(f"challenge detected at {detected_level} (status {res.status}); "
                "stopped with no retries — use an alternative source "
                "(official API, RSS, partner feed, or archive)"))


async def fetch_with_ladder(
    url: str,
    policy: LadderPolicy,
    http_fetcher: AsyncFetcher,
    browser_fetcher: Optional[BrowserFetcher] = None,
    js_only_min_chars: int = JS_ONLY_MIN_CHARS,
) -> LadderResult:
    levels: List[str] = []

    # ---- L0: plain HTTP ----
    levels.append("L0")
    r0 = await http_fetcher.fetch_text(url, respect_robots=policy.respect_robots)
    if r0.error == "disallowed by robots.txt":
        return LadderResult(url=url, status=ROBOTS_DISALLOWED, final_level="L0",
                            levels_tried=levels, reason="robots.txt disallows this URL")
    if r0.blocked:
        return _blocked(url, "L0", levels, r0)
    if not r0.ok or not r0.text:
        return LadderResult(url=url, status=HTTP_ERROR, final_level="L0",
                            levels_tried=levels, final_url=r0.final_url,
                            reason=r0.error or f"HTTP {r0.status}")

    text0 = visible_text(r0.text)
    len0 = len(text0)

    need_render = (policy.render == "always"
                   or (policy.render == "auto" and len0 < js_only_min_chars))
    if not need_render:
        status = OK if len0 >= js_only_min_chars else JS_ONLY
        reason = None if status == OK else "js_only; rendering not allowed by policy"
        return LadderResult(url=url, status=status, final_level="L0",
                            levels_tried=levels, text=text0, html=r0.text,
                            text_len_l0=len0, final_url=r0.final_url, reason=reason)

    if browser_fetcher is None:
        return LadderResult(url=url, status=JS_ONLY, final_level="L0",
                            levels_tried=levels, text=text0, html=r0.text,
                            text_len_l0=len0, final_url=r0.final_url,
                            reason="js_only; rendering required but no browser available")

    # ---- L1: render ----
    levels.append("L1")
    r1 = await browser_fetcher.fetch(url, respect_robots=policy.respect_robots)
    if r1.error == "disallowed by robots.txt":
        return LadderResult(url=url, status=ROBOTS_DISALLOWED, final_level="L1",
                            levels_tried=levels, text_len_l0=len0,
                            reason="robots.txt disallows rendering this URL")
    if r1.blocked:
        return _blocked(url, "L1", levels, r1)
    if not r1.ok or not r1.text:
        return LadderResult(url=url, status=STILL_JS_ONLY, final_level="L1",
                            levels_tried=levels, text=text0, html=r0.text,
                            rendered=True, text_len_l0=len0, text_len_l1=0,
                            final_url=r1.final_url,
                            reason=f"render failed: {r1.error or 'HTTP ' + str(r1.status)}")

    text1 = visible_text(r1.text)
    len1 = len(text1)
    status = RENDERED_OK if len1 >= js_only_min_chars else STILL_JS_ONLY
    reason = None if status == RENDERED_OK else "rendered text still under the js_only threshold"
    return LadderResult(url=url, status=status, final_level="L1", levels_tried=levels,
                        text=text1, html=r1.text, rendered=True,
                        text_len_l0=len0, text_len_l1=len1,
                        final_url=r1.final_url, reason=reason)
