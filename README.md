# auto-coding

Local foundation for the auto-coding development workflow, with read-only prerequisite diagnostics, logging, SQLite task storage, and pytest setup. Browser automation, Codex execution, GitHub automation, and the dashboard are intentionally not implemented yet.

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
