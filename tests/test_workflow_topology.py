"""M2-19: Guard against Temporal/LangGraph topology drift.

The workflow owns the canonical stage order; worker registration owns whether
those activities can actually execute. These tests intentionally inspect the
source-level topology so a stage rename in the workflow fails unless the
activity registration and backing implementation move with it.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Iterable

from darkfactory.runtime import activities as activities_mod


ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / "src" / "darkfactory" / "cli.py"
WORKFLOW_PATH = ROOT / "src" / "darkfactory" / "runtime" / "workflow.py"
ISSUE_WORKFLOW_PATH = (
    ROOT / "src" / "darkfactory" / "runtime" / "issue_workflow.py"
)
WORKER_MAIN_PATH = ROOT / "src" / "darkfactory" / "runtime" / "worker_main.py"
ORCHESTRATOR_MAIN_PATH = (
    ROOT / "src" / "darkfactory" / "runtime" / "orchestrator_main.py"
)

SUPERVISOR_ACTIVITIES = {"setup_worker_activity", "teardown_worker_activity"}
# Source-order sequence of `execute_activity` calls in the manual workflow
# `run()` body. After Task 6.4 the merge gate has fix/rebuild branches that
# rerun the verify and reviewer stages before merging:
#   hydrate -> discovery -> build -> verify-fix loop -> pr_creator -> reviewer
#   -> merge_gate (on /df fix:    fixer  -> verify -> reviewer)
#   -> merge_gate (on /df rebuild: build -> verify -> reviewer)
#   -> merge_branch
EXPECTED_STAGE_ACTIVITIES = (
    "hydrate_stage",
    "discovery_stage",
    "build_stage",
    "verify_stage",
    "fixer_stage",
    "pr_creator_stage",
    "reviewer_stage",
    "fixer_stage",
    "verify_stage",
    "reviewer_stage",
    "build_stage",
    "verify_stage",
    "reviewer_stage",
    "merge_branch",
)
ISSUE_STAGE_ACTIVITIES = ("triage_stage",)
COMPATIBILITY_STAGE_ACTIVITIES = ("code_quality_stage",)
REGISTERED_STAGE_ACTIVITIES = (
    EXPECTED_STAGE_ACTIVITIES
    + ISSUE_STAGE_ACTIVITIES
    + COMPATIBILITY_STAGE_ACTIVITIES
)

STAGE_BACKINGS = {
    "hydrate_stage": ("darkfactory.stages.hydrator", ("hydrate", "hydrator_node")),
    "triage_stage": ("darkfactory.stages.triage", ("triage_subgraph",)),
    "discovery_stage": ("darkfactory.stages.discovery", ("discovery_subgraph",)),
    "build_stage": ("darkfactory.stages.build", ("build_subgraph",)),
    "verify_stage": ("darkfactory.stages.verify", ("verify_subgraph",)),
    "fixer_stage": ("darkfactory.agents.fixer", ("run_fixer",)),
    "code_quality_stage": ("darkfactory.runtime.activities", ("reviewer_stage",)),
    # M4 replaces these registered stubs with SDK-native agents. Until then,
    # their activity functions are the explicit backing implementation.
    "reviewer_stage": ("darkfactory.runtime.activities", ("reviewer_stage",)),
    "pr_creator_stage": ("darkfactory.runtime.activities", ("pr_creator_stage",)),
    "merge_branch": ("darkfactory.runtime.activities", ("merge_branch",)),
}


class _ExecuteActivityVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute_activity"
            and node.args
        ):
            name = _activity_name_from_ast(node.args[0])
            if name is not None:
                self.names.append(name)
        self.generic_visit(node)


def _activity_name_from_ast(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return node.id
    return None


def _workflow_run_tree(
    path: Path = WORKFLOW_PATH,
    class_name: str = "DarkFactoryWorkflow",
) -> ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text())
    for item in tree.body:
        if isinstance(item, ast.ClassDef) and item.name == class_name:
            for member in item.body:
                if isinstance(member, ast.AsyncFunctionDef) and member.name == "run":
                    return member
    raise AssertionError(f"{class_name}.run was not found")


def _workflow_class_tree(
    path: Path,
    class_name: str,
) -> ast.ClassDef:
    tree = ast.parse(path.read_text())
    for item in tree.body:
        if isinstance(item, ast.ClassDef) and item.name == class_name:
            return item
    raise AssertionError(f"{class_name} was not found in {path}")


def _workflow_method_tree(
    path: Path,
    class_name: str,
    method_name: str,
) -> ast.AsyncFunctionDef:
    cls = _workflow_class_tree(path, class_name)
    for member in cls.body:
        if isinstance(member, ast.AsyncFunctionDef) and member.name == method_name:
            return member
    raise AssertionError(f"{class_name}.{method_name} was not found")


class _SelfMethodCallVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
        ):
            self.names.append(func.attr)
        self.generic_visit(node)


def _workflow_activity_names() -> list[str]:
    visitor = _ExecuteActivityVisitor()
    visitor.visit(_workflow_run_tree())
    return visitor.names


def _activity_definition_name(fn) -> str:
    defn = getattr(fn, "__temporal_activity_definition", None)
    assert defn is not None, f"{fn} missing @activity.defn metadata"
    return defn.name


def _stage_activity_names() -> set[str]:
    return {
        _activity_definition_name(fn)
        for fn in activities_mod.STAGE_ACTIVITIES
    }


def _has_starred_stage_activities(node: ast.AST) -> bool:
    return isinstance(node, ast.Starred) and isinstance(node.value, ast.Name) and (
        node.value.id == "STAGE_ACTIVITIES"
    )


def _worker_activity_lists() -> Iterable[ast.List]:
    tree = ast.parse(WORKER_MAIN_PATH.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "Worker":
            continue
        for keyword in node.keywords:
            if keyword.arg == "activities" and isinstance(keyword.value, ast.List):
                yield keyword.value


def _imports_name(path: Path, *, module: str, name: str) -> bool:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module == module and any(alias.name == name for alias in node.names):
            return True
    return False


def _call_has_keyword(path: Path, *, call_name: str, keyword: str) -> bool:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_named_call = isinstance(func, ast.Name) and func.id == call_name
        is_attr_call = isinstance(func, ast.Attribute) and func.attr == call_name
        if (is_named_call or is_attr_call) and any(
            kw.arg == keyword for kw in node.keywords
        ):
            return True
    return False


def test_workflow_stage_order_matches_architecture() -> None:
    names = [
        name
        for name in _workflow_activity_names()
        if name not in SUPERVISOR_ACTIVITIES
    ]

    assert names == list(EXPECTED_STAGE_ACTIVITIES)


def test_issue_workflow_reviews_after_pr_creation() -> None:
    run_tree = _workflow_run_tree(
        ISSUE_WORKFLOW_PATH,
        "DarkFactoryIssueWorkflow",
    )
    activity_visitor = _ExecuteActivityVisitor()
    activity_visitor.visit(run_tree)
    run_activities = [
        name
        for name in activity_visitor.names
        if name in {"pr_creator_stage", "merge_branch"}
    ]
    assert run_activities == ["pr_creator_stage", "merge_branch"]

    method_visitor = _SelfMethodCallVisitor()
    method_visitor.visit(run_tree)
    run_helper_calls = [
        name
        for name in method_visitor.names
        if name == "_run_review_and_merge_gate"
    ]
    assert run_helper_calls == ["_run_review_and_merge_gate"]

    review_tree = _workflow_method_tree(
        ISSUE_WORKFLOW_PATH,
        "DarkFactoryIssueWorkflow",
        "_run_review_and_merge_gate",
    )
    review_visitor = _ExecuteActivityVisitor()
    review_visitor.visit(review_tree)
    assert "reviewer_stage" in review_visitor.names

    # Verify the reviewer helper is invoked between pr_creator and merge_branch.
    ordered: list[str] = []

    class _OrderedVisitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "execute_activity"
                and node.args
            ):
                name = _activity_name_from_ast(node.args[0])
                if name in {"pr_creator_stage", "merge_branch"}:
                    ordered.append(name)
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
                and func.attr == "_run_review_and_merge_gate"
            ):
                ordered.append("_run_review_and_merge_gate")
            self.generic_visit(node)

    _OrderedVisitor().visit(run_tree)
    assert ordered == [
        "pr_creator_stage",
        "_run_review_and_merge_gate",
        "merge_branch",
    ]


def test_workflow_stage_activities_match_worker_registration_bundle() -> None:
    workflow_stages = {
        name
        for name in _workflow_activity_names()
        if name not in SUPERVISOR_ACTIVITIES
    }
    registered_manual_stages = (
        _stage_activity_names()
        - set(ISSUE_STAGE_ACTIVITIES)
        - set(COMPATIBILITY_STAGE_ACTIVITIES)
    )

    assert workflow_stages == registered_manual_stages
    assert set(ISSUE_STAGE_ACTIVITIES) <= _stage_activity_names()
    assert set(COMPATIBILITY_STAGE_ACTIVITIES) <= _stage_activity_names()


def test_worker_entrypoint_registers_stage_activity_bundle() -> None:
    activity_lists = list(_worker_activity_lists())

    assert activity_lists, "worker_main.py does not construct a Worker with activities="
    assert any(
        any(_has_starred_stage_activities(element) for element in activity_list.elts)
        for activity_list in activity_lists
    )


def test_temporal_entrypoints_wire_tracing_interceptors() -> None:
    entrypoints = (CLI_PATH, WORKER_MAIN_PATH, ORCHESTRATOR_MAIN_PATH)
    for path in entrypoints:
        assert _imports_name(
            path,
            module="temporalio.contrib.opentelemetry",
            name="TracingInterceptor",
        )
        assert _call_has_keyword(path, call_name="connect", keyword="interceptors")

    for path in (WORKER_MAIN_PATH, ORCHESTRATOR_MAIN_PATH):
        assert _call_has_keyword(path, call_name="Worker", keyword="interceptors")


def test_each_workflow_stage_has_backing_implementation() -> None:
    for activity_name in REGISTERED_STAGE_ACTIVITIES:
        module_name, attribute_names = STAGE_BACKINGS[activity_name]
        module = importlib.import_module(module_name)
        assert any(hasattr(module, attr) for attr in attribute_names), (
            f"{activity_name} must be backed by {module_name}."
            f"{'|'.join(attribute_names)}"
        )
