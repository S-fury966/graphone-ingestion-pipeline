"""Tests for the YC startup collector (offline fixture)."""
import json
from pathlib import Path

from src.crawler.yc_startups import dedup, map_company

FIXTURE = Path(__file__).parent / "fixtures" / "yc_sample.json"


def _raw():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _by_slug():
    return {r["slug"]: map_company(r) for r in _raw()}


def test_team_size_handling():
    recs = _by_slug()
    assert recs["openai-x"].employee_count == 500      # positive int
    assert recs["zeroco"].employee_count is None        # 0 -> None
    assert recs["nullco"].employee_count is None        # null -> None
    assert recs["strco"].employee_count is None         # "12" string -> None
    assert recs["negco"].employee_count is None         # negative -> None


def test_name_cleaned_and_source_url():
    recs = _by_slug()
    assert recs["zeroco"].entity_name == "ZeroCo"       # whitespace cleaned
    assert recs["openai-x"].source_url == "https://www.ycombinator.com/companies/openai-x"
    assert recs["openai-x"].source_name == "Y Combinator"


def test_invalid_website_maps_to_none_domain():
    recs = _by_slug()
    bad = recs["badweb"]
    assert bad.domain is None            # unparseable website
    assert bad.entity_name == "BadWeb"   # record still usable


def test_domain_extraction():
    recs = _by_slug()
    assert recs["openai-x"].domain == "openai.com"
    assert recs["dupdomain-b"].domain == "shared.com"  # www + path stripped


def test_dedup_by_name_and_domain():
    records = [map_company(r) for r in _raw()]
    kept, removed = dedup(records)
    # 9 input -> OpenAI dup (name+domain) and Alpha/Beta dup (domain) collapse -> 7
    assert len(kept) == 7
    assert removed == 2
    slugs = {r.slug for r in kept}
    # Winners keep the richer record.
    assert "openai-x" in slugs and "openai-inc" not in slugs   # openai-x has one_liner+batch
    assert "dupdomain-a" in slugs and "dupdomain-b" not in slugs  # alpha has one_liner


def test_dedup_is_order_independent_on_count():
    records = [map_company(r) for r in reversed(_raw())]
    kept, removed = dedup(records)
    assert len(kept) == 7 and removed == 2
