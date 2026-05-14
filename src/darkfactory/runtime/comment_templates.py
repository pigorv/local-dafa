"""Jinja2 environment for GitHub issue/PR comment templates.

Templates live in ``src/darkfactory/templates/comments/`` and are read into
memory once at import time (via ``importlib.resources``) so that:

* rendering is a pure in-memory string substitution — no filesystem access at
  workflow runtime, which keeps the Temporal workflow sandbox happy and keeps
  replay deterministic;
* template edits ship with the wheel and only take effect on worker restart.
"""

from __future__ import annotations

from collections.abc import Iterable
from importlib.resources import files
from typing import Any

from jinja2 import DictLoader, Environment


# Explicit list (rather than iterdir) so the loader never touches the
# filesystem at workflow-sandbox-restricted runtime — adding a new template
# means adding a name here.
_TEMPLATE_NAMES: tuple[str, ...] = (
    "phase_triage.md.j2",
    "phase_design.md.j2",
    "phase_build.md.j2",
    "phase_verify.md.j2",
    "phase_pr.md.j2",
    "phase_review.md.j2",
    "phase_merge.md.j2",
    "spec_markdown.md.j2",
    "verify_summary.md.j2",
    "build_findings.md.j2",
    "approval_instructions.md.j2",
    "merge_gate_instructions.md.j2",
    "clarification.md.j2",
    "needs_human.md.j2",
    "quarantine.md.j2",
)


def _load_templates() -> dict[str, str]:
    package = files("darkfactory.templates.comments")
    return {
        name: package.joinpath(name).read_text(encoding="utf-8")
        for name in _TEMPLATE_NAMES
    }


def compact_value(value: Any) -> str:
    """Render a value as a compact, comment-friendly string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, dict):
        parts = [f"{key}={compact_value(val)}" for key, val in value.items()]
        return ", ".join(part for part in parts if not part.endswith("="))
    if isinstance(value, Iterable):
        items = [compact_value(item) for item in value]
        return ", ".join(item for item in items if item)
    return str(value)


_env = Environment(
    loader=DictLoader(_load_templates()),
    keep_trailing_newline=False,
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,
)
_env.filters["compact"] = compact_value


def render(name: str, /, **context: Any) -> str:
    """Render a comment template by file name (e.g. ``"phase_triage.md.j2"``)."""
    return _env.get_template(name).render(**context)
