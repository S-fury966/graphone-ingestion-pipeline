"""Crawler: shared fetching + (later) source-specific parsing."""
from .fetcher import AsyncFetcher, FetchResult, fetcher_from_source

__all__ = ["AsyncFetcher", "FetchResult", "fetcher_from_source"]
