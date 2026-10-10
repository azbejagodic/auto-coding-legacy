"""Execute a single prepared task through the local Codex CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import math
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodexResult:
    """CLI outcome only; success does not certify the generated changes.

    Captured output may contain private task/session data. It is returned to
    the caller, never copied to application logs by this module.
    """

    success: bool
    model: str
    exit_code: int | None
    final_response: str | None
    stdout: str
    stderr: str
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    duration_seconds: float = 0.0
    timed_out: bool = False


def _text(output: str | bytes | None) -> str:
    # TimeoutExpired can contain bytes even when subprocess uses text mode.
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output or ""


def _parse_events(stdout: str) -> tuple[list[dict[str, Any]], str | None, str | None]:
    events: list[dict[str, Any]] = []
    final_response = None
    malformed = False
    completed = False
    failed = False
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed = True
            continue
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            malformed = True
            continue
        events.append(event)
        event_type = event["type"]
        if event_type == "turn.completed":
            completed = True
        elif event_type in ("turn.failed", "error"):
            failed = True
        elif event_type == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                malformed = True
            elif item.get("type") == "agent_message":
                if isinstance(item.get("text"), str):
                    final_response = item["text"]
                else:
                    malformed = True
    if failed:
        error = "Codex emitted an error or failed turn event"
    elif malformed:
        error = "Codex output contained malformed JSONL events"
    elif not completed:
        error = "Codex output did not contain a completed turn"
    else:
        error = None
    return events, final_response, error


def run_codex(
    prompt: str,
    model: str,
    working_directory: str | Path,
    timeout: float = 600.0,
) -> CodexResult:
    """Run one non-interactive task with workspace-write permissions.

    All expected validation, launch, exit, protocol, and timeout failures are
    returned as unsuccessful results. No retry, model fallback, or test/code
    verification is performed. The caller configures application logging.
    """
    started = time.monotonic()
    logger.info("Codex execution start: model=%r", model)

    def result(
        *,
        error: str | None = None,
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
    ) -> CodexResult:
        events, final_response, output_error = _parse_events(stdout)
        error = error or output_error
        duration = time.monotonic() - started
        success = error is None and exit_code == 0 and not timed_out
        # Do not log raw CLI output, prompts, exception messages, or session IDs.
        log = logger.info if success else logger.warning
        log("Codex execution %s: model=%r exit_code=%s duration=%.3fs timeout=%s error=%s",
            "completed" if success else "failed", model, exit_code, duration, timed_out, error)
        return CodexResult(success, model, exit_code, final_response, stdout, stderr,
                           events, error, duration, timed_out)

    if not isinstance(prompt, str) or not prompt.strip():
        return result(error="Prompt must be a nonempty string")
    if not isinstance(model, str) or not model.strip() or "\x00" in model:
        return result(error="Model must be a nonempty identifier without NUL characters")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        return result(error="Timeout must be a finite positive number of seconds")
    try:
        directory = Path(working_directory).resolve()
        if not directory.is_dir():
            return result(error="Working directory does not exist or is not a directory")
    except (OSError, ValueError, TypeError):
        return result(error="Working directory could not be accessed")
    executable = shutil.which("codex")
    if executable is None:
        return result(error="Codex CLI was not found on PATH")
    # Windows can implicitly run batch shims through cmd.exe even with
    # shell=False. Require the native CLI to keep arguments shell-independent.
    if Path(executable).suffix.lower() in (".cmd", ".bat"):
        return result(error="Codex resolves to a Windows batch shim; put the native codex.exe on PATH")
    arguments = [executable, "exec", "--model", model, "--sandbox", "workspace-write",
                 "--json", "--color", "never", "-"]
    try:
        completed = subprocess.run(
            arguments,
            input=prompt,
            cwd=directory,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run kills and waits for the direct child before raising.
        return result(error="Codex execution timed out", stdout=_text(exc.stdout),
                      stderr=_text(exc.stderr), timed_out=True)
    except OSError as exc:
        return result(error=f"Could not launch Codex CLI ({type(exc).__name__}, errno={exc.errno})")
    error = None if completed.returncode == 0 else f"Codex exited with code {completed.returncode}"
    return result(error=error, exit_code=completed.returncode,
                  stdout=_text(completed.stdout), stderr=_text(completed.stderr))
