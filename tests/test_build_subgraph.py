from __future__ import annotations

import asyncio

from darkfactory.agents._sdk_common import WorkerOutput
from darkfactory.stages import build as build_mod
from darkfactory.stages.build import build_subgraph


def _patch_runners(monkeypatch):
    async def fake_backend(state):
        return WorkerOutput(patches=[], summary="")

    async def fake_database(state):
        return WorkerOutput(patches=[], summary="")

    async def fake_unit_test(state):
        return WorkerOutput(patches=[], summary="")

    async def fake_frontend(state):
        return {"patches": [], "note": "no frontend work"}

    monkeypatch.setitem(build_mod.WORKER_RUNNERS, "backend", fake_backend)
    monkeypatch.setitem(build_mod.WORKER_RUNNERS, "database", fake_database)
    monkeypatch.setitem(build_mod.WORKER_RUNNERS, "unit_test", fake_unit_test)
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
            "depends_on": ["backend-1"],
        },
        {
            "story_id": "backend-1",
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

    assert out.get("build_order") == ["db-1", "backend-1", "test-1"]
    patches = out.get("patches") or []
    # Each worker was dispatched once, each emitted a completion marker.
    assert [p["slice_id"] for p in patches] == ["db-1", "backend-1", "test-1"]
    assert [p["author_agent"] for p in patches] == ["database", "backend", "unit_test"]


def test_build_subgraph_ends_immediately_with_no_spec(monkeypatch):
    _patch_runners(monkeypatch)
    graph = build_subgraph()
    out = asyncio.run(graph.ainvoke({"spec": [], "patches": []}))
    assert (out.get("patches") or []) == []
