# auto-coding

Local auto-coding foundation with read-only diagnostics, logging, SQLite task storage, Codex CLI execution, approved tests, and a locked single-task commit/push controller. Automated AI communication for task preparation and commit reviews is not yet connected; a supported CLI integration will be implemented in a separate task. Gemini CLI is being evaluated but is not implemented. Automatic multi-task scheduling, pull requests, and the dashboard are intentionally not implemented yet.

## Requirements

- Windows PowerShell
- Python 3.12
- Git
- GitHub CLI (`gh`)
- Codex CLI (`codex`)

## Setup

From the repository directory:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install --editable ".[test]"
Copy-Item config.example.toml config.toml
```

`config.toml` is local-only and is excluded from Git. Update it with environment-specific paths as needed; do not add credentials.

## Usage

Run the read-only environment check:

```powershell
python -m auto_coding doctor
```

The command reports the Python version and whether Git, GitHub CLI, and Codex CLI are available on `PATH`. It returns exit code `0` when all checks pass and `1` when any check fails.

Run tests:

```powershell
python -m pytest
```

The optional `test` dependency installs pytest for running the test suite.

On Windows venv installations that use a launcher/worker redirector, an existing
direct-process lock-termination test needs the base-interpreter command documented
in [known issues](docs/known-issues.md#existing-lock-termination-test-with-windows-venv-redirectors).
No application lock checks or test assertions are disabled.

Application logs are written to `logs\auto-coding.log` when the application configures logging.

## Prewritten task plans

Validate and explicitly approve an offline JSON batch:

```powershell
python -m auto_coding plan validate examples/task-plan.json
python -m auto_coding plan import examples/task-plan.json --db runtime/tasks.sqlite3 --approve
```

Validation is read-only. Import freezes all original task definitions and a
deterministic SHA-256 fingerprint in one SQLite transaction, without running
Codex, tests, Git or APIs. Batch/task collisions are rejected, including identical
reimports. Imported tasks cannot be claimed by the legacy controller: enforcement
of frozen scopes, tests, dependencies, reasoning effort and placeholders belongs
to a future plan-aware execution task. Existing manually added tasks are unchanged.
See the [versioned format and three-task example](docs/task-plans.md).

## Task storage

`TaskStore` accepts a configurable database file path and creates its parent
directories and schema when opened. Existing tasks are preserved on reopening.
The example configuration's `[storage].database_path` documents the local path;
configuration loading is not wired into the CLI. Pass the chosen path directly:

```python
from auto_coding.storage import TaskStatus, TaskStore

with TaskStore("runtime/tasks.sqlite3") as store:
    task = store.add_task(
        task_id="TASK-EXAMPLE",
        title="Example task",
        prompt="Full original Codex prompt",
        model="assigned-model",
        task_order=1,
        acceptance_criteria=["Tests pass"],
    )
    task = store.get_task(task.task_id)
    tasks = store.list_tasks()
    store.update_status(task.task_id, TaskStatus.RUNNING)
```

Run the example insertion only once per database: duplicate IDs raise
`DuplicateTaskError`. Invalid statuses raise `ValueError`. A missing lookup
returns `None`; updating a missing task raises `KeyError`. Tasks are listed by
`task_order`, with ties sorted by ID. Statuses are `queued`, `running`,
`ready_to_commit`, `awaiting_review`, `approved`, `rejected`, and `failed`.
General status updates do not impose transition rules; `claim_task(task_id)`
atomically changes only queued tasks to running, returning whether it succeeded.
Tasks with a saved commit cannot be claimed again, even if manually reset to queued.
Existing foundation databases are migrated transactionally on opening to permit
the new status while preserving their task data. Acceptance criteria are serialized as JSON, and timestamps
are UTC ISO 8601 strings. Database files and SQLite sidecar files are ignored
by Git. Use the context manager to close the connection, or call `close()`.

An additive `task_commits` table stores one immutable SHA/branch pair per task,
its credential-free GitHub commit URL when available, and push state. Retrieve it
with `store.get_commit(task_id)`; missing records return `None`. Existing databases
gain this table transactionally without changing task data or supported statuses.
`record_commit(...)` saves the local SHA before pushing; `mark_push_attempted(...)`
persists the URL and `push_uncertain` before network I/O. `complete_push(...)`
atomically saves `verified` metadata and `awaiting_review`, or rolls both back.
Only call the latter after independently verifying the remote. General-purpose
`update_status` remains available; controller policy governs automated transitions.

## Codex CLI runner

`auto_coding.integrations.codex.run_codex(prompt, model, working_directory,
timeout=600.0)` executes one prepared task and returns a typed `CodexResult`.
Calling this function starts a real Codex task using existing CLI authentication
and consumes the corresponding model usage. Configure Codex with your ChatGPT
login locally; the runner does not set credentials or call the OpenAI API directly.

The runner uses `codex exec --model <model> --sandbox workspace-write --json
--color never -`, passing the full prompt through stdin with `shell=False`.
The model is passed exactly as supplied, with no retry or fallback. The working
directory must exist and Codex's normal Git repository checks remain enabled.
On Windows, put the native `codex.exe` on `PATH`; `.cmd`/`.bat` shims are rejected
because Windows can implicitly launch them through a command shell.

The result includes success, requested model, exit code, last completed agent
message (if present), stdout, stderr, parsed events, error, duration in seconds,
and a timeout flag. Success requires exit code zero, a completed turn, and no
malformed/error events. Unknown event types are retained. Success describes CLI
completion only; code changes and tests still require independent verification.

Expected input, launch, CLI, output, and timeout failures return unsuccessful
results. On Windows 10 / Server 2016 or newer, Codex is created atomically in a
private Windows Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. The job
handle is non-inheritable and only the three stdio handles are passed to Codex.
Normal child creation inherits containment; breakaway is not enabled. No shell,
administrator rights, or relaxed Codex sandbox flags are used. Creation-time
assignment avoids an unprotected startup window, including controller crashes
during process creation. Unsupported OS/job configurations fail closed; there
is no fallback to an uncontained launch.

Timeouts cover stdin writes as well as waiting for output. On completion,
timeout, or interruption, remaining job members are terminated and the job's
active-process count must reach zero within five seconds. Output draining and
direct-process waiting also have bounded cleanup waits. Cleanup/verification or
handle-close failures produce unsuccessful results even with exit code zero and
a completed turn. A timeout remains marked `timed_out=True` when cleanup fails.
The controller propagates these errors, skips tests, persists failure where
possible, and does not retry. Killing the controller closes its job handle in
the OS and terminates contained descendants without Python cleanup handlers.

Containment is lifecycle management, not an additional security boundary:
processes delegated to external services/brokers (for example WMI) are not
ordinary job descendants. Existing outer-job/security policies may prevent
launch; these are reported rather than bypassed. During abrupt controller death,
OS termination is asynchronous: Python cannot verify cleanup or roll back files
already written, and workspace-lock release is not atomic with tree termination.
Inspect the workspace before manually recovering interrupted tasks. The Windows
adapter uses Python 3.12 Popen internals for pipes/waiting; the application remains
pinned to Python 3.12. See Microsoft's [job lifecycle documentation](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
and [creation-time attributes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute).

On non-Windows systems the existing `subprocess.run` behavior is unchanged:
timeouts kill/wait only the direct CLI child. Descendants can survive timeouts or
controller death; the Windows cleanup guarantees do not apply there.
Output is buffered in memory until exit.
The caller configures the existing logging system; the runner logs metadata
without prompts, output, tokens, or session IDs. Returned output can contain
private information and should be handled accordingly. Tests mock all Codex
execution and do not consume model usage.

Argument handling was checked against the installed CLI's `codex exec --help`.
JSONL parsing follows the [official non-interactive mode documentation](https://developers.openai.com/codex/noninteractive).

## Git integration

`auto_coding.integrations.git.GitRepository(repository_path, timeout=60.0)`
validates an existing repository root using the local Git executable. Each
operation is explicit; constructing the object does not fetch, switch, commit,
or push. The interface is independent of task storage and Codex:

- `validate()` returns repository path, active branch, full commit SHA,
  working-tree change status, and confirmed origin availability.
- `active_branch()`, `current_commit_sha()`, `verify_origin()`, and
  `has_uncommitted_changes()` provide individual checks.
- `create_development_branch("automation/batch-001")` requires a clean tree,
  fetches `origin/main`, creates a new branch at that commit, switches to it,
  and verifies the result. Omit the name for a UUID-based unique batch name.
  Existing local or remote names are rejected.
- `commit_files(expected_branch, ["src/example.py"], "Commit message")`
  stages only validated, explicitly selected files and returns a `GitResult`
  with the full commit SHA. The existing index must be empty. Ignored files,
  known credential/session paths, directories, symlinks, traversal, wildcard
  and pathspec-magic inputs, and paths outside the repository are rejected.
  Missing files are accepted only when the exact path was a regular file in
  `HEAD`; this stages an already-occurring deletion without deleting anything.
  Additions, modifications, and selected deletions can share one commit.
  Untracked missing files and empty commits are rejected.
  Optional `expected_base_sha` rejects HEAD drift from the tested baseline before
  staging. `validate_execution(expected_branch, allowed_files)` checks the clean
  branch, repository state, approved paths, and github.com origin without mutation.
  `changed_files(...)` discovers literal tracked/untracked changes and rejects
  staged files, unapproved paths, HEAD/origin drift, and unsafe selections.
- `push_branch(expected_branch, expected_commit_sha)` requires HEAD to match,
  pushes the exact SHA to only that branch without force, queries origin to verify its SHA,
  and returns a `GitResult`. Supported github.com HTTPS/SSH remotes also yield
  a credential-free commit URL; other remotes yield `None`.

Failures raise `GitError` with operation, detail, and an exit code when known.
Authentication, network, launch, and timeout errors are reported without raw
Git output or remote URLs that might contain credentials. Existing logging
configuration is used; commit messages and file contents are not logged.

Mutations refuse detached HEAD, protected `main`, unexpected branch state,
or unfinished merge/rebase/cherry-pick operations. Origin must have a single
matching fetch and push URL. Hooks and signing are disabled per invocation;
global Git configuration is never modified. Native executables are required
on Windows. Git authentication must already be configured locally.

Callers must serialize repository access; checks do not lock out other Git
processes. Failed staging or commits leave the index for manual inspection.
Network timeouts can leave the remote outcome uncertain; inspect remote state
before retrying. Credential-path checks cover known names, not secret detection
inside arbitrary source files. Git configuration and credential/transport
helpers are trusted local prerequisites. Tests use temporary local bare
remotes or mocked results and never contact GitHub.

## Approved test commands

`auto_coding.test_runner.run_tests(working_directory, commands, timeout=600.0)`
executes an ordered list of argument lists in the selected project directory:

```python
from auto_coding.test_runner import run_tests

result = run_tests(".", [["python", "-m", "pytest"]], timeout=120)
```

Commands must come from trusted, preapproved configuration supplied by the
caller. No commands are discovered from project files or Codex output. The
runner uses `shell=False`, installs no dependencies, and does not change project
files itself; invoked test programs may produce their usual artifacts. Argument
lists such as `["npm", "test", "--", "--runInBand"]` and `["cargo", "test"]`
are supported without choosing a programming language or test framework.
Executables are resolved on `PATH` before contained Windows launch. As with
stdlib `Popen`, Windows can interpret `.cmd`/`.bat` commands through its system
shell despite `shell=False`; use only trusted, preapproved commands/arguments.

The complete plan is validated before starting. Commands execute sequentially
with the same configurable per-command timeout and stop on the first failure.
Empty or invalid plans fail without launching anything. `TestRunResult`
contains overall success, command results, executed count, failed command,
total duration, and error details. Each `CommandResult` contains the exact
arguments, exit code, stdout/stderr, duration, success, timeout flag, and any
execution or cleanup error. Only exit code zero without execution errors passes.

On Windows 10 / Server 2016 or newer, each command uses the same creation-time
Job Object containment as Codex. No command executes before assignment; setup
failure never falls back to uncontained execution. Normal and detached descendants
inherit the job, with breakaway disabled. After the direct command exits, its real
exit code is retained and surviving job members are terminated before output is
returned or another command starts. Descendant-held output pipes do not extend
a normally exiting command to its timeout. Timeouts terminate the entire job.
Controller death closes its non-inheritable job handle, triggering OS cleanup.
No PID scanning, unrelated process termination, or administrator rights are used.
See Microsoft's [Job Object lifecycle](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
and [creation-time assignment](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute).

Cleanup normally has a five-second verification deadline. If tree termination or
verification fails, execution fails closed: a critical message is logged and the
job and caller's workspace lock remain held until Windows confirms zero active
members. This emergency wait can exceed the command timeout indefinitely if OS
termination/accounting remains unavailable; cancellation is deferred until the
job is empty. There are no task retries. Inspect the error and manually recover;
forcibly terminating the controller invokes OS kill-on-close as described above.
Even if cleanup subsequently completes, its original error remains a failure:
exit code zero cannot authorize a commit or GitHub review. Pipe/handle cleanup
errors are also reported, never treated as successful tests.

Non-Windows behavior is unchanged: timeout cleanup targets a dedicated process
group with bounded waits and a direct-child fallback. Successful commands do not
clean up background children; detached processes and children surviving controller
death can escape. Use trusted foreground commands there. On all platforms, work
delegated to external services is not an ordinary contained descendant. Job
containment is lifecycle management, not a security sandbox: test commands retain
the caller's permissions. Abrupt-death lock release and OS tree termination are
not atomic; inspect the workspace before manual recovery.

Logs identify the executable and argument count, timing, and status. Argument
values, environment variables, and captured output are not logged. Returned
arguments/output may contain private data; callers must handle them accordingly.
Output is buffered in memory. Configure logging through the existing logging
setup before calling the runner. No workflow, retry, Git, or database operations
are connected to this module.

## Single-task controller

`SingleTaskController` connects caller-owned storage to Codex, tests, and Git.
Calling `execute()` launches a real task, consumes Codex model usage and can
commit/push to GitHub; tests use mocks and temporary local remotes. Explicitly
choose one queued task and supply trusted, preapproved test commands, the expected
development branch, and a literal approved-file list:

```python
from auto_coding.controller import SingleTaskController
from auto_coding.storage import TaskStore

with TaskStore("runtime/tasks.sqlite3") as store:
    controller = SingleTaskController(store)
    result = controller.execute(
        "TASK-EXAMPLE", ".", [["python", "-m", "pytest"]],
        codex_timeout=600, test_timeout=120,
        expected_branch="feature/your-approved-batch",
        allowed_files=["src/example.py", "tests/test_example.py"],
    )
```

The task must already exist. Both new keyword arguments are required for execution;
omitting them returns a read-only failure rather than silently executing without
Git safety checks. Approved files come from the caller, never Codex output. They
must be literal file paths, not directories, globs, or pathspec magic; new files
may be approved before they exist. Changes outside this list fail the whole task,
not just the individual file. Tests may create ignored caches/logs; these are never
selected for commits. Known credential/session files are forbidden even if approved.

The repository root, origin, expected non-main branch, unfinished operations,
working directory and staging area are validated before locking and again under
the lock before claiming. Preflight/busy failures leave task state unchanged.
A conditional SQLite write claims `queued → running`, so two invocations cannot
both claim it. Test configuration is validated before model execution, and Codex
receives the saved prompt/model unchanged. After Codex and every required test
succeed, `ready_to_commit` is an intermediate persistence point. The controller
discovers approved additions/modifications/tracked deletions, creates one commit
named `<task ID>: <title>`, saves its SHA/branch, then pushes that exact SHA and
verifies the remote branch. Only verified push metadata saved atomically with
status permits `awaiting_review`. This is not review approval; no AI review,
next task, pull request, merge, branch creation, or retry is performed.

Failures after claiming attempt `running → failed`. If persistence fails, the
result explicitly reports `state_uncertain=True` and `final_status=None`. No
automatic retry occurs. `KeyboardInterrupt` and `SystemExit` propagate; a task
left incomplete is refused on future invocations and requires manual recovery.
Failures never reset/discard files, unstage changes, or remove existing commits.
A failed push may have reached the remote: inspect `get_commit(task_id)` and the
returned SHA/branch/URL before any manual recovery. Push states are `local`
(no push attempted), `push_uncertain` (intent saved, outcome not durably verified),
and `verified`. No automatic retry or second commit occurs. Even a manual status
reset cannot re-execute a task with saved commit metadata.

Git and SQLite cannot form a single transaction. A crash or SQLite failure between
local commit creation and recording its SHA can leave the commit only in Git;
ordinary error handling returns/reconstructs its SHA where possible, but a hard
crash cannot persist recovery details. Inspect HEAD/reflog and local changes for
incomplete tasks. If the push is verified but final SQLite persistence fails,
the result remains unsuccessful with `push_verified=True` and uncertainty; it
must not be treated as approval to rerun. Never manually requeue without inspection.

`ExecutionResult` includes task ID, assigned model, final status, original Codex
and test result objects, commit SHA, branch, GitHub URL, push verification, final
status, overall success, error, duration, and uncertainty flag.
Prompts, outputs, and raw exception messages are not copied into controller logs.
Configure logging through the existing logging setup. Inject `codex_runner` and
`test_runner`, and `repository_factory` callables for testing. Each invocation needs
its own `TaskStore` connection.

Before reading or claiming a task, the controller validates the workspace and
acquires a fail-fast OS-backed file lock for its resolved absolute path. The lock
is held through Codex, tests, commit, push/verification, and final SQLite persistence, including failure
updates. Contention returns `workspace_busy=True` without changing task data or
launching either runner; model and final status are `None` because no task is read.
Lock acquisition and release errors return unsuccessful results. A release error
does not undo an already-persisted task status or verified push.

Lock files live outside the target repository: `%LOCALAPPDATA%\auto-coding\workspace-locks`
on Windows, `~/Library/Application Support/auto-coding/workspace-locks` on macOS,
and `$XDG_DATA_HOME/auto-coding/workspace-locks` (default `~/.local/share`) on Linux.
Files remain after release; only OS lock ownership indicates a busy workspace.
The OS releases ownership when the controller exits, including abnormal exits.
All cooperating controllers must use the same local lock directory; constructor
injection via `lock_factory` permits isolated testing or a shared local location.

Different workspaces can run independently. Locks coordinate cooperating local
controllers only, not unrelated editors/processes or separate computers/checkouts.
On Windows, controller death triggers OS job cleanup for contained Codex and test
descendants, subject to the limitations described above. During normal execution,
the test runner confirms job emptiness before returning to the locked controller.
Elsewhere, orphaned Codex/test children may continue modifying files after the lock
is released; inspect and stop them before resuming. Crash recovery is not implemented.
