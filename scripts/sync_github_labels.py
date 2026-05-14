#!/usr/bin/env python3
"""Create or update Dark Factory GitHub labels from the docs."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOCS_PATH = REPO_ROOT / "docs" / "github-labels.md"


@dataclass(frozen=True)
class LabelSpec:
    name: str
    color: str
    description: str


def iter_bash_commands(markdown: str) -> Iterable[str]:
    """Yield shell commands from fenced bash/sh blocks, joining continuations."""
    in_shell_block = False
    command_parts: list[str] = []

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_shell_block:
                if command_parts:
                    yield " ".join(command_parts)
                    command_parts = []
                in_shell_block = False
            else:
                language = stripped.removeprefix("```").strip().lower()
                in_shell_block = language in {"bash", "sh", "shell"}
            continue

        if not in_shell_block:
            continue

        if not stripped or stripped.startswith("#"):
            if command_parts:
                yield " ".join(command_parts)
                command_parts = []
            continue

        if stripped.endswith("\\"):
            command_parts.append(stripped[:-1].rstrip())
            continue

        command_parts.append(stripped)
        yield " ".join(command_parts)
        command_parts = []


def parse_label_create_command(command: str) -> LabelSpec | None:
    tokens = shlex.split(command)
    if len(tokens) < 4 or tokens[:3] != ["gh", "label", "create"]:
        return None

    name = tokens[3]
    color: str | None = None
    description: str | None = None

    index = 4
    while index < len(tokens):
        token = tokens[index]

        if token in {"--repo", "-R"}:
            index += 2
            continue
        if token.startswith("--repo="):
            index += 1
            continue

        if token in {"--color", "-c"}:
            color = require_flag_value(tokens, index, token)
            index += 2
            continue
        if token.startswith("--color="):
            color = token.split("=", 1)[1]
            index += 1
            continue

        if token in {"--description", "-d"}:
            description = require_flag_value(tokens, index, token)
            index += 2
            continue
        if token.startswith("--description="):
            description = token.split("=", 1)[1]
            index += 1
            continue

        index += 1

    if color is None:
        raise ValueError(f"{name!r} is missing --color in docs")
    if description is None:
        raise ValueError(f"{name!r} is missing --description in docs")

    color = normalize_color(name, color)
    return LabelSpec(name=name, color=color, description=description)


def require_flag_value(tokens: list[str], index: int, flag: str) -> str:
    value_index = index + 1
    if value_index >= len(tokens) or tokens[value_index].startswith("-"):
        raise ValueError(f"{flag} is missing a value in: {shlex.join(tokens)}")
    return tokens[value_index]


def normalize_color(name: str, color: str) -> str:
    normalized = color.removeprefix("#").lower()
    if len(normalized) != 6 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError(f"{name!r} has invalid GitHub label color: {color!r}")
    return normalized


def load_label_specs(docs_path: Path) -> list[LabelSpec]:
    labels: list[LabelSpec] = []
    seen: set[str] = set()
    markdown = docs_path.read_text(encoding="utf-8")

    for command in iter_bash_commands(markdown):
        label = parse_label_create_command(command)
        if label is None:
            continue
        if label.name in seen:
            raise ValueError(f"duplicate label definition in {docs_path}: {label.name}")
        seen.add(label.name)
        labels.append(label)

    if not labels:
        raise ValueError(f"no `gh label create` commands found in {docs_path}")

    return labels


def build_gh_label_command(label: LabelSpec, repo: str, *, force: bool) -> list[str]:
    command = [
        "gh",
        "label",
        "create",
        label.name,
        "--repo",
        repo,
        "--color",
        label.color,
        "--description",
        label.description,
    ]
    if force:
        command.append("--force")
    return command


def sync_labels(labels: Iterable[LabelSpec], repo: str, *, dry_run: bool, force: bool) -> int:
    for label in labels:
        command = build_gh_label_command(label, repo, force=force)
        print(shlex.join(command))

        if dry_run:
            continue

        try:
            subprocess.run(command, check=True)
        except FileNotFoundError:
            print("error: GitHub CLI `gh` is not installed or not on PATH", file=sys.stderr)
            return 127
        except subprocess.CalledProcessError as exc:
            print(f"error: failed while syncing label {label.name!r}", file=sys.stderr)
            return exc.returncode or 1

    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create or update Dark Factory GitHub labels from docs/github-labels.md.",
    )
    parser.add_argument(
        "repo",
        nargs="?",
        help="Target GitHub repository as owner/name. Can also be provided via --repo or REPO.",
    )
    parser.add_argument(
        "-R",
        "--repo",
        dest="repo_flag",
        help="Target GitHub repository as owner/name, matching the GitHub CLI convention.",
    )
    parser.add_argument(
        "--docs",
        type=Path,
        default=DEFAULT_DOCS_PATH,
        help=f"Markdown file containing the documented gh label commands. Default: {DEFAULT_DOCS_PATH}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the gh commands without running them.",
    )
    parser.add_argument(
        "--no-force",
        action="store_true",
        help="Do not update existing labels; gh will fail if a label already exists.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    target_repo = args.repo_flag or args.repo or os.environ.get("REPO")

    if args.repo and args.repo_flag and args.repo != args.repo_flag:
        print("error: pass the target repo either positionally or via --repo, not both", file=sys.stderr)
        return 2
    if not target_repo:
        print("error: target repo is required (pass owner/name or set REPO=owner/name)", file=sys.stderr)
        return 2

    try:
        labels = load_label_specs(args.docs)
    except OSError as exc:
        print(f"error: could not read {args.docs}: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Syncing {len(labels)} labels from {args.docs} to {target_repo}")
    return sync_labels(labels, target_repo, dry_run=args.dry_run, force=not args.no_force)


if __name__ == "__main__":
    raise SystemExit(main())
