# Groq quick reviewer (TASK-010)

This component checks one approved task/commit, not a complete architecture.
It does not call Codex, run tests, change files, approve task status, retry a
review, switch providers, configure billing, push, or control execution.
TASK-011's [approved batch controller](batch-execution.md) calls this reviewer and
owns task-status finalization after independently verified complete approval.
The reviewer itself still makes no execution or task-state decisions.

## Installation and private credentials

Use Python 3.12 and `python -m pip install --editable ".[test]"`.
The declared dependency is the official `groq>=1.7.0,<2` SDK (tested with 1.7.0).
Install/import never makes an API request.

Keep the key in an OS secret manager or the current process environment, not
in code, task prompts, test arguments, Git, command history or local config files.
For a temporary PowerShell session, prompt without echoing the key:

```powershell
$groqSecureKey = Read-Host "Groq API key" -AsSecureString
$env:GROQ_API_KEY = [System.Net.NetworkCredential]::new("", $groqSecureKey).Password
try {
    # Explicit manual review, or the opt-in smoke command below.
} finally {
    Remove-Item Env:\GROQ_API_KEY -ErrorAction SilentlyContinue
    Remove-Variable groqSecureKey
}
```

The SDK must hold a plaintext key in process memory to authenticate. Do not
enable SDK/HTTP debug logging, dump the environment or share private databases.
The reviewer pins the default client's base URL to Groq's HTTPS endpoint rather
than accepting an environment-supplied alternate endpoint. Injected SDK clients
are trusted caller configuration.

## Initiate one review manually

The preferred programmatic interface is `GroqReviewer.review_task(...)`.
It requires an existing stored task in `awaiting_review` with matching commit
metadata, the exact clean development checkout and full lowercase 40-character
SHA. It works with legacy tasks and imported frozen-plan definitions.

For manual use in an application/Python session that still holds a successful
`SingleTaskController` result named `execution`:

```python
from auto_coding.integrations.groq import GroqReviewer
from auto_coding.reviews import ReviewConfig
from auto_coding.storage import TaskStore

# execution is an actual existing Python ExecutionResult, NOT model text.
# This snippet does not execute another coding task or run tests.
assert execution.success and execution.push_verified and not execution.state_uncertain
assert execution.commit_sha is not None and execution.test_result is not None

with TaskStore("runtime/tasks.sqlite3") as store:
    config = ReviewConfig(reasoning_effort="medium", timeout=45.0)
    with GroqReviewer(store, config) as reviewer:
        result = reviewer.review_task(
            execution.task_id, ".", execution.commit_sha, execution.test_result,
            tests_commit_sha=execution.commit_sha,
            safety_passed=True,  # attest the independent controller checks above
        )
    print(result.review_id, result.decision, result.confidence)
```

The caller must truthfully bind actual `TestRunResult` evidence to the committed
tree and independently verify safety. This is a trusted Python interface, not
cryptographic proof of test provenance. Never construct passing results from
Codex/Groq summaries. After restart, absent trustworthy test evidence means
`UNCERTAIN`, not approval. Imported plans additionally require the exact approved
test arguments, branch, scope (including both rename paths), integrity-checked
plan fingerprint and original configuration. The existing controller does not
yet execute imported tasks; this compatibility is tested using synthetic offline
runner results, not live task execution.

`build_review_packet` is separately reusable. Lower-level `review(packet)`
accepts a trusted caller-owned packet; use the Git-building interface above to
avoid supplying fabricated code evidence. Its mutable fields are snapshotted.
Missing task/commit identity raises `ReviewError` before any API call. For an
existing matching record, unavailable/unsafe Git or test evidence produces a
stored `UNCERTAIN` result without contacting Groq. Database failures propagate;
they cannot return an eligible approval.

## Evidence and response validation

Read-only Git operations inspect only the exact HEAD commit and its parent,
not Codex's summary or unrelated history. Root commits are supported; merge
commits, non-regular blobs, dirty/detached/wrong/protected branches, unsafe paths,
binary/non-UTF-8 changes and ambiguous evidence fail closed. External diff and
textconv helpers are disabled. Unfinished Git operations are rejected.
Only explicitly changed paths are read; no files are staged or removed.
Patch bytes preserve CRLF/LF differences, replacement objects are disabled for
evidence reads, and optional Git index-refresh writes are disabled.

The packaged template `src/auto_coding/templates/groq-review.md` is version 2
(explicit primary-fragment versus context-only semantics).
Its content digest and version are recorded, along with actual submitted
instructions. Bump the version when changing review policy.

Requests use strict JSON schema output, but Python independently rejects missing
or extra fields, duplicate JSON keys, wrong types, unknown labels, truncation,
tool calls, refusals, wrong returned model, task mismatch or any full-SHA
mismatch (including matching short prefixes). Approvals need checked criteria
and no blocking issues. Rejections need a blocking issue. Claimed requirements
must match supplied criteria. Overall approval requires every criterion checked,
all required chunks valid/approved, complete coverage and passing independent
test/safety evidence. Durable task/commit state drift during review blocks
approval. Groq cannot override failed tests or update the queue.

`eligible_to_proceed` expresses only eligibility for a future controller, not
authorization to bypass its own tests, locks, state checks or Git safeguards.
`REJECTED` and `UNCERTAIN` always stop a future batch.

## Budgeting and complete coverage

`ReviewConfig` defaults:

- Model `openai/gpt-oss-120b`; reasoning `medium` (`low`/`high` configurable).
- Timeout 45 seconds; SDK retries explicitly zero.
- Request budget 6,000; output/reasoning reservation 1,536; headroom 512.
- At most 16 chunks and 30,000 estimated tokens across the whole review.

Input is conservatively estimated using UTF-8 bytes of serialized request
arguments (including the schema/instructions/messages), not the common
characters-divided-by-four heuristic. Output reservation and headroom are
additional. This is intentionally restrictive and is not an exact tokenizer
or a claim about a user's free allowance. Configure smaller budgets for lower
limits; do not equate tokens-per-minute with safe per-request input capacity.

A complete small diff uses one request. Larger diffs keep whole files together
where possible, otherwise split at validated unified-diff hunk boundaries.
An oversized hunk is partitioned at complete LF-delimited diff lines; CRLF,
Unicode source and Git no-final-newline markers are retained exactly. A marker
stays attached to its preceding line. No source line is split or rewritten.
File headers, change types, rename source/destination, task/SHA, original
requirements, test evidence and the changed-file manifest accompany each chunk.
Each section has a stable identity; chunks have SHA-256 identities and ordered
coverage lists. All original hunks and metadata must be accounted for exactly
once. Hunk line counts are verified; text is never arbitrarily truncated.
Empty-file additions/deletions and pure renames have explicit metadata sections.

Unsplit sections retain their `file-index:hunk-index` identity and original
`header`/`body` format. Fragment IDs append `:start-end`, a zero-based half-open
range of the original hunk's body lines, including no-final-newline markers.
For fragments, `header` remains the exact file header; `body` is only that range's
primary evidence. The `fragment` metadata repeats the original `hunk_header`
(never a fabricated header for the slice), parent section ID and original body
line count. `old_range`/`new_range` give source-line cursors and consumed counts:
start at the original hunk's old/new starting line, advance on context/removal
or context/addition respectively; markers consume neither. Zero-count sides
retain their original cursor, including zero for new/deleted files.
`context_only.before`/`after` provide up to three neighboring source diff lines
with attached markers and explicit body-line ranges. They are overlap, never
additional primary coverage. Joining ordered primary bodies under the original
hunk header reconstructs the exact original hunk; contiguous ranges cover every
body line once. Reconstruction and ordered unique section coverage are checked
before requests. Persisted request content retains these references.

Every candidate and final request is measured with the complete serialized
schema, instructions, repeated requirements/configuration/tests/manifest,
section metadata and overlap, plus output reservation and headroom. Chunk
count/index sizing is conservative. No limits are increased by fragmentation.

The previous combined oversized-hunk/essential-evidence error could arise from
either a long hunk or large repeated essential evidence (or both); a commit's
line count alone cannot identify which. Essential evidence overflow now has a
separate error. Oversized single lines, required context/metadata that cannot
fit, excessive chunks or total review allowance, mode-only changes without useful
context, unreviewable sections or essential evidence too large to repeat safely
cause `UNCERTAIN` before requests. Git output is buffered before the 2 MB/200-file
evidence cap is checked, so this is not a hard process-memory limit.

Any valid rejection makes the overall review `REJECTED`; a rejection stops
further requests to conserve usage, leaving remaining coverage explicitly
incomplete. Otherwise any uncertainty, invalid/missing response or incomplete
coverage means `UNCERTAIN`. Only all-approved complete coverage can approve.
Requirements that cannot be assessed from partial context must be marked
uncertain by the reviewer. Multi-chunk approvals explicitly do not guarantee
cross-file architectural correctness.

Confidence is the model's self-assessment, not a calibrated probability.
Overall confidence conservatively takes the weakest reported label, never an
average; missing/invalid chunk responses lower it to `LOW`.

## Private history, errors and usage

Additive `reviews` and `review_requests` tables live in the existing configurable
task SQLite file (normally `runtime/tasks.sqlite3`). Requests are recorded before
I/O; crashes leave unknown attempts and an unfinished `UNCERTAIN` record.
History writes never change tasks, frozen plans or commit metadata.

```python
with TaskStore("runtime/tasks.sqlite3") as store:
    history = store.reviews.list("TASK-EXAMPLE")
    one_review = store.reviews.get("review-id")
    daily_estimate = store.reviews.usage()
    # Explicit UTC ISO bounds can represent a known provider quota period:
    period = store.reviews.usage(
        start="2026-10-11T00:00:00+00:00", end="2026-10-12T00:00:00+00:00"
    )
```

Records contain provider/model/effort/template, timestamps, decision/confidence,
findings, section coverage, per-chunk decisions, sanitized request/response
transcripts, provider errors and token counts. SDK response text is retained
when available, including malformed content, only after screening. Error payloads
and raw exception messages are not logged/stored; static categories distinguish
authentication, permission, timeout, network, rate limit, quota and HTTP failures.
Application logger messages contain model, request count, decision and sanitized
errors, not prompts, keys or model content. Call the existing `configure_logging`
explicitly if file logging is wanted; private transcripts stay in SQLite.

Before sending, detected credential patterns, environment-key matches, sensitive
paths or private-key material refuse the entire review rather than approve a
redacted diff. Responses and persistence are screened again, with detected
secrets redacted/withheld. Original frozen task definitions remain untouched.
False positives and missed/encoded/unusual secrets are possible: automatic
redaction does not guarantee removal of every secret. Human inspection before
disclosure is still required. SQLite is not encrypted; use a private directory
with suitable OS permissions. Database/WAL files, runtime data, logs and
`review-transcripts/`/`review-history/` are ignored by Git, not made secure by Git
ignore rules. Never force-add/export transcripts into a repository.

Usage sums actual provider-reported input/output/total tokens and records
unknown-usage attempts separately (never fabricated zero-cost success).
`api_requests` counts durable request attempts, including interrupted attempts
whose delivery is unknown. No generation is made to refresh usage.
Default daily estimates use a UTC calendar day, NOT an asserted account reset
boundary. Other programs/Playground usage is invisible, so account remaining
quota stays `None`. Provider limits/reset/retry headers are separately labeled
and timestamped. They may be stale and are not an exact remaining daily-token
allowance.

Known `Retry-After` and exhausted request/token limits block further calls until
the supplied deadline/reset; unknown reset information fails closed. There is
no automatic sleep/retry. Quota/API errors stop the review, never switch provider
or enable billing. Simultaneous external clients can invalidate observed limits;
future controller integration must serialize its own review activity.

## Optional live smoke check (not run during this task)

All pytest cases use fake SDK responses or `httpx.MockTransport`; they need no
API key and make no real Groq requests. The separate script is not a pytest test.
After privately setting `GROQ_API_KEY` as above, explicitly opt in:

```powershell
python scripts/groq_smoke.py --allow-live
```

This makes ONE tiny live structured-JSON connectivity request with a 30-second
timeout and no retries. It consumes provider allowance and may fail if reasoning
uses its bounded output reserve. It never sends repository content or records a
code approval. Without both the flag and environment key it refuses before
creating a client. Do not put the key on the command line.

Official references: [Groq SDK](https://github.com/groq/groq-python),
[structured output](https://console.groq.com/docs/structured-outputs),
[reasoning](https://console.groq.com/docs/reasoning),
[rate limits](https://console.groq.com/docs/rate-limits).
