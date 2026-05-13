from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from darkfactory.agents import registry as _registry_module
from darkfactory.agents.registry import (
    ManifestRegistryError,
    load_registry,
)
from darkfactory.runtime.worker_main import _load_manifest_registry


@pytest.fixture(autouse=True)
def _restore_default_registry() -> None:
    """Restore ``_DEFAULT_REGISTRY`` after tests that call
    ``_load_manifest_registry`` with throwaway directories.

    ``compose`` always consults ``get_default_registry()``; tests that
    point the default registry at an empty tmp dir would otherwise leak
    that state into downstream agent tests. Snapshot/restore here.
    """
    saved = _registry_module._DEFAULT_REGISTRY
    yield
    _registry_module._DEFAULT_REGISTRY = saved


def _manifest_payload(
    prompt_path: Path,
    *,
    role: str = "noop",
    hooks: list[dict] | None = None,
) -> dict:
    return {
        "identity": {
            "role": role,
            "description": "No-op role for registry tests.",
            "when_to_use": "Use only as a registry fixture.",
        },
        "llm": {
            "model": "claude-sonnet-4-5-20250929",
            "thinking": {"enabled": False},
            "prompt_path": str(prompt_path),
        },
        "tools": {
            "allowed": [],
            "disallowed": [],
            "argv_allowlist": [],
            "role_owned_argv_prefixes": [],
            "edit_path_allowlist": [],
        },
        "mcp": [],
        "hooks": hooks or [],
        "budgets": {
            "timeout": None,
            "heartbeat": None,
            "retry_caps": {},
        },
    }


def _write_manifest(manifests_dir: Path, filename: str, payload: dict) -> Path:
    path = manifests_dir / filename
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def test_load_registry_accepts_empty_directory(tmp_path: Path) -> None:
    registry = load_registry(tmp_path)

    assert len(registry) == 0
    assert registry.roles == ()
    assert registry.hook_names == ()


def test_worker_registry_startup_logs_empty_registry(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="darkfactory.runtime.worker_main")

    registry = _load_manifest_registry(tmp_path)

    assert len(registry) == 0
    assert "registry: 0 roles loaded" in caplog.text


def test_load_registry_returns_manifest_by_role(tmp_path: Path) -> None:
    prompt = tmp_path / "noop.md"
    prompt.write_text("system prompt", encoding="utf-8")
    manifest_path = _write_manifest(
        tmp_path,
        "noop.yaml",
        _manifest_payload(
            prompt,
            hooks=[
                {
                    "event": "PreToolUse",
                    "name": "call_cap",
                    "parameters": {"cap": 1},
                }
            ],
        ),
    )

    registry = load_registry(tmp_path)
    manifest = registry.get("noop")
    manifest.identity.role = "mutated"

    assert registry.roles == ("noop",)
    assert registry.hook_names == ("call_cap",)
    assert registry.source_path("noop") == manifest_path
    assert registry.get("noop").identity.role == "noop"


def test_load_registry_rejects_duplicate_roles(tmp_path: Path) -> None:
    prompt = tmp_path / "noop.md"
    prompt.write_text("system prompt", encoding="utf-8")
    payload = _manifest_payload(prompt)
    _write_manifest(tmp_path, "noop-a.yaml", payload)
    _write_manifest(tmp_path, "noop-b.yaml", payload)

    with pytest.raises(ManifestRegistryError, match="duplicate role"):
        load_registry(tmp_path)


def test_load_registry_rejects_unknown_hook(tmp_path: Path) -> None:
    prompt = tmp_path / "noop.md"
    prompt.write_text("system prompt", encoding="utf-8")
    _write_manifest(
        tmp_path,
        "noop.yaml",
        _manifest_payload(
            prompt,
            hooks=[
                {
                    "event": "PreToolUse",
                    "name": "definitely_not_exported",
                    "parameters": {},
                }
            ],
        ),
    )

    with pytest.raises(ManifestRegistryError, match="unknown hook"):
        load_registry(tmp_path)


def test_load_registry_rejects_missing_prompt_path(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        "noop.yaml",
        _manifest_payload(tmp_path / "missing.md"),
    )

    with pytest.raises(ManifestRegistryError, match="prompt_path .* does not exist"):
        load_registry(tmp_path)


def test_role_owned_argv_table_aggregates_across_manifests(tmp_path: Path) -> None:
    """The composer feeds make_permission_gate the union of every manifest's
    role-owned argv prefixes; the helper aggregates per-prefix allowed roles
    so cross-role denial works after Task 6.2 deletes the hardcoded table."""
    prompt_a = tmp_path / "alpha.md"
    prompt_a.write_text("system prompt", encoding="utf-8")
    prompt_b = tmp_path / "beta.md"
    prompt_b.write_text("system prompt", encoding="utf-8")

    payload_a = _manifest_payload(prompt_a, role="alpha")
    payload_a["tools"]["role_owned_argv_prefixes"] = [
        ["git", "push"],
        ["gh", "pr", "create"],
    ]
    payload_b = _manifest_payload(prompt_b, role="beta")
    payload_b["tools"]["role_owned_argv_prefixes"] = [["gh", "pr", "create"]]

    _write_manifest(tmp_path, "alpha.yaml", payload_a)
    _write_manifest(tmp_path, "beta.yaml", payload_b)

    registry = load_registry(tmp_path)
    table = registry.role_owned_argv_table()

    assert table[("git", "push")] == frozenset({"alpha"})
    assert table[("gh", "pr", "create")] == frozenset({"alpha", "beta"})


def test_role_owned_argv_table_immutable(tmp_path: Path) -> None:
    prompt = tmp_path / "noop.md"
    prompt.write_text("system prompt", encoding="utf-8")
    payload = _manifest_payload(prompt, role="noop")
    payload["tools"]["role_owned_argv_prefixes"] = [["git", "push"]]
    _write_manifest(tmp_path, "noop.yaml", payload)

    registry = load_registry(tmp_path)
    table = registry.role_owned_argv_table()
    with pytest.raises(TypeError):
        table[("git", "push")] = frozenset()  # type: ignore[index]


def test_load_registry_rejects_role_owned_prefix_overlapping_denied(
    tmp_path: Path,
) -> None:
    """A pr_creator-style manifest cannot add a globally-denied prefix (e.g.
    ``gh pr merge``) to its role-owned argv prefixes — refused at load time.

    Defense in depth: the gate also denies at runtime, but catching the
    misconfiguration here surfaces operator intent rather than masking it.
    """
    prompt = tmp_path / "pr_creator.md"
    prompt.write_text("system prompt", encoding="utf-8")
    payload = _manifest_payload(prompt, role="pr_creator")
    payload["tools"]["role_owned_argv_prefixes"] = [["gh", "pr", "merge"]]
    _write_manifest(tmp_path, "pr_creator.yaml", payload)

    with pytest.raises(ManifestRegistryError, match="widen the global denylist"):
        load_registry(tmp_path)
