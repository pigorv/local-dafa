"""Opt-in diagnostics for the Claude Agent SDK subprocess.

Why this exists: the worker builds ``ClaudeAgentOptions`` through
``compose() -> build_options()``, while the standalone skills probe builds
them directly. When skills/CLAUDE.md load in the probe but not in the worker,
the difference is in the worker-only options (structured output, hooks,
``can_use_tool``, manifest ``disallowed``) and in the *exact* CLI argv the SDK
spawns (does it pass ``--system-prompt ""``? is ``Skill`` in
``--allowedTools``? is ``--setting-sources=project`` present?).

The SDK builds that argv in ``SubprocessCLITransport._build_command`` and
never logs it. This module, when ``DARKFACTORY_LOG_SDK_ARGV`` is truthy,
(1) logs the compose-resolved options the argv can't show (hooks, output
format, permission gate), and (2) monkeypatches ``_build_command`` to log the
authoritative post-``_apply_skills_defaults`` argv. Long/noisy values
(``--json-schema``, ``--append-system-prompt``) are truncated; flags are kept
verbatim so presence/absence is unambiguous.

Everything is gated behind the env var and a no-op otherwise; safe to call
unconditionally at worker startup.
"""
from __future__ import annotations

import logging
import os
from typing import Any

DIAG_ENV = "DARKFACTORY_LOG_SDK_ARGV"

# WARNING level so it surfaces in worker container logs even though the app
# never configures logging (default root level is WARNING).
_LEVEL = logging.WARNING
log = logging.getLogger("darkfactory.sdk_diagnostics")

# Flags whose argv value can be huge or noisy; show length + prefix instead
# of the whole thing. The flag token itself is always kept verbatim.
_TRUNCATE_FLAGS = frozenset(
    {
        "--json-schema",
        "--append-system-prompt",
        "--system-prompt",
        "--settings",
        "--mcp-config",
    }
)
_MAX_VALUE_LEN = 160


def enabled() -> bool:
    return os.getenv(DIAG_ENV, "").strip().lower() in ("1", "true", "on", "yes")


def _short(value: str, *, force: bool = False) -> str:
    if force or len(value) > _MAX_VALUE_LEN:
        return f"<len={len(value)} {value[:_MAX_VALUE_LEN]!r}…>"
    return repr(value)


def _render_argv(argv: list[str]) -> str:
    out: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _TRUNCATE_FLAGS and i + 1 < len(argv):
            val = argv[i + 1]
            # Always show --system-prompt's value plainly (an empty '' here is
            # the exact "Claude Code prompt was wiped" tell we care about).
            shown = (
                repr(val)
                if tok == "--system-prompt" and len(val) <= _MAX_VALUE_LEN
                else _short(val, force=tok != "--system-prompt")
            )
            out.append(f"{tok} {shown}")
            i += 2
            continue
        out.append(tok)
        i += 1
    return " ".join(out)


def log_resolved_options(role: str, options: Any) -> None:
    """Log the compose-resolved options that the spawned argv won't reveal."""
    if not enabled():
        return
    sp = getattr(options, "system_prompt", None)
    if isinstance(sp, dict):
        sp_repr = {k: (v if k != "append" else f"<len={len(str(v))}>") for k, v in sp.items()}
    elif isinstance(sp, str):
        sp_repr = f"<str len={len(sp)}>"
    else:
        sp_repr = sp
    hooks = getattr(options, "hooks", {}) or {}
    of = getattr(options, "output_format", None)
    log.log(
        _LEVEL,
        "[sdk-diag] role=%s system_prompt=%r skills=%r setting_sources=%r "
        "cwd=%r allowed_tools=%r disallowed_tools=%r output_format=%s "
        "can_use_tool=%s hooks=%s",
        role,
        sp_repr,
        getattr(options, "skills", None),
        getattr(options, "setting_sources", None),
        getattr(options, "cwd", None),
        list(getattr(options, "allowed_tools", []) or []),
        list(getattr(options, "disallowed_tools", []) or []),
        (of.get("type") if isinstance(of, dict) else of),
        getattr(options, "can_use_tool", None) is not None,
        {ev: len(v or []) for ev, v in hooks.items()},
    )


_PATCH_MARKER = "_darkfactory_argv_logging"


def install_argv_logging() -> None:
    """Idempotently patch the SDK transport to log the exact spawned argv.

    No-op unless ``DARKFACTORY_LOG_SDK_ARGV`` is truthy. The patch wraps
    ``_build_command`` (the SDK's authoritative argv, post
    ``_apply_skills_defaults`` — so ``Skill`` injection and the
    ``--system-prompt``/preset decision are both visible).
    """
    if not enabled():
        return
    try:
        from claude_agent_sdk._internal.transport import subprocess_cli
    except Exception:  # pragma: no cover - SDK layout changed
        log.log(_LEVEL, "[sdk-diag] could not import SDK transport; argv logging off")
        return

    transport = subprocess_cli.SubprocessCLITransport
    if getattr(transport._build_command, _PATCH_MARKER, False):
        return

    original = transport._build_command

    def _build_command(self: Any) -> list[str]:
        cmd = original(self)
        try:
            argv = list(cmd)
            # argv[0] is the resolved CLI path; keep just its basename.
            if argv:
                argv[0] = os.path.basename(argv[0])
            log.log(_LEVEL, "[sdk-diag] claude argv: %s", _render_argv(argv))
        except Exception:  # pragma: no cover - never break the spawn
            log.log(_LEVEL, "[sdk-diag] failed to render argv", exc_info=True)
        return cmd

    setattr(_build_command, _PATCH_MARKER, True)
    transport._build_command = _build_command  # type: ignore[method-assign]
    log.log(_LEVEL, "[sdk-diag] argv logging installed (%s=1)", DIAG_ENV)
