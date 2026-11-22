"""AI-relevance keyword filter, loaded from config/ai_keywords.yaml.

Whole-word / phrase matching (case-insensitive) so short tokens like "ai" or
"ml" never match inside "email" or "html".
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import List

import yaml

DEFAULT_PATH = "config/ai_keywords.yaml"


@lru_cache(maxsize=4)
def _compiled(path: str = DEFAULT_PATH):
    terms: List[str] = []
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        terms = [str(t).strip() for t in data.get("terms", []) if str(t).strip()]
    except Exception:  # noqa: BLE001
        terms = []
    if not terms:
        terms = ["ai", "machine learning", "llm"]
    pattern = r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b"
    return re.compile(pattern, re.IGNORECASE)


def is_ai_relevant(text: str, path: str = DEFAULT_PATH) -> bool:
    return bool(_compiled(path).search(text or ""))
