import pytest


@pytest.fixture(autouse=True)
def _disable_langfuse_prompts(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PROMPTS_ENABLED", "false")
