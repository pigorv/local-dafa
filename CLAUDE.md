# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Dark Factory is an autonomous coding pipeline that turns a user prompt (or a labeled GitHub issue) into a reviewed pull request. A Temporal workflow drives a sequence of stages — hydrate, triage, planning (PO → Architect → Plan Critic), brief gate, build (Builder + Tester), verify, fixer loop, PR creation, reviewer, merge gate, deterministic merge — where each stage is one or more Claude Agent SDK roles running inside a per-workflow Docker worker container.

The repo is mid-migration from v1 to v2. The principle is **Planning describes intent; builders discover specifics** — planning emits an `ImplementationBrief` of work packages with verification predicates, and builders/testers use repo tools at execution time to discover concrete edits. Legacy `SpecSlice`/`affected_files` paths still exist as compatibility shims.

## Common commands

```bash
# Setup (Python 3.13, uv-managed)
uv sync

# Run the full local stack (Temporal + Langfuse + ClickHouse + Postgres + MinIO + OTel collector + orchestrator)
docker compose --profile worker-image build darkfactory-worker-image
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
3. **Planning loop** — up to `PLANNING_MAX_ATTEMPTS=5` calls of `discovery_stage` (PO → Architect → Plan Critic). On rejection, the critic's reason/edits are appended to `planning_feedback` and fed into the next pass. Cap exhaustion → `RunResult("needs_human", reason="planning_retry_cap")`.
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

Each role under `src/darkfactory/agents/` (po, architect, plan_critic, builder, tester, verifier_semantic, fixer, reviewer, pr_creator, triage) calls `darkfactory.llm_factory.build_options(role, ...)` to get a `ClaudeAgentOptions` with per-role model + thinking config. Defaults come from each role's manifest under `agents/manifests/` and can be overridden per-role with `LLM_<ROLE>_MODEL` / `LLM_<ROLE>_THINKING` env vars. Builder Supervisor (`agents/builder_supervisor.py`) is pure topo-sort, no LLM. Every work package is dispatched to Builder + Tester — there is no language-based routing.

`spec_adjustment.py` and the `spec_adjustment` role default are leftover from v1; the v2 replacement is `fixer.py` + `verifier_semantic.py`, and the activity is still exposed as `spec_adjustment_stage` for backwards compatibility. When touching planning/repair code, prefer the v2 names.

The SDK is wired in `bypassPermissions` mode with `setting_sources=["project"]`, so the target repo's `CLAUDE.md`, `.claude/skills/`, `.claude/settings.json`, and `.mcp.json` (rooted at the worker's `/workspace` cwd) load into every spawned session; host-level `~/.claude/` is intentionally excluded so the worker container stays hermetic. Project-level skills go through a separate gate: setting_sources discovers them on disk, but the SDK's `skills` option decides which are actually enabled. `tools.skills` in each role manifest defaults to `"all"`; narrow a role with `tools.skills: ["pdf", "docx"]` or disable with `tools.skills: []`. The env var `LLM_<ROLE>_SKILLS` overrides at compose time (`"all"`, `"name1,name2"`, or empty string to force-disable). Roles whose `tools.allowed` is `[]` (`plan_critic`, `verifier_semantic`) never load skills regardless of manifest — empty allowlist is a deliberate "no tools" statement. `Skill` does **not** appear in any role's `allowed_tools`; the SDK invokes skills implicitly when `skills` is set. Project-level MCP servers are loaded and callable by default: `tools.project_mcp_allowed` defaults to `["*"]` for every tool-using role, so any MCP declared in the target repo's `.mcp.json` is exposed as `mcp__*` in the role's `allowed_tools`. Narrow a role by setting `tools.project_mcp_allowed: ["filesystem", "linear"]` (each entry expands to `mcp__<name>__*`); disable entirely by setting `tools.project_mcp_allowed: []`. The env var `LLM_<ROLE>_PROJECT_MCP` overrides the manifest list at compose time (`"*"`, `"name1,name2"`, or empty string to force-disable). `tools.allowed` is normally an explicit list, but the pure-yolo sentinel `tools.allowed: "all"` lets a role call any tool the CLI exposes without enumerating each one; it resolves to an *empty* SDK `allowed_tools` (no `--allowedTools` flag), so since `bypassPermissions` already auto-approves every tool, the real guardrails for an `"all"` role are `tools.disallowed` and the `can_use_tool` argv gate — compose force-installs both the Bash argv gate and the `Edit`/`Write` path guard for `"all"` roles because they no longer self-trigger off the (now empty) resolved list. The env var `LLM_<ROLE>_TOOLS` overrides the manifest at compose time (`"all"`, `"name1,name2"`, or empty string to force zero-tool); `tools.allowed: []` (or `LLM_<ROLE>_TOOLS=""`) stays a deliberate "no tools" statement that also suppresses skills. The worker container is attached to the `darkfactory-net` Docker bridge network (`setup_worker_activity` runs it with `network="darkfactory-net"`), which has outbound egress — required because PR Creator must `git push` and run `gh`. Isolation is enforced by `cap_drop` / `no-new-privileges` / `pids_limit` / `mem_limit`, not by removing the network, so internet-dependent project MCPs can start. Shell access is per-role: read-only roles (PO, Architect, Reviewer) explicitly disallow `Bash`; the Builder, Tester, and Fixer run the built-in `Bash` directly with a pure-denylist policy (worker container is the isolation boundary, only `git push` blocked at the per-role layer); PR Creator also runs the built-in `Bash` but, unlike that pure-denylist, keeps a tight `argv_allowlist: [git, gh]` plus role-owned `git push` / `gh pr create` / `gh pr list` prefixes, with `gh issue` denied via `argv_denylist`. `hooks/permission_gate.py` enforces this on top of the `FORBIDDEN_TOKENS` deny-list and global no-merge enforcement. The former in-process `sandbox_bash` MCP tool (and its `darkfactory` MCP server) has been removed — no role used it after the `Bash` migration. `hooks/path_guard.py` adds an edit-path allowlist for `Edit`/`Write` calls.

### LangGraph subgraphs inside activities

Three stages internally use LangGraph rather than a single SDK call: `stages/discovery.py`, `stages/build.py`, `stages/verify.py`. They are exposed both as LangGraph Studio entry points (`langgraph.json`) and inlined into Temporal activities (`runtime/activities.py:discovery_stage`, `build_stage`, `verify_stage`). When wiring a new graph node, mind that the graph runs *inside* an activity coroutine, so node failures surface as `RetryPolicy(non_retryable_error_types=["ParseError"])`-bounded activity errors.

### Sandboxing

`tools/sandbox.py:RepoSandbox` is a per-task subprocess wrapper rooted at the target repo path. The worker container itself is the isolation boundary (Docker `cap_drop`, `no-new-privileges`, `pids_limit`, `mem_limit`; attached to the `darkfactory-net` bridge for required egress such as `git push` / `gh`) — there is no second nested container. `tools/shell.py` keeps a per-task registry of sandboxes and the `FORBIDDEN_TOKENS` list of shell metachars the permission gate rejects.

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
- **`ARCHITECTURE.md` is referenced in code comments but not present in the tree.** Treat those references as historical pointers; the live design lives in this file (`CLAUDE.md`).
