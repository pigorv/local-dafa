from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "sync_github_labels.py"


def load_script_module():
    spec = importlib.util.spec_from_file_location("sync_github_labels", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_loads_label_specs_from_docs():
    module = load_script_module()

    labels = module.load_label_specs(REPO_ROOT / "docs" / "github-labels.md")

    assert len(labels) == 17
    assert labels[0] == module.LabelSpec(
        name="df:ready",
        color="0e8a16",
        description="Queue this issue for Dark Factory",
    )
    assert labels[-1] == module.LabelSpec(
        name="df:failed",
        color="d93f0b",
        description="Run failed / terminated / timed out",
    )


def test_builds_idempotent_gh_command():
    module = load_script_module()
    label = module.LabelSpec(
        name="df:ready",
        color="0e8a16",
        description="Queue this issue for Dark Factory",
    )

    command = module.build_gh_label_command(label, "acme/widgets", force=True)

    assert command == [
        "gh",
        "label",
        "create",
        "df:ready",
        "--repo",
        "acme/widgets",
        "--color",
        "0e8a16",
        "--description",
        "Queue this issue for Dark Factory",
        "--force",
    ]
