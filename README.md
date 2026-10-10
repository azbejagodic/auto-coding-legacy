# auto-coding

Local foundation for the auto-coding development workflow. This task includes the package structure, read-only prerequisite diagnostics, logging, and pytest setup. Browser automation, Codex execution, GitHub automation, task storage, and the dashboard are intentionally not implemented yet.

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
