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
            "allowed": ["Read", "Edit", "Bash"],
            "disallowed": [],
            "argv_allowlist": ["cat"],
            "role_owned_argv_prefixes": [],
            "edit_path_allowlist": [],
        },
        "mcp": [],
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
    # A role that supplies its own prompt text (no prompt_as_user_message)
    # gets it appended onto the Claude Code preset, so CLAUDE.md/skills
    # scaffolding survives instead of being wiped by a bare --system-prompt.
    assert opts.system_prompt == {
        "type": "preset",
        "preset": "claude_code",
        "append": prompt,
    }
    # Default project_mcp_allowed=["*"] appends ``mcp__*`` to the
    # manifest's allowed list at compose time.
    assert list(opts.allowed_tools or []) == [*manifest.tools.allowed, "mcp__*"]
    assert list(opts.disallowed_tools or []) == list(manifest.tools.disallowed)
    assert (opts.mcp_servers or {}) == {}
    assert opts.can_use_tool is None
    assert opts.cwd == "/workspace"
    assert opts.permission_mode == "bypassPermissions"
    assert opts.setting_sources == ["project"]
    # Non-empty tools.allowed → skills resolves to manifest default "all".
    assert opts.skills == "all"
    assert opts.thinking is not None and opts.thinking["type"] == "disabled"


def test_compose_materializes_hooks_permission_gate_and_state(
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

    # In-process MCP servers were removed; the gate is still installed
    # because the role declares Bash + a non-empty argv_allowlist.
    assert (opts.mcp_servers or {}) == {}
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


def _mcp_manifest_payload(
    prompt_path: Path,
    *,
    role: str = "mcptest",
    project_mcp_allowed: list[str] | None = None,
    skills: str | list[str] | None = None,
    allowed: str | list[str] | None = None,
) -> dict[str, Any]:
    """Minimal manifest used to exercise the project_mcp_allowed knob."""
    payload: dict[str, Any] = {
        "identity": {
            "role": role,
            "description": "Project MCP knob fixture.",
            "when_to_use": "compose tests only.",
        },
        "llm": {
            "model": "claude-sonnet-4-5-20250929",
            "thinking": {"enabled": False, "budget_tokens": None},
            "prompt_path": str(prompt_path),
        },
        "tools": {
            "allowed": ["Read", "Grep", "Glob"],
            "disallowed": [],
            "argv_allowlist": [],
            "role_owned_argv_prefixes": [],
            "edit_path_allowlist": [],
        },
        "mcp": [],
        "hooks": [],
        "budgets": {"timeout": None, "heartbeat": None, "retry_caps": {}},
    }
    if project_mcp_allowed is not None:
        payload["tools"]["project_mcp_allowed"] = project_mcp_allowed
    if skills is not None:
        payload["tools"]["skills"] = skills
    if allowed is not None:
        payload["tools"]["allowed"] = allowed
    return payload


def _compose_tools_role(
    tmp_path: Path,
    *,
    allowed: str | list[str],
    role: str = "toolstest",
) -> Any:
    """Compose a role with the given ``tools.allowed`` and return options."""
    prompt = tmp_path / "tools.md"
    prompt.write_text("tools knob prompt", encoding="utf-8")
    manifests_dir = tmp_path / "manifests"
    _write_manifest(
        manifests_dir,
        _mcp_manifest_payload(prompt, role=role, allowed=allowed),
    )
    registry = load_registry(manifests_dir)
    client = compose(
        role,
        ComposeState(task_id="task-tools"),
        task_id="task-tools",
        overrides=ComposeOverrides(registry=registry),
    )
    return client.options


def _pre_tool_hook_names(options: Any) -> list[str]:
    hooks = options.hooks or {}
    matchers = hooks.get("PreToolUse") or []
    if not matchers:
        return []
    return [hook.__name__ for hook in matchers[0].hooks]


def test_tools_all_sentinel_is_pure_yolo(tmp_path: Path) -> None:
    """``allowed: "all"`` -> empty allowed_tools, no project-MCP expansion,
    but the Bash gate and Edit/Write path guard are force-installed."""
    opts = _compose_tools_role(tmp_path, allowed="all")

    assert list(opts.allowed_tools or []) == []
    assert not any(
        str(t).startswith("mcp__") for t in (opts.allowed_tools or [])
    )
    assert callable(opts.can_use_tool)
    assert _pre_tool_hook_names(opts) == ["path_guard_hook"]
    # "all" is not zero-tool, so skills still resolve (manifest default).
    assert opts.skills == "all"


def test_tools_all_sentinel_disallowed_bash_skips_gate(
    tmp_path: Path,
) -> None:
    """``allowed: "all"`` + ``disallowed: [Bash]`` -> Bash unreachable, so
    the argv gate is not installed (path guard still is)."""
    prompt = tmp_path / "tools.md"
    prompt.write_text("p", encoding="utf-8")
    manifests_dir = tmp_path / "manifests"
    payload = _mcp_manifest_payload(prompt, role="nobash", allowed="all")
    payload["tools"]["disallowed"] = ["Bash"]
    _write_manifest(manifests_dir, payload)
    registry = load_registry(manifests_dir)
    client = compose(
        "nobash",
        ComposeState(task_id="task-tools"),
        task_id="task-tools",
        overrides=ComposeOverrides(registry=registry),
    )

    assert client.options.can_use_tool is None
    assert _pre_tool_hook_names(client.options) == ["path_guard_hook"]


def test_tools_env_override_all_turns_list_role_yolo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_TOOLSTEST_TOOLS", "all")
    opts = _compose_tools_role(tmp_path, allowed=["Read", "Grep", "Glob"])

    assert list(opts.allowed_tools or []) == []
    assert callable(opts.can_use_tool)
    assert _pre_tool_hook_names(opts) == ["path_guard_hook"]


def test_tools_env_override_list_overrides_all_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_TOOLSTEST_TOOLS", "Read,Grep")
    opts = _compose_tools_role(tmp_path, allowed="all")

    # Back on the explicit-list path: project-MCP wildcard is appended.
    assert list(opts.allowed_tools or []) == ["Read", "Grep", "mcp__*"]


def test_tools_empty_env_forces_zero_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_TOOLSTEST_TOOLS", "")
    opts = _compose_tools_role(tmp_path, allowed="all")

    assert list(opts.allowed_tools or []) == []
    # Empty resolved list is zero-tool: skills disabled, no gate, no guard.
    assert opts.skills is None
    assert opts.can_use_tool is None
    assert _pre_tool_hook_names(opts) == []


def _compose_mcp_role(
    tmp_path: Path,
    *,
    project_mcp_allowed: list[str] | None,
) -> list[str]:
    prompt = tmp_path / "mcp.md"
    prompt.write_text("MCP knob prompt", encoding="utf-8")
    manifests_dir = tmp_path / "manifests"
    _write_manifest(
        manifests_dir,
        _mcp_manifest_payload(prompt, project_mcp_allowed=project_mcp_allowed),
    )
    registry = load_registry(manifests_dir)
    client = compose(
        "mcptest",
        ComposeState(task_id="task-mcp"),
        task_id="task-mcp",
        overrides=ComposeOverrides(registry=registry),
    )
    return list(client.options.allowed_tools or [])


def test_project_mcp_allowed_default_is_wildcard(tmp_path: Path) -> None:
    """Omitting the field defaults to ["*"] -> mcp__* gets appended."""
    tools = _compose_mcp_role(tmp_path, project_mcp_allowed=None)
    assert tools == ["Read", "Grep", "Glob", "mcp__*"]


def test_project_mcp_allowed_explicit_empty_list_disables(tmp_path: Path) -> None:
    """Manifest can opt out by setting project_mcp_allowed: []."""
    tools = _compose_mcp_role(tmp_path, project_mcp_allowed=[])
    assert tools == ["Read", "Grep", "Glob"]
    assert not any(t.startswith("mcp__") for t in tools)


def test_project_mcp_allowed_skipped_for_zero_tool_roles(tmp_path: Path) -> None:
    """Roles with allowed=[] short-circuit; default ["*"] is not expanded.

    Mirrors the plan_critic / verifier_semantic design: an empty
    allowed list is a deliberate "no tools" statement.
    """
    prompt = tmp_path / "zero.md"
    prompt.write_text("zero", encoding="utf-8")
    manifests_dir = tmp_path / "manifests"
    payload = _mcp_manifest_payload(prompt, role="zerotool")
    payload["tools"]["allowed"] = []
    _write_manifest(manifests_dir, payload)
    registry = load_registry(manifests_dir)
    client = compose(
        "zerotool",
        ComposeState(task_id="task-mcp"),
        task_id="task-mcp",
        overrides=ComposeOverrides(registry=registry),
    )
    assert list(client.options.allowed_tools or []) == []


def test_project_mcp_allowed_expands_server_names(tmp_path: Path) -> None:
    tools = _compose_mcp_role(
        tmp_path, project_mcp_allowed=["filesystem", "linear"]
    )
    assert tools == [
        "Read",
        "Grep",
        "Glob",
        "mcp__filesystem__*",
        "mcp__linear__*",
    ]


def test_project_mcp_allowed_wildcard_expands_to_mcp_star(tmp_path: Path) -> None:
    tools = _compose_mcp_role(tmp_path, project_mcp_allowed=["*"])
    assert tools[-1] == "mcp__*"
    assert tools[:3] == ["Read", "Grep", "Glob"]


def test_project_mcp_allowed_env_override_replaces_manifest_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_MCPTEST_PROJECT_MCP", "filesystem,sqlite")
    tools = _compose_mcp_role(tmp_path, project_mcp_allowed=["linear"])
    # The env list wins; the manifest's "linear" is not expanded.
    assert "mcp__linear__*" not in tools
    assert "mcp__filesystem__*" in tools
    assert "mcp__sqlite__*" in tools


def test_project_mcp_allowed_empty_env_disables_manifest_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty env string is an explicit override: no project MCPs."""
    monkeypatch.setenv("LLM_MCPTEST_PROJECT_MCP", "")
    tools = _compose_mcp_role(tmp_path, project_mcp_allowed=["filesystem"])
    assert not any(t.startswith("mcp__") for t in tools)


def test_project_mcp_allowed_env_wildcard_overrides_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_MCPTEST_PROJECT_MCP", "*")
    tools = _compose_mcp_role(tmp_path, project_mcp_allowed=["filesystem"])
    assert "mcp__*" in tools
    assert "mcp__filesystem__*" not in tools


def _compose_skills_role(
    tmp_path: Path,
    *,
    skills: str | list[str] | None,
    zero_tool: bool = False,
) -> str | list[str] | None:
    prompt = tmp_path / "skills.md"
    prompt.write_text("skills knob prompt", encoding="utf-8")
    manifests_dir = tmp_path / "manifests"
    payload = _mcp_manifest_payload(prompt, role="skilltest", skills=skills)
    if zero_tool:
        payload["tools"]["allowed"] = []
    _write_manifest(manifests_dir, payload)
    registry = load_registry(manifests_dir)
    client = compose(
        "skilltest",
        ComposeState(task_id="task-skills"),
        task_id="task-skills",
        overrides=ComposeOverrides(registry=registry),
    )
    return client.options.skills


def test_skills_default_is_all(tmp_path: Path) -> None:
    """Omitting the field defaults to "all" (every project skill enabled)."""
    assert _compose_skills_role(tmp_path, skills=None) == "all"


def test_skills_manifest_list_restricts(tmp_path: Path) -> None:
    assert _compose_skills_role(tmp_path, skills=["pdf", "docx"]) == ["pdf", "docx"]


def test_skills_manifest_empty_list_disables(tmp_path: Path) -> None:
    """Empty list is treated as "no skills" (returns None)."""
    assert _compose_skills_role(tmp_path, skills=[]) is None


def test_skills_zero_tool_role_disables(tmp_path: Path) -> None:
    """Roles with allowed=[] never load skills, regardless of manifest."""
    assert _compose_skills_role(tmp_path, skills="all", zero_tool=True) is None


def test_skills_env_override_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_SKILLTEST_SKILLS", "all")
    assert _compose_skills_role(tmp_path, skills=["pdf"]) == "all"


def test_skills_env_override_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_SKILLTEST_SKILLS", "pdf,docx")
    assert _compose_skills_role(tmp_path, skills="all") == ["pdf", "docx"]


def test_skills_empty_env_disables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_SKILLTEST_SKILLS", "")
    assert _compose_skills_role(tmp_path, skills="all") is None


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
