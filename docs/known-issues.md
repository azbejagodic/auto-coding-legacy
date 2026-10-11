# Known issues

## Frozen plan execution is not connected (TASK-009)

Plans validate/import offline, but the current controller does not enforce all
frozen fields. Imported tasks therefore cannot be claimed for execution; a future
approved task must add plan-aware enforcement and placeholder resolution first.
Statuses and commit records may still be updated independently. Model/reasoning
compatibility, executable availability and real filesystem/symlink containment
are not checked by the offline validator. Human approval must cover command
semantics and sensitive prompt content. Fingerprints are not signatures and
SQLite is not encrypted. See [task plans](task-plans.md).

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
