# Demo fixture setup

Each fixture under this directory ships as a plain source tree. The dark-factory orchestrator mounts the target repo at `/workspace` and runs `git checkout agent/{wf_id}` against it (see `runtime/activities.py:setup_worker_activity`), so the directory the CLI's `--repo` flag points at must be a real git repo with a committed initial state on `main`.

The fixtures cannot be initialised in place because this repo (`dark-factory`) is itself a git repo and nested git repos do not work with vanilla git. Copy each fixture to a scratch location first.

## One-time per-fixture init

```bash
FIXTURE=happy-path                  # or retry-induced, exhausted-retries
SRC="$(pwd)/tests/fixtures/demo/$FIXTURE"
DEST="$HOME/dark-factory-demo/$FIXTURE"

mkdir -p "$(dirname "$DEST")"
cp -R "$SRC" "$DEST"
cd "$DEST"
git init -q -b main
git add -A
git -c user.email=demo@local -c user.name=demo commit -q -m "seed: $FIXTURE fixture"
```

Then point the CLI at `$DEST`:

```bash
uv run darkfactory run "Add cursor-based pagination to /api/users with tests" --repo "$DEST"
```

## Resetting between rehearsals

```bash
cd "$DEST"
git checkout main
git branch -D "agent/$DEMO_WORKFLOW_ID" 2>/dev/null || true
docker rm -f "darkfactory-worker-$DEMO_WORKFLOW_ID" 2>/dev/null || true
```

## What the agent sees

- `main` branch with the fixture's seed code (offset pagination only).
- For `retry-induced`: the strict `CursorContractTest` is committed on `main` and fails until cursor pagination lands.
- For `exhausted-retries`: the `ImpossibleContractTest` is committed on `main` and fails forever.
- The agent creates `agent/{wf_id}`, makes commits there, and either pushes + opens a PR (`merged` outcome) or returns a non-merged `RunResult` with no remote side effects.

## Local mvn smoke (optional but recommended before a talk)

The worker container has `mvn` and Java 21. You can validate the seed compiles on the host before the demo:

```bash
cd "$DEST"
mvn -B -q -DskipTests package          # always passes
mvn -B -q -Dtest=UserControllerTest test  # passes for all three fixtures
```

For the retry-induced and exhausted-retries fixtures, the strict contract test is *expected* to fail on `main` — that is precisely what drives the retry loop in the demo.
