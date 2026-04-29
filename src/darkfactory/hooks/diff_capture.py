"""PostToolUse hook: capture a Patch after every successful Edit/Write.

Per ARCHITECTURE.md §5.6, this hook replaces the old ``apply_patch`` tool
wrapper. Whenever a build-stage role uses the SDK's built-in ``Edit`` or
``Write`` to mutate a file under the worker's bind-mounted ``/workspace``,
we want a record of *what changed* — keyed by the build slice the role is
working on, attributed to the role, and stamped with the current commit
sha. Those records flow into ``state['patches']`` (per the channel reducer
in ``state.py``) so downstream stages (``verify``, ``spec_adjustment``,
``code_quality``, ``pr_creator``) can reason about the change set.

The hook fires *after* the SDK has run the tool, so the working tree is
already in the post-edit state. We:

1. resolve the per-task ``RepoSandbox`` from ``tools/shell.py``'s registry,
2. run ``git diff -- <path>`` and ``git rev-parse HEAD`` inside it,
3. sanity-validate the diff via ``tools/patch_helpers.apply_unified_diff``,
4. append a ``Patch`` to a per-client sink list passed in by the caller.

The ``HookContext`` does not carry workflow state, so this module follows
the same factory pattern as the other hooks (``loop_breaker``, ``call_cap``,
``goal_pin``, ``permission_gate``): the role, slice id, task id and sink
list are closed over at construction time. The activity body that opens
the SDK client owns the sink list and reads its contents back into the
state delta when the SDK loop ends.

Failure modes (missing sandbox, git error, empty diff, malformed diff) all
result in a no-op return — PostToolUse hooks are advisory and must not
crash the SDK loop. Diagnostics go to a module logger.
"""
from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk.types import (
    HookContext,
    HookJSONOutput,
    PostToolUseHookInput,
)

from darkfactory.state import Patch
from darkfactory.tools.patch_helpers import apply_unified_diff
from darkfactory.tools.shell import get_sandbox

log = logging.getLogger(__name__)

CAPTURE_TOOLS: frozenset[str] = frozenset({"Edit", "Write"})


def _match_tool(tool_name: str, target: str) -> bool:
    return tool_name == target or tool_name.endswith(f"__{target}")


def _is_capture_tool(tool_name: str) -> bool:
    return any(_match_tool(tool_name, t) for t in CAPTURE_TOOLS)


def _extract_path(tool_input: dict[str, Any]) -> str | None:
    p = tool_input.get("file_path")
    if isinstance(p, str) and p:
        return p
    return None


def make_diff_capture(
    role: str,
    slice_id: str,
    task_id: str,
    sink: list[Patch],
):
    """Return a PostToolUse hook callback that records Edit/Write diffs.

    Parameters
    ----------
    role:
        Author tag stored on each captured ``Patch`` (e.g. ``"backend"``).
    slice_id:
        Build-slice identifier the role is currently working on; copied
        verbatim into ``Patch.slice_id``.
    task_id:
        Key used to look up the active ``RepoSandbox`` from the per-task
        registry in ``tools/shell.py``.
    sink:
        Mutable list the hook appends to. The caller (a stage activity
        body) reads this list once the SDK loop finishes and folds the
        entries into the state delta returned to the workflow.
    """

    async def diff_capture_hook(
        input_data: PostToolUseHookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        if not _is_capture_tool(input_data["tool_name"]):
            return {}

        path = _extract_path(input_data.get("tool_input") or {})
        if path is None:
            return {}

        sandbox = get_sandbox(task_id)
        if sandbox is None:
            log.warning("diff_capture: no sandbox for task_id=%r", task_id)
            return {}

        diff_result = sandbox.exec(["git", "diff", "--", path])
        if diff_result.get("returncode") != 0:
            log.warning(
                "diff_capture: git diff failed for path=%r rc=%s stderr=%s",
                path,
                diff_result.get("returncode"),
                (diff_result.get("stderr") or "")[:500],
            )
            return {}

        diff = diff_result.get("stdout") or ""
        if not diff.strip():
            return {}

        if not apply_unified_diff(diff):
            log.warning(
                "diff_capture: captured diff failed sanity validation for path=%r",
                path,
            )
            return {}

        sha = ""
        sha_result = sandbox.exec(["git", "rev-parse", "HEAD"])
        if sha_result.get("returncode") == 0:
            sha = (sha_result.get("stdout") or "").strip()

        patch: Patch = {
            "path": path,
            "diff": diff,
            "author_agent": role,
            "slice_id": slice_id,
            "sha": sha,
        }
        sink.append(patch)
        return {}

    return diff_capture_hook
