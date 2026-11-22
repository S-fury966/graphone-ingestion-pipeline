"""Deterministic name + domain normalization for dedup keys.

This is the lightweight normalizer used for dedup in Phase I. The full entity
resolver (fuzzy canonicalization against the seed list) comes later.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional
from urllib.parse import urlsplit

# Trailing legal suffixes to drop. NOTE: "labs" is intentionally NOT here.
_LEGAL_SUFFIXES = {"inc", "llc", "ltd", "corp", "co", "gmbh", "plc", "sa", "ag",
                   "bv", "pty", "limited", "incorporated", "corporation"}

# Multi-label public suffixes we recognize without a PSL dependency.
_MULTI_TLDS = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "co.jp", "co.in", "co.kr", "co.nz",
    "com.au", "com.br", "com.cn", "com.sg", "com.hk", "com.tr", "co.za",
}


def strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize_name(name: str) -> str:
    """Normalize a company name into a dedup key.

    lowercase -> strip accents -> punctuation to spaces -> collapse spaces ->
    drop trailing legal suffixes. Internal word spacing is preserved, so
    "open ai" and "openai" stay distinct.
    """
    if not name:
        return ""
    text = strip_accents(name).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)   # punctuation -> space
    text = re.sub(r"\s+", " ", text).strip()
    tokens = text.split(" ")
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def compact_key(name: str) -> str:
    """Compact dedup key: normalize_name, then remove spaces and hyphens.

    normalize_name already lowercases, strips accents, turns punctuation
    (including hyphens) into spaces, collapses whitespace, and drops trailing
    legal suffixes. On top of that we:
      * drop a single leading "the" ("The Browser Company" -> "browsercompany")
      * drop standalone "and" joiner tokens so "Foo and Bar" == "Foo & Bar"
      * remove the remaining spaces

    So "OpenAI", "OpenAI, Inc.", "Open AI", "open-ai", "OPENAI" all collapse to
    "openai".
    """
    norm = normalize_name(name)
    if not norm:
        return ""
    tokens = [t for t in norm.split(" ") if t]
    if len(tokens) > 1 and tokens[0] == "the":
        tokens = tokens[1:]
    stripped = [t for t in tokens if t != "and"]
    tokens = stripped or tokens          # never empty out a name that is just "and"
    return "".join(tokens)


def clean_display_name(name: str, legal_suffixes: Optional[set] = None) -> str:
    """Clean a raw name for use as a NEW_ENTITY canonical.

    Collapses whitespace and strips trailing legal suffixes ONLY. Original
    capitalization and internal words are preserved verbatim — we never invent
    casing for an unknown entity.
    """
    if not name:
        return ""
    suffixes = legal_suffixes if legal_suffixes is not None else _LEGAL_SUFFIXES
    text = re.sub(r"\s+", " ", name.strip())
    # Repeatedly peel a trailing legal suffix token (optionally comma-separated,
    # optionally with a trailing period), case-insensitively.
    while True:
        m = re.search(r"[,\s]+([A-Za-z]+)\.?$", text)
        if not m or m.group(1).lower() not in suffixes:
            break
        text = text[: m.start()].rstrip(" ,.")
    return text.strip(" ,")


def registrable_domain(url: Optional[str]) -> Optional[str]:
    """Registrable domain: lowercase, no www, no scheme/path. None if unusable.

    Heuristic multi-label TLD handling (no PSL dependency): keeps 3 labels for
    known compound TLDs (e.g. co.uk), otherwise the last 2 labels.
    """
    if not url:
        return None
    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw
    host = urlsplit(raw).netloc.lower()
    host = host.split("@")[-1].split(":")[0]  # drop auth/port
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return None
    labels = host.split(".")
    last_two = ".".join(labels[-2:])
    if last_two in _MULTI_TLDS and len(labels) >= 3:
        return ".".join(labels[-3:])
    return last_two
