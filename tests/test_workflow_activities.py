"""M2-13: Stage activities exist, are registrable, and dispatch correctly.

Each `@activity.defn` is exercised hermetically — subgraphs and SDK clients
are monkeypatched so the test does not require Docker, the Anthropic API,
or a live Temporal server. End-to-end behavior (hand-run reaching the merge
gate) lands once M2-14 (workflow rewrite) and M2-15 (stages call
`run_<role>` instead of broken `build_<role>_agent` factories) ship.
"""
from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from darkfactory.agents.fixer import FixerOutput
from darkfactory.state import ReviewerSummary, RunRequest, init_state, merge
from darkfactory.runtime import activities as activities_mod
from darkfactory.runtime.activities import (
    STAGE_ACTIVITIES,
    build_stage,
    code_quality_stage,
    reviewer_stage,
    discovery_stage,
    fixer_stage,
    hydrate_stage,
    merge_branch,
    ping_activity,
    pr_creator_stage,
    setup_worker_activity,
    teardown_worker_activity,
    triage_stage,
    verify_stage,
)


def _activity_definition(fn: Any):
    return getattr(fn, "__temporal_activity_definition", None)


def test_each_stage_activity_has_temporal_definition():
    expected_names = {
        "hydrate_stage",
        "triage_stage",
        "discovery_stage",
        "build_stage",
        "verify_stage",
        "fixer_stage",
        "reviewer_stage",
        "code_quality_stage",
        "pr_creator_stage",
        "merge_branch",
    }
    seen: set[str] = set()
    for fn in STAGE_ACTIVITIES:
        defn = _activity_definition(fn)
        assert defn is not None, f"{fn} missing @activity.defn metadata"
        seen.add(defn.name)
    assert seen == expected_names


def test_stage_activities_register_on_a_worker():
    """Construct a Temporal Worker against a (no-op) Client and confirm registration."""
    from temporalio.client import Client
    from temporalio.worker import Worker

    # Lazy stub: Worker only inspects the activities, never connects.
    class _StubClient:
        def __init__(self):
            self.config = lambda: {"namespace": "default"}

    # Use a real Temporal client constructed via from_url with no I/O is tricky;
    # instead, drive the public _Definition path which is what Worker uses.
    from temporalio.activity import _Definition  # noqa: PLC2701 — internal but stable

    for fn in (
        ping_activity,
        setup_worker_activity,
        teardown_worker_activity,
        *STAGE_ACTIVITIES,
    ):
        defn = _Definition.from_callable(fn)
        assert defn is not None
        assert defn.name


class _FakeContainer:
    def __init__(self):
        self.exec_calls: list[dict[str, Any]] = []

    def exec_run(self, cmd, **kwargs):
        self.exec_calls.append({"cmd": cmd, **kwargs})
        return types.SimpleNamespace(exit_code=0, output=b"")


class _FakeContainersAPI:
    def __init__(self, container: _FakeContainer):
        self._container = container
        self.run_calls: list[dict[str, Any]] = []

    def get(self, _name):
        from docker.errors import NotFound
        raise NotFound("not found")

    def run(self, image=None, **kwargs):
        call = {"image": image, **kwargs}
        self.run_calls.append(call)
        return self._container

    @property
    def run_kwargs(self) -> dict[str, Any] | None:
        # The worker container is the last `containers.run` call;
        # earlier calls (e.g. the alpine init that creates the volume subpath)
        # are inspected directly via `run_calls` when needed.
        for call in reversed(self.run_calls):
            if call.get("image") and "worker" in str(call["image"]):
                return call
        return self.run_calls[-1] if self.run_calls else None


class _FakeDockerClient:
    def __init__(self, container: _FakeContainer):
        self.containers = _FakeContainersAPI(container)


def _patch_docker_for_setup(monkeypatch) -> tuple[_FakeContainer, _FakeContainersAPI]:
    container = _FakeContainer()
    fake_client = _FakeDockerClient(container)
    monkeypatch.setattr(
        activities_mod.docker, "from_env", lambda: fake_client
    )
    return container, fake_client.containers


def _assert_transcripts_mount(run_kwargs: dict[str, Any], wf_id: str) -> None:
    mounts = run_kwargs.get("mounts") or []
    assert len(mounts) == 1, f"expected exactly one Mount, got {mounts!r}"
    m = mounts[0]
    assert m["Target"] == "/home/agent/.claude"
    assert m["Source"] == "darkfactory_raw-claude"
    assert m["Type"] == "volume"
    assert m["VolumeOptions"] == {"Subpath": wf_id}


def _assert_subpath_init(run_calls: list[dict[str, Any]], wf_id: str) -> None:
    init_calls = [c for c in run_calls if str(c.get("image", "")).startswith("alpine")]
    assert init_calls, f"expected an alpine init container, got {run_calls!r}"
    init = init_calls[0]
    cmd = init.get("command") or []
    assert wf_id in " ".join(cmd), f"alpine init must reference {wf_id}: {cmd!r}"
    assert "1000:1000" in " ".join(cmd), "init must chown subpath to uid 1000"
    assert init.get("remove") is True
    init_mounts = init.get("mounts") or []
    assert any(
        m["Source"] == "darkfactory_raw-claude" for m in init_mounts
    ), "init must mount the transcripts volume"


def test_setup_worker_activity_bind_mounts_local_path(monkeypatch):
    container, containers_api = _patch_docker_for_setup(monkeypatch)

    name = asyncio.run(setup_worker_activity("wf-local", "/host/path/to/repo"))

    assert name.endswith("wf-local")
    _assert_subpath_init(containers_api.run_calls, "wf-local")
    assert containers_api.run_kwargs is not None
    assert containers_api.run_kwargs["volumes"] == {
        "/host/path/to/repo": {"bind": "/workspace", "mode": "rw"}
    }
    _assert_transcripts_mount(containers_api.run_kwargs, "wf-local")
    cmds = [call["cmd"] for call in container.exec_calls]
    # Only the branch checkout should fire — no clone for local-path runs.
    assert all(cmd[0] == "git" for cmd in cmds)
    assert not any(cmd[0] == "gh" for cmd in cmds)


def test_setup_worker_activity_clones_when_repo_url_given(monkeypatch):
    container, containers_api = _patch_docker_for_setup(monkeypatch)
    url = "https://github.com/pigorv/dark-factory-target-test.git"

    name = asyncio.run(setup_worker_activity("wf-issue", url))

    assert name.endswith("wf-issue")
    _assert_subpath_init(containers_api.run_calls, "wf-issue")
    assert containers_api.run_kwargs is not None
    # No bind mount: the URL would be an invalid volume spec.
    assert containers_api.run_kwargs["volumes"] == {}
    _assert_transcripts_mount(containers_api.run_kwargs, "wf-issue")

    cmds = [call["cmd"] for call in container.exec_calls]
    assert cmds[0] == ["gh", "repo", "clone", url, "/workspace"]
    assert any(cmd[0] == "git" and "checkout" in cmd for cmd in cmds[1:])


def test_setup_worker_activity_raises_when_clone_fails(monkeypatch):
    container, _ = _patch_docker_for_setup(monkeypatch)

    def failing_exec(cmd, **kwargs):
        if cmd[:2] == ["gh", "repo"]:
            return types.SimpleNamespace(exit_code=128, output=b"auth required")
        return types.SimpleNamespace(exit_code=0, output=b"")

    container.exec_run = failing_exec  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="gh repo clone failed"):
        asyncio.run(
            setup_worker_activity("wf-bad", "https://github.com/foo/bar.git")
        )


def test_hydrate_stage_returns_repo_context(tmp_path):
    """`hydrate_stage` reads `repo_path` from state and returns a `repo_context` dict."""
    (tmp_path / "AGENTS.md").write_text("demo repo")
    out = asyncio.run(hydrate_stage({"repo_path": str(tmp_path)}))
    assert "repo_context" in out
    assert out["repo_context"]["agents_md"] == "demo repo"
    assert "repo_map" in out["repo_context"]


def test_hydrate_stage_manual_run_state_skips_issue_context(monkeypatch, tmp_path):
    """Manual `darkfactory run "<prompt>"` state does not trigger issue hydration."""
    (tmp_path / "AGENTS.md").write_text("manual repo")

    def fail_issue_collection(*_args, **_kwargs):
        raise AssertionError("issue collection should not run for manual runs")

    monkeypatch.setattr(
        "darkfactory.stages.hydrator._collect_issue_context",
        fail_issue_collection,
    )

    state = init_state(
        RunRequest(
            repo_url=str(tmp_path),
            repo_path=str(tmp_path),
            user_request="keep the manual flow working",
            model_profile=None,
        )
    )

    assert "issue" not in state
    out = asyncio.run(hydrate_stage(state))
    merged = merge(state, out)

    assert merged["user_request"] == "keep the manual flow working"
    assert merged["repo_context"]["agents_md"] == "manual repo"
    assert "issue" not in merged
    assert "issue_comments" not in merged


def _install_fake_module(monkeypatch, name: str, **attrs) -> types.ModuleType:
    """Replace `sys.modules[name]` with a fresh ModuleType carrying the given attrs.

    A real `ModuleType` (rather than a `type()()` instance) keeps unbound
    callables unbound so the lazy `from <name> import <attr>` inside an
    activity body retrieves the function, not a bound method.
    """
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


class _FakeRepoSandbox:
    def exec(self, argv, timeout=120):  # noqa: ARG002 — match RepoSandbox.exec
        return {"returncode": 0, "stdout": "", "stderr": ""}


def test_discovery_stage_invokes_subgraph(monkeypatch):
    """`discovery_stage` lazy-imports `discovery_subgraph` and returns the channels it produces."""
    recorded: dict[str, Any] = {}

    class FakeSubgraph:
        async def ainvoke(self, state):
            recorded["state"] = state
            return {
                "stories": [{"id": "US-1", "title": "x"}],
                "spec": [{"story_id": "US-1"}],
                "review_decision": {"approved": True, "reason": "", "edits": {}},
            }

    _install_fake_module(
        monkeypatch,
        "darkfactory.stages.discovery",
        discovery_subgraph=lambda: FakeSubgraph(),
    )

    out = asyncio.run(discovery_stage({"user_request": "hi"}))
    assert recorded["state"] == {"user_request": "hi"}
    assert out["stories"] == [{"id": "US-1", "title": "x"}]
    assert out["spec"] == [{"story_id": "US-1"}]
    assert out["review_decision"]["approved"] is True


def test_triage_stage_invokes_subgraph(monkeypatch):
    """`triage_stage` lazy-imports `triage_subgraph` and returns its decision delta."""
    recorded: dict[str, Any] = {}

    class FakeSubgraph:
        async def ainvoke(self, state):
            recorded["state"] = state
            return {
                **state,
                "ready_to_build": False,
                "clarification_questions": ["Which API should this use?"],
                "derived_user_request": "",
                "confidence": "medium",
                "rationale": "The issue names the symptom but not the API.",
            }

    _install_fake_module(
        monkeypatch,
        "darkfactory.stages.triage",
        triage_subgraph=lambda: FakeSubgraph(),
    )

    state = {"issue": {"number": 7, "title": "Add export"}}
    out = asyncio.run(triage_stage(state))
    assert recorded["state"] is state
    assert out == {
        "ready_to_build": False,
        "clarification_questions": ["Which API should this use?"],
        "derived_user_request": "",
        "confidence": "medium",
        "rationale": "The issue names the symptom but not the API.",
    }


def test_build_stage_invokes_subgraph_with_runctx(monkeypatch):
    """`build_stage` reconstructs `RunContext` from state fields and passes it as `context=`."""
    recorded: dict[str, Any] = {}
    monkeypatch.setattr(activities_mod, "get_sandbox", lambda _task_id: _FakeRepoSandbox())

    class FakeSubgraph:
        async def ainvoke(self, state, context=None):
            recorded["state"] = state
            recorded["context"] = context
            return {
                "build_order": ["a", "b"],
                "current_slice": "b",
                "coverage_entries": [
                    {
                        "wp_id": "a",
                        "predicate": "pagination behavior is covered",
                        "test_names": ["test_cursor_page"],
                    }
                ],
                "tester_findings": [
                    {
                        "kind": "behavior_mismatch",
                        "wp_id": "a",
                        "detail": "Cursor pagination still returns offsets.",
                    }
                ],
                "patches": [{"slice_id": "a", "path": "x", "diff": "", "author_agent": "builder"}],
            }

    _install_fake_module(
        monkeypatch,
        "darkfactory.stages.build",
        build_subgraph=lambda: FakeSubgraph(),
    )

    state = {
        "spec": [{"story_id": "a"}, {"story_id": "b"}],
        "patches": [],
        "repo_path": "/workspace",
        "task_id": "wf-build-1",
    }
    out = asyncio.run(build_stage(state))
    assert recorded["state"] is state
    assert recorded["context"] is not None
    assert recorded["context"].task_id == "wf-build-1"
    assert recorded["context"].repo_path == "/workspace"
    assert out["build_order"] == ["a", "b"]
    assert out["coverage_entries"][0]["wp_id"] == "a"
    assert out["tester_findings"][0]["wp_id"] == "a"
    assert out["patches"][0]["slice_id"] == "a"


def test_build_stage_returns_empty_tester_findings_when_subgraph_has_none(monkeypatch):
    monkeypatch.setattr(activities_mod, "get_sandbox", lambda _task_id: _FakeRepoSandbox())

    class FakeSubgraph:
        async def ainvoke(self, state, context=None):
            return {"build_order": [], "current_slice": "", "patches": []}

    _install_fake_module(
        monkeypatch,
        "darkfactory.stages.build",
        build_subgraph=lambda: FakeSubgraph(),
    )

    out = asyncio.run(
        build_stage(
            {
                "spec": [],
                "patches": [],
                "repo_path": "/workspace",
                "task_id": "wf-build-empty",
            }
        )
    )

    assert out["tester_findings"] == []


def test_verify_stage_invokes_subgraph_with_runctx(monkeypatch):
    """`verify_stage` runs the parallel subgraph and surfaces its three channels."""
    recorded: dict[str, Any] = {}
    monkeypatch.setattr(activities_mod, "get_sandbox", lambda _task_id: _FakeRepoSandbox())

    class FakeSubgraph:
        async def ainvoke(self, state, context=None):
            recorded["context"] = context
            return {
                "test_results": [{"runner": "maven", "returncode": 0,
                                  "passed": 3, "failed": 0, "errors": [], "duration_s": 0.1}],
                "findings": [],
                "verify_summary": {"passed": True, "failed_tests": 0, "hard_findings": 0},
                "verify_retries": 0,
            }

    _install_fake_module(
        monkeypatch,
        "darkfactory.stages.verify",
        verify_subgraph=lambda: FakeSubgraph(),
    )

    state = {"repo_path": "/workspace", "task_id": "wf-verify-1"}
    out = asyncio.run(verify_stage(state))
    assert recorded["context"].task_id == "wf-verify-1"
    assert out["verify_summary"]["passed"] is True
    assert out["test_results"][0]["passed"] == 3
    assert out["findings"] == []
    assert out["verify_retries"] == 0


def test_fixer_stage_records_fixer_patches(monkeypatch):
    """Fixer patches are surfaced with the new author and decision channel."""
    out_model = FixerOutput(
        decision="fixed",
        target_wp="WP-1",
        target_predicates=["GET /users/{id} returns 404"],
        summary="Mapped missing users to 404.",
        reason="The failure is within the approved brief.",
        patches=[
            {
                "path": "src/main/java/app/UserController.java",
                "diff": "--- a\n+++ b\n@@ -1 +1 @@\n-foo\n+bar\n",
                "author_agent": "builder",
                "slice_id": "",
            }
        ],
    )

    async def fake_run_fixer(state):
        return out_model

    _install_fake_module(
        monkeypatch,
        "darkfactory.agents.fixer",
        run_fixer=fake_run_fixer,
    )

    delta = asyncio.run(fixer_stage({}))
    assert delta["current_slice"] == "WP-1"
    assert delta["fixer_decision"]["decision"] == "fixed"
    assert delta["patches"][0]["author_agent"] == "fixer"
    assert delta["patches"][0]["slice_id"] == "WP-1"
    assert delta["patches"][0]["path"].endswith("UserController.java")


def test_fixer_stage_rejects_partial_patch():
    """Captured patches still need the fields required by workflow state."""
    bad = FixerOutput(
        decision="fixed",
        target_wp="WP-1",
        patches=[{"path": "pom.xml"}],
    )
    with pytest.raises(ValueError, match="fixer patch missing required fields"):
        activities_mod._fixer_delta(bad)


def test_reviewer_stage_flows_summary_into_review_decision(monkeypatch):
    """`reviewer_stage` returns the channel consumed by the HITL gate."""
    summary = ReviewerSummary(
        severity="low",
        issues=[],
        recommendation="approve",
    )

    async def fake_run_reviewer(state):
        assert state["verify_summary"]["passed"] is True
        return summary

    _install_fake_module(
        monkeypatch,
        "darkfactory.agents.reviewer",
        run_reviewer=fake_run_reviewer,
    )

    delta = asyncio.run(
        reviewer_stage(
            {"verify_summary": {"passed": True, "failed_tests": 0, "hard_findings": 0}}
        )
    )
    assert delta["review_decision"] == summary.model_dump()


def test_code_quality_stage_alias_flows_summary_into_review_decision(monkeypatch):
    """The historical activity name stays registered for old Temporal histories."""
    summary = ReviewerSummary(
        severity="low",
        issues=[],
        recommendation="approve",
    )

    async def fake_run_reviewer(state):
        assert state["verify_summary"]["passed"] is True
        return summary

    _install_fake_module(
        monkeypatch,
        "darkfactory.agents.reviewer",
        run_reviewer=fake_run_reviewer,
    )

    delta = asyncio.run(
        code_quality_stage(
            {"verify_summary": {"passed": True, "failed_tests": 0, "hard_findings": 0}}
        )
    )
    assert delta["review_decision"] == summary.model_dump()


def test_pr_creator_stage_flows_url_into_pr_url(monkeypatch):
    """`pr_creator_stage` returns the channel consumed by merge_branch."""
    monkeypatch.setattr(activities_mod, "get_sandbox", lambda _task_id: _FakeRepoSandbox())

    async def fake_run_pr_creator(state):
        assert state["gate_approved"] is True
        return "https://github.com/acme/demo/pull/42"

    _install_fake_module(
        monkeypatch,
        "darkfactory.agents.pr_creator",
        run_pr_creator=fake_run_pr_creator,
    )

    delta = asyncio.run(
        pr_creator_stage(
            {
                "wf_id": "wf-pr-1",
                "task_id": "wf-pr-1",
                "repo_path": "/workspace",
                "gate_approved": True,
            }
        )
    )
    assert delta == {"pr_url": "https://github.com/acme/demo/pull/42"}


def test_merge_branch_requires_pr_url(monkeypatch):
    """`merge_branch` should fail loudly if the PR creator did not publish a URL."""
    monkeypatch.setattr(activities_mod, "get_sandbox", lambda _task_id: _FakeRepoSandbox())

    with pytest.raises(ValueError, match="state\\['pr_url'\\]"):
        asyncio.run(
            merge_branch(
                {
                    "wf_id": "wf-merge-missing-url",
                    "task_id": "wf-merge-missing-url",
                    "repo_path": "/workspace",
                }
            )
        )
