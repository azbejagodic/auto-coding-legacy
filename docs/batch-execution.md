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
Oversized hunks split losslessly into line-bounded primary sections with labeled
context overlap. Unreviewable essential context, an indivisible line/context,
or per-request/total/chunk limits still stop; evidence is never truncated.

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

### Explicit review-only recovery (TASK-011-RECOVERY)

Development of TASK-011-RECOVERY did not access, recover or mutate
`VISUAL-BOARD-BATCH-002`, rerun VISUAL2-001, or review its existing pushed commit
`80c9098dc8cd084e97f1769d53b3cf06354c4e7f`. Leave its `awaiting_review` status,
execution checkpoints and all review history intact. Do not use `batch run`,
requeue/reset, delete rows, recommit or repush to retry this review.

A separate, explicitly authorized review-only recovery must first back up the
private history and inspect the original frozen plan, task/commit/execution
records and trusted Python test evidence. Under exclusive workspace ownership,
reverify the exact approved branch, clean HEAD, recorded full commit SHA, parent,
scope, origin and matching remote commit, with no surviving task processes.
Rebuild authoritative evidence for that existing commit, using the untouched
prompt/configuration and genuinely commit-bound tests. Missing or uncertain
test provenance after restart must remain UNCERTAIN; never invent passing results.
Any need to rerun approved tests requires separate explicit authorization.

Use these commands with the actual private database path and target repository:

```powershell
# Read-only preflight; no API key or Groq usage required.
python -m auto_coding batch review-retry BATCH_ID --db path/to/tasks.sqlite3 --repo path/to/repository --task TASK_ID

# Separately authorize ONE review of the existing commit.
python -m auto_coding batch review-retry BATCH_ID --db path/to/tasks.sqlite3 --repo path/to/repository --task TASK_ID --execute
```

Preflight opens SQLite read-only, never creates a missing database, logs or
implementation files, and makes read-only Git remote queries. Remote access and
authentication must work. It acquires the existing OS workspace lock; its
coordination file may be created in the application lock directory outside the
target repository. No application records change without `--execute`.
The CLI reports eligible/blocked, full SHA and review IDs, not prompts.
Set a private `GROQ_API_KEY` only for execution, following [Groq guidance](groq-reviews.md).

Eligibility is deliberately narrow: `awaiting_review`, the complete original
controller checkpoint sequence, successful Codex and exact approved Python
tests, verified push, unchanged frozen definitions, consistent finalized
predecessors, untouched later tasks, and a recognized local budget/preparation
error with zero API attempts and zero chunks. The old oversized-hunk error is
supported. Actual Groq UNCERTAIN judgments, rejected/finalized tasks, unsafe or
missing evidence, arbitrary errors and conflicting history are refused.
Restored test records are trusted controller provenance, not cryptographic proof.
Missing or inconsistent records require manual investigation, not invented
passing results or automatic retesting.

Model/effort remain `openai/gpt-oss-120b` / `medium`, timeout 45 seconds, zero
SDK retries, 12000 per-request/30000 total conservative budgets and 16 chunks.
Explicit `--review-budget` and `--review-token-limit` match normal batch options;
no limits increase automatically. CLI preflight blocks evidence that still cannot
fit. A programmatic authorized attempt encountering a budget error records a new
UNCERTAIN result without API I/O. Return codes: 0 eligible preflight/approved
recovery, 1 blocked/nonapproval, 2 syntax error, 130 interrupted.

An atomic unique `review_recoveries` claim precedes review. It retains the
original stopped execution/review, task/commit/workspace-binding snapshots,
configuration, timestamp and attempt ID. The new review records its original
failure reference and recovery ID/timestamp before requests; all old history
remains intact. Inspect privately:

```python
with TaskStore("path/to/tasks.sqlite3", read_only=True) as store:
    event = store.executions.recovery("TASK_ID")
    original = store.reviews.get(event["original_review_id"]) if event else None
    retried = store.reviews.get(event["review_id"]) if event and event["review_id"] else None
    all_reviews = store.reviews.list("TASK_ID")
```

Compare `original_execution` with the saved Codex/test checkpoints and original
baseline, local/remote full SHAs and Git commit count to confirm no coding work
was repeated. Recovery never calls Codex or the test runner, stages files, commits,
pushes, checks out, resets, merges, changes the plan or starts the next task.
It holds the existing lock through review and finalization. Complete approval
requires exact deterministic expected chunks, durable approved requests and
checked criteria, not just successful transport or HIGH confidence.
Plan, source, parent chain, origin and local/remote SHA are reverified before
transactional task/review/checkpoint/recovery finalization, using existing logic.
REJECTED sets rejected and stops; uncertainty/errors stay blocked in awaiting_review.
Review-only approval does not prove implementation or architectural correctness.

After approval, a **separate explicit** normal invocation may proceed:

```powershell
python -m auto_coding batch run BATCH_ID --db path/to/tasks.sqlite3 --repo path/to/repository --max-tasks 1 --execute
```

It verifies and skips the recovered finalized prefix. Recovery itself never calls
this loop. Any claimed/interrupted/nonapproving recovery blocks another attempt,
including after restart. Inspect the audit event, both reviews, Git and processes;
do not delete rows, reset statuses or seek another approval. No automatic recovery
of a recovery is implemented. Network and SQLite are not one transaction; crashes
after provider delivery can leave unknown usage requiring manual reconciliation.
The real batch/database was not inspected, so its eligibility and budget fit
remain unverified. This task made no live model requests or changes to
`auto-coding-test`.
