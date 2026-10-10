# auto-coding

Local foundation for the auto-coding development workflow, with read-only prerequisite diagnostics, logging, SQLite task storage, a single-task Codex CLI runner, explicit Git/GitHub operations, and pytest setup. Browser automation, workflow orchestration, and the dashboard are intentionally not implemented yet.

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

Application logs are written to `logs\auto-coding.log` when the application configures logging.

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
`awaiting_review`, `approved`, `rejected`, and `failed`; storage does not impose
transition rules. Acceptance criteria are serialized as JSON, and timestamps
are UTC ISO 8601 strings. Database files and SQLite sidecar files are ignored
by Git. Use the context manager to close the connection, or call `close()`.

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
results. Timeouts kill and wait for the direct CLI process; descendant process
tree termination is not guaranteed. Output is buffered in memory until exit.
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
  known credential/session paths, directories, symlinks, traversal, missing
  files, and paths outside the repository are rejected. Deletions are not
  supported by this initial interface. Empty commits are rejected.
- `push_branch(expected_branch, expected_commit_sha)` requires HEAD to match,
  pushes only that branch without force, queries origin to verify its SHA,
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
