from __future__ import annotations

import pytest
from pydantic import ValidationError

from darkfactory.agents.manifest import RoleManifest


def _manifest_payload() -> dict:
    return {
        "identity": {
            "role": "noop",
            "description": "No-op role for schema tests.",
            "when_to_use": "Use only as a schema fixture.",
        },
        "llm": {
            "model": "claude-sonnet-4-5-20250929",
            "thinking": {"enabled": False},
            "prompt_path": "src/darkfactory/prompts/noop.md",
        },
        "tools": {
            "allowed": [],
            "disallowed": [],
            "argv_allowlist": [],
            "role_owned_argv_prefixes": [],
            "edit_path_allowlist": [],
        },
        "mcp": [],
        "hooks": [],
        "budgets": {
            "timeout": None,
            "heartbeat": None,
            "retry_caps": {},
        },
    }


def test_role_manifest_accepts_minimal_valid_manifest() -> None:
    manifest = RoleManifest.model_validate(_manifest_payload())

    assert manifest.identity.role == "noop"
    assert manifest.llm.thinking.enabled is False
    assert manifest.tools.allowed == []
    assert manifest.mcp == []
    assert manifest.hooks == []


def test_role_manifest_preserves_hook_parameters_and_argv_prefixes() -> None:
    payload = _manifest_payload()
    payload["tools"]["role_owned_argv_prefixes"] = [["gh", "pr", "create"]]
    payload["hooks"] = [
        {
            "event": "PreToolUse",
            "name": "call_cap",
            "parameters": {"cap": 80},
        }
    ]

    manifest = RoleManifest.model_validate(payload)

    assert manifest.tools.role_owned_argv_prefixes == [("gh", "pr", "create")]
    assert manifest.hooks[0].name == "call_cap"
    assert manifest.hooks[0].parameters == {"cap": 80}


def test_role_manifest_requires_missing_fields() -> None:
    payload = _manifest_payload()
    del payload["llm"]

    with pytest.raises(ValidationError):
        RoleManifest.model_validate(payload)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        (None, "unexpected"),
        ("identity", "unexpected"),
        ("llm", "unexpected"),
        ("tools", "unexpected"),
        ("budgets", "unexpected"),
    ],
)
def test_role_manifest_rejects_unknown_extra_fields(
    section: str | None,
    field: str,
) -> None:
    payload = _manifest_payload()
    target = payload if section is None else payload[section]
    target[field] = "extra"

    with pytest.raises(ValidationError):
        RoleManifest.model_validate(payload)
