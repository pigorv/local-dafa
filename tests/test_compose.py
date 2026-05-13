from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
import yaml
from opentelemetry import trace

from darkfactory.agents.compose import ComposeOverrides, ComposeState, compose
from darkfactory.agents.registry import load_registry, resolve_prompt_path


FIXTURE_MANIFESTS = Path("tests/fixtures/manifests")


def _recording_tracer() -> Any:
    provider = trace.get_tracer_provider()
    if not hasattr(provider, "add_span_processor"):
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        provider = TracerProvider(
            resource=Resource.create({"service.name": "darkfactory-compose-test"})
        )
        trace.set_tracer_provider(provider)
    return trace.get_tracer("tests.test_compose")


def _manifest_payload(prompt_path: Path, *, role: str = "hooked") -> dict[str, Any]:
    return {
        "identity": {
            "role": role,
            "description": "Hooked role for composer tests.",
            "when_to_use": "Use only as a composer fixture.",
        },
        "llm": {
            "model": "claude-sonnet-4-5-20250929",
            "thinking": {"enabled": True, "budget_tokens": 1234},
            "prompt_path": str(prompt_path),
        },
        "tools": {
            "allowed": ["Read", "Edit", "sandbox_bash"],
            "disallowed": [],
            "argv_allowlist": ["cat"],
            "role_owned_argv_prefixes": [],
            "edit_path_allowlist": [],
        },
        "mcp": ["darkfactory"],
        "hooks": [
            {
                "event": "PreToolUse",
                "name": "call_cap",
                "parameters": {"max_turns": 3},
            },
            {
                "event": "PostToolUse",
                "name": "prompt_injection_guard",
                "parameters": {},
            },
            {
                "event": "UserPromptSubmit",
                "name": "goal_pin",
                "parameters": {"every_n": 2},
            },
        ],
        "budgets": {"timeout": None, "heartbeat": None, "retry_caps": {}},
    }


def _write_manifest(manifests_dir: Path, payload: dict[str, Any]) -> None:
    manifests_dir.mkdir()
    (manifests_dir / f"{payload['identity']['role']}.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def test_compose_noop_options_mirror_manifest() -> None:
    registry = load_registry(FIXTURE_MANIFESTS)
    manifest = registry.get("noop")
    prompt = resolve_prompt_path(manifest.llm.prompt_path).read_text(encoding="utf-8")
    state = ComposeState(task_id="task-123")

    client = compose(
        "noop",
        state,
        task_id="task-123",
        overrides=ComposeOverrides(registry=registry),
    )
    opts = client.options

    assert opts.model == manifest.llm.model
    assert opts.system_prompt == prompt
    assert list(opts.allowed_tools or []) == list(manifest.tools.allowed)
    assert list(opts.disallowed_tools or []) == list(manifest.tools.disallowed)
    assert (opts.mcp_servers or {}) == {}
    assert opts.can_use_tool is None
    assert opts.cwd == "/workspace"
    assert opts.permission_mode == "bypassPermissions"
    assert opts.setting_sources == []
    assert opts.thinking is not None and opts.thinking["type"] == "disabled"


def test_compose_materializes_hooks_mcp_permission_gate_and_state(
    tmp_path: Path,
) -> None:
    prompt = tmp_path / "hooked.md"
    prompt.write_text("Hooked prompt", encoding="utf-8")
    manifests_dir = tmp_path / "manifests"
    _write_manifest(manifests_dir, _manifest_payload(prompt))
    registry = load_registry(manifests_dir)

    client = compose(
        "hooked",
        ComposeState(
            slice_id="WP-1",
            task_id="task-hooked",
            gate_approved=True,
            dependency_changes_authorized=True,
            user_request="keep the goal pinned",
        ),
        task_id="task-hooked",
        overrides=ComposeOverrides(registry=registry),
    )
    opts = client.options

    assert "darkfactory" in opts.mcp_servers
    assert callable(opts.can_use_tool)
    assert opts.thinking is not None
    assert opts.thinking["type"] == "enabled"
    assert opts.thinking["budget_tokens"] == 1234

    pre_hooks = list(opts.hooks["PreToolUse"][0].hooks)
    # build_options prepends path_guard for Edit/Write roles.
    assert pre_hooks[0].__name__ == "path_guard_hook"
    assert pre_hooks[1].__name__ == "call_cap_hook"
    post_hooks = list(opts.hooks["PostToolUse"][0].hooks)
    assert [hook.__name__ for hook in post_hooks] == [
        "prompt_injection_guard_hook"
    ]
    submit_hooks = list(opts.hooks["UserPromptSubmit"][0].hooks)
    assert [hook.__name__ for hook in submit_hooks] == ["goal_pin_hook"]


def test_compose_applies_env_then_in_process_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = tmp_path / "hooked.md"
    prompt.write_text("Hooked prompt", encoding="utf-8")
    manifests_dir = tmp_path / "manifests"
    _write_manifest(manifests_dir, _manifest_payload(prompt))
    registry = load_registry(manifests_dir)
    monkeypatch.setenv("LLM_HOOKED_MODEL", "env-model")
    monkeypatch.setenv("LLM_HOOKED_THINKING", "off")

    client = compose(
        "hooked",
        ComposeState(task_id="task-hooked"),
        task_id="task-hooked",
        overrides=ComposeOverrides(
            registry=registry,
            model="process-model",
            thinking=True,
            thinking_budget_tokens=2222,
        ),
    )
    opts = client.options

    assert opts.model == "process-model"
    assert opts.thinking is not None
    assert opts.thinking["type"] == "enabled"
    assert opts.thinking["budget_tokens"] == 2222


def test_compose_stamps_manifest_and_prompt_sha_on_active_span() -> None:
    registry = load_registry(FIXTURE_MANIFESTS)
    manifest_path = registry.source_path("noop")
    prompt_path = resolve_prompt_path(registry.get("noop").llm.prompt_path)
    tracer = _recording_tracer()

    with tracer.start_as_current_span("compose-noop"):
        compose(
            "noop",
            ComposeState(task_id="task-otel"),
            task_id="task-otel",
            overrides=ComposeOverrides(registry=registry),
        )
        attrs = dict(getattr(trace.get_current_span(), "attributes", {}) or {})

    assert attrs["darkfactory.manifest_sha"] == sha256(
        manifest_path.read_bytes()
    ).hexdigest()
    assert attrs["darkfactory.prompt_sha"] == sha256(
        prompt_path.read_bytes()
    ).hexdigest()
