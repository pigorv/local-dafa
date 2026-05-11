# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Dark Factory is an autonomous coding pipeline that turns a user prompt (or a labeled GitHub issue) into a reviewed pull request. A Temporal workflow drives a sequence of stages — hydrate, triage, planning (PO → Architect → Plan Critic), brief gate, build (Builder + Tester), verify, fixer loop, reviewer, PR creation, merge gate, deterministic merge — where each stage is one or more Claude Agent SDK roles running inside a per-workflow Docker worker container.

The repo is mid-migration from v1 to v2 (see `docs/dark-factory-v2-implementation-plan.md`). The principle is **Planning describes intent; builders discover specifics** — planning emits an `ImplementationBrief` of work packages with verification predicates, and builders/testers use repo tools at execution time to discover concrete edits. Legacy `SpecSlice`/`affected_files` paths still exist as compatibility shims; the migration is tracked in `docs/dark-factory-v2-implementation-tracker.md`.

## Common commands

```bash
# Setup (Python 3.13, uv-managed)
uv sync

# Run the full local stack (Temporal + Langfuse + ClickHouse + Postgres + MinIO + OTel collector + orchestrator)
docker compose up -d

# Trigger a manual run against the local repo
uv run darkfactory run "implement X" --repo /path/to/target/repo

# Smoke-test the worker container plumbing only (no LLMs)
uv run darkfactory run --hello-worker --repo /path/to/repo

# GitHub issue watch (Temporal Schedule)
uv run darkfactory schedule install --repo owner/name --label df:ready --interval 60s
uv run darkfactory schedule list
uv run darkfactory schedule pause|resume|uninstall --repo owner/name

# Tests
uv run pytest tests/                      # full suite
uv run pytest tests/test_workflow_*.py    # workflow-only
uv run pytest -k "verify_retry" -x        # single test by name
uv run pytest -m "not integration"        # skip Docker/CLI/network-dependent tests
```

Workflow-level tests rely on Temporal's local time-skipping test server. The first run downloads the binary into `.cache/temporal-test-server/`; subsequent runs reuse the cache via `tests/temporal_testing.py`. To pre-warm or use a pre-staged binary, run `python scripts/bootstrap_temporal_test_server.py [--existing-path <path>]` (or set `TEMPORAL_TEST_SERVER_PATH`). Tests skip rather than fail when the binary is unreachable; set `TEMPORAL_TEST_SERVER_REQUIRED=1` to force failure offline.

`pytest` is configured via `pyproject.toml`: `pythonpath = ["src"]`, `testpaths = ["tests"]`, plus the `integration` marker for tests that need Docker/external CLIs.

## Architecture

### Process topology

Three Python entry points run as separate processes:

- **CLI** (`darkfactory.cli`): user-facing client. Connects to Temporal, starts a `DarkFactoryWorkflow`, opens the parent OTel span the workflow inherits.
- **Orchestrator** (`darkfactory.runtime.orchestrator_main`): one long-lived Temporal worker on the `supervisor-tq` task queue. Hosts the workflow definitions (`DarkFactoryWorkflow`, `DarkFactoryIssueWorkflow`, `IssuePollWorkflow`) and the supervisor activities — including `setup_worker_activity`, which `docker run`s a per-workflow worker container.
- **Per-workflow worker** (`darkfactory.runtime.worker_main`, container `darkfactory-worker-<wf_id>`): one Temporal worker on `agent-tq-<wf_id>`. Hosts `STAGE_ACTIVITIES` — every stage activity that executes Claude Agent SDK calls or runs the target repo's build/tests inside a `RepoSandbox`. Torn down by `teardown_worker_activity` in the workflow's `finally` block.

The orchestrator never runs the SDK or shells the target repo. The CLI never runs LLMs. All agent + repo work happens in the per-workflow worker.

### Workflow state machine

`DarkFactoryWorkflow.run` (`src/darkfactory/runtime/workflow.py`) is the spine. It is intentionally long-running (>1h), so per-execution Langfuse coalescing relies on `langfuse.session.id` rather than wall-clock-bounded traces. The flow:

1. `setup_worker_activity` (supervisor task queue) — boot the worker container.
2. `hydrate_stage` (agent task queue, hereafter "agent tq").
3. **Planning loop** — up to `PLANNING_MAX_ATTEMPTS=3` calls of `discovery_stage` (PO → Architect → Plan Critic). On rejection, the critic's reason/edits are appended to `planning_feedback` and fed into the next pass. Cap exhaustion → `RunResult("needs_human", reason="planning_retry_cap")`.
4. **Brief gate** — `wait_condition` blocks on `_brief_gate` / `_brief_revise` updates. Approve, reject, or revise (resets `planning_attempts` to 0 and re-enters the planning loop).
5. `build_stage` — Builder + Tester implement the brief.
6. **Verify/Fixer loop** — `verify_stage` produces a `VerifySummary`. If not passed, `_fixer_budget_exhaustion()` checks per-WP and per-predicate `FIXER_MAX_ATTEMPTS=2` budgets; on overrun, return `needs_human`. Otherwise increment `fixer_attempts_by_wp` / `fixer_attempts_by_predicate`, run `fixer_stage`, check the fixer's own `decision` for `needs_brief_change` / `cannot_fix` escalations, then re-verify.
7. `pr_creator_stage`, `reviewer_stage`.
8. **Merge gate** — `wait_condition` blocks on `_merge_gate` / `_merge_action`. Actions: `approve` → `merge_branch` (deterministic, not an agent), `fix` → `fixer_stage` + `verify_stage` + `reviewer_stage`, `rebuild` → `build_stage` + `verify_stage` + `reviewer_stage`, `reject`.
9. `teardown_worker_activity` (always runs in `finally`).

The issue-driven workflow (`runtime/issue_workflow.py`) wraps the same stages with extra triage, GitHub label lifecycle (`df:triaging` → `df:designing` → `df:awaiting-approval` → `df:building` → `df:verifying` → `df:awaiting-merge` → `df:done`), and phase comments rendered via `runtime/phase_comment.py`. `IssuePollWorkflow` runs on a Temporal Schedule (see `runtime/schedule_admin.py`) and starts/updates per-issue workflow runs from `df:ready` issues.

### Pipeline state

`PipelineState` (`src/darkfactory/state.py`) is a `TypedDict` with reducer-annotated channels (`Annotated[T, reducer]`). `merge(state, delta)` — used by the workflow on every activity return — applies the per-channel reducer (`add` for append-only logs, `merge_specs` for spec slices, `overwrite` for last-writer-wins). New top-level fields belong here so the reducer is consistent across activities and LangGraph subgraphs.

The first-class artifact moving through the pipeline is `ImplementationBrief` (problem, expected_behavior, current_understanding, proposed_design, contract_changes, work_packages with verification predicates and `repo_areas`/`candidate_files`). `SpecSlice` is the legacy compatibility shape; `work_package_from_spec_slice` and `spec_slice_from_work_package` keep both views in sync during migration.

### Agents and LLM configuration

Each role under `src/darkfactory/agents/` (po, architect, spec_reviewer, builder, tester, verifier_semantic, fixer, reviewer, pr_creator, triage) calls `darkfactory.llm_factory.build_options(role, ...)` to get a `ClaudeAgentOptions` with per-role model + temperature + thinking config. Defaults live in `_ROLE_DEFAULTS` in `llm_factory.py` and can be overridden per-role with `LLM_<ROLE>_MODEL` / `LLM_<ROLE>_TEMPERATURE` / `LLM_<ROLE>_THINKING` env vars. Builder Supervisor (`agents/builder_supervisor.py`) is pure topo-sort, no LLM. `frontend.py` is a no-op stub for the Java-only target app.

`spec_adjustment.py` and the `spec_adjustment` role default are leftover from v1; the v2 replacement is `fixer.py` + `verifier_semantic.py`, and the activity is still exposed as `spec_adjustment_stage` for backwards compatibility. When touching planning/repair code, prefer the v2 names.

The SDK is wired in `bypassPermissions` mode with `setting_sources=[]` for hermetic runs. `Bash` and `ToolSearch` are globally `disallowed_tools` — agents reach the host via the MCP `sandbox_bash` tool only, and that tool is in turn gated by `hooks/permission_gate.py` (per-role argv allowlist, forbidden-token check, no-merge enforcement, role-owned commands like `git push` restricted to the PR Creator). `hooks/path_guard.py` adds an edit-path allowlist for `Edit`/`Write` calls.

### LangGraph subgraphs inside activities

Three stages internally use LangGraph rather than a single SDK call: `stages/discovery.py`, `stages/build.py`, `stages/verify.py`. They are exposed both as LangGraph Studio entry points (`langgraph.json`) and inlined into Temporal activities (`runtime/activities.py:discovery_stage`, `build_stage`, `verify_stage`). When wiring a new graph node, mind that the graph runs *inside* an activity coroutine, so node failures surface as `RetryPolicy(non_retryable_error_types=["ParseError"])`-bounded activity errors.

### Sandboxing

`tools/sandbox.py:RepoSandbox` is a per-task subprocess wrapper rooted at the target repo path. The worker container itself is the isolation boundary (Docker `cap_drop`, `no-new-privileges`, `pids_limit`, `mem_limit`, no network) — there is no second nested container. `tools/shell.py` keeps a per-task registry of sandboxes and the `FORBIDDEN_TOKENS` list of shell metachars the permission gate rejects.

### Observability

OTel is mandatory; `OTEL_SDK_DISABLED=true` is the only escape hatch. The CLI starts a `darkfactory.cli.run` parent span that the workflow inherits via `temporalio.contrib.opentelemetry.TracingInterceptor`. The orchestrator + worker each call `bootstrap.init_observability(service_name)`, which installs a `SessionStampingSpanProcessor` that stamps `langfuse.session.id` (== `wf_id`) on every span.

The bundled `claude` CLI emits its own native spans (`claude_code.interaction`, `claude_code.tool`, `claude_code.llm_request`). Most of those are emitted as roots; W3C TRACEPARENT only adopts the first. The `otel-collector-config.yaml` runs a `transform/coalesce_trace_id` processor that derives a deterministic trace_id from `temporalWorkflowID` / `langfuse.session.id` so all spans for one workflow execution coalesce into one Langfuse trace. `llm_factory._otel_resource_attributes_with_parent_span` stamps `darkfactory.cli_parent_span_id` so the collector can rewrite `parent_span_id` on the orphan CLI spans. **Do not add custom OTel spans inside `@workflow.defn` bodies** — Temporal replays the workflow on history fetch and would multiply the spans. Custom instrumentation belongs in `@activity.defn` functions.

## Conventions worth knowing

- **Workflow code is replay-deterministic.** No `datetime.now()`, no random, no host I/O. Use `workflow.now()`, `workflow.execute_activity(...)`, `workflow.wait_condition(...)`. New gates / signals go through `@workflow.update`, not `@workflow.signal` — updates synchronize on resolution and propagate validation errors back to the caller.
- **Activity timeouts and retries are explicit.** When adding a new activity, set `start_to_close_timeout`, `heartbeat_timeout` if the activity loops, and a `RetryPolicy` if you want bounded retries — there is no implicit default that matches the existing stages.
- **State channel reducers matter.** If you add a list field expecting append-only behaviour, annotate it `Annotated[..., add]` in `PipelineState`; otherwise `merge()` last-writes-wins it and earlier activity output is lost.
- **Fixer budget is per-WP and per-predicate, not global.** `_fixer_failure_targets()` derives targets from the verifier's `predicate_coverage` plus blocking `tester_findings`; `VERIFY_RETRY_CAP` (= `FIXER_MAX_ATTEMPTS + 1`) is a compatibility alias for old tests, not the live cap.
- **Brief gate vs merge gate.** Brief gate uses `approve_brief` / `revise_brief` / `reject_brief`; merge gate uses `approve_merge` / `reject_merge` / `trigger_fix` / `trigger_rebuild`. The legacy `approve_gate` / `reject_gate` aliases route by `_pending_gate`; prefer the dedicated update method on new code.
- **GitHub state lives in labels.** The issue workflow drives a strict `df:*` label lifecycle through `swap_state_label_activity`; the schedule's `IssuePollRequest.label` is the entry filter. Comment-driven approval signals are detected by `detect_approval_signal_activity` (see `runtime/approval.py`).
- **Compatibility shims are intentional.** `spec_adjustment_stage`, `SpecSlice`, `affected_files`, `VERIFY_RETRY_CAP` etc. exist to keep older tests green during the v2 migration. Prefer the v2 path in new code; remove a shim only when its callers and tests are gone.
- **`ARCHITECTURE.md` is referenced in code comments but not present in the tree.** Treat those references as historical pointers; the live design is in `docs/dark-factory-v2-implementation-plan.md` plus this file.
