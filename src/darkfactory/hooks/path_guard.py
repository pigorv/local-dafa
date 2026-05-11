"""PreToolUse hook: deny edits to protected paths before file tools run."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from claude_agent_sdk.types import HookContext, HookJSONOutput, PreToolUseHookInput

FILE_MUTATION_TOOLS: frozenset[str] = frozenset({"Edit", "Write"})

LOCKFILE_NAMES: frozenset[str] = frozenset(
    {
        "bun.lock",
        "bun.lockb",
        "cargo.lock",
        "composer.lock",
        "deno.lock",
        "flake.lock",
        "gemfile.lock",
        "go.sum",
        "npm-shrinkwrap.json",
        "package-lock.json",
        "packages.lock.json",
        "pipfile.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pubspec.lock",
        "uv.lock",
        "yarn.lock",
    }
)

PRIVATE_KEY_NAMES: frozenset[str] = frozenset(
    {
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
)

PRIVATE_KEY_SUFFIXES: tuple[str, ...] = (
    ".key",
    ".pem",
    ".p12",
    ".pfx",
)

# Exact basenames treated as sensitive. Substring matches like
# ``Order``+``token`` in ``OrderTokenTest.java`` produce false positives
# and lock agents out of legitimate domain code.
SENSITIVE_BASENAMES: frozenset[str] = frozenset(
    {
        "secrets",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        ".secrets",
        "credentials",
        "credentials.json",
        "credentials.yaml",
        "credentials.yml",
        ".credentials",
        "token",
        "tokens",
        "tokens.json",
        ".token",
        ".tokens",
        ".npmrc",
        ".netrc",
    }
)

# Top-level directories that hold credential material. Agents may still
# read these via the SDK ``Read`` tool; Edit/Write into them is blocked.
SENSITIVE_TOPLEVEL_DIRS: frozenset[str] = frozenset(
    {"secrets", "credentials", "private", ".secrets", ".credentials"}
)


def _match_tool(tool_name: str, target: str) -> bool:
    return tool_name == target or tool_name.endswith(f"__{target}")


def _is_file_mutation_tool(tool_name: str) -> bool:
    return any(_match_tool(tool_name, target) for target in FILE_MUTATION_TOOLS)


def _extract_path(tool_input: Mapping[str, Any]) -> str | None:
    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _normalize_path(path: str, cwd: str | None = None) -> str:
    raw = path.replace("\\", "/")
    cwd_norm = (cwd or "").replace("\\", "/").rstrip("/")
    if cwd_norm and raw.startswith(f"{cwd_norm}/"):
        raw = raw[len(cwd_norm) + 1 :]
    elif raw == cwd_norm:
        raw = ""
    elif raw.startswith("/workspace/"):
        raw = raw[len("/workspace/") :]

    while raw.startswith("./"):
        raw = raw[2:]

    parts: list[str] = []
    for part in PurePosixPath(raw).parts:
        if part in ("", ".", "/"):
            continue
        if part == ".." and parts and parts[-1] != "..":
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _field(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _truthy_permission(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {
            "1",
            "true",
            "yes",
            "allowed",
            "authorize",
            "authorized",
        }
    if isinstance(value, Mapping):
        return any(
            _truthy_permission(value.get(key))
            for key in ("allowed", "allow", "authorized", "enabled")
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return bool(value)
    return False


def _dependency_changes_authorized(state: Any | None) -> bool:
    if state is None:
        return False

    for key in (
        "dependency_changes",
        "allow_dependency_changes",
        "dependency_changes_authorized",
    ):
        if _truthy_permission(_field(state, key)):
            return True

    brief = (
        _field(state, "implementation_brief")
        or _field(state, "brief")
        or _field(state, "approved_brief")
        or state
    )
    contract_changes = _field(brief, "contract_changes")
    if contract_changes is None:
        return False

    for key in ("dependency_changes", "dependencies", "dependency_files"):
        if _truthy_permission(_field(contract_changes, key)):
            return True
    return False


def protected_path_reason(
    path: str,
    *,
    dependency_changes_authorized: bool = False,
) -> str | None:
    """Return the protection reason for ``path``, or ``None`` when allowed."""
    normalized = _normalize_path(path)
    lower = normalized.lower()
    parts = [part for part in lower.split("/") if part]
    basename = parts[-1] if parts else lower

    if any(
        part == ".github"
        and index + 1 < len(parts)
        and parts[index + 1] == "workflows"
        for index, part in enumerate(parts)
    ):
        return "GitHub workflow files are protected"

    if basename == ".env" or basename.startswith(".env."):
        return "environment files are protected"

    if parts and parts[0] in SENSITIVE_TOPLEVEL_DIRS:
        return f"{parts[0]}/ is a protected directory"

    if basename in SENSITIVE_BASENAMES:
        return f"{basename} is a protected filename"

    if (
        basename in PRIVATE_KEY_NAMES
        or basename.endswith(PRIVATE_KEY_SUFFIXES)
        or "private_key" in basename
        or "private-key" in basename
    ):
        return "private key files are protected"

    is_lockfile = (
        basename in LOCKFILE_NAMES
        or basename.endswith(".lock")
        or basename.endswith(".lockfile")
    )
    if is_lockfile and not dependency_changes_authorized:
        return "lockfiles require brief-authorized dependency changes"

    return None


def is_path_allowed(path: str, state: Any | None = None, *, cwd: str | None = None) -> bool:
    normalized = _normalize_path(path, cwd)
    return (
        protected_path_reason(
            normalized,
            dependency_changes_authorized=_dependency_changes_authorized(state),
        )
        is None
    )


def _deny(reason: str) -> HookJSONOutput:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def make_path_guard(state: Any | None = None):
    """Return a PreToolUse guard for SDK ``Edit`` and ``Write`` file paths."""

    async def path_guard_hook(
        input_data: PreToolUseHookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        if not _is_file_mutation_tool(input_data["tool_name"]):
            return {}

        path = _extract_path(input_data.get("tool_input") or {})
        if path is None:
            return _deny("Edit/Write blocked: missing file path")

        normalized = _normalize_path(path, input_data.get("cwd"))
        reason = protected_path_reason(
            normalized,
            dependency_changes_authorized=_dependency_changes_authorized(state),
        )
        if reason is None:
            return {}

        return _deny(f"{input_data['tool_name']} blocked for {normalized!r}: {reason}")

    return path_guard_hook
