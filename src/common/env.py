"""Environment / secret loading.

Secrets live in .env (never committed). Values are never printed or logged.
A tier whose key is missing is treated as unavailable by callers (and skipped).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Names of the secrets the pipeline may use.
SECRET_KEYS = (
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "DEEPSEEK_API_KEY",
    "CEREBRAS_API_KEY",
    "MISTRAL_API_KEY",
    "GITHUB_TOKEN",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "SHEET_ID",
)

_loaded = False


def load_env(path: str | Path = ".env") -> None:
    """Load .env into the process environment (idempotent)."""
    global _loaded
    load_dotenv(dotenv_path=Path(path), override=False)
    _loaded = True


def get_secret(name: str) -> Optional[str]:
    """Return a secret value or None. Never logs the value."""
    if not _loaded:
        load_env()
    value = os.getenv(name)
    return value or None


def has_secret(name: str) -> bool:
    """Whether a secret is present (without exposing its value)."""
    return get_secret(name) is not None


def available_secrets() -> dict[str, bool]:
    """Map of known secret names -> present? (booleans only, never values)."""
    return {key: has_secret(key) for key in SECRET_KEYS}
