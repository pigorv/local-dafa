"""Tests for RepoSandbox. No Docker required — runs commands via subprocess."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from darkfactory.tools.sandbox import MAX_STDERR, MAX_STDOUT, RepoSandbox


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not available",
)


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


@pytest.fixture
def sandbox(repo):
    sb = RepoSandbox(str(repo))
    try:
        yield sb
    finally:
        sb.close()


def test_git_status_succeeds(sandbox):
    result = sandbox.exec(["git", "status"])
    assert result["returncode"] == 0
    assert "branch" in result["stdout"].lower() or "nothing to commit" in result["stdout"].lower()


def test_exec_runs_in_repo_path(sandbox, repo):
    result = sandbox.exec(["cat", "README.md"])
    assert result["returncode"] == 0
    assert result["stdout"].strip() == "hello"
    assert sandbox.repo_path == str(repo.resolve())


def test_missing_binary_returns_127(sandbox):
    result = sandbox.exec(["this-binary-does-not-exist-12345"])
    assert result["returncode"] == 127
    assert result["timed_out"] is False
    assert "this-binary-does-not-exist-12345" in result["stderr"]


def test_timeout_kills_runaway(sandbox):
    result = sandbox.exec(["sleep", "30"], timeout=1)
    assert result["timed_out"] is True
    assert result["returncode"] == 124


def test_empty_argv_raises(sandbox):
    with pytest.raises(ValueError):
        sandbox.exec([])


def test_stdout_stderr_truncated(sandbox):
    big = MAX_STDOUT + 50
    result = sandbox.exec(["python3", "-c", f"print('a' * {big})"])
    if result["returncode"] != 0:
        pytest.skip("python3 not available in test env")
    assert len(result["stdout"]) == MAX_STDOUT
    assert len(result["stderr"]) <= MAX_STDERR
