"""Probe: does Anthropic's structured-output API surface JSON Schema
`description` fields to the model?

Strategy: send two queries with identical user messages but only the
treatment schema adds a `description` to the output field. The
description instructs the model to begin its value with a unique
canary token. If the canary appears only in the treatment run,
descriptions are read by the model.

Cost: two Haiku 4.5 calls, ~a few cents combined.
Requires the same auth your local `claude` CLI uses.
"""
from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock

USER_MESSAGE = "What color is the sky on a clear day? Reply in one short sentence."
MARKER = "CANARY_8f3a91"
DESCRIPTION = (
    f"Begin the value with the literal token {MARKER} followed by a single "
    "space, then the actual answer."
)


def make_schema(description: str | None) -> dict[str, Any]:
    field_spec: dict[str, Any] = {"type": "string"}
    if description is not None:
        field_spec["description"] = description
    return {
        "type": "object",
        "properties": {"answer": field_spec},
        "required": ["answer"],
        "additionalProperties": False,
    }


async def call(schema: dict[str, Any]) -> str:
    options = ClaudeAgentOptions(
        model="claude-haiku-4-5-20251001",
        system_prompt="",
        allowed_tools=[],
        disallowed_tools=[],
        mcp_servers={},
        cwd=".",
        setting_sources=[],
        permission_mode="bypassPermissions",
        output_format={"type": "json_schema", "schema": schema},
    )
    structured: dict[str, Any] | None = None
    async with ClaudeSDKClient(options=options) as client:
        await client.query(USER_MESSAGE)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if (
                        isinstance(block, ToolUseBlock)
                        and block.name == "StructuredOutput"
                    ):
                        structured = block.input
    return repr(structured)


async def main() -> None:
    print("=== Control: schema with NO description on `answer` ===")
    raw_ctrl = await call(make_schema(description=None))
    print(raw_ctrl)

    print(f"\n=== Treatment: description tells the model to prefix {MARKER} ===")
    raw_treat = await call(make_schema(description=DESCRIPTION))
    print(raw_treat)

    ctrl_has = MARKER in raw_ctrl
    treat_has = MARKER in raw_treat
    print(f"\nControl contains {MARKER!r}:   {ctrl_has}")
    print(f"Treatment contains {MARKER!r}: {treat_has}")
    if treat_has and not ctrl_has:
        print("\n→ Descriptions ARE surfaced to the model.")
    elif treat_has and ctrl_has:
        print("\n→ Inconclusive: marker appears in both runs.")
    else:
        print("\n→ Descriptions do NOT appear to influence output.")


if __name__ == "__main__":
    asyncio.run(main())
