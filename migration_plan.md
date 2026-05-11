 Migration Plan — Consolidated Agent Harness

 Goal

 Make every agent role's complete harness fit on one screen, in one file, in one format.

 Concretely: every per-role concern that today is scattered across llm_factory.py, prompts/<role>.md, agents/<role>.py, hooks/permission_gate.py, and hooks/path_guard.py is consolidated into a single declarative manifest per role (agents/manifests/<role>.yaml) plus the markdown prompt sidecar. A small composer materializes those manifests into a runnable Claude SDK client at activity invocation time. Defense-in-depth invariants (merge bans, secret-path tables, shell-metachar denylists) stay in code as code-declared rules that no manifest can widen.

 Why this is the goal

 1. Reviewability — today a reviewer needs to open ~6 files to audit one role's capability surface. After this work, the answer is one YAML + one markdown.
 2. Safe additions — new roles get added by writing a manifest, not by touching five subsystems. Onboarding cost for new agents drops to near zero.
 3. Audit trail — hashing the manifest + prompt as OTel span attributes turns harness changes into a measurable, queryable property of every workflow execution. Replays that observe a different hash than the original execution become a detectable determinism violation.
 4. Anti-drift — Fowler's harness-engineering essay warns that scattered feedforward/feedback controls become a maintenance liability. A singledeclarative surface tightens that loop.
 5. Industry direction — Anthropic's harness-design article, Addy Osmani'sanatomy, and the A2A agent-card protocol all converge on declarative per-role manifests as the highest-leverage configuration surface in modern agent systems.

 Out of scope

 - No changes to the Temporal workflow state machine, brief gate, merge gate, orfixer-budget logic.
 - No changes to LangGraph subgraphs in stages/build.py, stages/verify.py,stages/discovery.py.
 - No changes to the per-workflow worker container lifecycle or thesupervisor/agent task-queue split.
 - No changes to MCP server implementation or RepoSandbox.
 - No changes to hook behaviors — only how hooks are attached and parameterized.
 - This refactor does not subsume the active v1→v2 migration. Both touch agentmodules; we coordinate to avoid two open refactors on the same role at once.

 ---
 Where the sprawl lives today

 ┌───────────────────────────────┬────────────────────────────────────────────┐
 │            Concern            │             Where it lives now             │
 ├───────────────────────────────┼────────────────────────────────────────────┤
 │ Model / temperature /         │ llm_factory.py _ROLE_DEFAULTS              │
 │ thinking                      │                                            │
 ├───────────────────────────────┼────────────────────────────────────────────┤
 │ System prompt                 │ prompts/<role>.md                          │
 ├───────────────────────────────┼────────────────────────────────────────────┤
 │ Allowed / disallowed tools    │ each agents/<role>.py make_<role>_client() │
 ├───────────────────────────────┼────────────────────────────────────────────┤
 │ MCP servers                   │ each agent module, wired imperatively      │
 ├───────────────────────────────┼────────────────────────────────────────────┤
 │ Per-role argv allowlist       │ BUILDER_ALLOWLIST, TESTER_ALLOWLIST,       │
 │                               │ FIXER_ALLOWLIST, PR_CREATOR_ALLOWLIST      │
 ├───────────────────────────────┼────────────────────────────────────────────┤
 │ Role-owned argv prefixes      │ permission_gate.py                         │
 │                               │ ROLE_OWNED_ARGV_PREFIXES                   │
 ├───────────────────────────────┼────────────────────────────────────────────┤
 │ Global denylists (gh pr       │ permission_gate.py, tools/shell.py,        │
 │ merge, shell metachars,       │ path_guard.py                              │
 │ secret paths)                 │                                            │
 ├───────────────────────────────┼────────────────────────────────────────────┤
 │ Hook attachments + parameters │ each agent module composes its own hook    │
 │                               │ tuple                                      │
 ├───────────────────────────────┼────────────────────────────────────────────┤
 │ Hook default knobs (call cap, │                                            │
 │  loop window, goal-pin        │ inside each hook module                    │
 │ cadence)                      │                                            │
 ├───────────────────────────────┼────────────────────────────────────────────┤
 │ Invocation glue               │ runtime/activities.py, stages/*.py import  │
 │                               │ roles directly                             │
 └───────────────────────────────┴────────────────────────────────────────────┘

 ---
 Target Architecture

 Five layers

 Layer 1 — Manifests. src/darkfactory/agents/manifests/<role>.yaml, one per role, Pydantic-validated at worker startup. Fields:

 - identity: role, description, when_to_use
 - llm: model, temperature, thinking budget, prompt_path (markdown sidecar —unchanged)
 - tools: allowed, disallowed, argv allowlist, role-owned argv prefixes,edit-path allowlist
 - mcp: list of MCP server names this role mounts (server instances are stillbuilt per task)
 - hooks: declarative attachments with parameters (call_cap: {cap: 80},loop_breaker: {window: 8, min_repeats: 3}, etc.)
 - budgets: timeout, heartbeat cadence, retry caps the workflow may consult
 - io_contract: which PipelineState channels the role reads/writes (documentation today, enforceable later)

 Prompts stay as markdown sidecars referenced by path. Prompt-only diffs stayreviewable; _sdk_common.load_prompt() stays usable.

 Layer 2 — Registry. src/darkfactory/agents/registry.py. Loads every manifest once at worker startup, validates uniqueness, exposes typed lookup by role name, aggregates per-role policies into the view permission_gate consumes. Does NOT derive global denylists — those stay in code. Never consulted from@workflow.defn bodies (replay determinism).

 Layer 3 — Composer. src/darkfactory/agents/compose.py. Single function compose(role, state_slice, *, task_id, overrides=…) → ClaudeSDKClient that:

 1. Reads the role's manifest.
 2. Applies the env-var override layer (LLM_<ROLE>_<KEY> — kept as the ops escape hatch).
 3. Applies in-process overrides (test-hermeticity path; no os.environ pollution).
 4. Materializes hook instances, injecting per-task state (slice_id, task_id, patches_sink, gate_approved, dependency_changes_authorized) into hook closures.
 5. Mounts per-task MCP servers via the existing build_mcp_server(task_id).
 6. Calls build_options(...) internally — the function survives, narrowed toenv-merge + path-guard wiring.
 7. Stamps darkfactory.manifest_sha and darkfactory.prompt_sha on the active OTel span.

 The composer is stateful because four hook factories close over runtime values. compose(role) alone is insufficient — this is acknowledged in the design, nothidden.

 Layer 4 — Slim role modules. Each agents/<role>.py collapses to a thin run_<role>(state) that calls compose(...) and shapes inputs/outputs. Noimperative hook composition, no allowlist constants, no MCP wiring.

 Layer 5 — Invocation glue. runtime/activities.py and stages/*.py look up roles by name. LangGraph node names stay stable across the migration — their identityis load-bearing for tracing.

 What stays in code (defense in depth)

 Not manifest-declarable. A manifest typo (or a permissive PR) must not silently widen them:

 - DENIED_ARGV_PREFIXES (gh pr merge) — global merge ban.
 - FORBIDDEN_TOKENS in tools/shell.py — shell metachar denylist.
 - Lockfile / private-key / secret tables in hooks/path_guard.py.
 - The MERGE_TOOLS set.

 Composer-time enforcement is union(code-declared invariants, manifest-declared role policies) with code-declared rules unremovable.

 ---
 Migration plan

 Migration invariant (held throughout): build_options(role, …) keeps its current signature and observable behavior. Migrated roles route through compose;un-migrated roles keep their imperative make_<role>_client(). During the transition window, a parity assertion validates that manifest-derived optionsequal the imperative output. Drift caught before deletion.

 One open refactor per role at a time. The v1→v2 migration and this harness consolidation must not touch the same agent module simultaneously.

 Phase 0 — Scaffolding (no role migrated yet)

 Deliverables:
 - src/darkfactory/agents/manifests/ directory + Pydantic schema (manifest.py).
 - agents/registry.py: load + validate at worker startup; expose lookup; raise on duplicates or unknown hook names.
 - agents/compose.py: stateful factory taking (role, state_slice, task_id,overrides=…). Wires hooks, MCP servers, env overrides; calls build_options internally; stamps OTel attributes.
 - Parity harness: a test helper that, given a role, asserts manifest-derived options equal imperative-derived options.
 - CI check: every manifest validates against the schema; every hook name referenced exists; every prompt path exists.

 Exit criteria:
 - uv run pytest tests/ is green.
 - darkfactory roles list (new CLI introspection command) prints zero migratedroles plus the registry-load summary.
 - Worker startup logs the registry contents and refuses to start on validationfailure.

 Phase 1 — Architect & Plan Critic

 The cheapest, lowest-risk roles. No tools, no MCP, no stateful hooks. Pure declarative test of the schema.

 Deliverables:
 - manifests/architect.yaml, manifests/plan_critic.yaml.
 - agents/architect.py and the plan-critic role slim down to run_<role> only.
 - Parity test green for both roles.

 Exit criteria:
 - Full local-stack run (docker compose up -d; uv run darkfactory run "implementX" --repo …) completes the planning loop unchanged.
 - One Langfuse trace per workflow, now showing manifest_sha / prompt_sha onarchitect/critic agent spans.
 - uv run pytest tests/ green, including test_build_subgraph.py.

 Phase 2 — PR Creator

 First role with a stateful closure (gate_approved), argv allowlist, role-ownedprefixes (gh pr create, gh pr list, git push). Proves the stateful-composer path before generalizing.

 Deliverables:
 - manifests/pr_creator.yaml declaring its role-owned prefixes.
 - permission_gate.py consumes registry-derived role-owned prefixes for PRCreator; other roles keep the hardcoded path.
 - agents/pr_creator.py slim-down.

 Exit criteria:
 - PR Creator can gh pr create and git push; other roles still get rejected attempting the same (verified by a permission-gate test).
 - The end-to-end demo fixtures (tests/fixtures/demo/happy-path, retry-induced, exhausted-retries) all produce PRs.

 Phase 3 — Read-mostly roles (PO, Triage, Verifier-Semantic, Reviewer)

 Deliverables:
 - Four manifests, four module slim-downs.
 - Registry-derived role-owned prefixes table extended; manifest parity assertion still green.

 Exit criteria:
 - Workflow tests (tests/test_workflow_*.py, tests/test_builder_supervisor.py)green.
 - Issue-driven workflow's triage stage still labels issues correctly throughdf:triaging → df:designing → df:awaiting-approval.

 Phase 4 — Tester and Builder

 The diff-capture + path-guard + MCP combination. These roles share most harness shape; migrate them together so any pattern duplication surfaces and getsfactored into the manifest schema rather than into code.

 Deliverables:
 - manifests/builder.yaml, manifests/tester.yaml.
 - BUILDER_ALLOWLIST, TESTER_ALLOWLIST constants deleted from agent modules.
 - agents/builder.py, agents/tester.py slim down to run_<role>.

 Exit criteria:
 - End-to-end run on the demo Java fixtures (happy-path) produces a successful build + tests + PR.
 - tests/test_agents_workers.py green; diff-capture sink populated correctly per task.

 Phase 5 — Fixer (last)

 Heaviest closure logic — computed justification, target-WP derivation fromverify_summary.predicate_coverage. Migrated last because the composer's state-injection contract is proven on simpler roles first.

 Deliverables:
 - manifests/fixer.yaml.
 - FIXER_ALLOWLIST constant deleted.
 - agents/fixer.py slim down; _patch_justification(...) and target derivation moved either to a fixer-local helper called by run_fixer, or into the composer's state-injection contract if it's general enough.

 Exit criteria:
 - Fixer loop on retry-induced demo fixture converges within FIXER_MAX_ATTEMPTS=2 per WP/predicate.
 - Fixer budget exhaustion on exhausted-retries demo fixture produces needs_human with the expected reason.

 Phase 6 — Cleanup

 Deliverables:
 - Delete _ROLE_DEFAULTS from llm_factory.py; narrow build_options to itsenv-merge + path-guard role.
 - Delete ROLE_OWNED_ARGV_PREFIXES table from permission_gate.py (nowregistry-derived).
 - Delete make_<role>_client() imperative factories.
 - Delete the parity assertion harness (no longer two paths to compare).
 - Delete legacy compatibility shims if any role-specific ones became unreachable (spec_adjustment shim audit).

 Exit criteria:
 - grep -r "_ROLE_DEFAULTS\|make_.*_client\|<ROLE>_ALLOWLIST" src/ returnsnothing meaningful.
 - darkfactory roles list prints every role's full harness, derived entirely from manifests.
 - Full test suite + end-to-end demo runs green.

 ---
 Risks and mitigations

 ┌──────────────────────────────┬────────────────────────────────────────────┐
 │             Risk             │                 Mitigation                 │
 ├──────────────────────────────┼────────────────────────────────────────────┤
 │ Manifests read inside        │ Registry is immutable after worker         │
 │ @workflow.defn → replay      │ startup; consulted only inside activities. │
 │ non-determinism              │  CI lint forbids registry import in        │
 │                              │ workflow modules.                          │
 ├──────────────────────────────┼────────────────────────────────────────────┤
 │ Composer runs at             │ Composer is a function called inside the   │
 │ module-import time → wrong   │ activity span, never at import. Lint check │
 │ OTel span context            │  on top-level compose(...) calls.          │
 ├──────────────────────────────┼────────────────────────────────────────────┤
 │ MCP server cached in         │ Manifest declares MCP server names;        │
 │ registry → leaks across      │ instances are built per task via the       │
 │ tasks                        │ existing build_mcp_server(task_id).        │
 ├──────────────────────────────┼────────────────────────────────────────────┤
 │ options.patches_sink runtime │ Explicit retention in the composer;        │
 │  monkey-patch deleted by     │ documented as a known seam.                │
 │ declarative-purity instinct  │                                            │
 ├──────────────────────────────┼────────────────────────────────────────────┤
 │ Parity drift between         │ Parity assertion test runs in CI for every │
 │ manifest and imperative path │  migrated role until that role's           │
 │  during migration            │ imperative factory is deleted in Phase 6.  │
 ├──────────────────────────────┼────────────────────────────────────────────┤
 │ Conflict with the active     │ One open refactor per role at a time.      │
 │ v1→v2 migration              │ Migration ordering documented above avoids │
 │                              │  touching roles currently in flux.         │
 ├──────────────────────────────┼────────────────────────────────────────────┤
 │ Safety regression from       │ Global denylists stay code-declared;       │
 │ manifest-derived denylists   │ aggregation is code ∪ manifest with code   │
 │                              │ unremovable.                               │
 ├──────────────────────────────┼────────────────────────────────────────────┤
 │ Hidden coupling:             │ Manifest declares the MCP server name, not │
 │ mcp__darkfactory__*          │  a generic slot. Registry validation       │
 │ permission-gate namespace    │ rejects manifests that reference unknown   │
 │ match                        │ server names.                              │
 └──────────────────────────────┴────────────────────────────────────────────┘

 ---
 Rollback strategy

 This refactor is incrementally reversible. Each phase's commits are atomicper-role; reverting one role's commits returns it to the imperative path, which still exists until Phase 6. The parity assertion guarantees that, prior to Phase 6, the manifest path produces identical ClaudeAgentOptions to the imperative path — so a revert has no behavioral effect, only a configuration-surfaceeffect.

 The point of no return is Phase 6 cleanup. Before merging Phase 6, verify the full demo matrix end-to-end. If a regression surfaces post-Phase-6, the rollback is to re-introduce _ROLE_DEFAULTS and the imperative factories from git history rather than to "un-migrate" manifests.

 ---
 Critical files

 - src/darkfactory/llm_factory.py — survives, narrows.
 - src/darkfactory/agents/_sdk_common.py — keeps load_prompt.
 - src/darkfactory/hooks/permission_gate.py — keeps invariants; loses per-role tables.
 - src/darkfactory/hooks/path_guard.py — keeps secret tables; gains a declarative entry point.
 - src/darkfactory/agents/fixer.py — heaviest closure logic; migrated last.
 - New: src/darkfactory/agents/manifests/<role>.yaml, agents/registry.py,agents/compose.py, agents/manifest.py (schema).

 ---
 Verification

 End-to-end at the close of each phase:

 - uv run pytest tests/ green.
 - Demo fixtures: uv run darkfactory run --repo tests/fixtures/demo/happy-path produces a green build and a PR.
 - Langfuse trace: one trace per workflow execution; agent spans carry manifest_sha and prompt_sha attributes.
 - darkfactory roles list introspection: every migrated role's full harness prints from the registry — the audit surface this refactor exists to create.
 - Permission-gate negative test: a non-PR-Creator role attempting git push or gh pr create is denied; gh pr merge is denied for every role.

 After Phase 6:

 - grep -r "_ROLE_DEFAULTS\|make_.*_client\|<ROLE>_ALLOWLIST" src/ is empty.
 - Worker startup log shows the registry loaded N roles, all valid, with hashes.
 - A deliberate manifest edit (e.g., bumping call_cap for Builder) appears in the OTel trace as a different manifest_sha, validating the audit trail.

 ---
 Sources

 - Harness design for long-running application development — Anthropic
 - Harness engineering for coding agent users — Martin Fowler
 - Agent Harness Engineering — Addy Osmani
 - awesome-harness-engineering — taxonomy of 12 primitives
 - Multi-Agent Architecture Guide (March 2026) — A2A agent cards