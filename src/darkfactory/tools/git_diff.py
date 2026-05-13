"""Compute the ground-truth ``git diff`` for a Work Package turn.

This is the PR B replacement for the ``diff_capture`` PostToolUse hook on
the Builder. Instead of intercepting every ``Edit`` / ``Write`` call and
inferring patches from the side effects, the build subgraph snapshots
``HEAD`` before dispatching a worker and asks ``git`` what changed
afterwards. The result is the same shape (`Patch` TypedDict) and folds
into the same ``patches`` channel; the difference is provenance — the
agent never declares patches, and a hook never injects them into the
agent's apparent output. The diff is a deterministic post-step.

``git add -N -A`` is required so untracked-but-uncommitted new files
appear in ``git diff <pre_sha>``; otherwise ``git`` silently omits them.
This mirrors the recovery the legacy hook did at
``hooks/diff_capture.py:130``.
"""
from __future__ import annotations

import logging
from typing import Any

from darkfactory.state import Patch
from darkfactory.tools.patch_helpers import apply_unified_diff

log = logging.getLogger(__name__)


def _exec(sandbox: Any, argv: list[str]) -> dict[str, Any]:
    return sandbox.exec(argv)


def _head_sha(sandbox: Any) -> str:
    """Return the current ``HEAD`` sha, or ``""`` on any error."""
    result = _exec(sandbox, ["git", "rev-parse", "HEAD"])
    if result.get("returncode") != 0:
        return ""
    return (result.get("stdout") or "").strip()


def snapshot_head(sandbox: Any | None) -> str:
    """Return ``HEAD`` sha for a later :func:`compute_wp_diff` call.

    Returns ``""`` when the sandbox is missing or ``git`` errors out.
    Callers treat an empty pre-sha as "no diff baseline available" and
    skip the deterministic patch computation.
    """
    if sandbox is None:
        return ""
    return _head_sha(sandbox)


def _parse_name_status(stdout: str) -> list[tuple[str, str]]:
    """Parse ``git diff --name-status`` output into ``(status, path)`` tuples.

    The status code is the first whitespace-delimited token (``A``,
    ``M``, ``D``, ``R``, ``C``…); the path is the rest of the line. For
    rename/copy entries (``R<score>`` / ``C<score>``), git emits two
    paths separated by a tab — we record the destination as the touched
    path and drop the source. Build subgraph consumers care about *which
    files exist on disk now*, not the rename history.
    """
    out: list[tuple[str, str]] = []
    for raw in (stdout or "").splitlines():
        line = raw.rstrip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0].strip()
        path = parts[-1].strip()
        if not path:
            continue
        out.append((status, path))
    return out


_STATUS_TO_OPERATION: dict[str, str] = {
    "A": "create",
    "M": "modify",
    "D": "delete",
    "R": "modify",  # rename: post-rename path treated as a modification
    "C": "create",  # copy: destination treated as a creation
    "T": "modify",  # type change
}


def operation_for_status(status: str) -> str:
    if not status:
        return "modify"
    return _STATUS_TO_OPERATION.get(status[0].upper(), "modify")


def compute_wp_diff(
    sandbox: Any | None,
    pre_sha: str,
    *,
    role: str,
    slice_id: str,
) -> list[Patch]:
    """Return the per-file unified diffs between ``pre_sha`` and the working tree.

    Returns an empty list when no sandbox is registered, when ``pre_sha``
    is missing, when ``git`` errors, or when every captured diff fails
    the unified-diff sanity check (matches the silent no-op semantics of
    the legacy hook). Each returned :class:`Patch` is stamped with the
    ``author_agent=role`` and ``slice_id`` the caller supplied so
    existing consumers (``tester.py:_builder_signal``, PR creator,
    reviewer) continue to work unchanged.
    """
    if sandbox is None or not pre_sha:
        return []

    # Make untracked new files visible to `git diff`.
    _exec(sandbox, ["git", "add", "-N", "-A"])

    name_status = _exec(sandbox, ["git", "diff", "--name-status", pre_sha])
    if name_status.get("returncode") != 0:
        log.warning(
            "compute_wp_diff: name-status failed pre_sha=%r rc=%s stderr=%s",
            pre_sha,
            name_status.get("returncode"),
            (name_status.get("stderr") or "")[:500],
        )
        return []

    entries = _parse_name_status(name_status.get("stdout") or "")
    if not entries:
        return []

    head_sha = _head_sha(sandbox)

    patches: list[Patch] = []
    for status, path in entries:
        per_file = _exec(sandbox, ["git", "diff", pre_sha, "--", path])
        if per_file.get("returncode") != 0:
            log.warning(
                "compute_wp_diff: per-file diff failed path=%r rc=%s",
                path,
                per_file.get("returncode"),
            )
            continue
        diff = per_file.get("stdout") or ""
        if not apply_unified_diff(diff):
            log.warning(
                "compute_wp_diff: diff failed sanity validation path=%r",
                path,
            )
            continue
        patch: Patch = {
            "path": path,
            "diff": diff,
            "author_agent": role,
            "slice_id": slice_id,
        }
        if head_sha:
            patch["sha"] = head_sha
        patch["edit_kind"] = operation_for_status(status)
        patches.append(patch)
    return patches


def reconcile_paths(
    claimed_paths: list[str],
    actual_paths: list[str],
) -> dict[str, list[str]]:
    """Compare a Builder/Fixer's declared edit paths against the git diff.

    Returns a dict with two keys: ``claimed_not_applied`` (paths the
    agent declared but git did not record) and ``undeclared`` (paths
    git recorded that the agent did not declare). Either may be empty;
    when both are empty the agent's declaration matched ground truth.
    """
    claimed = {p for p in claimed_paths if p}
    actual = {p for p in actual_paths if p}
    return {
        "claimed_not_applied": sorted(claimed - actual),
        "undeclared": sorted(actual - claimed),
    }
