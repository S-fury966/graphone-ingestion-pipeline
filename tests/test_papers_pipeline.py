"""Pipeline tests: resumability + schema rejection (no real internet)."""
from datetime import datetime, timezone

from src.crawler.arxiv import ArxivPaper
from src.crawler.github_api import RepoInfo
from src.pipeline.papers import Stats, process_page
from src.storage.db import Database

NOW = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _paper(arxiv_id, repo_url):
    return ArxivPaper(
        arxiv_id=arxiv_id, title=f"Paper {arxiv_id}", authors=["A. Author"],
        published=NOW, abs_url=f"http://arxiv.org/abs/{arxiv_id}",
        pdf_url=f"http://arxiv.org/pdf/{arxiv_id}",
        abstract=f"Great work. Code: {repo_url}", comment=None, journal_ref=None)


class FakeGH:
    """Maps 'owner/repo' -> RepoInfo; default = not found."""
    def __init__(self, mapping):
        self.mapping = mapping

    async def get_repo(self, owner, repo, force=False):
        key = f"{owner}/{repo}"
        return self.mapping.get(key, RepoInfo(owner, repo, False, None, None))


async def test_resumable_second_run_adds_nothing(tmp_path):
    db = Database(tmp_path / "state.db")
    papers = [
        _paper("2401.00001", "https://github.com/ok/repo"),      # verified
        _paper("2401.00002", "https://github.com/missing/repo"),  # 404
    ]
    gh = FakeGH({"ok/repo": RepoInfo("ok", "repo", True, 10,
                                     "https://github.com/ok/repo")})

    added1 = await process_page(db, papers, gh, None, Stats())
    assert added1 == 1
    assert db.count_papers() == 1

    added2 = await process_page(db, papers, gh, None, Stats())
    assert added2 == 0  # verified one skipped; the 404 still does not persist
    assert db.count_papers() == 1
    db.close()


async def test_invalid_record_skipped_stars_without_url(tmp_path):
    db = Database(tmp_path / "state.db")
    papers = [_paper("2401.00003", "https://github.com/ok/repo")]
    # exists but html_url is None while stars set -> schema must reject.
    gh = FakeGH({"ok/repo": RepoInfo("ok", "repo", True, 5, None)})
    stats = Stats()
    added = await process_page(db, papers, gh, None, stats)
    assert added == 0
    assert stats.invalid == 1
    assert db.count_papers() == 0
    db.close()


async def test_dry_run_does_not_persist(tmp_path):
    db = Database(tmp_path / "state.db")
    papers = [_paper("2401.00004", "https://github.com/ok/repo")]
    gh = FakeGH({"ok/repo": RepoInfo("ok", "repo", True, 10,
                                     "https://github.com/ok/repo")})
    stats = Stats()
    added = await process_page(db, papers, gh, None, stats, dry_run=True)
    assert added == 1 and stats.verified == 1
    assert db.count_papers() == 0  # nothing written in dry-run
    db.close()
