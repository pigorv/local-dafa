# Migration Tracker — Consolidated Agent Harness

Companion to `migration_plan.md`. Each task below is scoped so a single
Claude Code session can pick it up, finish it, and ship it without needing
the rest of the tracker open.

## How to use this tracker

1. Pick the lowest-numbered open task whose dependencies are all `[x] done`.
2. Read the section in `migration_plan.md` named in the task's `Plan ref`.
   Treat that as authoritative.
3. Implement the `Scope` items, run the listed tests, and tick the boxes.
4. Update `Status` to one of: `[ ] open`, `[~] in-progress {owner/session}`,
   `[!] blocked {reason}`, `[x] done {commit-sha}`.
5. If you discover new work, add a follow-up task under "Follow-ups
   discovered during implementation" rather than widening an in-flight task.

Legend: `[ ]` open · `[~]` in-progress · `[x]` done · `[!]` blocked

## Conventions (binding for every task)

- **One open refactor per role at a time.** The active v1→v2 migration and
  this harness consolidation must not touch the same agent module
  simultaneously. Before starting a role task, check
  `docs/dark-factory-v2-implementation-tracker.md` — if that role has an
  open task there, block this one until it lands.
- **Parity assertion is a hard gate.** From Task 0.4 onward, every migrated
  role must keep the parity test green until Task 6.4 deletes it. A failing
  parity assertion blocks merge; never delete the imperative factory until
  the parity test for that role has been green for at least one full CI run.
- **Global denylists stay code-declared.** No task may move
  `DENIED_ARGV_PREFIXES`, `FORBIDDEN_TOKENS`, the secret/lockfile tables in
  `hooks/path_guard.py`, or the `MERGE_TOOLS` set into a manifest. These are
  intentionally not manifest-declarable.
- **Replay-determinism rail.** Registry and composer must never be imported
  from `@workflow.defn` bodies. CI lint (Task 0.5) enforces this; tasks
  that touch workflow modules must respect it.
- **LangGraph node names are load-bearing for tracing.** Do not rename
  graph nodes during this migration.
- **`build_options(role, …)` keeps its current signature until Task 6.1.**
  This is the migration invariant from the plan. Migrated roles route
  through `compose(...)`; un-migrated roles keep their imperative
  `make_<role>_client()`. Both paths coexist until Phase 6.
- **No git commands inside a task session.** Leave version control to the
  user.

---

## Phase 0 — Scaffolding (no role migrated yet)

Plan ref: §"Phase 0 — Scaffolding (no role migrated yet)".
Goal: stand up manifests, registry, composer, parity, lint, introspection.
No role is migrated in this phase — only the surfaces those roles will
plug into.

### Task 0.1 — Pydantic manifest schema (`agents/manifest.py`)
- Status: `[x] done codex-session`
- Depends on: —
- Plan ref: §"Layer 1 — Manifests".
- Scope:
  - Create `src/darkfactory/agents/manifest.py` with a Pydantic model
    matching the field list in the plan: `identity` (role, description,
    when_to_use), `llm` (model, temperature, thinking, prompt_path),
    `tools` (allowed, disallowed, argv_allowlist, role_owned_argv_prefixes,
    edit_path_allowlist), `mcp` (server names), `hooks` (declarative
    attachments with parameters), `budgets` (timeout, heartbeat, retry
    caps), `io_contract` (read/write channels — documentation today).
  - Strict mode (`extra="forbid"`); unknown fields raise.
  - No callers yet. No registry. No filesystem reads.
- Files (likely):
  - `src/darkfactory/agents/manifest.py` (new)
- Acceptance:
  - `python -c "from darkfactory.agents.manifest import RoleManifest"` works.
  - A unit test constructs a minimal valid manifest and asserts an invalid
    one (missing required field, unknown extra field) raises
    `pydantic.ValidationError`.
- Tests to run: `pytest tests/test_manifest_schema.py` (new file)

### Task 0.2 — Manifests directory + registry (`agents/registry.py`)
- Status: `[x] done codex-session`
- Depends on: 0.1
- Plan ref: §"Layer 2 — Registry".
- Scope:
  - Create empty `src/darkfactory/agents/manifests/` with a `.gitkeep`.
  - Create `src/darkfactory/agents/registry.py` exposing:
    `load_registry(manifests_dir: Path) -> Registry` and
    `Registry.get(role: str) -> RoleManifest`.
  - Validate uniqueness (duplicate role → raise).
  - Validate hook names against the set of names exported by
    `darkfactory.hooks` (unknown → raise).
  - Validate every `llm.prompt_path` exists on disk (missing → raise).
  - Registry is immutable after construction.
  - Wire registry load into `darkfactory/runtime/worker_main.py` at worker
    startup; log the loaded role count + hook-name summary; refuse to
    start on validation failure.
  - Do **not** import registry from any `@workflow.defn` body.
- Files (likely):
  - `src/darkfactory/agents/manifests/.gitkeep` (new)
  - `src/darkfactory/agents/registry.py` (new)
  - `src/darkfactory/runtime/worker_main.py`
- Acceptance:
  - Worker starts with zero manifests and logs
    `registry: 0 roles loaded`.
  - Unit test asserts duplicate role / unknown hook / missing prompt path
    each raise at load time.
- Tests to run: `pytest tests/test_manifest_schema.py tests/test_registry.py`
  (new file), `pytest tests/test_agents_workers.py`

### Task 0.3 — Composer (`agents/compose.py`)
- Status: `[x] done codex-session`
- Depends on: 0.2
- Plan ref: §"Layer 3 — Composer".
- Scope:
  - Create `src/darkfactory/agents/compose.py` exposing
    `compose(role: str, state_slice: ComposeState, *, task_id: str,
    overrides: ComposeOverrides | None = None) -> ClaudeSDKClient`.
  - `ComposeState` is a typed container with the runtime values hook
    factories close over: `slice_id`, `task_id`, `patches_sink`,
    `gate_approved`, `dependency_changes_authorized`. Document each field
    as a known seam (mentioned in the plan's risks table).
  - Implementation steps inside `compose`:
    1. Look up manifest via the registry.
    2. Apply env-var override layer (`LLM_<ROLE>_<KEY>`) — preserve the
       existing ops escape hatch.
    3. Apply in-process `overrides` (test-hermeticity path; never mutate
       `os.environ`).
    4. Materialize hook instances; inject per-task state into closures.
    5. Mount per-task MCP servers via the existing
       `build_mcp_server(task_id)`.
    6. Call `build_options(role, ...)` internally for env-merge +
       path-guard wiring.
    7. Stamp `darkfactory.manifest_sha` and `darkfactory.prompt_sha` on
       the active OTel span.
  - Composer must only be called inside an activity span; CI lint in 0.5
    enforces this at module-import time. No invocation at module top-level.
- Files (likely):
  - `src/darkfactory/agents/compose.py` (new)
  - `src/darkfactory/llm_factory.py` (no behavior change; light internal
    helpers if needed)
- Acceptance:
  - Synthetic fixture manifest under `tests/fixtures/manifests/` proves
    that `compose("noop", ...)` returns a client whose
    `ClaudeAgentOptions` matches what `build_options("noop", ...)` would
    produce with the same inputs.
  - OTel span attributes include `darkfactory.manifest_sha` and
    `darkfactory.prompt_sha`.
  - No real LLM call in tests.
- Tests to run: `pytest tests/test_compose.py` (new file)

### Task 0.4 — Parity harness
- Status: `[x] done claude-opus-session`
- Depends on: 0.3
- Plan ref: §"Migration invariant" (top of "Migration plan"),
  §"Phase 0 — Scaffolding" parity bullet.
- Scope:
  - Add a test helper `tests/parity.py` with
    `assert_manifest_imperative_parity(role: str)` that:
    1. Builds the imperative `ClaudeAgentOptions` via the existing
       `make_<role>_client()` factory.
    2. Builds the manifest-derived options via `compose(role, ...)` with
       identical inputs (env, state_slice).
    3. Asserts deep equality of fields enumerated in the plan's
       Layer 1 manifest contract (model, temperature, thinking,
       prompt, allowed/disallowed tools, MCP server list, hook tuple
       names + parameters).
  - Add a parametrized test stub
    `tests/test_manifest_parity.py::test_parity[role]` that iterates the
    set of migrated roles. Initially the set is empty (skips with reason)
    — Phase 1+ tasks add their role to the set as their final step.
  - Document in the helper's docstring that this harness is deleted in
    Task 6.4.
- Files (likely):
  - `tests/parity.py` (new)
  - `tests/test_manifest_parity.py` (new)
- Acceptance:
  - `pytest tests/test_manifest_parity.py` runs and skips with reason
    "no roles migrated yet".
- Tests to run: `pytest tests/test_manifest_parity.py tests/test_compose.py`

### Task 0.5 — CI lint (manifest validation + import guards)
- Status: `[x] done claude-opus-session`
- Depends on: 0.2
- Plan ref: §"Phase 0 — Scaffolding" CI-check bullet, §"Risks and
  mitigations" rows 1 & 2.
- Scope:
  - Add a CI step (script under `scripts/` or pytest collect-only hook)
    that:
    1. Loads every manifest under `agents/manifests/` against the
       schema.
    2. Asserts every hook name referenced exists in `darkfactory.hooks`.
    3. Asserts every `llm.prompt_path` exists.
    4. Greps `src/darkfactory/runtime/workflow.py`,
       `runtime/issue_workflow.py`, and `runtime/issue_poll_workflow.py`
       (or wherever `@workflow.defn` lives) for imports of
       `darkfactory.agents.registry` and
       `darkfactory.agents.compose` — fails on hit.
    5. Greps `src/darkfactory/agents/` for top-level `compose(...)`
       calls — fails on hit.
  - Wire into `pyproject.toml`'s pytest collect or a pre-commit-style
    check. Match the rest of the project's CI conventions.
- Files (likely):
  - `scripts/lint_manifests.py` (new) OR `tests/test_manifest_lint.py`
    (new)
  - `pyproject.toml` (entry point or marker)
- Acceptance:
  - The lint fails when a deliberately broken manifest is dropped in.
  - The lint fails if a workflow module imports the registry.
  - The lint passes on the current tree.
- Tests to run: `pytest tests/test_manifest_lint.py` (new), full suite
  via `pytest tests/ -x -q`

### Task 0.6 — `darkfactory roles list` introspection CLI
- Status: `[x] done claude-opus-session`
- Depends on: 0.2
- Plan ref: §"Phase 0 — Scaffolding" exit-criteria bullet 2,
  §"Verification" bullet 4.
- Scope:
  - Add `darkfactory roles list` to the CLI (`darkfactory/cli.py` or
    wherever subcommands are registered).
  - For each manifest the registry knows, print: role name, model,
    prompt path, allowed-tool count, hook names, MCP server names,
    manifest sha, prompt sha.
  - With zero manifests, prints
    `0 roles registered (migration not started)` and exits 0.
  - Must not require a running Temporal/worker stack — pure local read.
- Files (likely):
  - `src/darkfactory/cli.py`
  - `src/darkfactory/agents/registry.py` (small surface for listing)
- Acceptance:
  - `uv run darkfactory roles list` runs against the current tree and
    prints the zero-roles message.
  - After Task 1.1 lands, the same command lists `architect`.
- Tests to run: `pytest tests/test_cli.py` (extend existing file or
  create), `uv run darkfactory roles list` manually

---

## Phase 1 — Architect & Plan Critic

Plan ref: §"Phase 1 — Architect & Plan Critic".
Cheapest, lowest-risk roles. No tools, no MCP, no stateful hooks. These
two tasks prove the schema and the composer/parity loop end-to-end.

### Task 1.1 — Migrate Architect
- Status: `[x] done claude-opus-session`
- Depends on: 0.3, 0.4, 0.5
- Plan ref: §"Phase 1 — Architect & Plan Critic".
- Scope:
  - Author `src/darkfactory/agents/manifests/architect.yaml` matching
    the current behavior of `agents/architect.py` (model, temperature,
    thinking, prompt_path, hooks).
  - Slim `agents/architect.py` to a thin `run_architect(state)` that
    calls `compose("architect", ...)` and shapes inputs/outputs.
  - Keep `make_architect_client()` if it exists (imperative path stays
    until Phase 6).
  - Register `architect` in the parity test's role set.
- Files (likely):
  - `src/darkfactory/agents/manifests/architect.yaml` (new)
  - `src/darkfactory/agents/architect.py`
  - `tests/test_manifest_parity.py` (add `architect` to the role set)
- Acceptance:
  - Parity test green for `architect`.
  - `uv run darkfactory roles list` includes `architect`.
  - End-to-end local-stack run completes the planning loop unchanged.
  - Langfuse trace shows `manifest_sha`/`prompt_sha` on the architect
    agent span.
- Tests to run: `pytest tests/test_manifest_parity.py
  tests/test_build_subgraph.py tests/test_agents_workers.py`

### Task 1.2 — Migrate Plan Critic
- Status: `[x] done claude-opus-session`
- Depends on: 0.3, 0.4, 0.5
- Plan ref: §"Phase 1 — Architect & Plan Critic".
- Scope:
  - Author `src/darkfactory/agents/manifests/plan_critic.yaml`.
  - Slim `agents/plan_critic.py` to `run_plan_critic(state)`.
  - Keep imperative factory until Phase 6.
  - Register `plan_critic` in the parity test's role set.
- Files (likely):
  - `src/darkfactory/agents/manifests/plan_critic.yaml` (new)
  - `src/darkfactory/agents/plan_critic.py`
  - `tests/test_manifest_parity.py`
- Acceptance:
  - Parity test green for `plan_critic`.
  - Planning loop's reject/accept behavior unchanged on demo fixtures.
- Tests to run: `pytest tests/test_manifest_parity.py
  tests/test_build_subgraph.py tests/test_agents_workers.py`

---

## Phase 2 — PR Creator (first stateful role)

Plan ref: §"Phase 2 — PR Creator".
First role with a stateful closure (`gate_approved`), argv allowlist, and
role-owned prefixes (`gh pr create`, `gh pr list`, `git push`). Proves the
stateful-composer path before generalizing.

### Task 2.1 — PR Creator manifest with role-owned prefixes
- Status: `[x] done claude-opus-session`
- Depends on: 1.1
- Plan ref: §"Phase 2 — PR Creator".
- Scope:
  - Author `src/darkfactory/agents/manifests/pr_creator.yaml` declaring
    `tools.argv_allowlist`, `tools.role_owned_argv_prefixes` (`gh pr
    create`, `gh pr list`, `git push`), and the `gate_approved`
    state-closure requirement.
  - Manifest only — do not yet wire `permission_gate` to consume it.
- Files (likely):
  - `src/darkfactory/agents/manifests/pr_creator.yaml` (new)
- Acceptance:
  - Manifest validates under CI lint.
  - `uv run darkfactory roles list` shows `pr_creator` with the
    role-owned prefixes.
  - Imperative path still in use; behavior unchanged.
- Tests to run: `pytest tests/test_manifest_lint.py tests/test_cli.py`

### Task 2.2 — `permission_gate` reads PR Creator policy from the registry
- Status: `[x] done claude-opus-session`
- Depends on: 2.1
- Plan ref: §"Phase 2 — PR Creator", §"What stays in code (defense in
  depth)".
- Scope:
  - Extend `hooks/permission_gate.py` so that, for `pr_creator`, it
    derives role-owned argv prefixes from
    `registry.get("pr_creator").tools.role_owned_argv_prefixes`. Other
    roles keep the hardcoded path (this task does not generalize).
  - Aggregation is `union(code-declared invariants, manifest-declared
    role policies)`. `DENIED_ARGV_PREFIXES`, `MERGE_TOOLS`,
    `FORBIDDEN_TOKENS` stay code-declared and unremovable — verify the
    manifest cannot widen them (negative test required).
- Files (likely):
  - `src/darkfactory/hooks/permission_gate.py`
  - `tests/test_permission_gate.py`
- Acceptance:
  - Negative test: a non-PR-Creator role attempting `git push` or `gh
    pr create` is denied.
  - Negative test: a `pr_creator` manifest that tries to add `gh pr
    merge` to its allowlist is rejected at registry-load time.
  - `gh pr merge` denied for every role.
- Tests to run: `pytest tests/test_permission_gate.py
  tests/test_registry.py`

### Task 2.3 — Slim down `agents/pr_creator.py`
- Status: `[x] done claude-opus-session`
- Depends on: 2.2
- Plan ref: §"Phase 2 — PR Creator", §"Layer 4 — Slim role modules".
- Scope:
  - Reduce `agents/pr_creator.py` to a thin `run_pr_creator(state)` that
    calls `compose("pr_creator", state_slice=..., task_id=...)`.
  - Pass `gate_approved` through `state_slice`.
  - Register `pr_creator` in the parity test's role set.
- Files (likely):
  - `src/darkfactory/agents/pr_creator.py`
  - `tests/test_manifest_parity.py`
- Acceptance:
  - Parity test green for `pr_creator`.
  - End-to-end demo fixtures (`tests/fixtures/demo/happy-path`,
    `retry-induced`, `exhausted-retries`) all produce PRs.
- Tests to run: `pytest tests/test_manifest_parity.py
  tests/test_pr_creator.py tests/test_workflow_*.py`

---

## Phase 3 — Read-mostly roles

Plan ref: §"Phase 3 — Read-mostly roles (PO, Triage, Verifier-Semantic,
Reviewer)".
Each of the four tasks below is independent once 2.3 is done. They can
run in any order (or in parallel sessions).

### Task 3.1 — Migrate PO
- Status: `[x] done claude-opus-session`
- Depends on: 2.3
- Plan ref: §"Phase 3 — Read-mostly roles".
- Scope:
  - Author `agents/manifests/po.yaml`.
  - Slim `agents/po.py` to `run_po(state)`.
  - Add `po` to the parity test's role set.
- Files (likely):
  - `src/darkfactory/agents/manifests/po.yaml` (new)
  - `src/darkfactory/agents/po.py`
  - `tests/test_manifest_parity.py`
- Acceptance:
  - Parity test green for `po`.
  - Workflow tests unchanged.
- Tests to run: `pytest tests/test_manifest_parity.py
  tests/test_workflow_*.py`

### Task 3.2 — Migrate Triage
- Status: `[x] done claude-opus-session`
- Depends on: 2.3
- Plan ref: §"Phase 3 — Read-mostly roles".
- Scope:
  - Author `agents/manifests/triage.yaml`.
  - Slim `agents/triage.py` to `run_triage(state)`.
  - Add `triage` to the parity test's role set.
- Files (likely):
  - `src/darkfactory/agents/manifests/triage.yaml` (new)
  - `src/darkfactory/agents/triage.py`
  - `tests/test_manifest_parity.py`
- Acceptance:
  - Parity test green for `triage`.
  - Issue-driven workflow's triage stage still labels issues through
    `df:triaging` → `df:designing` → `df:awaiting-approval`.
- Tests to run: `pytest tests/test_manifest_parity.py
  tests/test_workflow_*.py tests/test_builder_supervisor.py`

### Task 3.3 — Migrate Verifier-Semantic
- Status: `[x] done claude-opus-session`
- Depends on: 2.3
- Plan ref: §"Phase 3 — Read-mostly roles".
- Scope:
  - Author `agents/manifests/verifier_semantic.yaml`.
  - Slim `agents/verifier_semantic.py` to `run_verifier_semantic(state)`.
  - Add `verifier_semantic` to the parity test's role set.
- Files (likely):
  - `src/darkfactory/agents/manifests/verifier_semantic.yaml` (new)
  - `src/darkfactory/agents/verifier_semantic.py`
  - `tests/test_manifest_parity.py`
- Acceptance:
  - Parity test green for `verifier_semantic`.
  - Verify stage continues to produce a correct `VerifySummary` on demo
    fixtures.
- Tests to run: `pytest tests/test_manifest_parity.py
  tests/test_workflow_*.py`

### Task 3.4 — Migrate Reviewer
- Status: `[x] done claude-opus-session`
- Depends on: 2.3
- Plan ref: §"Phase 3 — Read-mostly roles".
- Scope:
  - Author `agents/manifests/reviewer.yaml`.
  - Slim `agents/reviewer.py` to `run_reviewer(state)`.
  - Add `reviewer` to the parity test's role set.
- Files (likely):
  - `src/darkfactory/agents/manifests/reviewer.yaml` (new)
  - `src/darkfactory/agents/reviewer.py`
  - `tests/test_manifest_parity.py`
- Acceptance:
  - Parity test green for `reviewer`.
  - Reviewer's output unchanged on demo fixtures.
- Tests to run: `pytest tests/test_manifest_parity.py
  tests/test_workflow_*.py`

---

## Phase 4 — Tester and Builder

Plan ref: §"Phase 4 — Tester and Builder".
The diff-capture + path-guard + MCP combination. Migrate in order so that
shared patterns surface and get factored into the manifest schema (not
into code) when Tester lands.

### Task 4.1 — Migrate Builder
- Status: `[x] done claude-opus-session`
- Depends on: 3.1, 3.2, 3.3, 3.4
- Plan ref: §"Phase 4 — Tester and Builder".
- Scope:
  - Author `agents/manifests/builder.yaml` covering: model knobs,
    `BUILDER_ALLOWLIST` (moved into manifest), MCP servers, hooks
    (`diff_capture`, `path_guard`, `call_cap`, `loop_breaker`,
    `goal_pin`), `dependency_changes_authorized` state slot.
  - Slim `agents/builder.py` to `run_builder(state)`.
  - Delete the `BUILDER_ALLOWLIST` constant from `agents/builder.py`.
  - Add `builder` to the parity test's role set.
- Files (likely):
  - `src/darkfactory/agents/manifests/builder.yaml` (new)
  - `src/darkfactory/agents/builder.py`
  - `tests/test_manifest_parity.py`
- Acceptance:
  - Parity test green for `builder`.
  - End-to-end run on the demo Java fixtures (`happy-path`) produces a
    successful build + tests + PR.
  - `tests/test_agents_workers.py` green; diff-capture sink populated
    correctly per task.
- Tests to run: `pytest tests/test_manifest_parity.py
  tests/test_agents_workers.py tests/test_build_subgraph.py
  tests/test_workflow_*.py`

### Task 4.2 — Migrate Tester
- Status: `[x] done claude-opus-session`
- Depends on: 4.1
- Plan ref: §"Phase 4 — Tester and Builder".
- Scope:
  - Author `agents/manifests/tester.yaml`.
  - Slim `agents/tester.py` to `run_tester(state)`.
  - Delete the `TESTER_ALLOWLIST` constant from `agents/tester.py`.
  - Add `tester` to the parity test's role set.
  - If 4.1 surfaced shared harness shape with Builder, refactor the
    manifest schema (Task 0.1's model) — do not copy-paste structure
    between manifests. Acceptable scope creep: schema additions; **not**
    acceptable: behavior change in other roles.
- Files (likely):
  - `src/darkfactory/agents/manifests/tester.yaml` (new)
  - `src/darkfactory/agents/tester.py`
  - `src/darkfactory/agents/manifest.py` (only if shared-shape refactor
    is needed)
  - `tests/test_manifest_parity.py`
- Acceptance:
  - Parity test green for `tester`.
  - End-to-end demo run still produces a green build + PR.
- Tests to run: `pytest tests/test_manifest_parity.py
  tests/test_agents_workers.py tests/test_build_subgraph.py
  tests/test_workflow_*.py`

---

## Phase 5 — Fixer (last)

Plan ref: §"Phase 5 — Fixer (last)".
Heaviest closure logic: computed justification, target-WP derivation from
`verify_summary.predicate_coverage`. Migrated last so the composer's
state-injection contract is proven on simpler roles first.

### Task 5.1 — Migrate Fixer
- Status: `[x] done claude-opus-session`
- Depends on: 4.1, 4.2
- Plan ref: §"Phase 5 — Fixer (last)".
- Scope:
  - Author `agents/manifests/fixer.yaml`.
  - Slim `agents/fixer.py` to `run_fixer(state)`.
  - Delete the `FIXER_ALLOWLIST` constant.
  - Decide: does `_patch_justification(...)` + target-WP derivation move
    to a fixer-local helper, or into the composer's state-injection
    contract? Default: keep it fixer-local unless the same pattern was
    already needed for Builder/Tester in Phase 4 — only generalize on
    second use.
  - Add `fixer` to the parity test's role set.
- Files (likely):
  - `src/darkfactory/agents/manifests/fixer.yaml` (new)
  - `src/darkfactory/agents/fixer.py`
  - `src/darkfactory/agents/compose.py` (only if state-injection
    contract is extended)
  - `tests/test_manifest_parity.py`
- Acceptance:
  - Parity test green for `fixer`.
  - Fixer loop on `retry-induced` demo fixture converges within
    `FIXER_MAX_ATTEMPTS=2` per WP/predicate.
  - Fixer budget exhaustion on `exhausted-retries` demo fixture produces
    `needs_human` with the expected reason.
- Tests to run: `pytest tests/test_manifest_parity.py
  tests/test_workflow_*.py tests/test_agents_workers.py`

---

## Phase 6 — Cleanup (point of no return)

Plan ref: §"Phase 6 — Cleanup", §"Rollback strategy".
Verify the full demo matrix end-to-end before merging any Phase 6 task.
Post-Phase-6, rollback is "re-introduce from git history", not
"un-migrate manifests."

### Task 6.1 — Delete `_ROLE_DEFAULTS`; narrow `build_options`
- Status: `[x] done claude-opus-session`
- Depends on: 5.1
- Plan ref: §"Phase 6 — Cleanup" bullet 1.
- Scope:
  - Delete `_ROLE_DEFAULTS` from `src/darkfactory/llm_factory.py`.
  - Narrow `build_options(...)` to its env-merge + path-guard role —
    no more role-defaults lookup.
  - Callers must now go through `compose(...)` exclusively.
- Files (likely):
  - `src/darkfactory/llm_factory.py`
- Acceptance:
  - `grep -r "_ROLE_DEFAULTS" src/` returns nothing.
  - Full test suite green.
- Tests to run: `pytest tests/ -x -q`

### Task 6.2 — Delete `ROLE_OWNED_ARGV_PREFIXES` table from `permission_gate.py`
- Status: `[x] done claude-opus-session`
- Depends on: 5.1
- Plan ref: §"Phase 6 — Cleanup" bullet 2.
- Scope:
  - Delete the hardcoded `ROLE_OWNED_ARGV_PREFIXES` mapping from
    `hooks/permission_gate.py`. All role-owned-prefix lookups now go
    through the registry.
  - Code-declared invariants (`DENIED_ARGV_PREFIXES`, `MERGE_TOOLS`,
    `FORBIDDEN_TOKENS`, secret tables) stay.
- Files (likely):
  - `src/darkfactory/hooks/permission_gate.py`
- Acceptance:
  - Negative permission-gate tests from Task 2.2 still green.
  - `gh pr merge` denied for every role; non-PR-Creator roles still
    denied `git push`.
- Tests to run: `pytest tests/test_permission_gate.py tests/ -x -q`

### Task 6.3 — Delete `make_<role>_client()` imperative factories
- Status: `[x] done claude-opus-session (bundled with 6.4 — parity harness deleted in the same session because the harness imports the factories at module load)`
- Depends on: 5.1
- Plan ref: §"Phase 6 — Cleanup" bullet 3.
- Scope:
  - Delete each `make_<role>_client()` function across `agents/<role>.py`
    modules.
  - Update any straggling callers to use `compose(...)`.
- Files (likely):
  - All `src/darkfactory/agents/<role>.py` modules.
- Acceptance:
  - `grep -rE "def make_.*_client" src/` returns nothing.
  - Full test suite green.
- Tests to run: `pytest tests/ -x -q`

### Task 6.4 — Delete parity assertion harness
- Status: `[x] done claude-opus-session (bundled into 6.3 — parity.py imported the make_<role>_client factories at module load, so deleting them required deleting the harness in the same session to keep the full test suite green)`
- Depends on: 6.3
- Plan ref: §"Phase 6 — Cleanup" bullet 4.
- Scope:
  - Delete `tests/parity.py` and `tests/test_manifest_parity.py`. The
    parity test exists only to compare two paths; with the imperative
    path gone (6.3), it compares nothing.
- Files (likely):
  - `tests/parity.py` (delete)
  - `tests/test_manifest_parity.py` (delete)
- Acceptance:
  - `grep -r "assert_manifest_imperative_parity" tests/` returns nothing.
  - Full test suite green.
- Tests to run: `pytest tests/ -x -q`

### Task 6.5 — `spec_adjustment` shim audit
- Status: `[x] done claude-opus-session`
- Audit report (2026-05-11):
  - `spec_adjustment` / `spec_adjustment_stage`: zero references in `src/`
    or `tests/`. Already fully removed by prior phases. Nothing to delete.
  - `SpecSlice`: defined as a `WorkPackageDict` alias in `state.py:158`
    but **never imported anywhere**. Deleted. (Single line removal; no
    callers to update.)
  - `affected_files`: load-bearing legacy field on the durable
    `WorkPackageDict` shape. Live readers/writers: `state.py`
    (`work_package_from_dict`, `work_package_dict_from_model`),
    `agents/architect.py` (`WorkPackagePlanModel`),
    `agents/builder_supervisor.py`, `runtime/phase_comment.py`. Live
    fixtures across ~10 test files. **Kept** — the v1→v2 tracker owns
    the decision to retire this alongside `candidate_files`/`repo_areas`.
  - `VERIFY_RETRY_CAP`: defined in `runtime/workflow.py:24` as a
    compatibility alias (`FIXER_MAX_ATTEMPTS + 1`). Imported and asserted
    by `tests/test_verify_retry_cap.py`. **Kept** — has a live test
    caller; CLAUDE.md documents it as intentional.
- Depends on: 5.1
- Plan ref: §"Phase 6 — Cleanup" bullet 5 (legacy compatibility shims).
- Scope:
  - Grep for remaining references to `spec_adjustment`,
    `spec_adjustment_stage`, `SpecSlice`, `affected_files`,
    `VERIFY_RETRY_CAP`.
  - For each: confirm it is reachable from the live code path. Delete if
    unreachable; document if still load-bearing.
  - This task is bounded: do not delete shims that have live callers
    just because Phase 6 is happening. The v1→v2 tracker owns those
    decisions.
- Files (likely):
  - `src/darkfactory/runtime/activities.py`
  - `src/darkfactory/state.py`
  - `tests/` (if test-only shims surface)
- Acceptance:
  - A short report (in the PR description) of what was reachable, what
    was deleted, what stays and why.
  - Full test suite green.
- Tests to run: `pytest tests/ -x -q`

### Task 6.6 — Final verification matrix
- Status: `[~] in-progress claude-opus-session`
- Depends on: 6.1, 6.2, 6.3, 6.4, 6.5
- Plan ref: §"Verification", §"Phase 6 — Cleanup" exit criteria.
- Scope:
  - Run the full demo fixture matrix end-to-end:
    `uv run darkfactory run --repo tests/fixtures/demo/happy-path`,
    `retry-induced`, `exhausted-retries`. All three must reach their
    expected terminal state.
  - Inspect Langfuse: one trace per workflow execution; every agent
    span carries `manifest_sha` and `prompt_sha`.
  - Edit one manifest knob (e.g. bump `call_cap` for Builder) and
    confirm the OTel trace shows a different `manifest_sha`. Revert.
  - Run `grep -r "_ROLE_DEFAULTS\|make_.*_client\|<ROLE>_ALLOWLIST"
    src/` — must be empty.
  - Run `uv run darkfactory roles list` — every role's full harness
    prints from the registry.
- Files (likely): —
- Acceptance:
  - All checks above pass.
  - Tracker is updated with a short closing note under "Follow-ups
    discovered during implementation" capturing anything noticed during
    verification.
- Tests to run: `pytest tests/ -x -q` plus the manual matrix above.

---

## Dependency graph (text form)

```
0.1 ──► 0.2 ──► 0.3 ──► 0.4 ──► 1.1 ──► 2.1 ──► 2.2 ──► 2.3 ──┬─► 3.1 ─┐
        │       │                                              ├─► 3.2 ─┤
        │       │                                              ├─► 3.3 ─┤
        │       │                                              └─► 3.4 ─┤
        │       │                                                       │
        │       └────────► 1.2 (parallel with 1.1)                      │
        │                                                               │
        ├──► 0.5  (lint; gates Phase 1)                                 │
        └──► 0.6  (CLI list; auxiliary)                                 │
                                                                        ▼
                                                                       4.1
                                                                        │
                                                                        ▼
                                                                       4.2
                                                                        │
                                                                        ▼
                                                                       5.1
                                                                        │
                                                            ┌───────────┼───────────┬───────────┐
                                                            ▼           ▼           ▼           ▼
                                                           6.1         6.2         6.3         6.5
                                                                                    │
                                                                                    ▼
                                                                                   6.4
                                                                                    │
                                                                                    ▼
                                                                                   6.6 (after 6.1, 6.2, 6.5 too)
```

Notes on the graph:
- 1.1 and 1.2 are independent of each other; both gate Phase 2 (2.1
  depends on 1.1 only, but in practice land both before opening 2.1 to
  catch schema gaps on two roles).
- 3.1–3.4 are mutually independent and can run in parallel sessions.
- 4.1 must precede 4.2 so any shared harness shape is factored into the
  schema once, not duplicated.
- 6.1, 6.2, 6.3, 6.5 are mutually independent; 6.4 must follow 6.3; 6.6
  must follow all of 6.1–6.5.

---

## Follow-ups discovered during implementation

_(Add new tasks here rather than widening an in-flight task.)_

### FU-2 — Triage env-var compatibility note
- Status: `[ ] open`
- Discovered during: Task 3.2.
- Context: pre-migration `agents/triage.py` honored
  `LLM_TRIAGE_MAX_TOKENS` and `LLM_TRIAGE_SDK_MAX_RETRIES` env vars
  because it called the Anthropic SDK directly. The migrated path uses
  the Claude Agent SDK via `compose("triage", ...)`, which does not
  expose either knob — those env vars are now silently ignored. The
  Temporal-level retry policy in `runtime/issue_workflow.py` still
  applies (5 attempts). If any operator deployment relies on those
  env vars, document the change in release notes or restore an
  equivalent knob.

### FU-3 — `justification_template` is the shared diff_capture seam
- Status: `[ ] open`
- Discovered during: Task 4.1.
- Context: To preserve runtime parity for the Builder's diff_capture
  ``justification`` text (today computed as
  ``f"WP {slice_id}: {intent}" if intent else f"WP {slice_id}"``), Task 4.1
  added two coupled pieces: a ``slice_intent`` field on
  ``ComposeState`` (populated from ``state["spec"]`` + ``current_slice``
  in ``ComposeState.from_mapping``), and a ``justification_template``
  parameter on diff_capture hook attachments that ``compose`` interpolates
  via ``_format_justification`` (strips a trailing colon when intent is
  empty). The Builder manifest uses
  ``"WP {slice_id}: {slice_intent}"``.
- Why this matters: Tester's imperative justification today is
  ``f"WP {slice_id} tests: {intent}" if intent else f"WP {slice_id} tests"``,
  which fits the same template shape with a different prefix. Task 4.2 can
  reuse the seam verbatim by declaring
  ``justification_template: "WP {slice_id} tests: {slice_intent}"`` in
  ``tester.yaml`` — **no schema change needed**. If 4.2 surfaces a third
  pattern (e.g., Fixer in 5.1) that doesn't fit ``{slice_id}/{slice_intent}``
  format-string interpolation, that is the moment to refactor the manifest
  schema (per the Phase 4 plan note about acceptable scope creep).
- Suggested scope: documentation-only — re-read this note when starting
  Task 4.2 to avoid re-inventing the seam.

### FU-4 — `ComposeState.patch_justification` is the Fixer-shape diff_capture seam
- Status: `[ ] open`
- Discovered during: Task 5.1.
- Context: The Fixer's per-edit justification (today computed by
  ``_patch_justification(state_slice)`` from ``verify_summary.predicate_coverage``
  + ``tester_findings`` + ``current_slice``) does not fit the ``{slice_id}/
  {slice_intent}`` template that Builder/Tester use (FU-3). Rather than
  refactor the manifest schema, Task 5.1 added a ``patch_justification: str``
  field to ``ComposeState`` and extended ``_format_justification`` to
  interpolate ``{patch_justification}`` alongside the existing placeholders.
  ``run_fixer`` precomputes the string and assigns it onto the
  ``ComposeState`` instance before calling ``compose``. The Fixer manifest
  declares ``justification_template: "{patch_justification}"``.
- Why this matters: This is the second extension of the composer's
  state-injection contract for diff_capture (the first was ``slice_intent``
  in FU-3). If a fourth role surfaces a justification pattern that doesn't
  fit ``{slice_id}/{slice_intent}/{patch_justification}`` interpolation, that
  is the moment to step back and rework the schema rather than keep
  bolting on per-role fields.
- Suggested scope: documentation-only — re-read this note when migrating any
  future role that captures diffs to decide whether to reuse the existing
  seams or rework them.

### FU-5 — Dead helpers left in the agent modules after Task 6.1
- Status: `[x] done claude-opus-session (resolved during Task 6.3 — ALLOWED_TOOLS constants deleted from builder/tester/fixer/pr_creator; _slice_intent deleted from tester. PR_CREATOR_ALLOWLIST kept since tests/test_tools_shell_allowlist.py and tests/test_agents_workers.py still import it as a code-declared invariant alongside the manifest.)`
- Discovered during: Task 6.1.
- Context: Converting each ``make_<role>_client`` to delegate to ``compose``
  orphaned a handful of module-level constants and private helpers that the
  imperative path was the only caller of:
  - ``agents/builder.py``: ``ALLOWED_TOOLS`` (also unused by ``run_builder``).
  - ``agents/tester.py``: ``ALLOWED_TOOLS`` and ``_slice_intent``.
  - ``agents/fixer.py``: ``ALLOWED_TOOLS``.
  - ``agents/pr_creator.py``: ``ALLOWED_TOOLS``. ``PR_CREATOR_ALLOWLIST`` is
    still imported by ``tests/test_tools_shell_allowlist.py`` and
    ``tests/test_agents_workers.py``; treat it as live until those tests are
    rewritten against the manifest.
- Why this matters: Task 6.3 already plans to delete the imperative shims;
  the orphaned constants/helpers should fall out of the same edit. Calling
  them out so the 6.3 implementer prunes them in the same pass rather than
  leaving dead module-level state behind.
- Suggested scope: bundle into Task 6.3.

### FU-6 — Hook structure normalization between compose and tests
- Status: `[ ] open`
- Discovered during: Task 6.1.
- Context: ``compose`` emits ``hooks[event] = [HookMatcher(hooks=[a]),
  HookMatcher(hooks=[b]), …]`` — one matcher per manifest attachment — while
  the pre-6.1 imperative path bundled them into ``[HookMatcher(hooks=[a, b,
  c])]``. Functionally equivalent under the Claude Agent SDK (all matchers
  on an event fire), but a handful of unit tests asserted the bundled shape
  via ``opts.hooks[event][0].hooks``. Task 6.1 flattened those assertions
  across all matchers (``test_agents_workers.py``,
  ``test_discovery_agents.py``, ``test_fixer_agent.py``).
- Why this matters: If a future role ever needs per-matcher ``matcher`` regex
  selectivity, the per-attachment shape lets each attachment carry its own
  regex without re-bundling. If the bundled shape is desirable for some other
  reason (e.g. timeout coupling), it would need to be a composer-level
  policy, not a manifest knob.
- Suggested scope: documentation-only — re-read this note if a future task
  reaches for ``HookMatcher.matcher`` or ``HookMatcher.timeout`` per role.

### FU-7 — Default-registry cleanup leak surfaced by 6.1
- Status: `[ ] open`
- Discovered during: Task 6.1.
- Context: ``tests/test_registry.py::test_worker_registry_startup_logs_empty_registry``
  calls ``_load_manifest_registry(tmp_path)`` which sets the global
  ``_DEFAULT_REGISTRY`` to an empty registry and never restored it. Pre-6.1
  this was harmless because imperative ``make_<role>_client`` never consulted
  the default registry. Post-6.1 every ``make_<role>_client`` goes through
  ``compose`` → ``get_default_registry()``; the leak now breaks downstream
  agent tests. Task 6.1 added an autouse fixture in ``test_registry.py``
  that snapshots/restores ``_DEFAULT_REGISTRY``.
- Why this matters: The fix is local but the deeper symptom is that the
  registry exposes a single global mutable cache without a public reset
  hook. Tests that exercise worker startup are the natural canary; if more
  appear (e.g. integration tests that boot a real worker registry), the
  autouse fixture pattern should move into a shared conftest.
- Suggested scope: documentation-only; if a second test file needs the
  same fixture, lift it into ``tests/conftest.py``.

### FU-1 — Thread manifest `role_owned_argv_prefixes` through `compose`
- Status: `[x] done claude-opus-session (bundled into Task 6.2)`
- Discovered during: Task 2.3.
- Context: `agents/compose.py:_materialize_permission_gate` calls
  `make_permission_gate(role, allowlist, gate_approved=...)` but does not
  forward `manifest.tools.role_owned_argv_prefixes`. The imperative
  `make_pr_creator_client` already passes the registry-derived prefixes.
  Today this works at runtime only because the code-declared
  `ROLE_OWNED_ARGV_PREFIXES` table in `hooks/permission_gate.py` covers
  pr_creator's three prefixes. The parity test compares hook signatures
  by `__name__` only, so the latent divergence is not surfaced.
- Why this matters: Task 6.2 deletes the hardcoded
  `ROLE_OWNED_ARGV_PREFIXES` table. Before (or during) that task, compose
  must thread `manifest.tools.role_owned_argv_prefixes` into
  `make_permission_gate`, or pr_creator (and any future role with
  role-owned prefixes) will lose the prefix-allowance pathway. Bundling
  it into 6.2 is reasonable; calling it out here so it is not lost.
- Suggested scope: extend `_materialize_permission_gate` to forward
  `manifest.tools.role_owned_argv_prefixes`; optionally extend the parity
  helper to compare the captured prefix set so the divergence cannot
  silently recur.
- Resolution: bundled into Task 6.2. Implementation went beyond the
  narrow suggestion — instead of forwarding only the *current role's*
  prefixes, the composer now feeds `make_permission_gate` the full
  registry-derived prefix→allowed-roles mapping via the new
  `Registry.role_owned_argv_table()` helper. This was necessary because
  cross-role denial (e.g. builder denied `git push`) requires the gate
  to know which prefixes other roles own, not just its own. The
  `make_permission_gate` signature changed from
  `Iterable[Iterable[str]]` to `Mapping[Sequence[str], Iterable[str]]`.

### FU-8 — `make_permission_gate` role-owned prefix API shape
- Status: `[ ] open`
- Discovered during: Task 6.2.
- Context: Task 6.2 changed `make_permission_gate`'s
  `role_owned_argv_prefixes` parameter from
  `Iterable[Iterable[str]]` ("prefixes the calling role owns; merged with
  a hardcoded global table") to
  `Mapping[Sequence[str], Iterable[str]]`
  ("the full registry-derived prefix→allowed-roles table"). The
  composer builds this mapping via the new
  `Registry.role_owned_argv_table()` helper. Tests that previously
  exercised the per-role list shape were rewritten to construct the
  mapping shape directly (see
  `tests/test_permission_gate.py::test_manifest_prefixes_grant_current_role_on_existing_entries`,
  `::test_manifest_prefixes_isolated_per_gate`,
  `::test_manifest_prefixes_cannot_bypass_denied_argv_prefixes`, and
  `tests/test_tools_shell_allowlist.py::test_role_command_policy_for_push_and_pr_create`).
- Why this matters: When Task 6.3 deletes `make_pr_creator_client`,
  ensure callers that construct gates outside the composer (notably
  test helpers) keep building the table from
  `Registry.role_owned_argv_table()` rather than re-introducing a
  list-of-prefixes shape. The current closure copies values to
  `frozenset` defensively but is not designed for the older shape.
- Suggested scope: documentation-only; consult this note if a future
  task touches `make_permission_gate`'s signature again.
