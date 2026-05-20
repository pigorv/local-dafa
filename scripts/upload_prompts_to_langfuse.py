from __future__ import annotations

import argparse
import logging
import subprocess
from pathlib import Path

from langfuse import Langfuse

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "src/darkfactory/prompts"
EXPECTED_PROMPTS = {
    "architect",
    "builder",
    "fixer",
    "plan_critic",
    "po",
    "pr_creator",
    "reviewer",
    "tester",
    "triage",
    "verifier_semantic",
    "verify_planner",
}

log = logging.getLogger(__name__)


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            text=True,
        ).strip()
    except Exception:
        return "unknown"


def _prompt_files() -> list[Path]:
    files = sorted(PROMPTS_DIR.glob("*.md"))
    names = {path.stem for path in files}
    missing = EXPECTED_PROMPTS - names
    extra = names - EXPECTED_PROMPTS
    if missing or extra:
        raise SystemExit(
            "prompt set mismatch: "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )
    return files


def _content_matches(lf: Langfuse, name: str, label: str, content: str) -> bool:
    try:
        current = lf.get_prompt(name, label=label, type="text")
    except Exception:
        return False
    return getattr(current, "prompt", None) == content


def upload(*, label: str, dry_run: bool, force: bool) -> int:
    lf = Langfuse()
    sha = _git_sha()
    pushed = 0
    skipped = 0
    for path in _prompt_files():
        name = path.stem
        content = path.read_text(encoding="utf-8")
        if not force and _content_matches(lf, name, label, content):
            log.info("skip %s (content matches label=%s)", name, label)
            skipped += 1
            continue
        log.info("upload %s (label=%s sha=%s)", name, label, sha)
        if not dry_run:
            lf.create_prompt(
                name=name,
                prompt=content,
                labels=[label],
                tags=[f"sha:{sha}", "channel:disk-mirror"],
                type="text",
                commit_message=f"sync {name} from git {sha}",
            )
            pushed += 1
    log.info("done: %d pushed, %d skipped", pushed, skipped)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="production")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Upload even when Langfuse content already matches disk",
    )
    args = parser.parse_args()
    return upload(label=args.label, dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
