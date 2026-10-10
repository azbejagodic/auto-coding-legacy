# Known issues

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
