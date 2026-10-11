# Approved batch execution (TASK-011)

This is an explicit, sequential execution loop, not a scheduler. Import and
status are offline. **Running consumes Codex/Groq usage and commits/pushes to
the approved development branch.** Nothing automatically merges, repairs,
retries, generates prompts, or creates PRs.

## Prepare and approve

Use the installed Python 3.12 environment. Configure the disposable development
repository yourself: existing clean checkout, local commit identity, authenticated
GitHub origin with one matching fetch/push URL, and the exact unprotected branch
specified in the plan. The controller does not create or switch branches.
Native Git and Codex executables must be on PATH; authenticate Codex separately.
Install target-project test dependencies yourself.

Review the complete [JSON plan](task-plans.md), including every prompt, model,
reasoning effort, dependency, literal file scope and test argument array. Each
executable task must include:

```json
"execution_limits": {"codex_timeout": 600, "test_timeout": 120}
```

Both are finite positive seconds, at most 86400; the test timeout applies to
each approved command. This optional schema-version-1 extension is fingerprinted.
Old frozen plans still load unchanged, but cannot execute without limits:
approve a new batch/task identity with limits rather than changing stored data.
No default timeout is silently added to an old approval.

Test commands are trusted programs, not a sandboxed description. They may have
the user's permissions. Approve exact argument arrays and explicit files, never
whole directories or globs. Configure ignored test caches before execution;
unexpected nonignored files stop the task. The example uses illustrative models
and repository-specific tests: verify model/account compatibility yourself.
Unsupported model/effort combinations fail without fallback.

Keep SQLite/private history outside the target Git repository when practical
(or in an already ignored runtime location). Never permit tasks to modify that
database, credentials or transcripts.

```powershell
python -m auto_coding plan validate examples/task-plan.json
python -m auto_coding plan import examples/task-plan.json --db runtime/tasks.sqlite3 --approve
python -m auto_coding batch status --db runtime/tasks.sqlite3
python -m auto_coding batch status --db runtime/tasks.sqlite3 --batch EXAMPLE-BATCH-001
```

Import performs no execution and freezes all details atomically. Status opens
SQLite in read-only mode, creates no database or logs, and makes no process or
network calls. It displays fingerprints, task states/checkpoints, full SHAs,
review decisions and inspection requirements. Missing databases fail, not create.

## Explicit controlled run

Set a private Groq key only in the current environment. See
[credential guidance](groq-reviews.md). For a later disposable-repository trial:

```powershell
$groqSecureKey = Read-Host "Groq API key" -AsSecureString
$env:GROQ_API_KEY = [System.Net.NetworkCredential]::new("", $groqSecureKey).Password
try {
    python -m auto_coding batch run EXAMPLE-BATCH-001 --db runtime/tasks.sqlite3 --repo path/to/disposable-repo --max-tasks 1 --execute
} finally {
    Remove-Item Env:\GROQ_API_KEY -ErrorAction SilentlyContinue
    Remove-Variable groqSecureKey
}
python -m auto_coding batch status --db runtime/tasks.sqlite3 --batch EXAMPLE-BATCH-001
```

Replace the repository path with the real root and use a plan approved for it.
The same command continues only from a verified finalized prefix. The successful
task limit counts newly approved tasks in this invocation, not attempts or
historical tasks. Omit it only when intentionally authorizing the entire batch.
Missing `--execute` launches nothing. Return codes: 0 completed/controlled limit,
1 safely stopped, 2 invalid CLI syntax, 130 keyboard interruption.

The Groq model/effort remain `openai/gpt-oss-120b` / `medium`, timeout 45 seconds,
zero retries. Batch reviews use a 12000 per-request conservative wire budget
(UTF-8 byte upper bound plus output/headroom), with the existing 30000 total
review budget and chunk limit. Full frozen configuration needs more essential
context than standalone legacy tasks. Override with `--review-budget` and
`--review-token-limit` before starting, or pass a `ReviewConfig` to
`BatchController`. These budgets are not exact token/account quota estimates.
Unreviewable essential context or a single oversized hunk stops, never truncates.

## Safety and durable state

The batch validates its fingerprint, materialized task definitions/membership,
strict order, dependencies, status, scopes, tests and limits before execution.
It binds to the canonical workspace, fingerprint, initial HEAD and hashed origin
identity. Approved placeholders alone are resolved from the verified branch/HEAD;
the original prompt stays frozen. stdin is unchanged otherwise.

The existing OS workspace lock is held through claim, contained Codex, scope
inspection, contained tests, commit, verified push, authoritative review and
finalization. The existing atomic SQLite claim also records intent. Legacy
single-task claims still refuse imported tasks. Codex receives the exact model
and explicit reasoning configuration, plus a separate developer instruction
that Python owns staging/committing/pushing and testing. This instruction uses
the documented [Codex configuration overrides](https://learn.chatgpt.com/docs/config-file/config-reference);
it does not rewrite the frozen prompt or relax sandbox protection.

Actual changed files must be within the frozen literal scope. Additions,
modifications, tracked deletions and both rename paths use existing Git checks;
unexpected staging, symlinks, sensitive paths, branch/HEAD/origin drift and no
changes stop without an invented commit. Approved source bytes must remain
unchanged during tests. Every required argv/result/cleanup must match and pass.
Python stages explicit paths only. Existing Windows Job Object containment is
reused without modification; its OS/lifecycle limitations still apply.

The local full 40-character SHA is saved before push intent. Remote SHA and
identity are independently rechecked before and after Groq review. Approval
requires durable complete per-chunk coverage and checked criteria, not just HIGH
confidence. Final status and finalized checkpoint are transactional.
Rejected commits stay pushed; task status becomes `rejected` and the batch stops.
Uncertain/error/incomplete reviews remain `awaiting_review` with a stopped
execution record. No next task runs in either case.

## Inspect and recover

Private additive `batch_workspaces` / `task_executions` tables retain model,
reasoning, limits, fingerprints, baseline, timestamps, checkpoints, Codex outcome,
sanitized test results/arguments, local/remote SHAs, review ID and stop/recovery
details. Raw Codex/session output is not copied into history. The existing review
history retains findings, confidence, suggestions, requests, usage and rate limits.
Programmatically retrieve:

```python
with TaskStore("runtime/tasks.sqlite3", read_only=True) as store:
    execution = store.executions.get("EXAMPLE-001")
    commit = store.get_commit("EXAMPLE-001")
    review = store.reviews.get(execution["review_id"]) if execution and execution.get("review_id") else None
    usage = store.reviews.usage()
```

Do not share these databases; redaction is conservative, not encryption or a
guarantee that unusual secrets are removed.

On restart, finalized tasks require consistent durable reviews/commits, a linear
saved commit chain, exact current HEAD, clean workspace and matching remote SHA.
Only that proven prefix is skipped. External commits/remote drift stop execution.
Any incomplete claim or stopped record blocks replay, even after a manual
`queued` status reset. Reconciliation inspects local HEAD/dirty state and, if a
commit was saved, queries the remote without pushing. A hard crash after Git
commit creation but before recording leaves the durable pre-commit intent and
actual HEAD available for manual inspection.

There is deliberately no automatic incomplete-stage recovery command: inspect
checkpoint/test/review history, working tree/index, HEAD/reflog, branch, origin,
remote SHA and any surviving processes. Preserve the database and changes.
Do not simply delete execution rows, requeue tasks, re-run Codex, recommit,
repush, force-push or reset history. Resolve discrepancies with human review;
if further implementation is required, explicitly approve a new task/batch.
No review-only retry is automatically initiated after interruption.
