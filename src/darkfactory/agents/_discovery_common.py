"""Legacy shim kept until M2-12 converts spec_adjustment to the SDK.

Re-exports `load_prompt` from `_sdk_common` so the still-LangChain
`spec_adjustment` agent can keep importing from here without a parallel edit.
The old `build_discovery_agent` factory and its LangChain middleware imports
were removed in M2-9; po/architect/spec_reviewer now build SDK clients directly.
"""
from __future__ import annotations

from darkfactory.agents._sdk_common import load_prompt

__all__ = ["load_prompt"]
