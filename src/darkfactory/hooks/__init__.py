"""Public hook factory names available to role manifests."""
from __future__ import annotations

from types import MappingProxyType

from darkfactory.hooks.call_cap import make_call_cap
from darkfactory.hooks.goal_pin import make_goal_pin
from darkfactory.hooks.heartbeat import make_heartbeat
from darkfactory.hooks.loop_breaker import make_loop_breaker
from darkfactory.hooks.path_guard import make_path_guard
from darkfactory.hooks.prompt_injection_guard import make_prompt_injection_guard
from darkfactory.hooks.structured_output_hint import make_structured_output_hint

call_cap = make_call_cap
goal_pin = make_goal_pin
heartbeat = make_heartbeat
loop_breaker = make_loop_breaker
path_guard = make_path_guard
prompt_injection_guard = make_prompt_injection_guard
structured_output_hint = make_structured_output_hint

MANIFEST_HOOKS = MappingProxyType(
    {
        "call_cap": call_cap,
        "goal_pin": goal_pin,
        "heartbeat": heartbeat,
        "loop_breaker": loop_breaker,
        "path_guard": path_guard,
        "prompt_injection_guard": prompt_injection_guard,
        "structured_output_hint": structured_output_hint,
    }
)

__all__ = [
    "MANIFEST_HOOKS",
    "call_cap",
    "goal_pin",
    "heartbeat",
    "loop_breaker",
    "path_guard",
    "prompt_injection_guard",
    "structured_output_hint",
]
