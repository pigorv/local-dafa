from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from darkfactory.state import PipelineState

REPO_MAP_CHAR_BUDGET = 4096  # ~1024 tokens at ~4 chars/token

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


def hydrate(root: str | Path) -> dict:
    """Build repo_context from a repo root: AGENTS.md, repo map, git log."""
    repo_root = Path(root).resolve()
    return {
        "repo_root": str(repo_root),
        "agents_md": _read_agents_md(repo_root),
        "repo_map": _build_repo_map(repo_root),
        "git_log": _git_log_oneline(repo_root, 10),
    }


def hydrator_node(state: PipelineState, runtime=None) -> dict:
    """LangGraph node: reads repo root from runtime context or state, emits repo_context."""
    root: str | Path | None = None
    if runtime is not None:
        ctx = getattr(runtime, "context", None)
        if ctx is not None:
            root = getattr(ctx, "repo_path", None)
    if not root:
        root = state.get("repo_context", {}).get("repo_root") if isinstance(state.get("repo_context"), dict) else None
    if not root:
        root = Path.cwd()
    return {"repo_context": hydrate(root)}
