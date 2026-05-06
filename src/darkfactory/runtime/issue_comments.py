from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TypeVar

# Matches any Dark Factory bot marker (phase, clarification, and quarantine).
# Both are internal bookkeeping comments and must not bleed into workflow
# context (clarification prompts, triage state, etc.).
_DF_MARKER_RE = re.compile(r"<!--\s*df-(?:phase|clarify|quarantine):[^>]*-->")
_DF_CLARIFY_MARKER_RE = re.compile(r"<!--\s*df-clarify:[^>]*-->")

_T = TypeVar("_T")


def _comment_body(comment: object) -> str:
    if isinstance(comment, dict):
        return str(comment.get("body") or "")
    return str(getattr(comment, "body", "") or "")


def has_dark_factory_clarify_marker(comment: object) -> bool:
    return bool(_DF_CLARIFY_MARKER_RE.search(_comment_body(comment)))


def has_dark_factory_marker(comment: object) -> bool:
    return bool(_DF_MARKER_RE.search(_comment_body(comment)))


def filter_dark_factory_marker_comments(comments: Iterable[_T]) -> list[_T]:
    """Drop any comment authored by the Dark Factory bot (df-clarify / df-quarantine)."""
    return [
        comment
        for comment in comments
        if not has_dark_factory_marker(comment)
    ]
