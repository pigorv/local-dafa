from __future__ import annotations

from langgraph.graph import END

from darkfactory.agents.builder_supervisor import (
    builder_supervisor_node,
    route_slice,
    topo_sort,
)
from darkfactory.state import Patch, WorkPackageDict


def _slice(
    story_id: str,
    *,
    depends_on: list[str] | None = None,
    affected_files: list[str] | None = None,
    new_files: list[str] | None = None,
    test_files: list[str] | None = None,
) -> WorkPackageDict:
    return WorkPackageDict(
        story_id=story_id,
        approach="",
        affected_files=affected_files or [],
        new_files=new_files or [],
        test_files=test_files or [],
        risks=[],
        depends_on=depends_on or [],
    )


def _patch(slice_id: str, author_agent: str) -> Patch:
    return Patch(path="x", diff="", author_agent=author_agent, slice_id=slice_id)


def _builder_output(slice_id: str, status: str = "done") -> dict:
    return {
        "wp_id": slice_id,
        "status": status,
        "edits": [],
        "blockers": [],
        "summary": "",
    }


def test_topo_sort_db_api_test_order():
    spec = [
        _slice("test-1", depends_on=["api-1"]),
        _slice("api-1", depends_on=["db-1"]),
        _slice("db-1"),
    ]
    assert topo_sort(spec) == ["db-1", "api-1", "test-1"]


def test_route_slice_assigns_workers_by_paths():
    db = _slice("d", new_files=["src/main/resources/db/migration/V3__x.sql"])
    api = _slice("b", affected_files=["src/main/java/foo/UserController.java"])
    test = _slice("t", test_files=["src/test/java/foo/UserControllerTest.java"])
    frontend = _slice("f", affected_files=["web/src/App.tsx"])
    assert route_slice(db) == "builder"
    assert route_slice(api) == "builder"
    assert route_slice(test) == "builder"
    assert route_slice(frontend) == "frontend"


def _tester_output(slice_id: str, *, parse_failure: bool = False) -> dict:
    return {
        "wp_id": slice_id,
        "summary": "",
        "coverage": [],
        "findings": [],
        "parse_failure": parse_failure,
    }


def test_supervisor_dispatches_builder_then_tester_per_slice_in_dependency_order():
    """PR C: Builder and Tester completion are keyed on
    ``builder_outputs`` / ``tester_outputs`` entries rather than on
    sentinel patches. Frontend keeps the legacy patch-sentinel path.
    """
    spec = [
        _slice(
            "test-1",
            depends_on=["api-1"],
            test_files=["src/test/java/foo/UserControllerTest.java"],
        ),
        _slice(
            "api-1",
            depends_on=["db-1"],
            affected_files=["src/main/java/foo/UserController.java"],
        ),
        _slice("db-1", new_files=["src/main/resources/db/migration/V3__add_cursor.sql"]),
    ]
    state: dict = {
        "spec": spec,
        "patches": [],
        "builder_outputs": [],
        "tester_outputs": [],
    }

    cmd1 = builder_supervisor_node(state)
    assert cmd1.goto == "builder"
    assert cmd1.update["current_slice"] == "db-1"
    assert cmd1.update["build_order"] == ["db-1", "api-1", "test-1"]

    state.update(cmd1.update)
    state["builder_outputs"].append(_builder_output("db-1"))
    cmd2 = builder_supervisor_node(state)
    assert cmd2.goto == "tester"
    assert cmd2.update["current_slice"] == "db-1"

    state.update(cmd2.update)
    state["tester_outputs"].append(_tester_output("db-1"))
    cmd3 = builder_supervisor_node(state)
    assert cmd3.goto == "builder"
    assert cmd3.update["current_slice"] == "api-1"

    state.update(cmd3.update)
    state["builder_outputs"].append(_builder_output("api-1"))
    cmd4 = builder_supervisor_node(state)
    assert cmd4.goto == "tester"
    assert cmd4.update["current_slice"] == "api-1"

    state.update(cmd4.update)
    state["tester_outputs"].append(_tester_output("api-1"))
    cmd5 = builder_supervisor_node(state)
    assert cmd5.goto == "builder"
    assert cmd5.update["current_slice"] == "test-1"

    state.update(cmd5.update)
    state["builder_outputs"].append(_builder_output("test-1"))
    cmd6 = builder_supervisor_node(state)
    assert cmd6.goto == "tester"
    assert cmd6.update["current_slice"] == "test-1"

    state.update(cmd6.update)
    state["tester_outputs"].append(_tester_output("test-1"))
    cmd7 = builder_supervisor_node(state)
    assert cmd7.goto == END


def test_supervisor_finishes_when_no_spec():
    cmd = builder_supervisor_node({})
    assert cmd.goto == END
