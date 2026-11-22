"""Publication-date extraction tests — offline HTML snippets."""
from datetime import datetime, timezone

from src.crawler.article_dates import (SRC_JSONLD, SRC_META, SRC_RELATIVE,
                                       SRC_URL, extract_publication_date)
from src.schemas.enums import DateConfidence

NOW = datetime(2026, 11, 21, 21, 0, 0, tzinfo=timezone.utc)


def test_jsonld_datepublished_wins():
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@type":"NewsArticle","datePublished":"2026-11-21T10:00:00Z",
     "dateModified":"2026-11-21T20:00:00Z"}
    </script>
    <meta property="article:published_time" content="2026-11-20T10:00:00Z"/>
    </head><body><article>hi</article></body></html>
    """
    p, src = extract_publication_date(html, "https://x.com/a", now=NOW)
    assert src == SRC_JSONLD
    assert p.dt == datetime(2026, 11, 21, 10, 0, 0, tzinfo=timezone.utc)


def test_jsonld_graph_nested():
    html = """
    <script type="application/ld+json">
    {"@graph":[{"@type":"WebSite"},{"@type":"Article",
      "datePublished":"2026-11-21T09:30:00+00:00"}]}
    </script>"""
    p, src = extract_publication_date(html, now=NOW)
    assert src == SRC_JSONLD and p.dt.hour == 9


def test_meta_published_when_no_jsonld():
    html = '<meta property="article:published_time" content="2026-11-21T08:00:00Z">'
    p, src = extract_publication_date(html, now=NOW)
    assert src == SRC_META and p.dt.hour == 8


def test_modified_only_is_ignored():
    html = """
    <script type="application/ld+json">{"dateModified":"2026-11-21T20:00:00Z"}</script>
    <meta property="article:modified_time" content="2026-11-21T20:00:00Z">
    <article><time class="updated" datetime="2026-11-21T20:00:00Z">x</time></article>
    """
    # Only modified fields present -> treated as undated (None), not fresh-by-edit.
    assert extract_publication_date(html, now=NOW) is None


def test_time_tag_inside_article():
    html = '<article><time datetime="2026-11-21T07:15:00Z">Nov 21</time></article>'
    p, src = extract_publication_date(html, now=NOW)
    assert src == SRC_META and p.dt.hour == 7


def test_url_pattern_fallback_day_margin():
    html = "<html><body>no dates here</body></html>"
    p, src = extract_publication_date(html, "https://site.com/2026/11/21/a-story/", now=NOW)
    assert src == SRC_URL
    assert p.dt.date() == datetime(2026, 11, 21).date()
    assert p.margin.days == 1  # widened for day-granularity


def test_relative_text_last_resort():
    html = "<article><span>Published 2 hours ago by Someone</span></article>"
    p, src = extract_publication_date(html, now=NOW)
    assert src == SRC_RELATIVE and p.confidence == DateConfidence.RELATIVE


def test_priority_meta_over_url():
    html = '<meta name="pubdate" content="2026-11-21T06:00:00Z">'
    p, src = extract_publication_date(html, "https://s.com/2026/11/19/x/", now=NOW)
    assert src == SRC_META and p.dt.hour == 6


def test_nothing_found():
    assert extract_publication_date("<p>plain</p>", "https://s.com/story", now=NOW) is None
    assert extract_publication_date("", now=NOW) is None
