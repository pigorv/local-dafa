from __future__ import annotations

from langgraph.graph import END

from darkfactory.agents.builder_supervisor import (
    builder_supervisor_node,
    route_slice,
    topo_sort,
)
from darkfactory.state import Patch, SpecSlice


def _slice(
    story_id: str,
    *,
    depends_on: list[str] | None = None,
    affected_files: list[str] | None = None,
    new_files: list[str] | None = None,
    test_files: list[str] | None = None,
) -> SpecSlice:
    return SpecSlice(
        story_id=story_id,
        approach="",
        affected_files=affected_files or [],
        new_files=new_files or [],
        test_files=test_files or [],
        risks=[],
        depends_on=depends_on or [],
    )


def _patch(slice_id: str) -> Patch:
    return Patch(path="x", diff="", author_agent="t", slice_id=slice_id)


def test_topo_sort_db_backend_test_order():
    spec = [
        _slice("test-1", depends_on=["backend-1"]),
        _slice("backend-1", depends_on=["db-1"]),
        _slice("db-1"),
    ]
    assert topo_sort(spec) == ["db-1", "backend-1", "test-1"]


def test_route_slice_assigns_workers_by_paths():
    db = _slice("d", new_files=["src/main/resources/db/migration/V3__x.sql"])
    backend = _slice("b", affected_files=["src/main/java/foo/UserController.java"])
    test = _slice("t", test_files=["src/test/java/foo/UserControllerTest.java"])
    frontend = _slice("f", affected_files=["web/src/App.tsx"])
    assert route_slice(db) == "database"
    assert route_slice(backend) == "backend"
    assert route_slice(test) == "unit_test"
    assert route_slice(frontend) == "frontend"


def test_supervisor_dispatches_db_then_backend_then_test_in_order():
    spec = [
        _slice(
            "test-1",
            depends_on=["backend-1"],
            test_files=["src/test/java/foo/UserControllerTest.java"],
        ),
        _slice(
            "backend-1",
            depends_on=["db-1"],
            affected_files=["src/main/java/foo/UserController.java"],
        ),
        _slice("db-1", new_files=["src/main/resources/db/migration/V3__add_cursor.sql"]),
    ]
    state: dict = {"spec": spec, "patches": []}

    cmd1 = builder_supervisor_node(state)
    assert cmd1.goto == "database"
    assert cmd1.update["current_slice"] == "db-1"
    assert cmd1.update["build_order"] == ["db-1", "backend-1", "test-1"]

    state.update(cmd1.update)
    state["patches"] = [_patch("db-1")]
    cmd2 = builder_supervisor_node(state)
    assert cmd2.goto == "backend"
    assert cmd2.update["current_slice"] == "backend-1"

    state.update(cmd2.update)
    state["patches"].append(_patch("backend-1"))
    cmd3 = builder_supervisor_node(state)
    assert cmd3.goto == "unit_test"
    assert cmd3.update["current_slice"] == "test-1"

    state.update(cmd3.update)
    state["patches"].append(_patch("test-1"))
    cmd4 = builder_supervisor_node(state)
    assert cmd4.goto == END


def test_supervisor_finishes_when_no_spec():
    cmd = builder_supervisor_node({})
    assert cmd.goto == END
