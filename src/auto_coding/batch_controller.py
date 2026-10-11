"""Sequential execution of frozen approved plans, with a durable Groq gate."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import contextmanager
import hashlib
import logging
import os
from pathlib import Path
import re
import shutil
from typing import Callable

from .controller import SingleTaskController
from .integrations.codex import run_codex
from .integrations.git import GitError
from .integrations.groq import GroqReviewer
from .review_history import utc_now
from .reviews import ReviewConfig, ReviewResult
from .storage import PushState, TaskStatus, TaskStore
from .test_runner import validate_test_plan
from .workspace_lock import WorkspaceLockError


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BatchResult:
    batch_id: str
    success: bool
    completed: bool
    successful_tasks: int
    current_task: str | None = None
    last_completed_task: str | None = None
    commit_sha: str | None = None
    review_status: str | None = None
    stopped_stage: str | None = None
    error: str | None = None


def _approval(record: dict | None, task_id: str, sha: str, criteria: list[str]) -> bool:
    """Require durable complete reviewer coverage, not confidence or a status flag."""
    if (not record or record.get("task_id") != task_id or record.get("commit_sha") != sha
            or record.get("decision") != "APPROVED" or record.get("coverage_complete") is not True
            or record.get("error") or record.get("blocking_issues")
            or not set(criteria) <= set(record.get("requirements_checked", []))):
        return False
    chunks, requests = record.get("chunks", []), record.get("requests", [])
    if (not chunks or record.get("chunk_count") != len(chunks)
            or record.get("api_requests") != len(chunks) or len(requests) != len(chunks)):
        return False
    return all(
        request.get("completed") is True and request.get("error") is None
        and request.get("decision") == "APPROVED"
        and request.get("chunk_id") == chunk["chunk_id"]
        and request.get("coverage") == chunk["coverage"]
        for request, chunk in zip(requests, chunks)
    )


class BatchController:
    """Hold the existing workspace lock through execution, review and finalization.

    A restart can continue after a proven finalized prefix only. Any unfinished
    intent stops for manual inspection, even if its task status was reset.
    Dependencies/runners are injectable for offline tests; no retries occur.
    """

    def __init__(self, store: TaskStore, *, single: SingleTaskController | None = None,
                 reviewer_factory: Callable[..., GroqReviewer] = GroqReviewer,
                 review_config: ReviewConfig | None = None):
        self.store = store
        self.single = single or SingleTaskController(store)
        if self.single.store is not store:
            raise ValueError("Controllers must share the same caller-owned store")
        self.reviewer_factory = reviewer_factory
        # Frozen configuration adds essential context. Keep the existing reviewer
        # and model/effort defaults, with an explicit bounded batch wire budget.
        self.review_config = review_config or ReviewConfig(request_budget=12000)

    def run(self, batch_id: str, working_directory: str | Path, *, max_tasks: int | None = None) -> BatchResult:
        current = last = sha = decision = None
        count = 0
        stage = "preflight"
        owned = False
        progress_uncertain = False

        def stopped(error: str) -> BatchResult:
            return BatchResult(batch_id, False, False, count, current, last, sha, decision, stage, error)

        def checkpoint(name: str, details: dict) -> None:
            nonlocal stage
            self.store.executions.checkpoint(current, name, details)
            if name != "stopped":
                stage = name
            logger.info("Batch checkpoint: batch=%s task=%s stage=%s", batch_id, current, name)

        def persist_stop(error: str) -> None:
            if owned:
                details = {"failure_stage": stage, "error": error,
                           "recovery_required": True, "ended_at": utc_now()}
                if sha is not None:
                    details["local_sha"] = sha
                checkpoint("stopped", details)

        @contextmanager
        def protected_workspace():
            nonlocal owned, progress_uncertain
            with self.single.lock_factory(working_directory) as lock:
                try:
                    yield lock
                except BaseException as exc:
                    # Persist failure while still owning the workspace, including
                    # interrupts. The outer handler never mutates a losing claim.
                    try:
                        detail = str(exc) if isinstance(exc, (GitError, WorkspaceLockError, ValueError)) else (
                            f"Execution interrupted or failed ({type(exc).__name__}); manual inspection required"
                        )
                        persist_stop(detail)
                    except Exception:
                        progress_uncertain = True
                        exc.add_note("Progress persistence failed; inspect durable checkpoints and Git state")
                        logger.error("Could not persist failed/interrupted batch checkpoint")
                    finally:
                        owned = False
                    raise

        try:
            if max_tasks is not None and (type(max_tasks) is not int or max_tasks < 1):
                raise ValueError("max_tasks must be a positive integer")
            plan = self.store.get_plan(batch_id)
            if plan is None:
                raise ValueError("Approved frozen batch not found; import with --approve first")
            definition = plan.to_dict()
            branch, tasks = definition["expected_branch"], definition["tasks"]
            self.store.validate_plan_tasks(plan)
            for task in tasks:
                if "execution_limits" not in task:
                    raise ValueError("Frozen execution_limits are required; approve a new batch with explicit timeouts")
            repository = self.single.repository_factory(working_directory)
            with protected_workspace() as lock:
                if repository.path != lock.workspace:
                    raise ValueError("Workspace changed before lock acquisition")
                # Validate the whole materialized plan again under the lock.
                if self.store.get_plan(batch_id) != plan:
                    raise ValueError("Frozen plan changed")
                self.store.validate_plan_tasks(plan)
                origin = repository.verify_origin()
                origin_id = hashlib.sha256(origin.encode("utf-8")).hexdigest()
                binding = self.store.executions.binding(batch_id)
                if binding and (
                    binding["workspace"] != str(lock.workspace) or binding["origin"] != origin_id
                    or binding["fingerprint"] != plan.fingerprint
                ):
                    raise ValueError("Batch workspace, origin or fingerprint differs from its binding")
                approved_shas = []
                pending = []
                for task in tasks:
                    state = self.store.get_task(task["task_id"])
                    execution = self.store.executions.get(task["task_id"])
                    commit = self.store.get_commit(task["task_id"])
                    if state.status == TaskStatus.APPROVED:
                        if (pending or not binding or not execution or execution["stage"] != "finalized"
                                or execution["fingerprint"] != plan.fingerprint or not commit
                                or commit.branch != branch or commit.push_state != PushState.VERIFIED
                                or execution.get("local_sha") != commit.commit_sha
                                or execution.get("remote_sha") != commit.commit_sha
                                or execution["baseline_sha"] != (approved_shas[-1] if approved_shas else binding["initial_sha"])
                                or not _approval(self.store.reviews.get(execution.get("review_id", "")),
                                                 task["task_id"], commit.commit_sha, task["acceptance_criteria"])):
                            raise ValueError("Approved task lacks consistent finalized execution/review evidence")
                        approved_shas.append(commit.commit_sha)
                        last, sha, decision = task["task_id"], commit.commit_sha, "APPROVED"
                    elif state.status != TaskStatus.QUEUED or execution or commit:
                        # Inspect real Git without mutating or retrying any prior stage.
                        current = task["task_id"]
                        stage = "reconciliation"
                        head = repository.current_commit_sha()
                        sha = commit.commit_sha if commit else head
                        detail = "workspace has changes" if repository.has_uncommitted_changes() else "workspace is clean"
                        if commit:
                            try:
                                repository.verify_remote_commit(branch, commit.commit_sha, origin)
                                detail += "; remote SHA matches saved commit"
                            except GitError:
                                detail += "; local/remote identity could not be confirmed"
                        raise ValueError("Incomplete task requires manual inspection; no stages replayed; " + detail)
                    else:
                        pending.append(task)
                scope = tuple(dict.fromkeys(path for task in tasks for path in task["permitted_files"]))
                baseline = repository.validate_execution(branch, scope)
                if not re.fullmatch(r"[0-9a-f]{40}", baseline.commit_sha):
                    raise ValueError("Approved batches require full 40-character Git SHAs")
                binding = self.store.bind_batch(plan, str(lock.workspace), origin_id, baseline.commit_sha)
                repository.verify_linear_history(binding["initial_sha"], approved_shas)
                if approved_shas:
                    repository.verify_remote_commit(branch, approved_shas[-1], origin)
                if not pending:
                    return BatchResult(batch_id, True, True, 0, last_completed_task=last,
                                       commit_sha=sha, review_status=decision)
                # Validate every remaining command and limit BEFORE spending model usage.
                for task in pending:
                    validate_test_plan(lock.workspace, task["test_commands"], task["execution_limits"]["test_timeout"])
                if self.single.codex_runner is run_codex:
                    executable = shutil.which("codex")
                    if not executable or Path(executable).suffix.casefold() in (".bat", ".cmd"):
                        raise ValueError("Native Codex CLI is unavailable on PATH")
                if self.reviewer_factory is GroqReviewer and not os.environ.get("GROQ_API_KEY", "").strip():
                    raise ValueError("GROQ_API_KEY is unavailable; no task was claimed")
                with self.reviewer_factory(self.store, config=self.review_config) as reviewer:
                    for task in pending:
                        current, owned, decision, sha = task["task_id"], False, None, None
                        stage = "preflight"
                        if self.store.get_plan(batch_id) != plan:
                            raise ValueError("Frozen plan changed")
                        self.store.validate_plan_tasks(plan)
                        baseline = repository.validate_execution(branch, task["permitted_files"])
                        expected_head = approved_shas[-1] if approved_shas else binding["initial_sha"]
                        if baseline.commit_sha != expected_head or repository.verify_origin() != origin:
                            raise ValueError("Repository identity drifted before task claim")
                        prompt = task["prompt"]
                        values = {"BRANCH": branch, "LATEST_SHA": baseline.commit_sha}
                        for name in task.get("approved_placeholders", []):
                            prompt = prompt.replace("{{" + name + "}}", values[name])

                        def task_checkpoint(name: str, details: dict) -> None:
                            nonlocal owned
                            owned = True  # Only called after the atomic claim created the record.
                            checkpoint(name, details)

                        result = self.single._execute_locked(
                            current, lock.workspace, task["test_commands"],
                            codex_timeout=task["execution_limits"]["codex_timeout"],
                            test_timeout=task["execution_limits"]["test_timeout"],
                            repository=repository, baseline=baseline, expected_origin=origin,
                            expected_branch=branch, allowed_files=task["permitted_files"],
                            plan=plan, resolved_prompt=prompt, checkpoint=task_checkpoint,
                        )
                        sha = result.commit_sha
                        if not result.success or not result.push_verified:
                            error = result.error or "Task execution was not verified"
                            persist_stop(error)
                            return stopped(error)
                        repository.verify_remote_commit(branch, sha, origin)
                        checkpoint("review_started", {})
                        review = reviewer.review_task(
                            current, lock.workspace, sha, result.test_result,
                            tests_commit_sha=sha, safety_passed=True,
                        )
                        if not isinstance(review, ReviewResult):
                            raise ValueError("Reviewer returned an invalid result")
                        decision = review.decision
                        persisted = self.store.reviews.get(review.review_id)
                        if (not persisted or persisted["task_id"] != current or persisted["commit_sha"] != sha
                                or any(persisted.get(key) != value for key, value in asdict(review).items()
                                       if key not in ("requirements_checked", "blocking_issues", "needs_final_review"))):
                            raise ValueError("Review result differs from durable history")
                        checkpoint("review_finished", {"review_id": review.review_id, "review_status": decision})
                        if not review.eligible_to_proceed or not _approval(
                            persisted, current, sha, task["acceptance_criteria"]
                        ):
                            error = "Groq review did not provide complete approval; inspect private review history"
                            if decision == "REJECTED":
                                self.store.finalize_plan_task(current, review.review_id, approved=False)
                                owned = False
                            else:
                                persist_stop(error)
                            return stopped(error)
                        # Review cannot override a changed branch, remote, workspace or frozen definition.
                        repository.verify_remote_commit(branch, sha, origin)
                        if self.store.get_plan(batch_id) != plan:
                            raise ValueError("Frozen plan changed during review")
                        self.store.validate_plan_tasks(plan)
                        self.store.finalize_plan_task(current, review.review_id, approved=True)
                        owned = False
                        last = current
                        approved_shas.append(sha)
                        count += 1
                        if max_tasks is not None and count >= max_tasks:
                            return BatchResult(batch_id, True, count == len(pending), count, current, last,
                                               sha, decision, "limit" if count < len(pending) else None)
                return BatchResult(batch_id, True, True, count, current, last, sha, decision)
        except (KeyboardInterrupt, SystemExit):
            try:
                persist_stop("Execution interrupted; inspect workspace and saved Git state before recovery")
            except Exception:
                logger.error("Could not persist interrupted batch; inspect durable checkpoints")
            raise
        except Exception as exc:
            error = str(exc) if isinstance(exc, (GitError, WorkspaceLockError, ValueError)) else (
                f"Batch operation failed ({type(exc).__name__}); manual inspection required"
            )
            try:
                persist_stop(error)
            except Exception:
                progress_uncertain = True
            if progress_uncertain:
                error += "; progress persistence failed; state is uncertain"
            return stopped(error)
