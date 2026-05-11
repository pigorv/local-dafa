from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from darkfactory.agents.architect import run_architect
from darkfactory.agents.plan_critic import run_plan_critic
from darkfactory.agents.po import run_po
from darkfactory.state import (
    ImplementationBrief,
    PipelineState,
    WorkPackage,
    work_package_dict_from_model,
)


def _expected_behavior_from_stories(stories: Any) -> list[str]:
    out: list[str] = []
    for story in stories or []:
        for criterion in (story.get("acceptance_criteria") or []) if isinstance(story, dict) else []:
            text = str(criterion).strip()
            if text and text not in out:
                out.append(text)
    return out


async def po_node(state: PipelineState) -> dict:
    result = await run_po(state)
    return {"stories": list(result.get("stories") or [])}


async def architect_node(state: PipelineState) -> dict:
    result = await run_architect(state)
    stories = state.get("stories") or []
    work_packages = [
        WorkPackage.model_validate(wp) for wp in result.get("work_packages") or []
    ]
    brief = ImplementationBrief(
        rev=int(state.get("latest_spec_rev") or 1),
        problem=str(state.get("user_request") or "").strip(),
        expected_behavior=_expected_behavior_from_stories(stories),
        current_understanding=result.get("current_understanding", ""),
        proposed_design=result.get("proposed_design", ""),
        contract_changes=result.get("contract_changes")
        or {"api": [], "data": [], "events": []},
        compatibility_risks=[],
        open_assumptions=[],
        test_strategy=result.get("test_strategy", ""),
        work_packages=work_packages,
    )
    return {
        "spec": [work_package_dict_from_model(wp) for wp in work_packages],
        "work_packages": [wp.model_dump() for wp in work_packages],
        "implementation_brief": brief.model_dump(mode="json"),
    }


async def plan_critic_node(state: PipelineState) -> dict:
    result = await run_plan_critic(state)
    return {"review_decision": result}


def discovery_subgraph() -> Any:
    """Discovery subgraph: PO → Architect → Plan Critic (sequential).

    Roles are resolved by name and run as SDK clients inside the node bodies.
    """
    g = StateGraph(PipelineState)
    g.add_node("po", po_node)
    g.add_node("architect", architect_node)
    g.add_node("plan_critic", plan_critic_node)
    g.add_edge(START, "po")
    g.add_edge("po", "architect")
    g.add_edge("architect", "plan_critic")
    g.add_edge("plan_critic", END)
    return g.compile()
