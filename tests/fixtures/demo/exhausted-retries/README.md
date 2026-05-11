# Exhausted-retries demo fixture

Same shape as the happy-path fixture, but with a deliberately unsatisfiable contract test (`ImpossibleContractTest`) that no implementation can pass. Used to demo the architecture's `RunResult(status="needs_human", reason="fixer_budget_exhausted")` terminal state.

## Demo prompt

```
Add cursor-based pagination to /api/users with tests
```

## Expected terminal status

`needs_human` with `reason="fixer_budget_exhausted"`. The workflow runs `build_stage → verify_stage → fixer_stage` until the per-predicate/WP Fixer budget is exhausted, each verify still fails, and the workflow returns without entering `reviewer_stage`, the gate, or PR creation.

## Why this never converges

`src/test/java/com/example/users/ImpossibleContractTest.java` asserts two mutually exclusive cursor shapes inside a single `assertAll`:

1. `nextCursor` must base64-decode to `"id:5"` (opaque-encoding contract).
2. `nextCursor` must `Integer.parseInt` to the integer `5` (legacy plain-integer contract).

No string is simultaneously a base64-encoded `"id:5"` and the decimal characters `"5"`. Fixer can read the verify summary and repair within the approved brief, but each attempted repair can satisfy one assertion only at the cost of the other.

## What the demo audience sees

- Temporal UI: repeated `build_stage → verify_stage → fixer_stage` attempts, then the workflow finishes without `reviewer_stage` or `pr_creator_stage`.
- The workflow's `RunResult` payload is `{"status": "needs_human", "reason": "fixer_budget_exhausted", ...}`.
- No PR is opened against the target repo (R6 outer-ring guarantee: nothing destructive ever ran).
- No worker container is left behind (R5: teardown still runs in `finally`).

## Local sanity check

This fixture is intentionally broken; both `mvn -B test` invocations should *fail* even before the agent runs:

```bash
cd tests/fixtures/demo/exhausted-retries
mvn -B -q test || echo "expected failure: ImpossibleContractTest is unsatisfiable by design"
```

Only `UserControllerTest` (the seed offset-pagination test) is expected to pass at any point; `ImpossibleContractTest` exists to drive the retry loop.

## Initialising as a target repo

See `tests/fixtures/demo/SETUP.md`. Commit the impossible test on `main` so the agent inherits it on `agent/{wf_id}`.
