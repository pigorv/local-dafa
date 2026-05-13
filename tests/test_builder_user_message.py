"""Builder user-message rendering: brief Markdown + trimmed repo_summary.

These tests pin the format of the first user message the builder sends
(``prompt_as_user_message: true``). The prompt template lives at
``src/darkfactory/prompts/builder.md`` and substitutes
``$user_request`` / ``$repo_context`` / ``$implementation_brief`` /
``$work_package``. The Markdown brief and the trimmed ``repo_summary``
are the load-bearing format choices we want to catch regressions on.
"""
from __future__ import annotations

from darkfactory.agents.builder import (
    _brief_as_markdown,
    _render_user_prompt,
)


def _sample_brief() -> dict:
    return {
        "problem": "Users want cursor pagination.",
        "expected_behavior": [
            "GET /api/users supports ?cursor=",
            "response includes next_cursor",
        ],
        "current_understanding": "Today the endpoint paginates by offset.",
        "proposed_design": "Add a CursorEncoder and swap the controller.",
        "contract_changes": {
            "api": ["GET /api/users accepts cursor query param"],
            "data": [],
            "events": [],
        },
        "compatibility_risks": ["Existing clients passing offset"],
        "test_strategy": "Integration test against the controller.",
        "work_packages": [
            {
                "id": "WP-1",
                "story_id": "US-1",
                "title": "Cursor encoder",
                "intent": "Build a stable cursor encode/decode helper.",
            },
            {
                "id": "WP-2",
                "story_id": "US-1",
                "title": "Controller swap",
                "intent": "Wire the encoder into UserController.",
            },
        ],
    }


def test_brief_as_markdown_renders_expected_sections() -> None:
    rendered = _brief_as_markdown(_sample_brief())

    # Each section header is present and lives at H2.
    for header in (
        "## Problem",
        "## Expected behavior",
        "## Current understanding",
        "## Proposed design",
        "## Contract changes",
        "## Compatibility risks",
        "## Test strategy",
        "## Work packages",
    ):
        assert header in rendered, header

    # Bullets are rendered as Markdown list items, not JSON arrays.
    assert "- GET /api/users supports ?cursor=" in rendered
    assert "WP-1" in rendered and "WP-2" in rendered
    assert '"' not in rendered.split("## Work packages")[1].splitlines()[0]


def test_brief_as_markdown_handles_missing_brief() -> None:
    assert _brief_as_markdown(None) == "(none)"
    assert _brief_as_markdown({}) == "(none)"


def test_render_user_prompt_substitutes_all_placeholders() -> None:
    state_slice = {
        "user_request": "Add cursor pagination to /api/users",
        "current_slice": "US-1",
        "implementation_brief": _sample_brief(),
        "repo_context": {
            "agents_md": "DO NOT INCLUDE",  # builder excludes agents_md
            "repo_map": "UserController.java\n  class UserController",
            "style_configs": [
                {"path": "checkstyle.xml", "content": "<module>JavadocPackage</module>"},
            ],
            "git_log": ["abc123 init"],  # builder excludes git_log
        },
        "spec": [
            {
                "story_id": "US-1",
                "approach": "cursor pagination",
                "affected_files": ["UserController.java"],
                "intent": "Add cursor encoding",
            }
        ],
    }

    rendered = _render_user_prompt(state_slice)

    # User request substituted.
    assert "Add cursor pagination to /api/users" in rendered

    # Brief rendered as Markdown, not JSON.
    assert "## Problem" in rendered
    assert "Users want cursor pagination" in rendered

    # Trimmed repo_summary: repo_map + style_configs only.
    assert "UserController.java" in rendered
    assert "checkstyle.xml" in rendered
    assert "JavadocPackage" in rendered
    # Excluded sections genuinely missing.
    assert "DO NOT INCLUDE" not in rendered
    assert "Recent commits" not in rendered

    # Work Package JSON block carries story_id.
    assert "US-1" in rendered
    assert "cursor pagination" in rendered

    # No unresolved Template placeholders leak through.
    for placeholder in (
        "$user_request",
        "$repo_context",
        "$implementation_brief",
        "$work_package",
    ):
        assert placeholder not in rendered, placeholder
