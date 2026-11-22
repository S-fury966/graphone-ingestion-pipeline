"""Per-source JOB adapters: raw API/RSS response -> list[JobItem].

Adapters are deterministic parsers only. Date parsing, freshness, AI filtering,
is_remote / role_family classification, and storage all live in the pipeline
(src.pipeline.jobs) so every source shares one code path.
"""
from .base import (JobItem, classify_role, decide_remote, load_board_slugs,
                   load_role_table)

__all__ = ["JobItem", "classify_role", "decide_remote", "load_board_slugs",
           "load_role_table"]
