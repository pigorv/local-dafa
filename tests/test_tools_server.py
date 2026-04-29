"""M2-4: in-process MCP server exposing `sandbox_bash`.

Hermetic — no Docker, no real `RepoSandbox`. Drives the tool body through a
fake sandbox registered directly in `tools/shell.py`'s registry.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from darkfactory.tools import shell
from darkfactory.tools.sandbox import MAX_STDERR, MAX_STDOUT
from darkfactory.tools.server import (
    DEFAULT_TIMEOUT_S,
    SANDBOX_BASH_INPUT_SCHEMA,
    build_mcp_server,
)


class _FakeSandbox:
    """Records exec calls and returns scripted dicts."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[list[str], int]] = []
        self._response = response or {
            "returncode": 0,
            "stdout": "ok\n",
            "stderr": "",
            "timed_out": False,
        }

    def exec(self, argv: list[str], timeout: int = 120) -> dict[str, Any]:
        self.calls.append((list(argv), timeout))
        return dict(self._response)


@pytest.fixture
def task_id() -> str:
    return "test-task-server"


@pytest.fixture
def fake_sandbox(task_id: str):
    sb = _FakeSandbox()
    shell._SANDBOXES[task_id] = sb  # type: ignore[index]
    try:
        yield sb
    finally:
        shell._SANDBOXES.pop(task_id, None)  # type: ignore[arg-type]


def _get_tool(server_config: dict[str, Any], name: str):
    """Pull a registered tool out of the McpSdkServerConfig instance."""
    instance = server_config["instance"]
    handlers = getattr(instance, "request_handlers", {})
    assert handlers, "MCP server should register request handlers"
    return instance


def test_build_mcp_server_returns_sdk_config(task_id: str):
    config = build_mcp_server(task_id)
    assert config["type"] == "sdk"
    assert config["name"] == "darkfactory"
    assert config["instance"] is not None


def test_build_mcp_server_custom_name_and_version(task_id: str):
    config = build_mcp_server(task_id, name="custom-ns", version="2.3.4")
    assert config["name"] == "custom-ns"


def test_sandbox_bash_input_schema_shape():
    assert SANDBOX_BASH_INPUT_SCHEMA["type"] == "object"
    props = SANDBOX_BASH_INPUT_SCHEMA["properties"]
    assert props["argv"]["type"] == "array"
    assert props["argv"]["items"] == {"type": "string"}
    assert props["argv"]["minItems"] == 1
    assert props["timeout"]["type"] == "integer"
    assert props["timeout"]["default"] == DEFAULT_TIMEOUT_S
    assert SANDBOX_BASH_INPUT_SCHEMA["required"] == ["argv"]
    assert SANDBOX_BASH_INPUT_SCHEMA["additionalProperties"] is False


def _list_tools_via_mcp(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Use the underlying MCP server's list_tools handler to enumerate tools."""
    import mcp.types as mcp_types

    instance = config["instance"]
    handler = instance.request_handlers[mcp_types.ListToolsRequest]
    request = mcp_types.ListToolsRequest(method="tools/list")
    result = asyncio.run(handler(request))
    tools = result.root.tools
    return [
        {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
        for t in tools
    ]


def test_server_exposes_only_sandbox_bash(task_id: str):
    config = build_mcp_server(task_id)
    tools = _list_tools_via_mcp(config)
    assert len(tools) == 1
    spec = tools[0]
    assert spec["name"] == "sandbox_bash"
    assert "argv" in spec["inputSchema"]["properties"]
    assert spec["inputSchema"]["required"] == ["argv"]


async def _call_tool(config: dict[str, Any], name: str, args: dict[str, Any]):
    import mcp.types as mcp_types

    instance = config["instance"]
    handler = instance.request_handlers[mcp_types.CallToolRequest]
    request = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=args),
    )
    return await handler(request)


def test_sandbox_bash_dispatches_to_registered_sandbox(task_id, fake_sandbox):
    config = build_mcp_server(task_id)
    response = asyncio.run(
        _call_tool(config, "sandbox_bash", {"argv": ["git", "status"], "timeout": 30})
    )
    assert fake_sandbox.calls == [(["git", "status"], 30)]
    payload = json.loads(response.root.content[0].text)
    assert payload == {
        "returncode": 0,
        "stdout": "ok\n",
        "stderr": "",
        "timed_out": False,
    }
    assert response.root.isError in (False, None)


def test_sandbox_bash_default_timeout(task_id, fake_sandbox):
    config = build_mcp_server(task_id)
    asyncio.run(_call_tool(config, "sandbox_bash", {"argv": ["ls"]}))
    assert fake_sandbox.calls == [(["ls"], DEFAULT_TIMEOUT_S)]


def test_sandbox_bash_truncates_oversized_streams(task_id):
    big_out = "a" * (MAX_STDOUT + 50)
    big_err = "b" * (MAX_STDERR + 50)
    sb = _FakeSandbox(
        {"returncode": 0, "stdout": big_out, "stderr": big_err, "timed_out": False}
    )
    shell._SANDBOXES[task_id] = sb  # type: ignore[index]
    try:
        config = build_mcp_server(task_id)
        response = asyncio.run(_call_tool(config, "sandbox_bash", {"argv": ["echo"]}))
        payload = json.loads(response.root.content[0].text)
        assert len(payload["stdout"]) == MAX_STDOUT
        assert len(payload["stderr"]) == MAX_STDERR
    finally:
        shell._SANDBOXES.pop(task_id, None)  # type: ignore[arg-type]


def test_sandbox_bash_returns_error_when_no_sandbox_registered():
    config = build_mcp_server("unregistered-task")
    response = asyncio.run(
        _call_tool(config, "sandbox_bash", {"argv": ["git", "status"]})
    )
    payload = json.loads(response.root.content[0].text)
    assert payload["returncode"] == -1
    assert "no sandbox registered" in payload["stderr"]
    assert payload["timed_out"] is False
    assert response.root.isError is True


def test_each_call_resolves_sandbox_at_call_time(task_id):
    """Tool resolves the sandbox per-call; replacing it mid-flight works."""
    sb1 = _FakeSandbox({"returncode": 1, "stdout": "first", "stderr": "", "timed_out": False})
    shell._SANDBOXES[task_id] = sb1  # type: ignore[index]
    try:
        config = build_mcp_server(task_id)
        asyncio.run(_call_tool(config, "sandbox_bash", {"argv": ["x"]}))
        assert sb1.calls == [(["x"], DEFAULT_TIMEOUT_S)]

        sb2 = _FakeSandbox({"returncode": 2, "stdout": "second", "stderr": "", "timed_out": False})
        shell._SANDBOXES[task_id] = sb2  # type: ignore[index]
        response = asyncio.run(_call_tool(config, "sandbox_bash", {"argv": ["y"]}))
        payload = json.loads(response.root.content[0].text)
        assert payload["stdout"] == "second"
        assert sb2.calls == [(["y"], DEFAULT_TIMEOUT_S)]
        assert sb1.calls == [(["x"], DEFAULT_TIMEOUT_S)]
    finally:
        shell._SANDBOXES.pop(task_id, None)  # type: ignore[arg-type]
