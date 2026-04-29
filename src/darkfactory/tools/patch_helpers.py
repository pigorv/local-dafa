"""Helpers for validating captured unified diffs."""
from __future__ import annotations


def apply_unified_diff(diff: str) -> bool:
    """Return True when ``diff`` has unified-diff headers and at least one hunk."""
    if not diff or not diff.strip():
        return False
    has_header = any(
        line.startswith(("diff --git ", "--- ", "Index: "))
        for line in diff.splitlines()[:20]
    )
    has_hunk = any(line.startswith("@@") for line in diff.splitlines())
    return has_header and has_hunk
