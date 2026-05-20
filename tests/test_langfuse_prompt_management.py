from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from darkfactory.agents import _sdk_common


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts/upload_prompts_to_langfuse.py"
PROMPTS_DIR = REPO_ROOT / "src/darkfactory/prompts"


def _clear_prompt_client_cache() -> None:
    _sdk_common._langfuse_prompt_client.cache_clear()


def _load_upload_script(monkeypatch: pytest.MonkeyPatch, fake_langfuse: type):
    fake_module = ModuleType("langfuse")
    fake_module.Langfuse = fake_langfuse
    monkeypatch.setitem(sys.modules, "langfuse", fake_module)
    module_name = f"_upload_prompts_to_langfuse_test_{id(fake_langfuse)}"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load upload script spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resolve_prompt_reads_disk_when_disabled(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PROMPTS_ENABLED", "false")
    _clear_prompt_client_cache()

    prompt = _sdk_common.resolve_prompt("architect")

    assert prompt.source == "disk"
    assert prompt.version is None
    assert prompt.text


def test_resolve_prompt_uses_langfuse_label(monkeypatch):
    fake_client = MagicMock()
    fake_client.get_prompt.return_value = MagicMock(
        prompt="from langfuse",
        version=7,
    )
    monkeypatch.setenv("LANGFUSE_PROMPTS_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PROMPT_LABEL", "staging")
    monkeypatch.setattr(_sdk_common, "_langfuse_prompt_client", lambda: fake_client)

    prompt = _sdk_common.resolve_prompt("architect")

    assert prompt.source == "langfuse"
    assert prompt.version == 7
    assert prompt.text == "from langfuse"
    fake_client.get_prompt.assert_called_once_with(
        "architect",
        label="staging",
        type="text",
    )


def test_resolve_prompt_falls_back_on_langfuse_error(monkeypatch, caplog):
    fake_client = MagicMock()
    fake_client.get_prompt.side_effect = RuntimeError("down")
    monkeypatch.setenv("LANGFUSE_PROMPTS_ENABLED", "true")
    monkeypatch.setattr(_sdk_common, "_langfuse_prompt_client", lambda: fake_client)

    prompt = _sdk_common.resolve_prompt("architect")

    assert prompt.source == "disk"
    assert prompt.text
    assert "falling back to disk" in caplog.text


def test_render_role_user_message_uses_resolver(monkeypatch):
    monkeypatch.setattr(
        _sdk_common,
        "resolve_prompt",
        lambda name, *, disk_path=None: _sdk_common.PromptResolution(
            name=name,
            text="Hello $name",
            source="langfuse",
            label="test",
            version=3,
            disk_path=disk_path,
            disk_sha="abc",
        ),
    )

    rendered = _sdk_common.render_role_user_message("architect", name="World")

    assert rendered == "Hello World"


def test_upload_dry_run_does_not_create_prompts(monkeypatch):
    class FakeLangfuse:
        create_calls: list[dict] = []

        def get_prompt(self, name: str, *, label: str, type: str):  # noqa: A002
            raise RuntimeError("missing")

        def create_prompt(self, **kwargs):
            self.create_calls.append(kwargs)

    module = _load_upload_script(monkeypatch, FakeLangfuse)

    assert module.upload(label="test", dry_run=True, force=False) == 0
    assert FakeLangfuse.create_calls == []


def test_upload_skips_when_content_matches(monkeypatch):
    class FakeLangfuse:
        create_calls: list[dict] = []

        def get_prompt(self, name: str, *, label: str, type: str):  # noqa: A002
            return SimpleNamespace(
                prompt=(PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
            )

        def create_prompt(self, **kwargs):
            self.create_calls.append(kwargs)

    module = _load_upload_script(monkeypatch, FakeLangfuse)

    assert module.upload(label="test", dry_run=False, force=False) == 0
    assert FakeLangfuse.create_calls == []


def test_upload_force_creates_even_when_content_matches(monkeypatch):
    class FakeLangfuse:
        create_calls: list[dict] = []

        def get_prompt(self, name: str, *, label: str, type: str):  # noqa: A002
            return SimpleNamespace(
                prompt=(PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
            )

        def create_prompt(self, **kwargs):
            self.create_calls.append(kwargs)

    module = _load_upload_script(monkeypatch, FakeLangfuse)

    assert module.upload(label="test", dry_run=False, force=True) == 0
    assert len(FakeLangfuse.create_calls) == 11


def test_upload_prompt_set_mismatch_fails_loudly(monkeypatch, tmp_path):
    class FakeLangfuse:
        pass

    module = _load_upload_script(monkeypatch, FakeLangfuse)
    (tmp_path / "architect.md").write_text("only one prompt", encoding="utf-8")
    monkeypatch.setattr(module, "PROMPTS_DIR", tmp_path)

    with pytest.raises(SystemExit, match="prompt set mismatch"):
        module._prompt_files()
