# Retry-induced demo fixture

Same shape as the happy-path fixture, but with a strict pre-existing `CursorContractTest` that pins the cursor-pagination response format tightly enough that a naive first iteration is likely to fail.

## Demo prompt

```
Add cursor-based pagination to /api/users with tests
```

## Expected terminal status

`merged`, after the architecture's verify/Fixer loop in `runtime/workflow.py` has executed at least one full `build → verify → fixer → verify` cycle. Demo-able trace in Temporal UI: three iterations of the build/verify pair before reviewer runs.

## How retries are induced

`src/test/java/com/example/users/CursorContractTest.java` constrains the API surface in three ways that any "obvious" first-pass implementation tends to miss:

1. The response body must contain **exactly** the keys `items`, `nextCursor`, `limit`. A first pass that retains the offset-style `offset`/`total` fields, or adds a `prevCursor`, will fail the key-set assertion.
2. `nextCursor` must be `null` (JSON null) on the last page — **not** an empty string, **not** an absent key. First passes that omit the field on the last page or return `""` will fail.
3. Cursors are opaque base64 of `id:<lastIdInPage>`. First passes that return the last id as a plain integer or use a different sentinel format will fail decoding.

When iteration 1 fails verify, `fixer_stage` reads the test failure summary and applies bounded repairs tied to the approved brief. Iteration 2's verify pass picks up the corrected implementation and converges.

## Why this is a "likely retry" rather than guaranteed

The Plan Critic in the discovery stage may notice the strict contract test on first read and produce a tight enough initial spec to converge in one pass. That is the system working as designed and is also a fine demo outcome — `merged` either way. If you want to *force* a retry for the talk, swap to the exhausted-retries fixture instead, or temporarily add a second contract assertion (e.g., a custom HTTP header) that the discovery stage cannot anticipate without trying.

## Local sanity check

Before the demo, the seed code's `UserControllerTest` should pass and `CursorContractTest` should fail (cursor pagination does not exist yet on `main`):

```bash
cd tests/fixtures/demo/retry-induced
mvn -B -q -Dtest=UserControllerTest test
mvn -B -q -Dtest=CursorContractTest test || echo "expected failure on main: cursor pagination not yet implemented"
```

## Initialising as a target repo

See `tests/fixtures/demo/SETUP.md` for the copy + `git init` recipe. The demo expects this repo to be on `main` with both tests committed; the agent's `agent/{wf_id}` branch is what introduces cursor pagination.
