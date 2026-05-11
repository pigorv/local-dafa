"""CI lint for the consolidated agent harness.

Runs as a normal pytest collection so it executes wherever the rest of the
suite does. Covers four invariants from the migration plan:

1. Every manifest under ``agents/manifests/`` validates against the schema,
   has a known hook list, and resolves its prompt path.
2. ``@workflow.defn`` modules never import the registry or composer
   (replay-determinism rail; risk-table row 1).
3. ``agents/*.py`` modules never invoke ``compose(...)`` at module top
   level (risk-table row 2 — wrong OTel span context).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from darkfactory.agents.registry import (
    DEFAULT_MANIFESTS_DIR,
    ManifestRegistryError,
    load_registry,
)


_REPO_ROOT = Path(__file__).resolve().parents[1]
_AGENTS_DIR = _REPO_ROOT / "src" / "darkfactory" / "agents"
_RUNTIME_DIR = _REPO_ROOT / "src" / "darkfactory" / "runtime"

_FORBIDDEN_WORKFLOW_IMPORTS = (
    "darkfactory.agents.registry",
    "darkfactory.agents.compose",
)


def test_default_manifests_directory_loads_cleanly() -> None:
    """Bullets 1–3: schema, hook names, prompt paths."""
    load_registry(DEFAULT_MANIFESTS_DIR)


def test_broken_manifest_is_rejected(tmp_path: Path) -> None:
    """Negative control: the lint must fail when a manifest is broken."""
    bad = tmp_path / "broken.yaml"
    bad.write_text("identity: not-a-mapping\n", encoding="utf-8")

    with pytest.raises(ManifestRegistryError):
        load_registry(tmp_path)


def _iter_workflow_modules() -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in _RUNTIME_DIR.glob("*.py")
            if _module_defines_workflow(path)
        )
    )


def _module_defines_workflow(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    return "@workflow.defn" in source


def _forbidden_workflow_imports(module_path: Path, source: str) -> list[str]:
    offenders: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _FORBIDDEN_WORKFLOW_IMPORTS:
                    offenders.append(
                        f"{module_path}:{node.lineno}: import {alias.name}"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module in _FORBIDDEN_WORKFLOW_IMPORTS:
                offenders.append(
                    f"{module_path}:{node.lineno}: "
                    f"from {node.module} import ..."
                )
    return offenders


def test_workflow_modules_do_not_import_registry_or_composer() -> None:
    """Risk-table row 1: registry/composer imports break replay determinism."""
    offenders: list[str] = []
    for module_path in _iter_workflow_modules():
        offenders.extend(
            _forbidden_workflow_imports(
                module_path, module_path.read_text(encoding="utf-8")
            )
        )

    assert not offenders, (
        "workflow modules must not import registry or composer "
        "(replay-determinism rail):\n" + "\n".join(offenders)
    )


def test_workflow_import_lint_catches_registry_import() -> None:
    """Negative control: synthetic source with the forbidden import is flagged."""
    source = (
        "from temporalio import workflow\n"
        "from darkfactory.agents.registry import get_default_registry\n"
        "@workflow.defn\n"
        "class Bad:\n"
        "    pass\n"
    )

    offenders = _forbidden_workflow_imports(Path("synthetic.py"), source)

    assert offenders, "lint should reject registry import in workflow module"


def test_workflow_import_lint_catches_composer_import() -> None:
    source = (
        "from temporalio import workflow\n"
        "import darkfactory.agents.compose\n"
        "@workflow.defn\n"
        "class Bad:\n"
        "    pass\n"
    )

    offenders = _forbidden_workflow_imports(Path("synthetic.py"), source)

    assert offenders, "lint should reject composer import in workflow module"


def test_workflow_modules_grep_guard_matches_ast() -> None:
    """Belt-and-suspenders: a plain grep over the same modules also returns
    nothing. Catches imports introduced via dynamic patterns the AST walk
    above might miss (e.g. ``importlib.import_module`` literals)."""
    pattern = re.compile(
        r"darkfactory\.agents\.(registry|compose)",
    )
    offenders: list[str] = []
    for module_path in _iter_workflow_modules():
        source = module_path.read_text(encoding="utf-8")
        for lineno, line in enumerate(source.splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{module_path}:{lineno}: {line.strip()}")

    assert not offenders, (
        "workflow modules reference registry/composer:\n"
        + "\n".join(offenders)
    )


def _iter_agent_modules() -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in _AGENTS_DIR.glob("*.py")
            if path.name not in {"compose.py"}
        )
    )


def _top_level_compose_calls(module_path: Path, source: str) -> list[str]:
    offenders: list[str] = []
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Expr) and _is_compose_call(node.value):
            offenders.append(
                f"{module_path}:{node.lineno}: top-level compose(...) call"
            )
        elif isinstance(node, ast.Assign) and _is_compose_call(node.value):
            offenders.append(
                f"{module_path}:{node.lineno}: top-level compose(...) call"
            )
    return offenders


def test_agents_do_not_call_compose_at_module_top_level() -> None:
    """Risk-table row 2: composer must run inside an activity span only."""
    offenders: list[str] = []
    for module_path in _iter_agent_modules():
        offenders.extend(
            _top_level_compose_calls(
                module_path, module_path.read_text(encoding="utf-8")
            )
        )

    assert not offenders, (
        "agent modules must not call compose(...) at module top level:\n"
        + "\n".join(offenders)
    )


def test_top_level_compose_lint_catches_offenders() -> None:
    """Negative control: top-level compose calls (expr + assign) flagged."""
    source = (
        "from darkfactory.agents.compose import compose\n"
        "compose('builder', None)\n"
        "_CLIENT = compose('tester', None)\n"
        "def run():\n"
        "    return compose('po', None)  # this one is fine\n"
    )

    offenders = _top_level_compose_calls(Path("synthetic.py"), source)

    assert len(offenders) == 2, offenders


def _is_compose_call(node: ast.AST | None) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == "compose":
        return True
    if isinstance(func, ast.Attribute) and func.attr == "compose":
        return True
    return False
