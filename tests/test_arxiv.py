"""arXiv Atom parsing tests (offline fixture)."""
from datetime import timezone
from pathlib import Path

from src.crawler.arxiv import build_query, parse_atom

FIXTURE = Path(__file__).parent / "fixtures" / "arxiv_sample.xml"


def _papers():
    return parse_atom(FIXTURE.read_text(encoding="utf-8"))


def test_parses_all_entries():
    papers = _papers()
    assert len(papers) == 3


def test_version_suffix_stripped():
    ids = [p.arxiv_id for p in _papers()]
    assert ids == ["2401.01234", "2401.05678", "2312.00001"]  # v2/v1 removed


def test_title_whitespace_collapsed():
    p = _papers()[0]
    assert p.title == "A Great Model for Everything"


def test_authors_and_dates():
    p = _papers()[0]
    assert p.authors == ["Ada Lovelace", "Alan Turing"]
    assert p.published.tzinfo == timezone.utc
    assert p.published.year == 2024 and p.published.hour == 10


def test_urls_and_optional_fields():
    p1, p2, p3 = _papers()
    assert p1.abs_url == "http://arxiv.org/abs/2401.01234v2"
    assert p1.pdf_url == "http://arxiv.org/pdf/2401.01234v2"
    assert p1.comment == "9 pages, 3 figures"
    assert p2.journal_ref == "NeurIPS 2024"
    assert p3.comment is None and p3.journal_ref is None


def test_search_text_includes_abstract_and_comment():
    p2 = _papers()[1]
    assert "github.com/foo/bar-baz" in p2.search_text


def test_build_query():
    q = build_query()
    assert "cat:cs.AI" in q and "cat:stat.ML" in q and "abs:github" in q
