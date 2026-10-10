"""Read-only checks for local prerequisites."""

from __future__ import annotations

from dataclasses import dataclass
import platform
import shutil
import subprocess
import sys


@dataclass(frozen=True)
class DiagnosticResult:
    name: str
    available: bool
    detail: str

    def format(self) -> str:
        marker = "PASS" if self.available else "FAIL"
        return f"[{marker}] {self.name}: {self.detail}"


def _tool_result(name: str, executable: str, version_args: tuple[str, ...]) -> DiagnosticResult:
    path = shutil.which(executable)
    if path is None:
        return DiagnosticResult(name, False, f"{executable} was not found on PATH")
    try:
        completed = subprocess.run(
            [path, *version_args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DiagnosticResult(name, False, f"found at {path}, but could not run it: {exc}")
    output = (completed.stdout or completed.stderr).strip().splitlines()
    detail = output[0] if output else f"available at {path}"
    return DiagnosticResult(name, completed.returncode == 0, detail)


def run_doctor() -> list[DiagnosticResult]:
    """Return read-only diagnostics for the required local tools."""
    required = (3, 12)
    current = sys.version_info[:2]
    python_ok = current == required
    python_detail = f"{platform.python_version()} ({sys.executable})"
    if not python_ok:
        python_detail += " — Python 3.12 is required"
    return [
        DiagnosticResult("Python", python_ok, python_detail),
        _tool_result("Git", "git", ("--version",)),
        _tool_result("GitHub CLI", "gh", ("--version",)),
        _tool_result("Codex CLI", "codex", ("--version",)),
    ]

