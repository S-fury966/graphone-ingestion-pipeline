"""Intelligent HTML->text cleaning and budget-aware truncation."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence

from bs4 import BeautifulSoup

from ..common.logging import get_logger

log = get_logger("llm.chunker")

_DROP_TAGS = ("script", "style", "nav", "footer", "header", "aside", "form",
              "noscript", "svg", "iframe", "button")
_BLOCK_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th",
               "blockquote", "figcaption", "dd", "dt")
_COOKIE_RE = re.compile(r"cookie|consent|gdpr|banner|subscribe|newsletter", re.I)
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

# Keywords that generally mark task-dense content (pricing extraction etc.).
_DEFAULT_KEYWORDS = ("price", "pricing", "plan", "free", "month", "year",
                     "enterprise", "per user", "billed", "$", "subscription")


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (chars/4)."""
    return max(1, len(text) // 4)


@dataclass
class Block:
    tag: str
    text: str


def _looks_like_html(text: str) -> bool:
    return bool(re.search(r"<[a-zA-Z/][^>]*>", text or ""))


def html_to_text(html: str) -> tuple[str, List[Block]]:
    """Return (header_text, blocks).

    header_text = title + meta description + og tags (always kept first).
    blocks = cleaned headings/paragraphs/list-items/table-cells, boilerplate
    and hidden/cookie elements removed.
    """
    soup = BeautifulSoup(html, "lxml")

    header_parts: List[str] = []
    if soup.title and soup.title.string:
        header_parts.append("TITLE: " + " ".join(soup.title.string.split()))
    for name in ("description", "og:title", "og:description"):
        tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
        if tag and tag.get("content"):
            header_parts.append(f"{name.upper()}: " + " ".join(tag["content"].split()))

    for el in soup(list(_DROP_TAGS)):
        el.decompose()
    # Hidden / cookie-banner elements.
    for el in soup.find_all(attrs={"hidden": True}):
        el.decompose()
    for el in soup.find_all(attrs={"aria-hidden": "true"}):
        el.decompose()
    for el in soup.find_all(attrs={"style": re.compile(r"display\s*:\s*none", re.I)}):
        el.decompose()
    for el in soup.find_all(class_=_COOKIE_RE):
        el.decompose()
    for el in soup.find_all(id=_COOKIE_RE):
        el.decompose()

    blocks: List[Block] = []
    seen = set()
    for el in soup.find_all(_BLOCK_TAGS):
        txt = " ".join(el.get_text(" ", strip=True).split())
        if len(txt) < 3:
            continue
        key = txt.lower()
        if key in seen:  # drop repeated lines (nav/boilerplate echoes)
            continue
        seen.add(key)
        blocks.append(Block(tag=el.name, text=txt))

    return "\n".join(header_parts), blocks


def _density_score(block: Block, keywords: Sequence[str]) -> float:
    text = block.text
    low = text.lower()
    length = min(len(text), 400)
    digits = sum(c.isdigit() for c in text)
    kw_hits = sum(low.count(k) for k in keywords)
    score = length + 15 * digits + 40 * kw_hits
    if block.tag in _HEADING_TAGS:
        score += 60  # headings carry structure
    if len(text) < 25:
        score -= 20  # penalize tiny fragments
    return score


def _first_sentences(text: str, n: int = 2) -> str:
    parts = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(parts[:n])


def fit(text: str, budget: int, focus_keywords: Optional[Sequence[str]] = None,
        always_lead: int = 0) -> str:
    """Fit text under `budget` tokens, keeping the densest content.

    Order: header (title/meta/og) -> the first `always_lead` blocks (the hero,
    kept regardless of density) -> headings + pricing/dense blocks by density.
    Guarantees the result is under budget (hard-truncates as a floor).

    Pre-cleaned, already-small plain text (not HTML) is returned unchanged so a
    caller that has pre-trimmed per page is not re-ordered or re-chunked.
    """
    budget = max(1, budget)
    if not _looks_like_html(text) and estimate_tokens(text) <= budget:
        return text
    keywords = list(_DEFAULT_KEYWORDS) + [k.lower() for k in (focus_keywords or [])]

    if _looks_like_html(text):
        header, blocks = html_to_text(text)
    else:
        header = ""
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        blocks = [Block(tag="p", text=" ".join(p.split())) for p in paras]

    kept: List[str] = []
    used = 0

    def try_add(segment: str) -> bool:
        nonlocal used
        cost = estimate_tokens(segment) + 1
        if used + cost > budget:
            return False
        kept.append(segment)
        used += cost
        return True

    if header:
        try_add(header)

    order_added: List[int] = []
    # Always keep the first `always_lead` blocks (the hero) in document order.
    lead_idx = set()
    for idx in range(min(always_lead, len(blocks))):
        block = blocks[idx]
        seg = ("## " + block.text) if block.tag in _HEADING_TAGS else block.text
        if try_add(seg):
            order_added.append(idx)
            lead_idx.add(idx)

    # Rank the remaining blocks; headings also carry structure.
    ranked = sorted((ib for ib in enumerate(blocks) if ib[0] not in lead_idx),
                    key=lambda ib: _density_score(ib[1], keywords), reverse=True)
    for idx, block in ranked:
        seg = block.text
        if block.tag in _HEADING_TAGS:
            seg = "## " + seg
        if try_add(seg):
            order_added.append(idx)
        if used >= budget:
            break

    # Reorder kept blocks into original document order for readability.
    head = [header] if header else []
    body_in_order = [("## " + blocks[i].text) if blocks[i].tag in _HEADING_TAGS
                     else blocks[i].text for i in sorted(order_added)]
    result = "\n\n".join(head + body_in_order)

    # Hard guarantee: never exceed budget.
    if estimate_tokens(result) > budget:
        result = result[: budget * 4]
    return result
