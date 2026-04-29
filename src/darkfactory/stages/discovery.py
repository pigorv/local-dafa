from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from darkfactory.agents.architect import run_architect
from darkfactory.agents.po import run_po
from darkfactory.agents.spec_reviewer import run_spec_reviewer
from darkfactory.state import PipelineState


async def po_node(state: PipelineState) -> dict:
    result = await run_po(state)
    return {"stories": [s.model_dump() for s in result.stories]}


async def architect_node(state: PipelineState) -> dict:
    result = await run_architect(state)
    return {"spec": [s.model_dump() for s in result.spec]}


async def spec_reviewer_node(state: PipelineState) -> dict:
    result = await run_spec_reviewer(state)
    return {"review_decision": result.model_dump()}


def discovery_subgraph() -> Any:
    """Discovery subgraph: PO → Architect → SpecReviewer (sequential).

    Roles are resolved by name and run as SDK clients inside the node bodies.
    """
    g = StateGraph(PipelineState)
    g.add_node("po", po_node)
    g.add_node("architect", architect_node)
    g.add_node("spec_reviewer", spec_reviewer_node)
    g.add_edge(START, "po")
    g.add_edge("po", "architect")
    g.add_edge("architect", "spec_reviewer")
    g.add_edge("spec_reviewer", END)
    return g.compile()
