"""Legacy prompt-loading shim for old discovery imports.

Re-exports `load_prompt` from `_sdk_common`. The old `build_discovery_agent`
factory and its LangChain middleware imports were removed in M2-9;
po/architect/plan_critic now build SDK clients directly.
"""
from __future__ import annotations

from darkfactory.agents._sdk_common import load_prompt

__all__ = ["load_prompt"]
