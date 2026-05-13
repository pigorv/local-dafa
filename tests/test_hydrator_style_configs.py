"""Hydrator surfaces lint/style configs so the Builder can match the
repo's checkstyle/lint policy without generating hard findings on the
first commit.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from darkfactory.agents._sdk_common import repo_summary
from darkfactory.stages.hydrator import (
    STYLE_CONFIG_SNIPPET_BUDGET,
    _collect_style_configs,
    hydrate,
)


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_collect_style_configs_finds_known_files(tmp_path: Path) -> None:
    _write(tmp_path, "checkstyle.xml", "<module>checks</module>")
    _write(tmp_path, ".editorconfig", "root = true\n")
    _write(tmp_path, ".prettierrc", '{"semi": false}')
    # README should be ignored — not a style config.
    _write(tmp_path, "README.md", "hello")

    configs = _collect_style_configs(tmp_path)

    paths = [entry["path"] for entry in configs]
    assert "checkstyle.xml" in paths
    assert ".editorconfig" in paths
    assert ".prettierrc" in paths
    assert "README.md" not in paths


def test_collect_style_configs_skips_pyproject_without_style_section(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname = "demo"\nversion = "0"\n',
    )

    assert _collect_style_configs(tmp_path) == []


def test_collect_style_configs_includes_pyproject_with_ruff_section(
    tmp_path: Path,
) -> None:
    body = '[project]\nname = "demo"\n\n[tool.ruff]\nline-length = 100\n'
    _write(tmp_path, "pyproject.toml", body)

    configs = _collect_style_configs(tmp_path)

    assert [entry["path"] for entry in configs] == ["pyproject.toml"]
    assert "tool.ruff" in configs[0]["content"]


def test_collect_style_configs_truncates_long_files(tmp_path: Path) -> None:
    long_body = "x" * (STYLE_CONFIG_SNIPPET_BUDGET * 4)
    _write(tmp_path, "checkstyle.xml", long_body)

    configs = _collect_style_configs(tmp_path)

    assert len(configs) == 1
    snippet = configs[0]["content"]
    assert len(snippet) <= STYLE_CONFIG_SNIPPET_BUDGET + 2  # +"\n…"
    assert snippet.endswith("…")


def test_hydrate_surfaces_style_configs_in_repo_context(tmp_path: Path) -> None:
    # Initialise a minimal git repo so _git_log_oneline doesn't error.
    _write(tmp_path, "checkstyle.xml", "<module/>")
    _write(tmp_path, "AGENTS.md", "Java demo repo.\n")

    context = hydrate(tmp_path)

    assert "style_configs" in context
    paths = [entry["path"] for entry in context["style_configs"]]
    assert "checkstyle.xml" in paths


def test_repo_summary_renders_style_configs() -> None:
    rendered = repo_summary(
        {
            "agents_md": "Java demo repo.",
            "repo_map": "Foo.java\n  class Foo",
            "git_log": ["abc1234 init"],
            "style_configs": [
                {"path": "checkstyle.xml", "content": "<module>JavadocPackage</module>"},
                {"path": ".editorconfig", "content": "root = true"},
            ],
        }
    )

    assert "Style / lint configs" in rendered
    assert "checkstyle.xml" in rendered
    assert "JavadocPackage" in rendered
    assert ".editorconfig" in rendered


def test_repo_summary_omits_style_section_when_empty() -> None:
    rendered = repo_summary({"agents_md": "Java demo"})
    assert "Style / lint configs" not in rendered


def test_repo_summary_include_trims_to_selected_sections() -> None:
    rendered = repo_summary(
        {
            "agents_md": "Java demo repo.",
            "repo_map": "Foo.java\n  class Foo",
            "git_log": ["abc1234 init"],
            "style_configs": [
                {"path": "checkstyle.xml", "content": "<module/>"},
            ],
        },
        include=("repo_map", "style_configs"),
    )

    # Selected sections render.
    assert "Repo map" in rendered
    assert "Style / lint configs" in rendered
    assert "checkstyle.xml" in rendered
    # Dropped sections do not.
    assert "AGENTS.md" not in rendered
    assert "Recent commits" not in rendered


def test_repo_summary_default_renders_all_sections_unchanged() -> None:
    # The ``include`` kwarg is opt-in; existing PO / Architect call
    # sites keep getting every section.
    rendered = repo_summary(
        {
            "agents_md": "Java demo repo.",
            "repo_map": "Foo.java\n  class Foo",
            "git_log": ["abc1234 init"],
            "style_configs": [],
        },
    )
    assert "AGENTS.md" in rendered
    assert "Repo map" in rendered
    assert "Recent commits" in rendered
