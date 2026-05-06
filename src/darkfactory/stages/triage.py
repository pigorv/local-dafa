from __future__ import annotations

from typing import Annotated, Any, Literal

from langgraph.graph import END, START, StateGraph

from darkfactory.agents.triage import TriageOutput, run_triage
from darkfactory.state import PipelineState, overwrite

TRIAGE_NODE = "triage"
READY_EDGE = "ready_to_build"
CLARIFY_EDGE = "needs_clarification"

TriageEdge = Literal["ready_to_build", "needs_clarification"]


class TriageState(PipelineState, total=False):
    ready_to_build: Annotated[bool, overwrite]
    clarification_questions: Annotated[list[str], overwrite]
    derived_user_request: Annotated[str, overwrite]
    confidence: Annotated[Literal["low", "medium", "high"], overwrite]
    rationale: Annotated[str, overwrite]


async def triage_node(state: TriageState) -> TriageOutput:
    return await run_triage(state)


def triage_edge(state: TriageState) -> TriageEdge:
    return READY_EDGE if state.get("ready_to_build") else CLARIFY_EDGE


def triage_subgraph() -> Any:
    """Triage subgraph: one reasoning node, then route by readiness."""
    g = StateGraph(TriageState)
    g.add_node(TRIAGE_NODE, triage_node)
    g.add_edge(START, TRIAGE_NODE)
    g.add_conditional_edges(
        TRIAGE_NODE,
        triage_edge,
        {
            READY_EDGE: END,
            CLARIFY_EDGE: END,
        },
    )
    return g.compile()
