# Required GitHub labels

Dark Factory drives the issue-triggered pipeline entirely through GitHub labels: the poller filters on a single entry label, the workflow swaps state labels at every stage transition, and humans send approval/cancel signals by applying action labels. The target repository must have **every** label below before scheduling a watch against it. Missing labels cause the issue workflow to fail on the first state swap.

Color suggestions are conventional — pick whatever fits the repo's existing palette, but keep the prefix `df:` exact (the workflow does case-sensitive name matches).

## Entry label (human-applied)

| Label | Purpose |
| --- | --- |
| `df:ready` | The label the `IssuePollWorkflow` schedule filters on. Applying it to an issue queues a workflow run; the workflow drops `df:ready` as soon as triage starts and re-adds it only via human action when retrying. Configurable via `darkfactory schedule install --label …` but `df:ready` is the default. |

## Lifecycle labels (workflow-managed)

The issue workflow swaps these via `swap_state_label_activity` as it moves through stages. Exactly one is set at any moment for an active run.

| Label | Stage / meaning |
| --- | --- |
| `df:triaging` | Triage agent is classifying the issue. |
| `df:needs-clarification` | Triage paused; awaiting a human reply on the issue. Returning a comment swaps back to `df:triaging`. |
| `df:designing` | PO → Architect → Plan Critic planning loop is running. |
| `df:awaiting-approval` | Brief gate is open; the implementation brief has been posted as a comment and is waiting for `df:approved` or a `revise`/`reject` comment. |
| `df:building` | Builder + Tester are executing the brief inside the worker container. |
| `df:verifying` | Verifier / fixer loop is running against the build output. |
| `df:reviewing` | Reviewer agent is running on the produced branch. |
| `df:awaiting-merge` | Merge gate is open; PR exists and is waiting for `df:approved`, `df:cancel`, or a `fix`/`rebuild` comment. |
| `df:fixing` | Fixer is re-running after a merge-gate `fix` action. |
| `df:in-progress` | Merge approved; deterministic merge + cleanup is running. |
| `df:done` | Workflow completed successfully (terminal). |

## Action labels (human-applied)

The workflow detects these via `detect_approval_signal_activity` while a gate is open. Applying one is equivalent to posting the corresponding comment command.

| Label | Effect |
| --- | --- |
| `df:approved` | Approves whichever gate is currently open (`df:awaiting-approval` → proceed to build; `df:awaiting-merge` → proceed to merge). |
| `df:cancel` | Cancels the running workflow. Replaced by `df:canceled` once the run tears down. |

## Quarantine / failure labels (workflow-applied, terminal)

Applied by the issue workflow or the poller when a run ends in a non-success state. They replace `df:ready` so the poller does not immediately re-queue the issue; a human re-adds `df:ready` to retry.

| Label | Meaning |
| --- | --- |
| `df:needs-human` | Workflow escalated to a human — typically planning retry cap exhausted, fixer budget exhausted, or an agent returned an explicit `cannot_fix` / `needs_brief_change` escalation. |
| `df:canceled` | Run was canceled via `df:cancel` label or a `cancel` comment. |
| `df:failed` | Workflow ended in a non-completed Temporal state (failed / terminated / timed out). |

## One-shot setup with `gh`

Use the helper script to create or update every label above on a target repository. It reads the documented `gh label create` commands below, so this file stays the source of truth.

```bash
./scripts/sync_github_labels.py owner/name

# Preview the GitHub CLI commands without changing the repo:
./scripts/sync_github_labels.py owner/name --dry-run
```

The raw `gh` commands are kept here for transparency and for the helper to parse.

```bash
REPO=owner/name   # e.g. acme/widgets

# Entry
gh label create "df:ready" --repo "$REPO" --color 0e8a16 \
  --description "Queue this issue for Dark Factory"

# Lifecycle
gh label create "df:triaging"            --repo "$REPO" --color fbca04 --description "Triage in progress"
gh label create "df:needs-clarification" --repo "$REPO" --color fbca04 --description "Awaiting human reply on the issue"
gh label create "df:designing"           --repo "$REPO" --color fbca04 --description "Planning (PO/Architect/Plan Critic)"
gh label create "df:awaiting-approval"   --repo "$REPO" --color fbca04 --description "Brief gate open"
gh label create "df:building"            --repo "$REPO" --color fbca04 --description "Builder + Tester running"
gh label create "df:verifying"           --repo "$REPO" --color fbca04 --description "Verifier / fixer loop"
gh label create "df:reviewing"           --repo "$REPO" --color fbca04 --description "Reviewer running"
gh label create "df:awaiting-merge"      --repo "$REPO" --color fbca04 --description "Merge gate open"
gh label create "df:fixing"              --repo "$REPO" --color fbca04 --description "Fixer re-running after merge-gate fix"
gh label create "df:in-progress"         --repo "$REPO" --color fbca04 --description "Merging"
gh label create "df:done"                --repo "$REPO" --color 0e8a16 --description "Workflow completed successfully"

# Action
gh label create "df:approved" --repo "$REPO" --color 0e8a16 --description "Approve the current Dark Factory gate"
gh label create "df:cancel"   --repo "$REPO" --color d93f0b --description "Cancel the running Dark Factory workflow"

# Quarantine
gh label create "df:needs-human" --repo "$REPO" --color d93f0b --description "Dark Factory escalated to a human"
gh label create "df:canceled"    --repo "$REPO" --color d93f0b --description "Run was canceled"
gh label create "df:failed"      --repo "$REPO" --color d93f0b --description "Run failed / terminated / timed out"
```

## Notes

- The schedule entry label is configurable (`darkfactory schedule install --label …`), but every other label name is hard-coded in `src/darkfactory/runtime/activities.py` and `src/darkfactory/runtime/issue_workflow.py`. Do not rename them on the GitHub side.
- The workflow expects to be able to *add and remove* these labels via the `gh` CLI authenticated inside the worker container; the GitHub token used must have `issues: write` on the target repository.
- Labels are the only durable workflow state visible to humans. If a run ends in `df:needs-human` / `df:failed` / `df:canceled`, the maintainer recovers by reading the issue comments (status comment + phase comments) and re-adding `df:ready`.
