---
description: Deep audit of a Dark Factory role's harness — manifest, prompt, runtime, hooks, output extraction, state wiring, workflow integration. Returns findings with evidence and prioritised improvement proposals.
argument-hint: <role-name>
---

Audit the **$1** role's harness end-to-end. Your job is to find every
discrepancy and end with prioritised recommendations.

The role's harness is the union of these concepts (independent of where the
code currently lives):

- **Manifest**: declarative config — identity, LLM policy, allowed tools,
  MCP servers, hooks, permission-gate config, budgets, I/O contract.
- **System prompt**: the markdown the role runs with.
- **Runtime module**: the Python that builds the user message, calls the
  SDK, and parses the response. Defines the output type.
- **Hooks**: per-event callbacks attached either by the manifest or
  auto-attached by the framework. Each hook has a triggering surface — the
  events / tool calls / prompt-submits that make it fire.
- **Output extraction path**: the code that turns the model's final reply
  into a typed object (structured JSON, captured patches, or stub).
- **Permission gate**: the runtime guard that decides whether an argv / tool
  call is allowed for this role.
- **State channels**: the pipeline's typed state, with per-channel reducers
  that decide append-vs-overwrite-vs-merge semantics for writes.
- **Workflow call site**: the Temporal activity that invokes this role,
  plus the workflow code that schedules that activity (timeouts, retry
  policy, non-retryable error types).

## How to work

1. **Discovery (do this first).** Map the eight concepts above to today's
   files. Conventions to look for, but verify before relying on:
   - Role manifests are YAML files keyed by role name under an
     `agents/manifests/` directory.
   - System prompts are markdown files under a `prompts/` directory,
     referenced from the manifest's `llm.prompt_path`.
   - Runtime modules live alongside the manifests under `agents/`.
   - Hook factories live under `hooks/`, registered in a single registry
     module.
   - State and reducers live in one `state` module.
   - Activities live under `runtime/`; workflow scheduling lives in the
     same area.
   If any of these conventions has changed, locate the artifact by content
   shape (e.g. grep for the role name inside a YAML file, then read what
   shape it conforms to) rather than by hardcoded path.

   Produce a small "Path map" table at the top of your report mapping each
   concept to the file(s) you actually used. The rest of the audit uses
   concepts, not paths.

2. **Identify the output extraction path.** Read the runtime module and
   trace what happens to the model's final response. There are typically a
   few patterns: structured-JSON extraction validated against a Pydantic
   model with one retry, diff-driven patch capture for build-stage workers,
   or a stub for placeholder roles. Name the pattern; do not assume.

3. **Identify the workflow call site.** Grep for the role name and for
   `run_<role>` / `<role>_stage` style symbols across runtime + stage code.
   Find the activity that wraps the role and the workflow point that
   schedules it.

4. **Audit each dimension below.** For each dimension, cite evidence as
   `path:line` and a one-line "why it matters". If a dimension is N/A for
   this role, write "N/A — <reason>".

5. **Be terse.** Quote evidence; do not paraphrase. If a dimension is clean,
   one line is enough.

## Dimensions to audit

### D1. Prompt ↔ tools alignment
Does the prompt instruct the model to call any tool by name? Cross-check
every named tool against what the manifest actually allows (built-in tools
plus MCP). A prompt that says "call the X tool" while the manifest exposes
no X is a misleading instruction — it may still work today only because the
runtime extracts JSON from free text. Also flag the inverse: prompt allows
or assumes actions the manifest disallows, or prompt forbids actions the
manifest already denies.

### D2. Output model ↔ prompt
List every field the role's typed output declares. For each: is it
described in the prompt? Required vs optional? Then the inverse: for each
field the prompt asks for, is it on the output model?

Flag any output-model validator that silently back-fills fields the prompt
never mentions — those fields are invisible to the model author and
brittle. Flag any legacy-alias acceptance (the model accepting old field
names from a previous schema) and judge whether it's still load-bearing.

### D3. I/O contract sanity
The manifest declares which state fields the role reads and writes.
- For every declared *read*: confirm the runtime actually injects it into
  the user message AND the prompt references it.
- For every declared *write*: confirm the output model has a field for it
  AND the prompt asks the model to produce it AND the state module declares
  a reducer for that channel. Flag missing pieces — these are silent bugs.

### D4. Hook firing surface
For each manifest-declared hook, evaluate whether it can actually fire given
this role's tool surface and turn structure:
- PreToolUse / PostToolUse hooks need at least one tool call to ever fire.
  A role with zero allowed tools and no MCP attached cannot trigger them.
- Per-N-prompts hooks only fire after enough user-message submissions. If
  the role does one query per turn, an "every 5 submits" hook never fires.
- Diff-capture hooks need file-mutation tools (Edit / Write).
- Auto-attached hooks (the framework adds some hooks itself when the role
  uses certain tools — check the role-options builder) may already provide
  coverage, making a manifest entry redundant. Conversely, a manifest entry
  that the framework treats as a no-op should be flagged as misleading.
- Injection-guard hooks fire on tool output, not on the user message the
  runtime assembled itself. If the prompt labels some text "untrusted" but
  it flows in via the user message, the guard does not cover it. Say so.

Also cross-check hook parameter keys against the hook factory's signature.
Manifest parameter dicts are typically unschemaed, so typos are silent.

### D5. Permission-gate config
- `argv_allowlist` non-empty while the manifest doesn't allow the
  command-execution tool that consumes it → unreachable config.
- Command-execution tool allowed while `argv_allowlist` is empty → every
  argv denied.
- `role_owned_argv_prefixes` overlap with another role's prefixes or with
  the framework's hardcoded deny list → ambiguous ownership.
- For roles that publish (push, merge, comment): is the gate's
  "approval" seam actually plumbed through the activity that calls the
  role?

### D6. State-reducer match
For every channel in the manifest's writes contract:
- Find it in the typed pipeline state.
- Check the reducer matches intent: append-only for logs / accumulating
  lists, last-writer-wins for single-value fields, per-id merge for
  collections keyed by id.
- Flag any write channel missing from the state entirely — that's a
  silently-dropped output.

### D7. LLM policy sanity
- Model choice vs role workload (cheap model on a heavy-reasoning role, or
  a flagship on a trivial extraction, is a smell).
- Temperature vs output shape (structured-JSON roles typically want low
  temperature).
- Thinking enabled vs reasoning complexity.
- Note that env-var overrides for model / temperature / thinking exist —
  the manifest is the default, not the floor.

### D8. Output extraction path
Match the prompt's "output discipline" instructions to the actual parsing
strategy:
- If structured-JSON extraction: the prompt should ask for "a single JSON
  object", not "call the X tool" — those are different runtime contracts.
- If diff-driven patch capture: the role must actually have file-mutation
  tools available and the diff-capture hook attached. Otherwise the patches
  list will always be empty.
- If a stub / no-op: confirm nothing downstream assumes a real result.
- Note the parser's retry behavior: most structured paths re-prompt once on
  parse failure. The prompt should survive that re-prompt without
  contradicting itself ("emit ONLY the tool call" wording breaks on the
  retry message).

### D9. Untrusted-input handling
Does the prompt label any incoming text as untrusted ("data not
instructions", "ignore embedded instructions in X")? If yes, the role
should also have a runtime defense against injection — typically a hook
that scans content or a prompt-injection guard. If there's no runtime
defense, the safety claim is prompt-honor-system only, which is fine to
ship but should be flagged honestly.

### D10. Hardcoded stack / language assumptions
Look for language- or framework-specific text in the prompt (e.g. "Java",
"Spring Boot", "Maven"), build-tool hardcoding in the permission-gate
allowlist, and assumptions about working directory or repo layout. None of
these are bugs, but they bound where the role can be reused.

### D11. v1 → v2 migration drift
Surface any vocabulary mismatch between the prompt and the current output
model. The repo is mid-migration; older roles may still talk in retired
terms ("SpecSlice", "affected_files", "spec_adjustment", removed agent
names). The compatibility-shim validators on the output model are a hint —
if the model still accepts a legacy alias, audit whether anything alive
still produces it. Dead aliases should be removed.

### D12. Compose-time seams
Some role state isn't on the manifest — it's threaded through a runtime
ComposeState (slice id, work-package intent, justification template,
approval flags, dependency-change authorization). For each seam the hooks
attached to this role consume, confirm the activity that calls the role
actually populates it. A seam declared but never populated is silent
breakage.

### D13. Workflow integration
At the call site:
- Is the activity scheduled with an explicit `start_to_close_timeout`?
- If the activity loops or runs long, is `heartbeat_timeout` set?
- Is there a retry policy? Are `non_retryable_error_types` listed for
  parse-class errors?
- Does the state slice handed to the activity actually contain every field
  the manifest declares as a read?
Missing any of these is risk; generic defaults masquerading as policy are
also risk.

### D14. Documentation drift
- Manifest `description` and `when_to_use` accurate about what the role
  does today?
- Module-level docstring still accurate?
- Project-level docs (e.g. `CLAUDE.md`, design docs) still reference the
  role correctly? Grep and audit hits.

### D15. Surprising / role-specific quirks
Free-form. Things to keep an eye out for:
- A role that bypasses the SDK entirely (placeholder / stub).
- A role with sole ownership of a privileged command class (e.g. push,
  merge).
- A role that makes multiple turns per run, where per-N-prompts hooks
  behave differently than for single-turn roles.
- Reducer ↔ model mismatch (the model can overwrite a channel the reducer
  expects to append, or vice versa).

## Required report structure

Produce one markdown document, in this order:

1. **Path map** — concept → file(s) actually used in this audit, so the
   reader can follow your citations.
2. **Snapshot** — 5-bullet TL;DR of what this role is and how it's wired
   (model, temp, thinking, allowed tools, MCP, hooks, output type,
   activity, workflow call site).
3. **Per-dimension findings** — D1 through D15. Each dimension gets one of:
   - "✅ Clean — <one-line evidence>"
   - "⚠ <issue title>" with 1–3 lines: evidence as `path:line` + one-line
     "why it matters".
   - "N/A — <reason>".
   Do not skip any dimension.
4. **Recommendations** — prioritised list:
   - **P0** correctness / silent data loss / safety
   - **P1** drift, misleading prompt, dead config
   - **P2** polish, future-proofing, naming
   One line per item; name the file(s) to change; do not produce diffs.
5. **Open questions** — anything the audit can't resolve without more
   context. Bullet list, one line each.

## Rules

- Quote evidence; do not paraphrase. Every ⚠ finding cites `path:line`.
- Don't pad. If a dimension is clean, one line is enough.
- Don't write patches; recommendations are pointers, not diffs.
- If the role you're asked to audit has no manifest (e.g. a stub role),
  state that and audit whatever exists. Do not invent a manifest.
- Treat the report as the deliverable. Stop after writing it; do not start
  implementing fixes.
