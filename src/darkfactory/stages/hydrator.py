from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable

from darkfactory.state import PipelineState

REPO_MAP_CHAR_BUDGET = 4096  # ~1024 tokens at ~4 chars/token
ISSUE_VIEW_JSON_FIELDS = "title,body,labels,comments,assignees,milestone"
DF_CLARIFY_MARKER_RE = re.compile(r"<!--\s*df-clarify:[^>]*-->")

SOURCE_EXTENSIONS = {
    ".py", ".java", ".kt", ".kts", ".ts", ".tsx", ".js", ".jsx",
    ".go", ".rs", ".rb", ".cs", ".scala", ".sql",
}

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "target", "build",
    "dist", ".gradle", ".idea", ".vscode", "__pycache__",
    ".mvn", ".langgraph_api", ".darkfactory",
}

DEF_PATTERNS = [
    re.compile(r"^\s*(?:public |private |protected |static |final |abstract )*class\s+\w+"),
    re.compile(r"^\s*(?:public |private |protected |static |final |abstract )*interface\s+\w+"),
    re.compile(r"^\s*(?:public |private |protected |static |final |abstract )*record\s+\w+"),
    re.compile(r"^\s*(?:public |private |protected |static |final |abstract )*enum\s+\w+"),
    re.compile(r"^\s*def\s+\w+"),
    re.compile(r"^\s*class\s+\w+"),
    re.compile(r"^\s*async\s+def\s+\w+"),
    re.compile(r"^\s*function\s+\w+"),
    re.compile(r"^\s*export\s+(?:default\s+)?(?:async\s+)?function\s+\w+"),
    re.compile(r"^\s*(?:public |private |protected )?[\w<>\[\],\s]+\s+\w+\s*\([^)]*\)\s*\{"),  # java-ish methods
]


def _iter_source_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() in SOURCE_EXTENSIONS:
            yield p


def _extract_defs(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    hits: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or len(stripped) > 200:
            continue
        for pat in DEF_PATTERNS:
            if pat.match(line):
                hits.append(stripped.rstrip("{").strip())
                break
    return hits


def _build_repo_map(root: Path, budget: int = REPO_MAP_CHAR_BUDGET) -> str:
    entries: list[tuple[Path, list[str]]] = []
    for f in _iter_source_files(root):
        defs = _extract_defs(f)
        if defs:
            entries.append((f, defs))
    # Rank: more defs → likely more central (poor-man's PageRank proxy).
    entries.sort(key=lambda e: len(e[1]), reverse=True)

    out: list[str] = []
    used = 0
    for path, defs in entries:
        rel = path.relative_to(root).as_posix()
        header = f"\n{rel}"
        chunk_lines = [header]
        for d in defs[:20]:
            chunk_lines.append(f"  {d}")
        chunk = "\n".join(chunk_lines)
        if used + len(chunk) + 1 > budget:
            if used == 0:
                out.append(chunk[:budget])
                used = budget
            break
        out.append(chunk)
        used += len(chunk) + 1
    return "\n".join(out).strip()


def _read_agents_md(root: Path) -> str:
    for name in ("AGENTS.md", "agents.md"):
        p = root / name
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
    return ""


def _git_log_oneline(root: Path, n: int = 10) -> list[str]:
    try:
        from git import Repo, InvalidGitRepositoryError
    except ImportError:
        return []
    try:
        repo = Repo(str(root), search_parent_directories=True)
    except Exception:
        return []
    try:
        return [
            f"{c.hexsha[:7]} {c.summary}"
            for c in repo.iter_commits(max_count=n)
        ]
    except Exception:
        return []


def _collect_issue_context(repo: str, issue_number: int) -> dict[str, Any]:
    """Collect GitHub issue context via `gh issue view`."""
    completed = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--repo",
            repo,
            "--json",
            ISSUE_VIEW_JSON_FIELDS,
        ],
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    issue = json.loads(completed.stdout or "{}")
    comments = issue.get("comments") or []
    return {
        "repo": repo,
        "number": issue_number,
        "title": issue.get("title") or "",
        "body": issue.get("body") or "",
        "labels": issue.get("labels") or [],
        "comments": [
            comment
            for comment in comments
            if not DF_CLARIFY_MARKER_RE.search(str(comment.get("body") or ""))
        ],
        "assignees": issue.get("assignees") or [],
        "milestone": issue.get("milestone"),
    }


def _state_value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _label_names(labels: Iterable[Any] | None) -> list[str]:
    names: list[str] = []
    for label in labels or []:
        if isinstance(label, str):
            name = label
        elif isinstance(label, dict):
            name = label.get("name") or ""
        else:
            name = getattr(label, "name", "")
        if name:
            names.append(str(name))
    return names


def _author_name(author: Any) -> str:
    if isinstance(author, str):
        return author
    if isinstance(author, dict):
        return str(author.get("login") or author.get("name") or "")
    return str(getattr(author, "login", None) or getattr(author, "name", "") or "")


def _normalise_issue_comment(comment: Any) -> dict[str, Any]:
    return {
        "id": _state_value(comment, "id", ""),
        "author": _author_name(_state_value(comment, "author", "")),
        "body": str(_state_value(comment, "body", "") or ""),
        "created_at": str(
            _state_value(comment, "created_at", None)
            or _state_value(comment, "createdAt", "")
            or ""
        ),
    }


def _normalise_issue(issue: Any, collected: dict[str, Any]) -> dict[str, Any]:
    return {
        "repo": collected.get("repo") or _state_value(issue, "repo", ""),
        "number": collected.get("number") or _state_value(issue, "number", 0),
        "url": _state_value(issue, "url", ""),
        "title": collected.get("title") or _state_value(issue, "title", ""),
        "body": collected.get("body") or _state_value(issue, "body", ""),
        "labels": _label_names(
            collected.get("labels") or _state_value(issue, "labels", [])
        ),
    }


def hydrate(root: str | Path) -> dict:
    """Build repo_context from a repo root: AGENTS.md, repo map, git log."""
    repo_root = Path(root).resolve()
    return {
        "repo_root": str(repo_root),
        "agents_md": _read_agents_md(repo_root),
        "repo_map": _build_repo_map(repo_root),
        "git_log": _git_log_oneline(repo_root, 10),
    }


def hydrate_state(state: PipelineState, root: str | Path) -> dict:
    """Build the hydrator state delta for repo context and optional issue context."""
    delta: dict[str, Any] = {"repo_context": hydrate(root)}
    issue = state.get("issue")
    if not issue:
        return delta

    repo = _state_value(issue, "repo")
    number = _state_value(issue, "number")
    if not repo or number is None:
        return delta

    collected = _collect_issue_context(str(repo), int(number))
    delta["issue"] = _normalise_issue(issue, collected)
    delta["issue_comments"] = [
        _normalise_issue_comment(comment)
        for comment in collected.get("comments") or []
    ]
    return delta


def hydrator_node(state: PipelineState, runtime=None) -> dict:
    """LangGraph node: reads repo root from runtime context or state, emits hydrator delta."""
    root: str | Path | None = None
    if runtime is not None:
        ctx = getattr(runtime, "context", None)
        if ctx is not None:
            root = getattr(ctx, "repo_path", None)
    if not root:
        root = state.get("repo_context", {}).get("repo_root") if isinstance(state.get("repo_context"), dict) else None
    if not root:
        root = Path.cwd()
    return hydrate_state(state, root)
