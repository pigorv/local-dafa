from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from darkfactory.stages import build as build_mod
from darkfactory.stages.build import build_subgraph


def _empty_tester_output() -> dict:
    return {"summary": "", "coverage": [], "findings": [], "patches": []}


def _builder_no_changes(slice_id: str) -> dict:
    """BuilderOutput-shape dict for a no-edit fixture turn."""
    return {
        "wp_id": slice_id,
        "status": "no_changes_needed",
        "edits": [],
        "blockers": [],
        "summary": "Fixture builder did not need to edit any files.",
        "patches": [],
    }


def _patch_runners(monkeypatch):
    async def fake_builder(state):
        # Default fixture: declare no_changes_needed so the reconciliation
        # path doesn't fire for tests that aren't exercising it.
        return _builder_no_changes(state.get("current_slice") or "")

    async def fake_tester(state):
        return _empty_tester_output()

    monkeypatch.setitem(build_mod.WORKER_RUNNERS, "builder", fake_builder)
    monkeypatch.setitem(build_mod.WORKER_RUNNERS, "tester", fake_tester)


def test_build_subgraph_routes_through_supervisor_in_dependency_order(monkeypatch):
    _patch_runners(monkeypatch)
    graph = build_subgraph()

    spec = [
        {
            "story_id": "test-1",
            "approach": "cover cursor pagination",
            "affected_files": [],
            "new_files": [],
            "test_files": ["src/test/java/app/UserControllerTest.java"],
            "risks": [],
            "depends_on": ["api-1"],
        },
        {
            "story_id": "api-1",
            "approach": "add cursor param",
            "affected_files": ["src/main/java/app/UserController.java"],
            "new_files": [],
            "test_files": [],
            "risks": [],
            "depends_on": ["db-1"],
        },
        {
            "story_id": "db-1",
            "approach": "add cursor column",
            "affected_files": [],
            "new_files": ["src/main/resources/db/migration/V3__cursor.sql"],
            "test_files": [],
            "risks": [],
            "depends_on": [],
        },
    ]

    out = asyncio.run(graph.ainvoke({"spec": spec, "patches": []}))

    assert out.get("build_order") == ["db-1", "api-1", "test-1"]
    # PR C: Neither Builder nor Tester emits (worker-completion) sentinels
    # — the supervisor advances on builder_outputs / tester_outputs.
    assert (out.get("patches") or []) == []
    builder_outputs = out.get("builder_outputs") or []
    assert [o["wp_id"] for o in builder_outputs] == ["db-1", "api-1", "test-1"]
    assert all(o["status"] == "no_changes_needed" for o in builder_outputs)
    tester_outputs = out.get("tester_outputs") or []
    assert [o["wp_id"] for o in tester_outputs] == ["db-1", "api-1", "test-1"]


def test_build_subgraph_preserves_worker_coverage_entries(monkeypatch):
    _patch_runners(monkeypatch)

    coverage_entries = [
        {
            "wp_id": "test-1",
            "predicate": "cursor pagination has a regression test",
            "test_names": ["UserControllerTest.cursorPagination"],
        }
    ]

    async def fake_tester(state):
        return {
            "summary": "",
            "coverage": coverage_entries,
            "findings": [],
            "patches": [],
        }

    monkeypatch.setitem(build_mod.WORKER_RUNNERS, "tester", fake_tester)
    graph = build_subgraph()

    spec = [
        {
            "story_id": "test-1",
            "approach": "cover cursor pagination",
            "affected_files": [],
            "new_files": [],
            "test_files": ["src/test/java/app/UserControllerTest.java"],
            "risks": [],
            "depends_on": [],
        }
    ]

    out = asyncio.run(graph.ainvoke({"spec": spec, "patches": []}))

    assert out.get("coverage_entries") == coverage_entries


def test_build_subgraph_preserves_tester_findings(monkeypatch):
    _patch_runners(monkeypatch)

    findings = [
        {
            "kind": "behavior_mismatch",
            "wp_id": "test-1",
            "detail": "The implementation returns offset pagination.",
        }
    ]

    async def fake_tester(state):
        return {"summary": "", "coverage": [], "findings": findings, "patches": []}

    monkeypatch.setitem(build_mod.WORKER_RUNNERS, "tester", fake_tester)
    graph = build_subgraph()

    spec = [
        {
            "story_id": "test-1",
            "approach": "cover cursor pagination",
            "affected_files": [],
            "new_files": [],
            "test_files": ["src/test/java/app/UserControllerTest.java"],
            "risks": [],
            "depends_on": [],
        }
    ]

    out = asyncio.run(graph.ainvoke({"spec": spec, "patches": []}))

    assert out.get("tester_findings") == findings


def test_build_subgraph_ends_immediately_with_no_spec(monkeypatch):
    _patch_runners(monkeypatch)
    graph = build_subgraph()
    out = asyncio.run(graph.ainvoke({"spec": [], "patches": []}))
    assert (out.get("patches") or []) == []


# ---------- builder_outputs channel + reconciliation_findings routing ----------


_BUILD_SPEC_SINGLE = [
    {
        "story_id": "wp-1",
        "approach": "implement cursor pagination",
        "affected_files": ["src/main/java/app/UserController.java"],
        "new_files": [],
        "test_files": [],
        "risks": [],
        "depends_on": [],
    }
]


def _patch_builder_only(monkeypatch, fake_builder):
    """Patch builder and tester so only ``fake_builder`` varies."""

    async def fake_tester(state):
        return _empty_tester_output()

    monkeypatch.setitem(build_mod.WORKER_RUNNERS, "builder", fake_builder)
    monkeypatch.setitem(build_mod.WORKER_RUNNERS, "tester", fake_tester)


def test_build_subgraph_records_builder_output_and_summary(monkeypatch):
    async def fake_builder(state):
        return {
            "wp_id": "wp-1",
            "status": "done",
            "edits": [
                {
                    "path": "src/main/java/app/UserController.java",
                    "operation": "modify",
                    "intent": "Wire cursor pagination through the list endpoint.",
                }
            ],
            "blockers": [],
            "summary": "Edited UserController for cursor pagination.",
            "patches": [
                {
                    "path": "src/main/java/app/UserController.java",
                    "diff": "diff --git a/x b/x\n+ ok\n",
                    "author_agent": "builder",
                    "slice_id": "wp-1",
                }
            ],
        }

    _patch_builder_only(monkeypatch, fake_builder)
    graph = build_subgraph()
    out = asyncio.run(
        graph.ainvoke({"spec": _BUILD_SPEC_SINGLE, "patches": []})
    )
    assert (
        out.get("builder_summary")
        == "Edited UserController for cursor pagination."
    )
    outputs = out.get("builder_outputs") or []
    assert len(outputs) == 1
    assert outputs[0]["wp_id"] == "wp-1"
    assert outputs[0]["status"] == "done"
    assert outputs[0]["edits"][0]["path"] == (
        "src/main/java/app/UserController.java"
    )
    # Real patches captured: no reconciliation finding, no tester findings.
    assert out.get("reconciliation_findings") in (None, [])
    assert out.get("tester_findings") in (None, [])


def test_build_subgraph_no_changes_needed_emits_no_finding(monkeypatch):
    async def fake_builder(state):
        return {
            "wp_id": state.get("current_slice") or "",
            "status": "no_changes_needed",
            "edits": [],
            "blockers": [],
            "summary": "Looked at the code; cursor pagination already works.",
            "patches": [],
        }

    _patch_builder_only(monkeypatch, fake_builder)
    graph = build_subgraph()
    out = asyncio.run(
        graph.ainvoke({"spec": _BUILD_SPEC_SINGLE, "patches": []})
    )
    assert out.get("reconciliation_findings") in (None, [])
    assert out.get("tester_findings") in (None, [])
    outputs = out.get("builder_outputs") or []
    assert outputs and outputs[0]["status"] == "no_changes_needed"


def test_build_subgraph_blocked_status_routes_to_reconciliation(monkeypatch):
    async def fake_builder(state):
        return {
            "wp_id": "wp-1",
            "status": "blocked",
            "edits": [],
            "blockers": ["package path missing from repo"],
            "summary": "Could not find the controller package.",
            "patches": [],
        }

    _patch_builder_only(monkeypatch, fake_builder)
    graph = build_subgraph()
    out = asyncio.run(
        graph.ainvoke({"spec": _BUILD_SPEC_SINGLE, "patches": []})
    )
    # The Builder's failure surfaces on reconciliation_findings, never
    # tester_findings — that channel belongs to the Tester agent alone.
    assert out.get("tester_findings") in (None, [])
    findings = out.get("reconciliation_findings") or []
    assert len(findings) == 1
    assert findings[0]["kind"] == "builder_blocked"
    assert findings[0]["wp_id"] == "wp-1"
    assert findings[0]["producer"] == "build_subgraph"
    assert "package path missing" in findings[0]["detail"]


def test_build_subgraph_done_with_no_patches_or_edits_flags_no_action(
    monkeypatch,
):
    async def fake_builder(state):
        return {
            "wp_id": "wp-1",
            "status": "done",
            "edits": [],
            "blockers": [],
            "summary": "I didn't do anything.",
            "patches": [],
        }

    _patch_builder_only(monkeypatch, fake_builder)
    graph = build_subgraph()
    out = asyncio.run(
        graph.ainvoke({"spec": _BUILD_SPEC_SINGLE, "patches": []})
    )
    assert out.get("tester_findings") in (None, [])
    findings = out.get("reconciliation_findings") or []
    assert len(findings) == 1
    assert findings[0]["kind"] == "builder_no_action"
    assert findings[0]["wp_id"] == "wp-1"
    assert findings[0]["producer"] == "build_subgraph"


def test_build_subgraph_done_with_edits_but_no_patches_flags_unapplied(
    monkeypatch,
):
    async def fake_builder(state):
        return {
            "wp_id": "wp-1",
            "status": "done",
            "edits": [
                {
                    "path": "src/main/java/app/UserController.java",
                    "operation": "modify",
                    "intent": "Wire cursor pagination.",
                }
            ],
            "blockers": [],
            "summary": "Claimed edits the hook never saw.",
            "patches": [],
        }

    _patch_builder_only(monkeypatch, fake_builder)
    graph = build_subgraph()
    out = asyncio.run(
        graph.ainvoke({"spec": _BUILD_SPEC_SINGLE, "patches": []})
    )
    assert out.get("tester_findings") in (None, [])
    findings = out.get("reconciliation_findings") or []
    assert len(findings) == 1
    assert findings[0]["kind"] == "claimed_edits_not_applied"
    assert findings[0]["wp_id"] == "wp-1"
    assert findings[0]["claimed_paths"] == [
        "src/main/java/app/UserController.java"
    ]
    assert findings[0]["actual_paths"] == []


class _BranchInitSandbox:
    def __init__(self, *, gh_result: dict | None = None):
        self.calls: list[list[str]] = []
        self.gh_result = gh_result or {
            "returncode": 1,
            "stdout": "",
            "stderr": "gh auth missing",
        }

    def exec(self, argv, timeout=120):  # noqa: ARG002 - match RepoSandbox.exec
        self.calls.append(list(argv))
        if argv == ["gh", "api", "/user"]:
            return dict(self.gh_result)
        if argv == ["git", "branch", "--show-current"]:
            return {"returncode": 0, "stdout": "main\n", "stderr": ""}
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            return {"returncode": 1, "stdout": "", "stderr": ""}
        return {"returncode": 0, "stdout": "", "stderr": ""}


def _runtime_context(task_id: str = "wf-build-1", feature_branch: str | None = None):
    return SimpleNamespace(
        context=SimpleNamespace(
            task_id=task_id,
            repo_path="/workspace",
            feature_branch=feature_branch,
        )
    )


def test_branch_init_uses_github_token_identity_by_default(monkeypatch):
    sandbox = _BranchInitSandbox(
        gh_result={
            "returncode": 0,
            "stdout": json.dumps(
                {
                    "login": "octocat",
                    "id": 7,
                    "name": "The Octocat",
                    "email": None,
                }
            ),
            "stderr": "",
        }
    )
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(build_mod, "get_sandbox", lambda _task_id: sandbox)
    monkeypatch.setattr(build_mod, "register_sandbox", lambda *_args: None)

    assert build_mod._branch_init_node({}, runtime=_runtime_context()) == {}
    assert ["gh", "api", "/user"] in sandbox.calls
    assert ["git", "config", "user.name", "octocat"] in sandbox.calls
    assert [
        "git",
        "config",
        "user.email",
        "7+octocat@users.noreply.github.com",
    ] in sandbox.calls


def test_branch_init_falls_back_to_default_identity_without_token(monkeypatch):
    sandbox = _BranchInitSandbox()
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(build_mod, "get_sandbox", lambda _task_id: sandbox)
    monkeypatch.setattr(build_mod, "register_sandbox", lambda *_args: None)

    assert build_mod._branch_init_node({}, runtime=_runtime_context()) == {}
    assert ["gh", "api", "/user"] not in sandbox.calls
    assert ["git", "config", "user.name", build_mod.DEFAULT_GIT_NAME] in sandbox.calls
    assert [
        "git",
        "config",
        "user.email",
        build_mod.DEFAULT_GIT_EMAIL,
    ] in sandbox.calls
