from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
from typing import Any, Callable, Literal


ApprovalKind = Literal["Approve", "Revise", "Reject", "Cancel"]

_COMMAND_RE = re.compile(
    r"^\s*(?:@\S+\s+)*\/df\s+"
    r"(?P<command>approve|revise|reject|cancel)\b"
    r"(?P<text>[^\n]*)",
    re.IGNORECASE,
)
_AUTHORIZED_PERMISSIONS = {"admin", "maintain", "write"}
_AUTH_CACHE: dict[tuple[str, str], bool] = {}


@dataclass(frozen=True)
class ApprovalSignal:
    kind: ApprovalKind
    author: str
    comment_id: int = 0
    text: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_any(cls, value: Any) -> "ApprovalSignal":
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls(
                kind=_normalise_kind(value.get("kind")),
                author=str(value.get("author") or ""),
                comment_id=_int_or_zero(value.get("comment_id")),
                text=str(value.get("text") or ""),
                created_at=str(value.get("created_at") or ""),
            )
        kind = getattr(value, "kind", None)
        return cls(
            kind=_normalise_kind(kind),
            author=str(getattr(value, "author", "") or ""),
            comment_id=_int_or_zero(getattr(value, "comment_id", 0)),
            text=str(getattr(value, "text", "") or ""),
            created_at=str(getattr(value, "created_at", "") or ""),
        )


def parse_command(
    comment_body: str,
    *,
    author: str = "",
    comment_id: int = 0,
    created_at: str = "",
) -> ApprovalSignal | None:
    lines = str(comment_body or "").splitlines()
    first_index = next(
        (index for index, line in enumerate(lines) if line.strip()),
        None,
    )
    if first_index is None:
        return None

    match = _COMMAND_RE.match(lines[first_index])
    if match is None:
        return None

    command = match.group("command").lower()
    rest = (match.group("text") or "").strip()
    following = "\n".join(lines[first_index + 1 :]).strip()
    text = "\n".join(part for part in (rest, following) if part).strip()
    if command in {"revise", "reject"} and not text:
        return None

    return ApprovalSignal(
        kind={
            "approve": "Approve",
            "revise": "Revise",
            "reject": "Reject",
            "cancel": "Cancel",
        }[command],
        author=author,
        comment_id=_int_or_zero(comment_id),
        text=text,
        created_at=created_at,
    )


def is_authorized(
    author: str,
    repo: str,
    *,
    runner: Callable[[list[str]], str] | None = None,
) -> bool:
    author = str(author or "").strip().lstrip("@")
    repo = str(repo or "").strip()
    if not author or not repo or "/" not in repo:
        return False

    cache_key = (repo, author)
    if runner is None and cache_key in _AUTH_CACHE:
        return _AUTH_CACHE[cache_key]

    argv = [
        "gh",
        "api",
        f"repos/{repo}/collaborators/{author}/permission",
    ]
    try:
        stdout = runner(argv) if runner is not None else _run_gh(argv)
        payload = json.loads(stdout or "{}")
    except Exception:
        allowed = False
    else:
        permission = str(payload.get("permission") or "").lower()
        allowed = permission in _AUTHORIZED_PERMISSIONS

    if runner is None:
        _AUTH_CACHE[cache_key] = allowed
    return allowed


def clear_authorization_cache() -> None:
    _AUTH_CACHE.clear()


def _normalise_kind(raw: Any) -> ApprovalKind:
    value = str(raw or "").strip().lower()
    mapping: dict[str, ApprovalKind] = {
        "approve": "Approve",
        "approved": "Approve",
        "revise": "Revise",
        "reject": "Reject",
        "rejected": "Reject",
        "cancel": "Cancel",
        "canceled": "Cancel",
        "cancelled": "Cancel",
    }
    if value not in mapping:
        raise ValueError(f"unknown approval signal kind: {raw!r}")
    return mapping[value]


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _run_gh(argv: list[str]) -> str:
    import os
    import subprocess
    import tempfile

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("approval authorization requires GH_TOKEN or GITHUB_TOKEN")
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    env["GITHUB_TOKEN"] = token
    env["GH_PROMPT_DISABLED"] = "1"
    with tempfile.TemporaryDirectory(prefix="darkfactory-gh-") as config_dir:
        env["GH_CONFIG_DIR"] = config_dir
        completed = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            env=env,
            text=True,
            timeout=30,
        )
    if completed.returncode != 0:
        raise RuntimeError(
            "gh authorization check failed "
            f"(rc={completed.returncode}, stdout={completed.stdout!r}, "
            f"stderr={completed.stderr!r})"
        )
    return completed.stdout or ""
