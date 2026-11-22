"""Config for the LLM tier chain (config/llm.yaml)."""
from __future__ import annotations

from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_LLM_CONFIG = Path("config/llm.yaml")
Provider = Literal["gemini", "groq", "deepseek", "openai", "mock"]


class TierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    provider: Provider
    model: str
    # For provider == "openai" (generic OpenAI-compatible): the chat-completions
    # endpoint URL. Ignored for the built-in providers.
    base_url: Optional[str] = None
    # Reasoning effort for models that support it (Groq gpt-oss). "low" roughly
    # halves output/reasoning tokens with no loss of JSON/evidence validity.
    reasoning_effort: Optional[Literal["low", "medium", "high"]] = None
    api_key_env: str = ""
    enabled: bool = True
    max_input_tokens: int = Field(gt=0, default=8000)
    max_output_tokens: int = Field(gt=0, default=2048)
    rpm_limit: float = Field(gt=0, default=15)
    # Tokens-per-minute cap (input + output). None = unlimited. May be a config
    # guess that gets overridden at runtime from the provider's rate-limit headers.
    tpm_limit: Optional[float] = Field(default=None, gt=0)
    # Output tokens the TPM limiter *reserves* per call when pacing. Separate from
    # max_output_tokens (the real API cap): the actual response is almost always
    # far smaller than max_output_tokens, so reserving the full cap wastes the
    # window. None => fall back to max_output_tokens.
    tpm_output_reserve: Optional[int] = Field(default=None, gt=0)
    timeout_s: float = Field(gt=0, default=30)


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_version: str = "1"
    global_concurrency: int = Field(gt=0, default=8)
    max_attempts_per_tier: int = Field(gt=0, default=3)
    circuit_threshold: int = Field(gt=0, default=5)      # consecutive failures
    circuit_cooldown_s: float = Field(ge=0, default=180)  # a few minutes
    backoff_base_s: float = Field(gt=0, default=0.5)
    backoff_max_s: float = Field(gt=0, default=20)
    # On a 429 whose reset time exceeds this, do NOT sleep: mark the tier
    # exhausted_until = now + reset and fall to the next tier immediately.
    max_wait_s: float = Field(gt=0, default=120)
    tiers: List[TierConfig] = Field(default_factory=list)


def load_llm_config(path: str | Path = DEFAULT_LLM_CONFIG) -> LLMConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return LLMConfig.model_validate(data)
