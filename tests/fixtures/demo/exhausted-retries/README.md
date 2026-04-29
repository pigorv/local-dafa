# Exhausted-retries demo fixture

Same shape as the happy-path fixture, but with a deliberately unsatisfiable contract test (`ImpossibleContractTest`) that no implementation can pass. Used to demo the architecture's `RunResult(status="exhausted_retries")` terminal state.

## Demo prompt

```
Add cursor-based pagination to /api/users with tests
```

## Expected terminal status

`exhausted_retries`. The workflow runs `build_stage → verify_stage → spec_adjustment_stage` for `VERIFY_RETRY_CAP = 3` iterations (see `runtime/workflow.py:17`), each verify still fails, and the workflow returns without entering `code_quality_stage`, the gate, or PR creation.

## Why this never converges

`src/test/java/com/example/users/ImpossibleContractTest.java` asserts two mutually exclusive cursor shapes inside a single `assertAll`:

1. `nextCursor` must base64-decode to `"id:5"` (opaque-encoding contract).
2. `nextCursor` must `Integer.parseInt` to the integer `5` (legacy plain-integer contract).

No string is simultaneously a base64-encoded `"id:5"` and the decimal characters `"5"`. spec_adjustment can read the verify summary, narrow either branch of the spec, and the agent will rewrite — but each iteration will satisfy one assertion at the cost of the other.

## What the demo audience sees

- Temporal UI: three iterations of `build_stage → verify_stage → spec_adjustment_stage`, then the workflow finishes without `code_quality_stage` or `pr_creator_stage`.
- The workflow's `RunResult` payload is `{"status": "exhausted_retries", ...}`.
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
