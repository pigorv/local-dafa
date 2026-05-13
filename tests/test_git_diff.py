"""Tests for ``tools/git_diff.py``: ground-truth patch computation.

Each test stands up a real temp git repo with ``git init`` and exercises
``compute_wp_diff`` end-to-end through ``RepoSandbox``. We don't mock the
git invocations because the helper's correctness lives in the
combination of ``git add -N -A``, ``git diff --name-status``, and
per-file ``git diff <pre_sha>`` — mocking those would not catch the
exact behaviours that bit the legacy hook (untracked file invisibility,
sanity-validation drops).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from darkfactory.tools.git_diff import (
    compute_wp_diff,
    reconcile_paths,
    snapshot_head,
)
from darkfactory.tools.sandbox import RepoSandbox


def _run(cwd: Path, *argv: str) -> None:
    subprocess.run(
        list(argv), cwd=cwd, check=True, capture_output=True
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "git", "init", "-q")
    _run(repo, "git", "config", "user.email", "t@example.com")
    _run(repo, "git", "config", "user.name", "tester")
    (repo / "existing.java").write_text("class Existing {}\n", encoding="utf-8")
    _run(repo, "git", "add", "existing.java")
    _run(repo, "git", "commit", "-q", "-m", "seed")
    return repo


def test_compute_wp_diff_returns_empty_when_sandbox_is_none() -> None:
    assert compute_wp_diff(None, "deadbeef", role="builder", slice_id="x") == []


def test_compute_wp_diff_returns_empty_when_pre_sha_missing(
    repo: Path,
) -> None:
    sandbox = RepoSandbox(repo_path=str(repo))
    assert compute_wp_diff(sandbox, "", role="builder", slice_id="x") == []


def test_compute_wp_diff_captures_new_untracked_file(repo: Path) -> None:
    sandbox = RepoSandbox(repo_path=str(repo))
    pre_sha = snapshot_head(sandbox)
    assert pre_sha

    (repo / "Order.java").write_text("class Order {}\n", encoding="utf-8")

    patches = compute_wp_diff(
        sandbox, pre_sha, role="builder", slice_id="WP-1"
    )
    assert len(patches) == 1
    patch = patches[0]
    assert patch["path"] == "Order.java"
    assert patch["author_agent"] == "builder"
    assert patch["slice_id"] == "WP-1"
    assert patch["edit_kind"] == "create"
    assert "+class Order {}" in patch["diff"]


def test_compute_wp_diff_captures_modified_tracked_file(repo: Path) -> None:
    sandbox = RepoSandbox(repo_path=str(repo))
    pre_sha = snapshot_head(sandbox)

    (repo / "existing.java").write_text(
        "class Existing { int x; }\n", encoding="utf-8"
    )

    patches = compute_wp_diff(
        sandbox, pre_sha, role="builder", slice_id="WP-1"
    )
    assert len(patches) == 1
    assert patches[0]["path"] == "existing.java"
    assert patches[0]["edit_kind"] == "modify"


def test_compute_wp_diff_captures_committed_changes(repo: Path) -> None:
    """Builder usually commits via ``Bash``; ``git diff <pre_sha>`` must
    still pick up the changes even after the commit lands."""
    sandbox = RepoSandbox(repo_path=str(repo))
    pre_sha = snapshot_head(sandbox)

    (repo / "Order.java").write_text("class Order {}\n", encoding="utf-8")
    _run(repo, "git", "add", "Order.java")
    _run(repo, "git", "commit", "-q", "-m", "WP-1: add Order")

    patches = compute_wp_diff(
        sandbox, pre_sha, role="builder", slice_id="WP-1"
    )
    paths = sorted(p["path"] for p in patches)
    assert paths == ["Order.java"]


def test_compute_wp_diff_captures_deleted_tracked_file(repo: Path) -> None:
    sandbox = RepoSandbox(repo_path=str(repo))
    pre_sha = snapshot_head(sandbox)

    (repo / "existing.java").unlink()

    patches = compute_wp_diff(
        sandbox, pre_sha, role="builder", slice_id="WP-1"
    )
    assert len(patches) == 1
    assert patches[0]["path"] == "existing.java"
    assert patches[0]["edit_kind"] == "delete"


def test_compute_wp_diff_returns_empty_when_no_changes(repo: Path) -> None:
    sandbox = RepoSandbox(repo_path=str(repo))
    pre_sha = snapshot_head(sandbox)
    assert (
        compute_wp_diff(sandbox, pre_sha, role="builder", slice_id="x") == []
    )


def test_compute_wp_diff_captures_multiple_files_in_one_turn(
    repo: Path,
) -> None:
    sandbox = RepoSandbox(repo_path=str(repo))
    pre_sha = snapshot_head(sandbox)

    (repo / "a.java").write_text("class A {}\n", encoding="utf-8")
    (repo / "b.java").write_text("class B {}\n", encoding="utf-8")
    (repo / "existing.java").write_text(
        "class Existing { void m() {} }\n", encoding="utf-8"
    )

    patches = compute_wp_diff(
        sandbox, pre_sha, role="builder", slice_id="WP-1"
    )
    paths = sorted(p["path"] for p in patches)
    assert paths == ["a.java", "b.java", "existing.java"]
    assert all(p["slice_id"] == "WP-1" for p in patches)
    assert all(p["author_agent"] == "builder" for p in patches)


# ---------- reconcile_paths ----------


def test_reconcile_paths_all_match() -> None:
    out = reconcile_paths(["a", "b"], ["b", "a"])
    assert out == {"claimed_not_applied": [], "undeclared": []}


def test_reconcile_paths_flags_missing() -> None:
    out = reconcile_paths(["a", "b"], ["a"])
    assert out == {"claimed_not_applied": ["b"], "undeclared": []}


def test_reconcile_paths_flags_undeclared() -> None:
    out = reconcile_paths(["a"], ["a", "b"])
    assert out == {"claimed_not_applied": [], "undeclared": ["b"]}


def test_reconcile_paths_handles_both_sides_diverging() -> None:
    out = reconcile_paths(["a", "b"], ["b", "c"])
    assert out == {"claimed_not_applied": ["a"], "undeclared": ["c"]}
