# Dark Factory — Project Summary

A self-contained briefing to seed a new Claude session for product and
architectural discussions. No implementation details — enough conceptual
ground to debate design changes.

---

## 1. What this project is

**Dark Factory** is an autonomous coding pipeline. Input: a freeform user
prompt or a labeled GitHub issue. Output: a reviewed pull request on a
target repository. Between those endpoints, a fixed sequence of LLM-driven
roles plans, builds, tests, verifies, repairs, and reviews the change,
with optional human gates at two strategic checkpoints.

It is positioned as a **learning artifact**, not a product — a working
composition of the current agentic-workflow stack (Temporal, Claude Agent
SDK, LangGraph, Langfuse, OpenTelemetry) meant to be forked and studied.
There is no SaaS, no signup, no roadmap. The repo runs as a local Docker
Compose stack against a local target repo or against issues on a GitHub
repo the operator owns.

### Core value proposition

- Turn a one-line intent ("add input validation to the login form") into a
  PR that has been planned, implemented, tested, verified against
  declared success predicates, repaired if needed, and code-reviewed —
  without humans driving each step.
- Keep the operator in control at exactly two points: **after planning**
  (the brief gate) and **before merge** (the merge gate). Everything in
  between is autonomous.
- Make the whole loop observable end-to-end: every span, every prompt,
  every tool call and cost is coalesced into a single Langfuse trace per
  workflow execution.

---

## 2. The product surface

There are two ways to start a run.

### Prompt run (CLI)

The operator runs `darkfactory run "<intent>" --repo <path>` against a
local checkout. The CLI starts a Temporal workflow and either blocks on
the result or fires-and-forgets. The two human gates are driven via a
`darkfactory gate` subcommand (`show`, `approve`, `revise`, `reject`,
`fix`, `rebuild`) keyed by workflow ID. A `--auto-approve-gates` flag
runs fully unattended, including the final merge.

### Issue-driven run (GitHub)

A Temporal Schedule polls a configured GitHub repo for issues carrying a
trigger label (default `df:ready`). Matching issues spawn an issue
workflow that drives a **strict label lifecycle** —
`df:ready → df:triaging → df:designing → df:awaiting-approval →
df:building → df:verifying → df:reviewing → df:awaiting-merge →
df:in-progress → df:done` — and posts phase comments at each stage,
including the implementation brief and the PR link.

Gate actions in issue mode are driven by GitHub labels or comments
(`approve`, `revise: …`, `reject`, `fix: …`, `rebuild`, plus
`df:approved` / `df:cancel` labels).

### Observability surfaces

- **Temporal UI** — workflow histories, task queues, schedules.
- **Langfuse** — coalesced traces per workflow execution, prompt
  versioning, cost, datasets, eval scores.
- **Claude Monitor** — per-session view of Claude Code activity.
- **LangGraph Studio** (optional) — interactive view of the four
  subgraph stages.

---

## 3. The pipeline (what actually happens to a request)

```
Hydrate
  → Triage
    → Discovery loop  [PO → Architect → Plan Critic]  × up to 5
      → BRIEF GATE  (human or auto)
        → Build  [Builder + Tester per work package]
          → Verify / Fixer loop  (budgeted per work package + per predicate)
            → PR Creator
              → Reviewer
                → MERGE GATE  (human or auto)
                  → Merge  (deterministic, not an agent)
```

### What each phase produces

- **Hydrate** — pulls the target repo into the worker container's
  `/workspace`, snapshots metadata.
- **Triage** — (issue-driven only) decides whether Dark Factory should
  even attempt this issue.
- **Discovery loop** — produces an **`ImplementationBrief`**. PO frames
  the problem, Architect drafts the brief, Plan Critic accepts or sends
  back revision notes. The loop is capped at 5 passes; exceeding the cap
  surfaces as `needs_human` rather than shipping a brief the critic
  rejected.
- **Brief gate** — operator can `approve`, `reject`, or `revise` (revise
  feeds new feedback and **resets the planning loop**).
- **Build** — Builder + Tester are dispatched per work package (topo-sorted
  by dependencies). Every work package gets both roles; there is no
  language-based routing.
- **Verify / Fixer loop** — Verifier evaluates each declared verification
  predicate against the working tree. Failures route to the Fixer, which
  is **budgeted per work package and per predicate** (cap = 2). Budget
  exhaustion surfaces as `needs_human`. The Fixer can also self-escalate
  with `needs_brief_change` or `cannot_fix`.
- **PR Creator** — pushes the branch and opens a PR with a structured
  description.
- **Reviewer** — performs a code review of the final branch.
- **Merge gate** — operator can `approve`, `reject`, request a `fix`
  (re-enters Fixer + Verify + Review), or request a `rebuild` (re-enters
  Build + Verify + Review).
- **Merge** — a deterministic step, not an agent.

### The central artifact: `ImplementationBrief`

This is the contract between planning and execution. It carries:

- `problem`, `expected_behavior`, `current_understanding`
- `proposed_design`, `contract_changes`
- A list of **work packages**, each with:
  - dependencies (for topo-sort)
  - `repo_areas` / `candidate_files` (hints, not prescriptions)
  - **verification predicates** — declarative success criteria the
    Verifier later evaluates

A core design principle governs how rich this artifact is allowed to be:

> **Planning describes intent; builders discover specifics.**

Planning emits work packages and predicates. Builders use repo tools at
execution time to figure out the concrete edits. The brief is deliberately
not a step-by-step recipe.

---

## 4. Process topology

Three Python processes, three responsibilities, **strictly separated**:

```
┌────────────────────────────────────────────────────────────────┐
│  Host                                                          │
│  CLI  (darkfactory run …)                                      │
│     • talks Temporal gRPC                                      │
│     • never runs LLMs                                          │
│     • opens the parent OTel span the workflow inherits         │
└─────────┬──────────────────────────────────────────────────────┘
          │
┌─────────▼──────────────────────────────────────────────────────┐
│  Docker Compose stack                                          │
│                                                                │
│  Orchestrator (long-lived, supervisor task queue)              │
│     • hosts workflow definitions                               │
│     • runs supervisor activities only                          │
│     • launches one worker container per workflow               │
│     • never runs the Claude SDK, never shells the target repo  │
│                                                                │
│  Per-workflow Worker (ephemeral, agent task queue <wf_id>)     │
│     • one container per workflow execution                     │
│     • hosts every stage activity                               │
│     • runs Claude Agent SDK calls                              │
│     • runs the target repo's build/test commands               │
│     • torn down in the workflow's `finally` block              │
│                                                                │
│  Support services: Temporal · Langfuse (+ Postgres,            │
│                    ClickHouse, Redis, MinIO) · OTel collector  │
│                    · Claude Monitor                            │
└────────────────────────────────────────────────────────────────┘
```

The separation is load-bearing for both isolation and operability:

- The orchestrator is the only thing that needs to be long-lived; it
  doesn't carry per-run state.
- Each worker is a blast-radius boundary. A misbehaving agent run, a
  broken target repo build, a runaway tool call — none of it can damage
  another workflow or the host.
- The CLI is a thin client. You could replace it with anything that
  speaks Temporal gRPC (UI, webhook, scheduler).

---

## 5. The workflow as a state machine

`DarkFactoryWorkflow.run` is the spine. Three things matter
architecturally about it:

1. **It is intentionally long-running** (>1h). Because of that, per-run
   trace coalescing relies on a session ID (`langfuse.session.id` ==
   `wf_id`) rather than on wall-clock-bounded traces.
2. **Workflow code is replay-deterministic.** No clock reads, no random,
   no host I/O. All side effects happen in activities. New control-flow
   points use `wait_condition` and `@workflow.update` rather than signals
   (updates synchronize on resolution and propagate validation errors
   back to the caller).
3. **Activity timeouts and retries are explicit per call.** There is no
   implicit default that matches existing stages — adding a stage means
   choosing its retry policy and timeout deliberately.

Two parallel workflow definitions exist:

- `DarkFactoryWorkflow` — prompt-driven runs.
- `DarkFactoryIssueWorkflow` — wraps the same stages with triage, the
  GitHub label lifecycle, and phase comments.
- `IssuePollWorkflow` — runs on the Temporal Schedule, starts/updates
  per-issue workflows from labeled issues.

### Pipeline state

`PipelineState` is a `TypedDict` with **reducer-annotated channels**.
Every activity returns a delta; the workflow's `merge(state, delta)`
applies the per-channel reducer (`add` for append-only logs,
`merge_specs` for spec slices, `overwrite` for last-writer-wins). When
adding a field, the reducer choice is a real design decision — an
unannotated list field silently loses earlier activity output.

---

## 6. The agent layer

Each role lives in `src/darkfactory/agents/` with a companion YAML
manifest under `agents/manifests/`. Every role calls
`llm_factory.build_options(role, …)` to materialize a
`ClaudeAgentOptions` from its manifest.

### Role roster

| Role | Purpose |
|---|---|
| **PO** | Frame the raw request as a structured problem statement |
| **Architect** | Produce the `ImplementationBrief` |
| **Plan Critic** | Approve the brief or request revisions (cap = 5) |
| **Builder** | Implement each work package against the target repo |
| **Tester** | Write/run tests for each work package |
| **Verifier (semantic)** | Evaluate each verification predicate |
| **Fixer** | Repair failing predicates (cap = 2 per WP / per predicate) |
| **Reviewer** | Code-review the final branch |
| **PR Creator** | Push the branch and open the PR |
| **Triage** | (Issue mode) Decide whether to attempt the issue |
| **Builder Supervisor** | Pure topo-sort, no LLM |

Defaults: Haiku for PO / PR Creator / Triage; Sonnet for everything else;
extended thinking on for Fixer only.

### Configuration model

Every per-role knob is layered the same way:

1. **Manifest YAML** declares the default model, thinking mode, allowed
   tools, allowed skills, allowed project MCPs, prompts.
2. **Environment variables** override at compose time
   (`LLM_<ROLE>_MODEL`, `LLM_<ROLE>_THINKING`, `LLM_<ROLE>_SKILLS`,
   `LLM_<ROLE>_TOOLS`, `LLM_<ROLE>_PROJECT_MCP`).
3. **Compose-time normalization** resolves tool/skill/MCP allowlists,
   installs the right hooks, and freezes the final options before the
   role runs.

### Tool/skill/MCP surface

The Claude Agent SDK runs in `bypassPermissions` mode with
`setting_sources=["project"]`. Crucial consequences:

- The target repo's `CLAUDE.md`, `.claude/skills/`, `.claude/settings.json`,
  and `.mcp.json` (rooted at `/workspace`) load into every spawned
  session.
- Host-level `~/.claude/` is intentionally **excluded** — the worker
  container stays hermetic.
- Skills are gated separately: discovery picks them up, but the SDK's
  `skills` option decides which are actually enabled. Default is "all";
  a manifest can narrow or disable.
- Project MCPs declared in the target repo's `.mcp.json` are exposed by
  default (`tools.project_mcp_allowed` defaults to `["*"]`).
- Roles with an explicit empty allowlist (Plan Critic, Verifier) are a
  deliberate "no tools, no skills" statement.

The pure-yolo `tools.allowed: "all"` sentinel is a deliberate escape
hatch for roles that need arbitrary tool access; for those roles the
real guardrails are `tools.disallowed`, the Bash argv gate, and the
`Edit`/`Write` path guard — all force-installed by the compose layer.

### Shell access

Per-role, intentional:

- **Read-only roles** (PO, Architect, Reviewer): no `Bash` at all.
- **Builder, Tester, Fixer**: built-in `Bash` with a pure-denylist
  policy. The worker container is the isolation boundary; only
  `git push` is blocked at the per-role layer.
- **PR Creator**: built-in `Bash` with a tight argv allowlist (`git`,
  `gh`) plus role-owned `git push` / `gh pr create` / `gh pr list`
  prefixes; `gh issue` is denied.

Hooks under `hooks/` enforce these guarantees: `permission_gate.py`
applies the argv allow/denylists and the global no-merge rule;
`path_guard.py` adds an edit-path allowlist for `Edit`/`Write`. Other
hooks (`call_cap.py`, `goal_pin.py`, `heartbeat.py`, `loop_breaker.py`,
`prompt_injection_guard.py`, `structured_output_hint.py`) handle
runaway-detection, goal anchoring, liveness, and output shaping.

### LangGraph subgraphs inside activities

Four stages internally use LangGraph rather than a single SDK call:
`triage`, `discovery`, `build`, `verify`. They are exposed both as
LangGraph Studio entry points (for interactive debugging) and inlined
into Temporal activities. A subgraph node failure surfaces as a normal
activity error subject to the activity's retry policy.

---

## 7. Sandboxing and security boundaries

- **Container is the isolation boundary.** `RepoSandbox` is a subprocess
  wrapper rooted at the target repo path inside the worker container;
  there is no nested second container.
- **Container hardening:** `cap_drop`, `no-new-privileges`, `pids_limit`,
  `mem_limit`.
- **Network:** worker is attached to the `darkfactory-net` Docker bridge
  with outbound egress. This is **required** (PR Creator needs to
  `git push` and run `gh`; some project MCPs need internet) and not
  considered a violation of the isolation model.
- **Shell guardrails:** the per-role permission gate, the `FORBIDDEN_TOKENS`
  list (shell metacharacters), and the edit-path allowlist sit on top of
  the container boundary.

---

## 8. Observability model

OTel is **mandatory**; `OTEL_SDK_DISABLED=true` is the only escape hatch.

- The CLI starts a parent `darkfactory.cli.run` span; the workflow
  inherits it via Temporal's tracing interceptor.
- Orchestrator and worker each call `bootstrap.init_observability` which
  installs a span processor that stamps `langfuse.session.id` (==
  `wf_id`) on every span.
- The bundled `claude` CLI emits its own native spans
  (`claude_code.interaction`, `claude_code.tool`, `claude_code.llm_request`),
  most as roots. The OTel collector's `transform/coalesce_trace_id`
  processor derives a deterministic trace ID from
  `temporalWorkflowID` / `langfuse.session.id` so all spans for one
  workflow execution coalesce into a single Langfuse trace.
- **Custom OTel spans must not be added inside workflow definitions**
  (Temporal replays the workflow on history fetch and would multiply
  spans). Custom instrumentation goes in activities.

### Prompts in Langfuse

When `LANGFUSE_PROMPTS_ENABLED=true` (default), roles fetch prompts from
Langfuse by label at startup; disk files in `src/darkfactory/prompts/`
are the fallback. Operators edit prompts in the Langfuse UI, promote a
version to the `production` label, and subsequent workflow starts pick
it up automatically.

---

## 9. Migration state (v1 → v2)

The repo is **mid-migration** from v1 to v2. This is worth knowing for
any architectural change discussion because compatibility shims are
intentional and shouldn't be casually removed:

- **`ImplementationBrief` (v2)** is the first-class artifact. `SpecSlice`
  (v1) is the legacy compatibility shape; `work_package_from_spec_slice`
  and `spec_slice_from_work_package` keep both views in sync.
- **`spec_adjustment.py`** and the `spec_adjustment` role default are v1
  leftovers. The v2 replacement is `fixer.py` + `verifier_semantic.py`.
  The activity is still exposed as `spec_adjustment_stage` for
  backwards-compatible tests.
- **`affected_files`** (v1) is the predecessor of the v2
  `repo_areas`/`candidate_files` hint pair.
- **`VERIFY_RETRY_CAP`** is a compatibility alias for old tests, not the
  live cap. The live cap is `FIXER_MAX_ATTEMPTS = 2`, applied **per work
  package and per predicate**, derived from the verifier's
  `predicate_coverage` plus blocking tester findings.
- Brief gate uses `approve_brief` / `revise_brief` / `reject_brief`;
  merge gate uses `approve_merge` / `reject_merge` / `trigger_fix` /
  `trigger_rebuild`. The legacy `approve_gate` / `reject_gate` aliases
  route by `_pending_gate` and exist for backwards compatibility only.

**Rule of thumb for proposals:** prefer the v2 name in new code; remove
a shim only when its callers and its tests are gone.

---

## 10. Conventions worth knowing for architectural changes

These are the load-bearing invariants. Breaking them is allowed, but
should be a conscious decision:

- **Three-process split is non-negotiable in spirit.** CLI never runs
  LLMs. Orchestrator never runs the SDK or shells the target repo.
  Worker is where all real work happens. Re-aggregating these requires
  rethinking blast radius and observability.
- **Workflow code is replay-deterministic.** All non-determinism lives
  in activities.
- **Reducer-annotated state channels.** Add a list field as
  `Annotated[..., add]` if you want append-only behavior; otherwise it
  is last-writer-wins and earlier activity output is lost.
- **Activity timeouts and retries are explicit per stage.**
- **GitHub state lives in labels**, driven by `swap_state_label_activity`
  on a strict lifecycle. Comment-driven approval signals are detected
  by `detect_approval_signal_activity`.
- **Fixer budget is per-WP and per-predicate, not global.** Architectural
  changes that touch verification need to keep that granularity.
- **Planning describes intent; builders discover specifics.** Don't push
  concrete edits up into the brief; don't push intent down into the
  builder.
- **Container is the security boundary.** Per-role tool/shell rules are
  defense in depth, not the primary boundary.

---

## 11. Glossary

| Term | Meaning |
|---|---|
| **Stage** | One named phase of the pipeline (Hydrate, Triage, Discovery, Build, Verify, Fixer, PR Creator, Reviewer, Merge). |
| **Role** | One LLM-driven agent persona (PO, Architect, Builder, …). |
| **Work package** | A unit of work inside an `ImplementationBrief`, with its own dependencies, repo-area hints, and verification predicates. |
| **Verification predicate** | A declarative success criterion attached to a work package, evaluated by the Verifier. |
| **`ImplementationBrief`** | The v2 planning artifact: problem, design, contract changes, list of work packages with predicates. |
| **`SpecSlice` / `affected_files`** | Legacy v1 shapes, kept as compatibility shims. |
| **Brief gate** | Human checkpoint after planning, before building. |
| **Merge gate** | Human checkpoint after review, before merging. |
| **`needs_human`** | Pipeline terminal state when caps are exhausted or escalation is requested. |
| **Discovery loop** | PO → Architect → Plan Critic, up to 5 passes. |
| **Fixer budget** | Per-WP and per-predicate cap (default 2) on Fixer attempts. |
| **Per-workflow worker** | Ephemeral Docker container running one workflow's stage activities. |
| **Supervisor activity** | Activity that runs on the orchestrator's task queue (setup/teardown only). |
| **Stage activity** | Activity that runs on the per-workflow worker's task queue. |
| **Hook** | A `hooks/*.py` module that intercepts SDK behavior (permission gating, path guarding, runaway detection, output shaping). |
| **Project MCP** | An MCP server declared in the target repo's `.mcp.json`. |
