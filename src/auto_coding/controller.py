"""Execute, test, commit and verify one explicitly selected stored task."""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging
from pathlib import Path
import time
from typing import Callable, Sequence

from .integrations.codex import CodexResult, run_codex
from .integrations.git import GitError, GitRepository, RepositoryState
from .storage import PushState, TaskStatus, TaskStore
from .test_runner import TestRunResult, run_tests, validate_test_plan
from .workspace_lock import WorkspaceBusyError, WorkspaceLock, WorkspaceLockError


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
    workspace_busy: bool = False
    commit_sha: str | None = None
    branch: str | None = None
    github_commit_url: str | None = None
    push_verified: bool = False


class SingleTaskController:
    """Use a caller-owned store and injectable synchronous runners.

    Each invocation needs its own SQLite connection. KeyboardInterrupt and
    SystemExit propagate, leaving an incomplete task for manual recovery.
    """

    def __init__(
        self,
        store: TaskStore,
        *,
        codex_runner: Callable[..., CodexResult] = run_codex,
        test_runner: Callable[..., TestRunResult] = run_tests,
        lock_factory: Callable[[str | Path], WorkspaceLock] = WorkspaceLock,
        repository_factory: Callable[[str | Path], GitRepository] = GitRepository,
    ) -> None:
        self.store = store
        self.codex_runner = codex_runner
        self.test_runner = test_runner
        self.lock_factory = lock_factory
        self.repository_factory = repository_factory

    def execute(
        self,
        task_id: str,
        working_directory: str | Path,
        test_commands: Sequence[Sequence[str]],
        *,
        codex_timeout: float = 600.0,
        test_timeout: float = 600.0,
        expected_branch: str | None = None,
        allowed_files: Sequence[str | Path] | None = None,
    ) -> ExecutionResult:
        started = time.monotonic()
        result = None
        try:
            if (not isinstance(expected_branch, str) or not expected_branch
                    or not isinstance(allowed_files, Sequence) or isinstance(allowed_files, (str, bytes))
                    or not allowed_files):
                raise GitError("execute", "An explicit expected development branch and approved file list are required")
            approved_files = tuple(allowed_files)
            repository = self.repository_factory(working_directory)
            repository.validate_execution(expected_branch, approved_files)
            with self.lock_factory(working_directory) as lock:
                if repository.path != lock.workspace:
                    raise GitError("execute", "Workspace changed before lock acquisition")
                baseline = repository.validate_execution(expected_branch, approved_files)
                origin = repository.verify_origin()
                result = self._execute_locked(task_id, lock.workspace, test_commands,
                                              codex_timeout=codex_timeout, test_timeout=test_timeout,
                                              repository=repository, baseline=baseline, expected_origin=origin,
                                              expected_branch=expected_branch, allowed_files=approved_files)
        except (WorkspaceLockError, GitError) as exc:
            busy = isinstance(exc, WorkspaceBusyError)
            logger.warning("Task preflight/lock operation failed: busy=%s", busy)
            if result is not None:
                return replace(result, success=False, error=f"{result.error + '; ' if result.error else ''}{exc}",
                               duration_seconds=time.monotonic() - started)
            return ExecutionResult(task_id, None, None, None, None, False, str(exc),
                                   time.monotonic() - started, workspace_busy=busy, branch=expected_branch)
        return replace(result, duration_seconds=time.monotonic() - started)

    def _execute_locked(
        self,
        task_id: str,
        working_directory: str | Path,
        test_commands: Sequence[Sequence[str]],
        *,
        codex_timeout: float = 600.0,
        test_timeout: float = 600.0,
        repository: GitRepository,
        baseline: RepositoryState,
        expected_origin: str,
        expected_branch: str,
        allowed_files: Sequence[str | Path],
    ) -> ExecutionResult:
        started = time.monotonic()
        model = None
        status = None
        codex = None
        tests = None
        commit_sha = None
        branch = expected_branch
        github_commit_url = None
        push_verified = False
        logger.info("Single task execution start: task_id=%r", task_id)

        def finish(error: str | None = None, *, uncertain: bool = False) -> ExecutionResult:
            duration = time.monotonic() - started
            success = error is None and status == TaskStatus.AWAITING_REVIEW and push_verified and not uncertain
            # Integration results and exception messages may contain private data.
            logger.info("Single task finished: task_id=%r status=%s success=%s uncertain=%s duration=%.3fs",
                        task_id, status, success, uncertain, duration)
            return ExecutionResult(task_id, model, status, codex, tests, success, error, duration, uncertain,
                                   commit_sha=commit_sha, branch=branch, github_commit_url=github_commit_url,
                                   push_verified=push_verified)

        def fail(error: str, *, uncertain: bool = False) -> ExecutionResult:
            nonlocal status, commit_sha, branch, github_commit_url
            # commit_files can fail after Git created a commit (e.g. timeout or
            # post-commit verification). Retain HEAD rather than hiding it.
            if commit_sha is None:
                try:
                    head = repository.current_commit_sha()
                    if head != baseline.commit_sha:
                        commit_sha, branch = head, repository.active_branch()
                        github_commit_url = repository.commit_url(head)
                        self.store.record_commit(task_id, head, branch, github_commit_url)
                        uncertain = True
                        error += "; HEAD advanced unexpectedly; commit retained for manual recovery"
                except Exception as exc:
                    uncertain = True
                    error += f"; could not inspect/persist partial Git state ({type(exc).__name__})"
            try:
                self.store.update_status(task_id, TaskStatus.FAILED)
                status = TaskStatus.FAILED
            except Exception as exc:
                status = None
                return finish(f"{error}; could not persist failed ({type(exc).__name__}); task state is uncertain",
                              uncertain=True)
            return finish(error, uncertain=uncertain)

        try:
            task = self.store.get_task(task_id)
            previous_commit = self.store.get_commit(task_id)
        except Exception as exc:
            return finish(f"Could not retrieve task ({type(exc).__name__})", uncertain=True)
        if task is None:
            return finish("Task does not exist")
        model = task.model
        status = task.status
        if previous_commit is not None:
            commit_sha, branch = previous_commit.commit_sha, previous_commit.branch
            github_commit_url = previous_commit.github_commit_url
            push_verified = previous_commit.push_state == PushState.VERIFIED
            return finish("Task already produced a commit; manual recovery/review is required")
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
        try:
            files = repository.changed_files(expected_branch, baseline.commit_sha, allowed_files, expected_origin)
            committed = repository.commit_files(expected_branch, files, f"{task.task_id}: {task.title}",
                                                 expected_base_sha=baseline.commit_sha)
            commit_sha, branch = committed.commit_sha, committed.branch
        except Exception as exc:
            detail = str(exc) if isinstance(exc, GitError) else type(exc).__name__
            return fail(f"Git commit failed: {detail}")
        try:
            self.store.record_commit(task_id, commit_sha, branch, None)
        except Exception as exc:
            return fail(f"Could not persist local commit ({type(exc).__name__}); commit remains in Git",
                        uncertain=True)
        try:
            if not committed.success or branch != expected_branch or commit_sha == baseline.commit_sha:
                return fail("Commit result could not be verified", uncertain=True)
            if repository.has_uncommitted_changes():
                return fail("Unexpected changes remain after commit; commit retained without pushing", uncertain=True)
            github_commit_url = repository.commit_url(commit_sha)
            if not github_commit_url:
                raise GitError("push", "GitHub commit URL is unavailable")
        except Exception as exc:
            detail = str(exc) if isinstance(exc, GitError) else type(exc).__name__
            return fail(f"Git commit metadata failed: {detail}")
        try:
            self.store.mark_push_attempted(task_id, github_commit_url)
        except Exception as exc:
            return fail(f"Could not persist local commit/push intent ({type(exc).__name__}); commit remains in Git",
                        uncertain=True)
        try:
            # Check again immediately before pushing; no remote mutation on drift.
            if repository.verify_origin() != expected_origin:
                raise GitError("push", "Origin changed before push")
            pushed = repository.push_branch(expected_branch, commit_sha)
            if (not pushed.success or pushed.branch != branch or pushed.commit_sha != commit_sha
                    or not pushed.github_commit_url or pushed.github_commit_url != github_commit_url):
                raise GitError("push", "Verified push metadata differs from the local commit")
            push_verified = True
        except Exception as exc:
            detail = str(exc) if isinstance(exc, GitError) else type(exc).__name__
            return fail(f"Git push/verification failed: {detail}; remote outcome is uncertain", uncertain=True)
        try:
            self.store.complete_push(task_id, commit_sha, branch, github_commit_url)
        except Exception as exc:
            return fail(f"Push verified but final persistence failed ({type(exc).__name__}); manual recovery required",
                        uncertain=True)
        status = TaskStatus.AWAITING_REVIEW
        return finish()
