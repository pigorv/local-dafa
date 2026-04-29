"""Unit tests for the diff_capture PostToolUse hook.

The hook is exercised against a mock RepoSandbox (no real Docker) — the
acceptance line for M2-7 calls for a hand-test against a real worker
container in addition to this hermetic suite.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from darkfactory.hooks.diff_capture import make_diff_capture
from darkfactory.state import Patch
from darkfactory.tools import shell as shell_mod


VALID_DIFF = """\
diff --git a/foo.txt b/foo.txt
--- a/foo.txt
+++ b/foo.txt
@@ -1 +1,2 @@
 hello
+world
"""


class FakeSandbox:
    """Records exec calls and serves canned responses."""

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._responses = list(responses or [])

    def queue(self, response: dict[str, Any]) -> None:
        self._responses.append(response)

    def exec(self, argv: list[str], timeout: int = 120) -> dict[str, Any]:
        self.calls.append(argv)
        if self._responses:
            return self._responses.pop(0)
        return {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False}


@pytest.fixture
def task_id(monkeypatch: pytest.MonkeyPatch) -> str:
    """Register a fresh sandbox slot under a per-test key, clean up after."""
    key = "diff-capture-test"
    yield key
    # Drop any sandbox registered during the test so suites don't bleed.
    monkeypatch.setattr(shell_mod, "_SANDBOXES", {}, raising=False)


def _post(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": "test",
        "transcript_path": "/tmp/transcript",
        "cwd": "/workspace",
        "agent_id": "agent-test",
        "agent_type": "backend",
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_response": {"ok": True},
        "tool_use_id": "tool-call-1",
    }


def _ctx() -> dict[str, Any]:
    return {"signal": None}


def test_captures_patch_on_edit(task_id: str) -> None:
    sandbox = FakeSandbox([
        {"returncode": 0, "stdout": VALID_DIFF, "stderr": "", "timed_out": False},
        {"returncode": 0, "stdout": "abc1234\n", "stderr": "", "timed_out": False},
    ])
    shell_mod.register_sandbox(task_id, sandbox)  # type: ignore[arg-type]

    sink: list[Patch] = []
    hook = make_diff_capture(
        role="backend", slice_id="slice-1", task_id=task_id, sink=sink,
    )

    out = asyncio.run(
        hook(_post("Edit", {"file_path": "foo.txt"}), "tu-1", _ctx())
    )
    assert out == {}
    assert sandbox.calls == [
        ["git", "diff", "--", "foo.txt"],
        ["git", "rev-parse", "HEAD"],
    ]
    assert len(sink) == 1
    assert sink[0] == {
        "path": "foo.txt",
        "diff": VALID_DIFF,
        "author_agent": "backend",
        "slice_id": "slice-1",
        "sha": "abc1234",
    }


def test_captures_patch_on_write(task_id: str) -> None:
    sandbox = FakeSandbox([
        {"returncode": 0, "stdout": VALID_DIFF, "stderr": "", "timed_out": False},
        {"returncode": 0, "stdout": "deadbeef\n", "stderr": "", "timed_out": False},
    ])
    shell_mod.register_sandbox(task_id, sandbox)  # type: ignore[arg-type]

    sink: list[Patch] = []
    hook = make_diff_capture(
        role="unit_test", slice_id="slice-2", task_id=task_id, sink=sink,
    )
    asyncio.run(hook(_post("Write", {"file_path": "src/Foo.java"}), "tu", _ctx()))

    assert len(sink) == 1
    assert sink[0]["path"] == "src/Foo.java"
    assert sink[0]["author_agent"] == "unit_test"
    assert sink[0]["slice_id"] == "slice-2"
    assert sink[0]["sha"] == "deadbeef"


def test_ignores_non_edit_tools(task_id: str) -> None:
    sandbox = FakeSandbox()
    shell_mod.register_sandbox(task_id, sandbox)  # type: ignore[arg-type]
    sink: list[Patch] = []
    hook = make_diff_capture(
        role="backend", slice_id="slice-1", task_id=task_id, sink=sink,
    )

    for tool in ("Read", "Grep", "Glob", "Bash", "sandbox_bash"):
        asyncio.run(hook(_post(tool, {"file_path": "foo.txt"}), "tu", _ctx()))

    assert sandbox.calls == []
    assert sink == []


def test_matches_mcp_prefixed_tool_names(task_id: str) -> None:
    """MCP-served Edit/Write arrive as ``mcp__<srv>__Edit`` etc.

    The hook should recognise the suffix to stay correct regardless of
    whether the role wires Edit through a built-in or an MCP server.
    """
    sandbox = FakeSandbox([
        {"returncode": 0, "stdout": VALID_DIFF, "stderr": "", "timed_out": False},
        {"returncode": 0, "stdout": "feedface\n", "stderr": "", "timed_out": False},
    ])
    shell_mod.register_sandbox(task_id, sandbox)  # type: ignore[arg-type]

    sink: list[Patch] = []
    hook = make_diff_capture(
        role="backend", slice_id="slice-x", task_id=task_id, sink=sink,
    )
    asyncio.run(
        hook(_post("mcp__foo__Edit", {"file_path": "foo.txt"}), "tu", _ctx())
    )
    assert len(sink) == 1


def test_no_sandbox_registered_is_silent_noop(task_id: str) -> None:
    # Deliberately do NOT register a sandbox.
    sink: list[Patch] = []
    hook = make_diff_capture(
        role="backend", slice_id="slice-1", task_id=task_id, sink=sink,
    )
    out = asyncio.run(
        hook(_post("Edit", {"file_path": "foo.txt"}), "tu", _ctx())
    )
    assert out == {}
    assert sink == []


def test_empty_diff_drops_patch(task_id: str) -> None:
    sandbox = FakeSandbox([
        {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False},
    ])
    shell_mod.register_sandbox(task_id, sandbox)  # type: ignore[arg-type]
    sink: list[Patch] = []
    hook = make_diff_capture(
        role="backend", slice_id="slice-1", task_id=task_id, sink=sink,
    )
    asyncio.run(hook(_post("Edit", {"file_path": "foo.txt"}), "tu", _ctx()))
    assert sink == []
    assert sandbox.calls == [["git", "diff", "--", "foo.txt"]]


def test_git_diff_error_drops_patch(task_id: str) -> None:
    sandbox = FakeSandbox([
        {"returncode": 128, "stdout": "", "stderr": "fatal: not a repo", "timed_out": False},
    ])
    shell_mod.register_sandbox(task_id, sandbox)  # type: ignore[arg-type]
    sink: list[Patch] = []
    hook = make_diff_capture(
        role="backend", slice_id="slice-1", task_id=task_id, sink=sink,
    )
    asyncio.run(hook(_post("Edit", {"file_path": "foo.txt"}), "tu", _ctx()))
    assert sink == []


def test_malformed_diff_fails_sanity_validation(task_id: str) -> None:
    sandbox = FakeSandbox([
        {"returncode": 0, "stdout": "not a diff at all\n", "stderr": "", "timed_out": False},
    ])
    shell_mod.register_sandbox(task_id, sandbox)  # type: ignore[arg-type]
    sink: list[Patch] = []
    hook = make_diff_capture(
        role="backend", slice_id="slice-1", task_id=task_id, sink=sink,
    )
    asyncio.run(hook(_post("Edit", {"file_path": "foo.txt"}), "tu", _ctx()))
    assert sink == []


def test_missing_path_input_is_noop(task_id: str) -> None:
    sandbox = FakeSandbox()
    shell_mod.register_sandbox(task_id, sandbox)  # type: ignore[arg-type]
    sink: list[Patch] = []
    hook = make_diff_capture(
        role="backend", slice_id="slice-1", task_id=task_id, sink=sink,
    )
    asyncio.run(hook(_post("Edit", {}), "tu", _ctx()))
    assert sandbox.calls == []
    assert sink == []


def test_sha_omitted_when_rev_parse_fails(task_id: str) -> None:
    sandbox = FakeSandbox([
        {"returncode": 0, "stdout": VALID_DIFF, "stderr": "", "timed_out": False},
        {"returncode": 1, "stdout": "", "stderr": "fatal: bad ref", "timed_out": False},
    ])
    shell_mod.register_sandbox(task_id, sandbox)  # type: ignore[arg-type]
    sink: list[Patch] = []
    hook = make_diff_capture(
        role="backend", slice_id="slice-1", task_id=task_id, sink=sink,
    )
    asyncio.run(hook(_post("Edit", {"file_path": "foo.txt"}), "tu", _ctx()))
    # Patch is still recorded even when sha lookup fails — sha is just empty.
    assert len(sink) == 1
    assert sink[0]["sha"] == ""


def test_each_factory_has_independent_sink(task_id: str) -> None:
    sandbox = FakeSandbox([
        {"returncode": 0, "stdout": VALID_DIFF, "stderr": "", "timed_out": False},
        {"returncode": 0, "stdout": "aaa\n", "stderr": "", "timed_out": False},
        {"returncode": 0, "stdout": VALID_DIFF, "stderr": "", "timed_out": False},
        {"returncode": 0, "stdout": "bbb\n", "stderr": "", "timed_out": False},
    ])
    shell_mod.register_sandbox(task_id, sandbox)  # type: ignore[arg-type]

    sink_a: list[Patch] = []
    sink_b: list[Patch] = []
    hook_a = make_diff_capture("backend", "s-a", task_id, sink_a)
    hook_b = make_diff_capture("database", "s-b", task_id, sink_b)

    asyncio.run(hook_a(_post("Edit", {"file_path": "x.txt"}), "1", _ctx()))
    asyncio.run(hook_b(_post("Edit", {"file_path": "y.txt"}), "2", _ctx()))

    assert len(sink_a) == 1 and sink_a[0]["author_agent"] == "backend"
    assert len(sink_b) == 1 and sink_b[0]["author_agent"] == "database"
