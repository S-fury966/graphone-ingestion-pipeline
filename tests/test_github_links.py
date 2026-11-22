"""GitHub URL normalization + extraction tests."""
import pytest

from src.crawler.github_links import extract_repo_urls, normalize_repo_url, split_repo


@pytest.mark.parametrize("raw,expected", [
    ("https://github.com/Owner/Repo1", "https://github.com/Owner/Repo1"),
    ("http://github.com/foo/bar.git", "https://github.com/foo/bar"),
    ("https://github.com/foo/bar/tree/main/sub", "https://github.com/foo/bar"),
    ("https://github.com/foo/bar/blob/main/f.py", "https://github.com/foo/bar"),
    ("https://www.github.com/foo/bar/", "https://github.com/foo/bar"),
    ("https://github.com/foo/bar?tab=readme#frag", "https://github.com/foo/bar"),
    ("https://github.com/foo/bar.", "https://github.com/foo/bar"),  # trailing period
    ("https://github.com/a-b/c_d.e", "https://github.com/a-b/c_d.e"),
])
def test_normalize_good(raw, expected):
    assert normalize_repo_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "https://github.com/features",          # site route, no repo
    "https://github.com/foo",               # user-only profile
    "https://github.com/sponsors/someone",  # reserved owner
    "https://gitlab.com/foo/bar",           # not github
    "not a url",
])
def test_normalize_bad(raw):
    assert normalize_repo_url(raw) is None


def test_extract_dedups_and_preserves_order():
    text = ("First https://github.com/foo/bar then https://github.com/baz/qux "
            "and a dup https://github.com/FOO/BAR again, plus github.com/features.")
    urls = extract_repo_urls(text)
    assert urls == ["https://github.com/foo/bar", "https://github.com/baz/qux"]


def test_split_repo():
    assert split_repo("https://github.com/owner/repo") == ("owner", "repo")
