# Known issues

## Quick reviewer boundaries (TASK-010)

The Groq reviewer is now connected by the approved batch controller.
Caller-owned Python test provenance is trusted, not
cryptographically verified. Review-only recovery restores only complete trusted
controller checkpoints, not arbitrary model summaries. Missing evidence fails
closed. Root and ordinary single-parent commits are supported; merge commits,
binary/non-UTF-8/non-regular files, mode-only evidence, indivisible oversized
lines/context and budgets too small for essential context require manual final
review. Large hunks now split losslessly at line boundaries; unchanged total
allowance/chunk limits can still stop a review. Git output
is buffered before evidence-size checks. Multi-chunk approval cannot guarantee
cross-file architecture. Secret screening can miss encoded/unusual secrets or
reject harmless examples; SQLite is private local storage, not encryption.
Usage is an application estimate, not exact account quota. The opt-in live smoke
check was not run during implementation. See [review documentation](groq-reviews.md).

## Approved batch recovery boundaries (TASK-011)

Plans validate/import offline; explicit approved execution now enforces frozen
fields and Groq approval under the existing lock. Historical plans without
explicit execution limits cannot run; approve a new identity with those limits.
Any unfinished execution stops for manual inspection, rather than resuming
Codex, committing or pushing. Explicit review-only recovery is limited to
recognized zero-request technical review-preparation failures with complete
original evidence. Only a consistent finalized prefix can resume normally.
A crash between Git and SQLite can leave a commit only
in Git; checkpoints and HEAD/reflog need manual reconciliation. No atomic
transaction spans Git, SQLite, remote pushes and provider requests.
Statuses and commit records may still be updated independently. Model/reasoning
compatibility, executable availability and real filesystem/symlink containment
are not checked by the offline validator. Human approval must cover command
semantics and sensitive prompt content. Fingerprints are not signatures and
SQLite is not encrypted. The controller cannot enforce developer instructions as
an OS security boundary: Codex must not commit/push, and unexpected Git state
stops execution, but approved local programs and local Git/auth configuration
remain trusted. Review budget limits can stop large prompts, indivisible lines,
or excessive total evidence even when chunking is enabled. No real unattended batch or live review was run during
implementation. See [batch execution](batch-execution.md) and [task plans](task-plans.md).

TASK-011-RECOVERY does not reconcile the real `VISUAL-BOARD-BATCH-002` stopped
review. Its committed/pushed task and history must remain untouched until a
separately authorized review-only recovery; see the [recovery procedure](batch-execution.md#explicit-review-only-recovery-task-011-recovery).
TASK-011-GROQ-UNBLOCK measured that packet read-only: a complete single request
fits 29,419 conservative units under the aligned 30,000 request/total defaults.
This is not provider token usage, recovery authorization, a remote-state check
or an approval. Larger commits, provider limits and model uncertainty can still
block review; the total cap, coverage requirements and fail-closed behavior
remain unchanged. See [budget measurements](groq-reviews.md#practical-budgeting-task-011-groq-unblock).
Ordinary recovery allows only one durably claimed attempt per stopped task.
TASK-011-GROQ-API-FIX adds one explicitly identity-bound second technical attempt
only for the historical completed unsupported-`store` HTTP failure. There is no
third compatibility attempt or retry of a substantive judgment. Interrupted/nonapproving
attempts require manual reconciliation; no atomic transaction spans provider
delivery and SQLite.
Preflight performs read-only remote queries and uses the existing external OS
coordination lock file, but creates no application records or model requests.

The legacy first recovery did not retain HTTP status, so its `provider_http_error`
alone cannot prove the provider's reason. The exception requires the exact
completed unsupported request, unchanged evidence/history, and operator-supplied
review ID/fingerprint/SHA/branch; known non-400/422 statuses are refused. Future
HTTP failures retain safe status/type/code/rate-limit diagnostics. Removing
`store` corrects request compatibility, but does not guarantee provider capacity,
model availability, token allowance or approval. No live request or recovery was
run for this fix, and the real SQLite database/target repository remain untouched.
See the [second-recovery procedure](batch-execution.md#one-second-technical-recovery-for-the-unsupported-groq-parameter).

TASK-011-GROQ-RUNTIME-FIX resolves the subsequent permanent local rate block:
the original timestamp was valid, but `1ms` resets were unrecognized and wire
units were compared to actual provider token allowance. Runtime requests now
use offline GPT-OSS token estimates and a provider ceiling as well as wire-byte
bounds. Tokenizer framing is still an estimate; organization-wide use and limits
not exposed in headers can still produce HTTP/429 failures. No automatic resend
is made. Missing/invalid timestamps or unknown exhausted resets still stop for
inspection. Subsequent chunk waits are bounded to 60 seconds.
Completed local blocks now support explicit identity-bound technical
continuation, preserving every audit/review. Unknown delivery, provider errors,
substantive judgments, unresolved findings and interrupted audits remain
ineligible. No live API/recovery or website repository mutation was performed;
the real database was inspected and packet-sized read-only with hash verification.
See [runtime handling](groq-reviews.md#runtime-token-sizing-and-rate-limits-task-011-groq-runtime-fix)
and [continuation](batch-execution.md#explicit-technical-review-only-continuation).

## Existing lock-termination test with Windows venv redirectors

During TASK-008 validation, `test_terminated_process_releases_os_lock` failed
under this Windows Python 3.12 venv: `Popen.pid` identified the venv launcher,
not the Python worker holding the lock. A diagnostic confirmed differing PIDs
under the venv and matching PIDs under the base interpreter. The unchanged
isolated test passed ten consecutive runs using the base interpreter and the
same local dependencies. No lock implementation or assertion was changed.
The ordinary venv command may still reproduce this pre-existing test limitation.
Run the unchanged complete suite using the actual Python 3.12 interpreter with
the same venv dependencies (from the repository, with the venv activated):

```powershell
$taskPython = python -c "import sys; print(sys._base_executable)"
$taskSitePackages = python -c "import sysconfig; print(sysconfig.get_path('purelib'))"
$taskPreviousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = (Join-Path (Get-Location) 'src') + ';' + $taskSitePackages
    & $taskPython -m pytest
} finally {
    $env:PYTHONPATH = $taskPreviousPythonPath
}
```

This changes only the current shell environment temporarily, not system settings.
It does not disable containment, skip tests, or alter application lock behavior.

## Background processes started by approved test commands — resolved on Windows

Resolved by TASK-007-SAFETY for ordinary Windows descendants, including detached
children. The test runner reuses Codex's atomic creation-time, kill-on-close Job
Object backend, observes the direct command's real exit code, and confirms an
empty job before returning, running another command, or releasing the controller's
workspace lock. Failed containment prevents launch; failed/uncertain cleanup
prevents commits and review. An emergency cleanup wait retains the lock until
emptiness is confirmed, even beyond the configured timeout. Persistent OS failures
can therefore require manual intervention rather than releasing unsafe ownership.

Coverage: `tests/test_windows_test_runner.py` verifies actual child/detached-child
termination after normal exit, timeout and forced controller death (including
during process creation), unrelated-process survival, real exit codes, Windows
batch-wrapper compatibility/containment, sequential cleanup, fail-closed
initialization, cleanup failures, and controller lock/commit
gating. `tests/test_windows_process.py` retains Codex containment regression tests;
`tests/test_test_runner.py` covers structured errors and the non-Windows backend.

Remaining limitations: non-Windows behavior is unchanged; successful commands,
detached descendants and controller death can leave background processes there.
Use trusted foreground tests. External services/brokers are not job descendants,
and containment is not a security sandbox. Abrupt controller death triggers OS
cleanup but cannot atomically coordinate tree termination with OS lock release;
inspect before manually recovering interrupted tasks. Workspace locks coordinate
cooperating controllers only, not unrelated editors or services. Unsupported
Windows/job configurations fail closed; Python 3.12 is required by the adapter.
