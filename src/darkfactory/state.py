from __future__ import annotations
import os
from typing import Annotated, Any, Literal, NotRequired, TypedDict, Optional, TYPE_CHECKING
from operator import add
from pydantic import BaseModel, Field, RootModel

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

class IssueRef(BaseModel):
    repo: str
    number: int
    url: str
    title: str
    body: str
    labels: list[str]


class IssueComment(BaseModel):
    id: int
    author: str
    body: str
    created_at: str


class IssueRunRequest(BaseModel):
    repo_url: str
    repo_path: str = "/workspace"
    issue: IssueRef
    model_profile: str | None = None


class IssuePollRequest(BaseModel):
    repo: str
    label: str = "df:ready"
    limit: int = 100


class RunRequest(BaseModel):
    repo_url: str
    repo_path: str
    user_request: str
    model_profile: str | None = None

class RunResult(BaseModel):
    status: Literal[
        "merged",
        "rejected",
        "canceled",
        "exhausted_retries",
        "needs_human",
        "abandoned",
        "failed",
    ]
    state: dict
    reason: str | None = None

class GateDecision(BaseModel):
    approved: bool
    reason: str
    edits: dict | None = None


class ApprovalRecord(BaseModel):
    author: str
    approved_at: str
    spec_rev: int
    comment_id: int = 0
    text: str = ""

# ---------- Domain records ----------

class UserStory(TypedDict):
    id: str
    title: str
    as_a: str
    i_want: str
    so_that: str
    acceptance_criteria: list[str]


class VerificationPredicate(RootModel[str]):
    """Observable predicate that Tester and Verifier can check."""


class ContractChanges(BaseModel):
    api: list[str]
    data: list[str]
    events: list[str]


class WorkPackage(BaseModel):
    id: str
    story_id: str
    title: str
    intent: str
    verification: list[VerificationPredicate]
    repo_areas: list[str]
    candidate_files: list[str]
    dependencies: list[str]
    estimated_scope: str
    notes: list[str]


class ImplementationBrief(BaseModel):
    rev: int = Field(default=1, ge=1)
    problem: str
    expected_behavior: list[str]
    current_understanding: str
    proposed_design: str
    contract_changes: ContractChanges
    compatibility_risks: list[str]
    open_assumptions: list[str]
    test_strategy: str
    work_packages: list[WorkPackage]


class WorkPackageDict(TypedDict):
    story_id: str
    approach: str
    affected_files: list[str]
    new_files: list[str]
    test_files: list[str]
    risks: list[str]
    depends_on: list[str]                # other slice ids — the Build DAG
    id: NotRequired[str]
    title: NotRequired[str]
    intent: NotRequired[str]
    verification: NotRequired[str | list[str]]
    repo_areas: NotRequired[list[str]]
    candidate_files: NotRequired[list[str]]
    dependencies: NotRequired[list[str]]
    estimated_scope: NotRequired[str]
    notes: NotRequired[list[str]]


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [str(item) for item in value if item is not None and str(item)]


def _dedupe_ordered(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def work_package_from_dict(work_package: WorkPackageDict) -> WorkPackage:
    """Normalize a state work-package dict into the v2 model."""

    legacy_id = str(work_package["story_id"])
    raw_dependencies = (
        _string_list(work_package.get("dependencies"))
        or _string_list(work_package.get("depends_on"))
    )
    risks = _string_list(work_package.get("risks"))
    notes = _string_list(work_package.get("notes"))
    candidate_files = _dedupe_ordered(
        _string_list(work_package.get("affected_files"))
        + _string_list(work_package.get("new_files"))
        + _string_list(work_package.get("candidate_files"))
    )

    return WorkPackage(
        id=str(work_package.get("id") or legacy_id),
        story_id=legacy_id,
        title=str(work_package.get("title") or legacy_id),
        intent=str(work_package.get("intent") or work_package.get("approach") or ""),
        verification=[
            VerificationPredicate.model_validate(predicate)
            for predicate in _string_list(work_package.get("verification"))
        ],
        repo_areas=_string_list(work_package.get("repo_areas")),
        candidate_files=candidate_files,
        dependencies=raw_dependencies,
        estimated_scope=str(work_package.get("estimated_scope") or "unknown"),
        notes=notes + [f"Risk: {risk}" for risk in risks],
    )


def work_package_dict_from_model(work_package: WorkPackage) -> WorkPackageDict:
    """Adapt a v2 WorkPackage model back to the durable state dict shape."""

    wp = WorkPackage.model_validate(work_package)
    # Split notes vs. risks on the legacy "Risk: " convention so a round-trip
    # through this adapter and back through `work_package_from_dict` is stable.
    risks: list[str] = []
    notes: list[str] = []
    for entry in wp.notes:
        if entry.startswith("Risk: "):
            risks.append(entry[len("Risk: "):])
        else:
            notes.append(entry)
    return {
        # The legacy field is the build-node id, so keep dependency edges
        # pointing at WorkPackage ids during migration.
        "story_id": wp.id,
        "approach": wp.intent,
        "affected_files": list(wp.candidate_files),
        "new_files": [],
        "test_files": [],
        "risks": risks,
        "depends_on": list(wp.dependencies),
        "id": wp.id,
        "title": wp.title,
        "intent": wp.intent,
        "verification": [predicate.root for predicate in wp.verification],
        "repo_areas": list(wp.repo_areas),
        "candidate_files": list(wp.candidate_files),
        "dependencies": list(wp.dependencies),
        "estimated_scope": wp.estimated_scope,
        "notes": notes,
    }

class Patch(TypedDict):
    path: str
    diff: str                             # unified diff
    author_agent: str
    slice_id: str
    sha: NotRequired[str]                 # git rev-parse HEAD at capture time
    edit_kind: NotRequired[str]
    justification: NotRequired[str]

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

class CoverageEntry(TypedDict):
    wp_id: str
    predicate: str
    test_names: list[str]

class TesterFinding(TypedDict):
    kind: Literal[
        "behavior_mismatch",
        "naming_mismatch",
        "unclear_predicate",
        "infeasible_predicate",
    ]
    wp_id: str
    detail: str

class PredicateCoverage(TypedDict):
    wp_id: str
    predicate: str
    status: Literal["covered", "uncovered", "weakly_covered"]
    evidence: str

class ReviewDecision(TypedDict):
    approved: bool
    reason: str
    edits: dict                           # optional {field: new_value}

class ReviewerSummary(BaseModel):
    severity: Literal["low", "medium", "high"]
    issues: list[str]
    recommendation: Literal["approve", "request_changes"]

class VerifySummary(TypedDict):
    passed: bool
    failed_tests: int
    hard_findings: int
    predicate_coverage: NotRequired[list[PredicateCoverage]]
    uncovered_predicates: NotRequired[int]
    blocking_tester_findings: NotRequired[int]

# ---------- Reducers ----------

def merge_work_packages(
    left: list[WorkPackageDict] | None,
    right: list[WorkPackageDict] | None,
) -> list[WorkPackageDict]:
    by_id = {s["story_id"]: s for s in (left or [])}
    for s in right or []:
        by_id[s["story_id"]] = s          # last write wins per slice
    return list(by_id.values())


def merge_issue_comments(
    left: list[Any] | None,
    right: list[Any] | None,
) -> list[Any]:
    """Append-with-dedup reducer for issue_comments.

    The hydrator pulls every comment from `gh issue view` (with GitHub
    global node ids like `IC_kwDO...`), and the poll-driven fanout pushes
    a parallel copy via `post_new_comments` (with REST numeric ids). The
    two id systems don't match, so plain append duplicates the same body
    twice. Dedup by id when present, falling back to body text.
    """
    def _key(c: Any) -> tuple[str, str]:
        if isinstance(c, dict):
            cid = c.get("id")
            body = c.get("body")
        else:
            cid = getattr(c, "id", None)
            body = getattr(c, "body", None)
        id_key = str(cid).strip() if cid not in (None, "", 0) else ""
        body_key = (str(body).strip() if body else "")
        return id_key, body_key

    seen_ids: set[str] = set()
    seen_bodies: set[str] = set()
    out: list[Any] = []
    for c in [*(left or []), *(right or [])]:
        id_key, body_key = _key(c)
        if id_key and id_key in seen_ids:
            continue
        if body_key and body_key in seen_bodies:
            continue
        if id_key:
            seen_ids.add(id_key)
        if body_key:
            seen_bodies.add(body_key)
        out.append(c)
    return out


def overwrite(_: object, new: object) -> object:
    return new                            # last-write-wins channel


def _bounded_add(env_var: str, default: int):
    """Concat reducer that caps the result to the last N entries.

    The cap is resolved once at reducer construction time. Reading
    ``os.environ`` per call would violate Temporal's workflow sandbox,
    since ``merge()`` is invoked from replay-deterministic workflow code.
    """
    try:
        cap = max(1, int(os.environ.get(env_var, default)))
    except (TypeError, ValueError):
        cap = default

    def reducer(left: list | None, right: list | None, *args: Any, **kwargs: Any) -> list:
        combined = list(left or []) + list(right or [])
        if len(combined) > cap:
            return combined[-cap:]
        return combined
    return reducer

# ---------- The pipeline state ----------

class PipelineState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]   # conversational backbone
    user_request: str                                      # original prompt
    issue: Annotated[IssueRef, overwrite]
    issue_comments: Annotated[list[IssueComment], merge_issue_comments]
    repo_context: Annotated[dict, overwrite]               # from Hydrator
    stories: Annotated[list[UserStory], add]
    implementation_brief: Annotated[Optional[ImplementationBrief], overwrite]
    spec: Annotated[list[WorkPackageDict], merge_work_packages]
    work_packages: Annotated[list[dict], overwrite]        # v2 WorkPackage dumps; consumed by Plan Critic
    build_order: Annotated[list[str], overwrite]           # topo-sorted slice ids
    current_slice: Annotated[str, overwrite]
    patches: Annotated[list[Patch], add]
    coverage_entries: Annotated[list[CoverageEntry], add]
    tester_findings: Annotated[list[TesterFinding], add]
    test_results: Annotated[list[TestResult], add]
    findings: Annotated[list[Finding], add]
    verify_summary: Annotated[Optional[VerifySummary], overwrite]
    verify_retries: Annotated[int, overwrite]
    fixer_attempts_by_predicate: Annotated[dict[str, int], overwrite]
    fixer_attempts_by_wp: Annotated[dict[str, int], overwrite]
    attempt_log: Annotated[list[dict[str, Any]], _bounded_add("DARKFACTORY_ATTEMPT_LOG_MAX", 50)]
    planning_attempts: Annotated[int, overwrite]
    planning_feedback: Annotated[list[str], overwrite]
    planning_attempt_log: Annotated[list[dict[str, Any]], _bounded_add("DARKFACTORY_PLANNING_ATTEMPT_LOG_MAX", 50)]
    review_decision: Annotated[Optional[ReviewDecision | ReviewerSummary], overwrite]
    pr_url: Annotated[Optional[str], overwrite]
    changelog_entry: Annotated[Optional[str], overwrite]
    phase_comment_ids: Annotated[dict[str, int], overwrite]
    latest_spec_rev: Annotated[int, overwrite]
    approval_record: Annotated[Optional[ApprovalRecord], overwrite]
    approved_spec_rev: Annotated[Optional[int], overwrite]
    approved_spec_markdown: Annotated[Optional[str], overwrite]
    last_seen_comment_id: Annotated[int, overwrite]


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
        "fixer_attempts_by_predicate": {},
        "fixer_attempts_by_wp": {},
        "attempt_log": [],
        "planning_attempts": 0,
        "planning_feedback": [],
        "planning_attempt_log": [],
        "gate_approved": False,
        "phase_comment_ids": {},
        "latest_spec_rev": 1,
        "approval_record": None,
        "approved_spec_rev": None,
        "approved_spec_markdown": None,
        "last_seen_comment_id": 0,
    }


def init_state_from_issue(req: IssueRunRequest) -> dict:
    """Build the initial workflow-level state dict from an `IssueRunRequest`."""
    return {
        "issue": req.issue,
        "issue_comments": [],
        "repo_path": req.repo_path,
        "repo_url": req.repo_url,
        "model_profile": req.model_profile or "claude",
        "verify_retries": 0,
        "fixer_attempts_by_predicate": {},
        "fixer_attempts_by_wp": {},
        "attempt_log": [],
        "planning_attempts": 0,
        "planning_feedback": [],
        "planning_attempt_log": [],
        "gate_approved": False,
        "phase_comment_ids": {},
        "latest_spec_rev": 1,
        "approval_record": None,
        "approved_spec_rev": None,
        "approved_spec_markdown": None,
        "last_seen_comment_id": 0,
    }
