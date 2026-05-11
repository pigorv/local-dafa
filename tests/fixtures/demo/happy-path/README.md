# Happy-path demo fixture

Minimal Spring Boot 3.3 / Java 21 web app exposing `/api/users` with offset+limit pagination over an in-memory list of 50 seeded users. No database, no JPA — `mvn test` is fast.

## Demo prompt

```
Add cursor-based pagination to /api/users with tests
```

## Expected terminal status

`merged` (i.e. `RunResult(status="merged", ...)`).

## Why this fixture converges

- The seed code already exposes `/api/users` and the existing test only asserts response shape under offset pagination, so the agent has a clear "before" surface to extend.
- Adding a `cursor` query param + a `nextCursor` field in the response is a small, mechanically obvious change: the agent's discovery stage produces one API work package and one test work package; build/verify pass on the first iteration; reviewer returns `approve`; the gate is the only human step.
- The 50-user seed is large enough that cursor pagination is meaningful but small enough that the test suite runs in seconds.

## Local sanity check (host-side)

Inside the worker container the agent runs `mvn -B test`. To smoke this locally before a demo, from a host with JDK 21 + Maven:

```bash
cd tests/fixtures/demo/happy-path
mvn -B -q test
```

This should pass before the demo so the only thing the workflow actually changes is the cursor-pagination code path.

## Initialising as a target repo

The dark-factory orchestrator mounts the target repo at `/workspace` and runs `git checkout agent/{wf_id}` against it, so the directory must be a real git repo with `main` (or whichever branch the host shell is on) checked in. See `tests/fixtures/demo/SETUP.md` for the one-time copy + `git init` recipe.
