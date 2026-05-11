from __future__ import annotations

import asyncio

from darkfactory.agents._sdk_common import WorkerOutput
from darkfactory.agents import tester as tester_mod
from darkfactory.stages import build as build_mod
from darkfactory.stages.build import build_subgraph


def _patch_runners(monkeypatch):
    async def fake_builder(state):
        return WorkerOutput(patches=[], summary="")

    async def fake_tester(state):
        return tester_mod.TesterOutput()

    async def fake_frontend(state):
        return {"patches": [], "note": "no frontend work"}

    monkeypatch.setitem(build_mod.WORKER_RUNNERS, "builder", fake_builder)
    monkeypatch.setitem(build_mod.WORKER_RUNNERS, "tester", fake_tester)
    monkeypatch.setitem(build_mod.WORKER_RUNNERS, "frontend", fake_frontend)


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
    patches = out.get("patches") or []
    # Builder and Tester each emitted a completion marker for each WP.
    assert [p["slice_id"] for p in patches] == [
        "db-1",
        "db-1",
        "api-1",
        "api-1",
        "test-1",
        "test-1",
    ]
    assert [p["author_agent"] for p in patches] == [
        "builder",
        "tester",
        "builder",
        "tester",
        "builder",
        "tester",
    ]


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
        return {"patches": [], "coverage_entries": coverage_entries}

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
        tester_mod.TesterFinding(
            kind="behavior_mismatch",
            wp_id="test-1",
            detail="The implementation returns offset pagination.",
        )
    ]

    async def fake_tester(state):
        return tester_mod.TesterOutput(findings=findings)

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

    assert out.get("tester_findings") == [finding.model_dump() for finding in findings]


def test_build_subgraph_ends_immediately_with_no_spec(monkeypatch):
    _patch_runners(monkeypatch)
    graph = build_subgraph()
    out = asyncio.run(graph.ainvoke({"spec": [], "patches": []}))
    assert (out.get("patches") or []) == []
