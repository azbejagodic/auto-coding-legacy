"""Execute explicitly approved test argument lists, independent of workflows."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Sequence


logger = logging.getLogger(__name__)
_WINDOWS = os.name == "nt"
_CLEANUP_TIMEOUT = 5.0


@dataclass(frozen=True)
class CommandResult:
    arguments: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    success: bool
    timed_out: bool = False
    error: str | None = None
    cleanup_error: str | None = None


@dataclass(frozen=True)
class TestRunResult:
    success: bool
    commands: tuple[CommandResult, ...]
    failed_command: CommandResult | None
    duration_seconds: float
    error: str | None = None

    @property
    def commands_executed(self) -> int:
        return len(self.commands)


def _text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _cleanup(process: subprocess.Popen[str]) -> str | None:
    """Kill only the launched tree/group and reap the direct process."""
    errors = []
    if _WINDOWS:
        # Retain Popen's process handle while targeting its PID. Never use
        # image-name matching, which could terminate unrelated test processes.
        if process.poll() is None:
            system_root = os.environ.get("SystemRoot")
            if not system_root:
                errors.append("SystemRoot is unavailable; Windows tree cleanup could not run")
            else:
                try:
                    killed = subprocess.run(
                        [str(Path(system_root) / "System32" / "taskkill.exe"),
                         "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=_CLEANUP_TIMEOUT, check=False, shell=False,
                    )
                    if killed.returncode != 0:
                        errors.append(f"Windows tree cleanup failed (exit code {killed.returncode})")
                except (OSError, subprocess.TimeoutExpired):
                    errors.append("Windows tree cleanup could not complete")
        else:
            errors.append("Parent already exited; Windows descendants cannot be safely identified")
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            errors.append("Process group cleanup failed")
    # Direct-child fallback is safe even if tree cleanup failed.
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=_CLEANUP_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        errors.append("Direct test process cleanup failed")
    return "; ".join(errors) or None


def _execute(arguments: tuple[str, ...], directory: Path, timeout: float) -> CommandResult:
    started = time.monotonic()
    logger.info("Test command start: executable=%r argument_count=%d",
                Path(arguments[0]).name, len(arguments) - 1)
    stdout = stderr = ""
    exit_code = None
    error = cleanup_error = None
    timed_out = False
    process = None
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if _WINDOWS else {"start_new_session": True}
    try:
        process = subprocess.Popen(
            list(arguments), cwd=directory, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", shell=False, **options,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            error = "Test command timed out"
            stdout, stderr = _text(exc.stdout), _text(exc.stderr)
            cleanup_error = _cleanup(process)
            try:
                # Bounded even if an escaped descendant retains the pipes.
                stdout, stderr = process.communicate(timeout=_CLEANUP_TIMEOUT)
            except subprocess.TimeoutExpired as remaining:
                stdout = _text(remaining.stdout) or stdout
                stderr = _text(remaining.stderr) or stderr
                cleanup_error = "; ".join(filter(None, [cleanup_error, "Output pipes remained open after cleanup"]))
        exit_code = process.returncode
        if error is None and exit_code != 0:
            error = f"Test command exited with code {exit_code}"
    except FileNotFoundError:
        error = "Test executable was not found"
    except OSError as exc:
        error = f"Test process error ({type(exc).__name__}, errno={exc.errno})"
        if process is not None:
            cleanup_error = _cleanup(process)
    except BaseException:
        # Do not abandon a running child on cancellation or unexpected errors.
        if process is not None:
            cleanup_error = _cleanup(process)
            if cleanup_error:
                logger.error("Test cleanup on interruption: %s", cleanup_error)
        raise
    duration = time.monotonic() - started
    success = exit_code == 0 and error is None and cleanup_error is None
    log = logger.info if success else logger.warning
    log("Test command %s: duration=%.3fs exit_code=%s timeout=%s error=%s cleanup_error=%s",
        "passed" if success else "failed", duration, exit_code, timed_out, error, cleanup_error)
    return CommandResult(arguments, exit_code, _text(stdout), _text(stderr), duration,
                         success, timed_out, error, cleanup_error)


def run_tests(
    working_directory: str | Path,
    commands: Sequence[Sequence[str]],
    timeout: float = 600.0,
) -> TestRunResult:
    """Run trusted, preapproved commands in order, stopping on first failure.

    The caller must supply approved argument lists. This function does not
    discover commands, install dependencies, or change project files itself.
    Test programs may modify their workspace as part of their normal operation.
    """
    started = time.monotonic()
    logger.info("Test execution start")

    def finish(results: list[CommandResult], error: str | None = None) -> TestRunResult:
        failed = next((item for item in results if not item.success), None)
        error = error or (failed.error if failed else None)
        if failed and failed.cleanup_error:
            error = "; ".join(filter(None, [error, failed.cleanup_error]))
        success = bool(results) and failed is None and error is None
        duration = time.monotonic() - started
        logger.info("Test execution finished: success=%s commands=%d duration=%.3fs error=%s",
                    success, len(results), duration, error)
        return TestRunResult(success, tuple(results), failed, duration, error)

    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        return finish([], "Timeout must be finite and positive")
    try:
        directory = Path(working_directory).resolve()
        if not directory.is_dir():
            return finish([], "Project directory does not exist or is not a directory")
    except (OSError, ValueError, TypeError):
        return finish([], "Project directory is invalid or inaccessible")
    if not isinstance(commands, (list, tuple)) or not commands:
        return finish([], "At least one approved test command is required")
    approved = []
    # Validate and copy the entire plan before executing any command.
    for command in commands:
        if (not isinstance(command, (list, tuple)) or not command
                or any(not isinstance(arg, str) or "\x00" in arg for arg in command)
                or not command[0].strip()):
            return finish([], "Each approved command must be a nonempty argument list without NUL characters")
        approved.append(tuple(command))
    results = []
    for command in approved:
        result = _execute(command, directory, timeout)
        results.append(result)
        if not result.success:
            break
    return finish(results)
