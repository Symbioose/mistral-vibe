from __future__ import annotations

from pathlib import Path

from git import Repo
import pytest

from vibe.core.worktree import (
    WorktreeError,
    commit_worktree,
    merge_branch,
    merge_report,
    prepare_worktree_session,
)


def _init_repo(workdir: Path) -> Repo:
    repo = Repo.init(workdir, initial_branch="main")
    repo.config_writer().set_value("user", "name", "Tester").release()
    repo.config_writer().set_value("user", "email", "t@example.com").release()
    (workdir / "file.txt").write_text("hello\n")
    repo.index.add(["file.txt"])
    repo.index.commit("initial")
    return repo


@pytest.fixture
def git_repo(tmp_path: Path) -> Repo:
    return _init_repo(tmp_path)


def test_commit_worktree_returns_none_when_clean(
    git_repo: Repo, tmp_path: Path
) -> None:
    worktree = prepare_worktree_session("worker-a", tmp_path)

    assert commit_worktree(worktree, "no-op") is None


def test_commit_worktree_commits_new_and_modified_files(
    git_repo: Repo, tmp_path: Path
) -> None:
    worktree = prepare_worktree_session("worker-a", tmp_path)
    (worktree.root / "new.txt").write_text("created\n")
    (worktree.root / "file.txt").write_text("modified\n")

    sha = commit_worktree(worktree, "worker changes")

    assert sha is not None
    committed = Repo(worktree.root).head.commit
    assert committed.hexsha == sha
    assert committed.message.strip() == "worker changes"
    assert {"new.txt", "file.txt"} <= set(committed.stats.files)


def test_commit_worktree_skips_build_artifacts(git_repo: Repo, tmp_path: Path) -> None:
    worktree = prepare_worktree_session("worker-a", tmp_path)
    (worktree.root / "code.py").write_text("x = 1\n")
    pycache = worktree.root / "__pycache__"
    pycache.mkdir()
    (pycache / "code.cpython-312.pyc").write_bytes(b"\x00")
    (worktree.root / ".DS_Store").write_bytes(b"\x00")

    sha = commit_worktree(worktree, "worker changes")

    assert sha is not None
    committed = set(Repo(worktree.root).head.commit.stats.files)
    assert committed == {"code.py"}


def test_commit_worktree_returns_none_when_only_artifacts_exist(
    git_repo: Repo, tmp_path: Path
) -> None:
    worktree = prepare_worktree_session("worker-a", tmp_path)
    pycache = worktree.root / "__pycache__"
    pycache.mkdir()
    (pycache / "junk.pyc").write_bytes(b"\x00")

    assert commit_worktree(worktree, "no-op") is None


def test_merge_report_clean_branch(git_repo: Repo, tmp_path: Path) -> None:
    worktree = prepare_worktree_session("worker-a", tmp_path)
    (worktree.root / "new.txt").write_text("created\n")
    commit_worktree(worktree, "worker changes")

    report = merge_report(tmp_path, worktree.branch)

    assert report.clean is True
    assert report.conflicting_paths == ()
    assert report.files_changed == ("new.txt",)


def test_merge_report_detects_conflicts_without_touching_checkout(
    git_repo: Repo, tmp_path: Path
) -> None:
    worktree = prepare_worktree_session("worker-a", tmp_path)
    (worktree.root / "file.txt").write_text("worker version\n")
    commit_worktree(worktree, "worker changes")
    (tmp_path / "file.txt").write_text("main version\n")
    git_repo.index.add(["file.txt"])
    git_repo.index.commit("main changes")

    report = merge_report(tmp_path, worktree.branch)

    assert report.clean is False
    assert report.conflicting_paths == ("file.txt",)
    assert (tmp_path / "file.txt").read_text() == "main version\n"
    assert not (tmp_path / ".git" / "MERGE_HEAD").exists()


def test_merge_branch_merges_clean_branch(git_repo: Repo, tmp_path: Path) -> None:
    worktree = prepare_worktree_session("worker-a", tmp_path)
    (worktree.root / "new.txt").write_text("created\n")
    commit_worktree(worktree, "worker changes")

    sha = merge_branch(tmp_path, worktree.branch)

    assert git_repo.head.commit.hexsha == sha
    assert (tmp_path / "new.txt").read_text() == "created\n"


def test_merge_branch_raises_and_leaves_checkout_pristine_on_conflict(
    git_repo: Repo, tmp_path: Path
) -> None:
    worktree = prepare_worktree_session("worker-a", tmp_path)
    (worktree.root / "file.txt").write_text("worker version\n")
    commit_worktree(worktree, "worker changes")
    (tmp_path / "file.txt").write_text("main version\n")
    git_repo.index.add(["file.txt"])
    git_repo.index.commit("main changes")

    with pytest.raises(WorktreeError):
        merge_branch(tmp_path, worktree.branch)

    assert (tmp_path / "file.txt").read_text() == "main version\n"
    assert not (tmp_path / ".git" / "MERGE_HEAD").exists()
    assert not git_repo.git.status("--porcelain").strip()
