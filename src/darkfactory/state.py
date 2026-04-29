from __future__ import annotations
from typing import Annotated, Any, Literal, NotRequired, TypedDict, Optional, TYPE_CHECKING
from operator import add
from pydantic import BaseModel

if TYPE_CHECKING:
    from langchain_core.messages import AnyMessage
else:
    AnyMessage = Any


def add_messages(left: list[Any], right: list[Any], *args: Any, **kwargs: Any) -> list[Any]:
    from langgraph.graph.message import add_messages as langgraph_add_messages

    return langgraph_add_messages(left, right, *args, **kwargs)

# ---------- Immutable run context (context_schema) ----------

class RunContext(BaseModel):
    repo_path: str                       # absolute path on host
    repo_url: Optional[str] = None       # for gh operations
    base_branch: str = "main"
    feature_branch: str = ""              # set on first Build entry; agent/<slug>
    task_id: str
    allow_auto_merge: bool = False
    model_profile: Literal["local", "claude", "mixed"] = "local"

# ---------- Temporal I/O ----------

class RunRequest(BaseModel):
    repo_url: str
    repo_path: str
    user_request: str
    model_profile: str | None = None

class RunResult(BaseModel):
    status: Literal["merged", "rejected", "exhausted_retries", "failed"]
    state: dict
    reason: str | None = None

class GateDecision(BaseModel):
    approved: bool
    reason: str
    edits: dict | None = None

# ---------- Domain records ----------

class UserStory(TypedDict):
    id: str
    title: str
    as_a: str
    i_want: str
    so_that: str
    acceptance_criteria: list[str]

class SpecSlice(TypedDict):
    story_id: str
    approach: str
    affected_files: list[str]
    new_files: list[str]
    test_files: list[str]
    risks: list[str]
    depends_on: list[str]                # other slice ids — the Build DAG

class Patch(TypedDict):
    path: str
    diff: str                             # unified diff
    author_agent: str
    slice_id: str
    sha: NotRequired[str]                 # git rev-parse HEAD at capture time

class TestResult(TypedDict):
    runner: str                           # pytest|npm|cargo|go
    returncode: int
    passed: int
    failed: int
    errors: list[str]
    duration_s: float

class Finding(TypedDict):
    tool: str                             # ruff|mypy|semgrep|bandit
    severity: Literal["info", "warn", "error", "critical"]
    file: str
    line: int
    rule: str
    message: str

class ReviewDecision(TypedDict):
    approved: bool
    reason: str
    edits: dict                           # optional {field: new_value}

class CodeQualitySummary(BaseModel):
    severity: Literal["low", "medium", "high"]
    issues: list[str]
    recommendation: Literal["approve", "request_changes"]

class VerifySummary(TypedDict):
    passed: bool
    failed_tests: int
    hard_findings: int

# ---------- Reducers ----------

def merge_specs(left: list[SpecSlice], right: list[SpecSlice]) -> list[SpecSlice]:
    by_id = {s["story_id"]: s for s in (left or [])}
    for s in right or []:
        by_id[s["story_id"]] = s          # last write wins per slice
    return list(by_id.values())

def overwrite(_: object, new: object) -> object:
    return new                            # last-write-wins channel

# ---------- The pipeline state ----------

class PipelineState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]   # conversational backbone
    user_request: str                                      # original prompt
    repo_context: Annotated[dict, overwrite]               # from Hydrator
    stories: Annotated[list[UserStory], add]
    spec: Annotated[list[SpecSlice], merge_specs]
    build_order: Annotated[list[str], overwrite]           # topo-sorted slice ids
    current_slice: Annotated[str, overwrite]
    patches: Annotated[list[Patch], add]
    test_results: Annotated[list[TestResult], add]
    findings: Annotated[list[Finding], add]
    verify_summary: Annotated[Optional[VerifySummary], overwrite]
    verify_retries: Annotated[int, overwrite]
    review_decision: Annotated[Optional[ReviewDecision | CodeQualitySummary], overwrite]
    pr_url: Annotated[Optional[str], overwrite]
    changelog_entry: Annotated[Optional[str], overwrite]


# ---------- Workflow-level helpers ----------

def _pipeline_reducers() -> dict[str, Any]:
    import typing as _typing

    hints = _typing.get_type_hints(PipelineState, include_extras=True)
    out: dict[str, Any] = {}
    for key, hint in hints.items():
        meta = getattr(hint, "__metadata__", ())
        if meta:
            out[key] = meta[0]
    return out


_PIPELINE_REDUCERS = _pipeline_reducers()


def merge(state: dict, delta: dict) -> dict:
    """Combine workflow-level state with an activity's state delta.

    Channels declared with `Annotated[..., reducer]` in `PipelineState` use
    that reducer; everything else (plain channels, ad-hoc keys) is
    last-write-wins.
    """
    out = dict(state)
    for key, value in (delta or {}).items():
        reducer = _PIPELINE_REDUCERS.get(key)
        existing = out.get(key)
        if reducer is None or existing is None or value is None:
            out[key] = value
        else:
            out[key] = reducer(existing, value)
    return out


def init_state(req: RunRequest) -> dict:
    """Build the initial workflow-level state dict from a `RunRequest`."""
    return {
        "user_request": req.user_request,
        "repo_path": req.repo_path,
        "repo_url": req.repo_url,
        "model_profile": req.model_profile or "claude",
        "verify_retries": 0,
        "gate_approved": False,
    }
