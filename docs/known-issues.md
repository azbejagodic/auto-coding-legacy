# Known issues

## ChatGPT browser transport: conservative UI contract (TASK-008)

- The initial selectors and response-evidence contract are tested against
  controlled local HTML, **not a live signed-in ChatGPT account**. UI changes,
  locales, account experiments or missing accessible names can require selector
  maintenance before live use. Unknown authentication/markup may return a
  startup/selection error instead of a specific login state. Manual login,
  CAPTCHA/MFA and account access remain the user's responsibility; no bypass.
- Responses require observed generation plus unique message/turn identities
  and completion controls with no streaming state. A response completed too
  quickly to observe generation, changed copy controls, transcript virtualization,
  multiple assistant/tool turns, or missing IDs may be reported uncertain even
  if visually complete. This is intentional: no text-stability heuristic, partial
  answer, or old reply is accepted as success.
- Temporary Chat must already have a completed manual exchange to establish a
  visible identity anchor; empty Temporary Chats are refused. Its identity is
  limited to the active document, mode, and transcript. It has no promised
  durable URL or recovery across reload, browser loss or restart. A lost chat
  requires explicit user intervention; no fallback normal chat is selected.
- A browser/transport crash around submission cannot prove whether the server
  received the message. Durable, credential-free submission intent blocks later
  sends until manual inspection/acknowledgement. There is no retry, automated
  recovery, server-side exactly-once guarantee, or transaction with ChatGPT.
  If profile data/journal is deleted externally, that recovery signal is lost.
- Profile locks coordinate cooperating local processes only. Chromium still owns
  its own profile lock; a surviving browser after abrupt Python death can prevent
  startup and requires manual inspection. This task does not extend Windows Job
  Object containment to Playwright or kill unrelated browsers. Failed cleanup
  retains application ownership while the Python process remains alive.
- Chromium is the tested browser. Installed Chrome/Edge channels are configurable
  but not exercised by these tests. Browser sandbox support depends on the local
  OS/policy; there is no fallback that disables sandboxing. Profile contents and
  returned conversation text are private even though logs/journals omit secrets.

Setup, manual smoke test, selector maintenance, and interruption handling:
[browser transport guide](browser-transport.md).

## Existing lock-termination test with Windows venv redirectors

During TASK-008 validation, `test_terminated_process_releases_os_lock` failed
under this Windows Python 3.12 venv: `Popen.pid` identified the venv launcher,
not the Python worker holding the lock. A diagnostic confirmed differing PIDs
under the venv and matching PIDs under the base interpreter. The unchanged
isolated test passed ten consecutive runs using the base interpreter and the
same local dependencies. No lock implementation or assertion was changed.
The ordinary venv command may still reproduce this pre-existing test limitation;
the [browser transport guide](browser-transport.md#windows-virtual-environment-test-caveat)
documents how to run the full suite with the actual Python 3.12 interpreter.
Browser transport tests pass under both interpreter entry points.

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
