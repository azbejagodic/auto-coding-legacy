"""Execute one explicitly selected stored task; no scheduling or Git actions."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import time
from typing import Callable, Sequence

from .integrations.codex import CodexResult, run_codex
from .storage import TaskStatus, TaskStore
from .test_runner import TestRunResult, run_tests, validate_test_plan


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionResult:
    task_id: str
    model: str | None
    final_status: TaskStatus | None
    codex_result: CodexResult | None
    test_result: TestRunResult | None
    success: bool
    error: str | None
    duration_seconds: float
    state_uncertain: bool = False


class SingleTaskController:
    """Use a caller-owned store and injectable synchronous runners.

    Each invocation needs its own SQLite connection. KeyboardInterrupt and
    SystemExit propagate, leaving a claimed task running for manual recovery.
    """

    def __init__(
        self,
        store: TaskStore,
        *,
        codex_runner: Callable[..., CodexResult] = run_codex,
        test_runner: Callable[..., TestRunResult] = run_tests,
    ) -> None:
        self.store = store
        self.codex_runner = codex_runner
        self.test_runner = test_runner

    def execute(
        self,
        task_id: str,
        working_directory: str | Path,
        test_commands: Sequence[Sequence[str]],
        *,
        codex_timeout: float = 600.0,
        test_timeout: float = 600.0,
    ) -> ExecutionResult:
        started = time.monotonic()
        model = None
        status = None
        codex = None
        tests = None
        logger.info("Single task execution start: task_id=%r", task_id)

        def finish(error: str | None = None, *, uncertain: bool = False) -> ExecutionResult:
            duration = time.monotonic() - started
            success = error is None and status == TaskStatus.READY_TO_COMMIT and not uncertain
            # Integration results and exception messages may contain private data.
            logger.info("Single task finished: task_id=%r status=%s success=%s uncertain=%s duration=%.3fs",
                        task_id, status, success, uncertain, duration)
            return ExecutionResult(task_id, model, status, codex, tests, success, error, duration, uncertain)

        def fail(error: str) -> ExecutionResult:
            nonlocal status
            try:
                self.store.update_status(task_id, TaskStatus.FAILED)
                status = TaskStatus.FAILED
            except Exception as exc:
                status = None
                return finish(f"{error}; could not persist failed ({type(exc).__name__}); task state is uncertain",
                              uncertain=True)
            return finish(error)

        try:
            task = self.store.get_task(task_id)
        except Exception as exc:
            return finish(f"Could not retrieve task ({type(exc).__name__})", uncertain=True)
        if task is None:
            return finish("Task does not exist")
        model = task.model
        status = task.status
        if status != TaskStatus.QUEUED:
            return finish("Only a queued task may be executed; manual recovery is required for interrupted tasks")
        try:
            claimed = self.store.claim_task(task_id)
        except Exception as exc:
            status = None
            return finish(f"Could not claim task ({type(exc).__name__}); task state is uncertain", uncertain=True)
        if not claimed:
            # Do not mark failed: another controller may own the running task.
            try:
                current = self.store.get_task(task_id)
                status = current.status if current else None
            except Exception as exc:
                status = None
                return finish(f"Task was not claimed and status could not be read ({type(exc).__name__}); state is uncertain",
                              uncertain=True)
            return finish("Task is no longer queued or another execution already claimed it")
        status = TaskStatus.RUNNING
        try:
            # Share runner validation; do not spend model usage on invalid tests.
            directory, approved = validate_test_plan(working_directory, test_commands, test_timeout)
        except ValueError as exc:
            return fail(f"Invalid test configuration: {exc}")
        try:
            codex = self.codex_runner(prompt=task.prompt, model=task.model,
                                      working_directory=directory, timeout=codex_timeout)
        except Exception as exc:
            return fail(f"Codex runner raised {type(exc).__name__}")
        if not codex.success or codex.timed_out or codex.exit_code != 0:
            return fail(f"Codex execution failed: {codex.error or 'unsuccessful CLI execution'}")
        try:
            tests = self.test_runner(working_directory=directory, commands=approved, timeout=test_timeout)
        except Exception as exc:
            return fail(f"Test runner raised {type(exc).__name__}")
        if (not tests.success or tests.commands_executed != len(approved)
                or any(not item.success or item.exit_code != 0 or item.timed_out
                       or item.error or item.cleanup_error for item in tests.commands)):
            return fail(f"Required tests failed: {tests.error or 'not all required commands passed'}")
        try:
            self.store.update_status(task_id, TaskStatus.READY_TO_COMMIT)
        except Exception as exc:
            return fail(f"Could not persist ready_to_commit ({type(exc).__name__})")
        status = TaskStatus.READY_TO_COMMIT
        return finish()
