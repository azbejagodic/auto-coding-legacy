"""Explicit, one-attempt recovery of a verified commit's technical review stop."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import math
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .batch_controller import _approval
from .integrations.git import GitError, GitRepository
from .integrations.groq import GroqReviewer
from .plans import FrozenPlan
from .review_evidence import build_review_packet
from .reviews import ReviewDiagnostics, ReviewError, ReviewPacket, ReviewResult, SHA, encoded, plan_chunks
from .storage import PushState, Task, TaskStatus
from .test_runner import CommandResult, TestRunResult
from .workspace_lock import WorkspaceLock, WorkspaceLockError

if TYPE_CHECKING:
    from .batch_controller import BatchController


# Only known local preparation/budget failures with zero attempted requests.
# Provider judgments, missing evidence, secrets and arbitrary errors are excluded.
TECHNICAL_FAILURES = frozenset({
    "A diff hunk or essential evidence exceeds the safe request budget",
    "Essential review evidence exceeds the safe request budget",
    "A diff line or required hunk context exceeds the safe request budget",
    "Diff metadata exceeds the safe request budget",
    "Final review request exceeds the safe request budget",
    "Excessive review chunks", "Total review allowance would be exceeded",
})
STOPPED_STAGES = (
    "claimed", "codex_started", "codex_finished", "tests_started", "tests_completed",
    "commit_started", "commit_created", "push_started", "push_verified",
    "review_started", "review_finished", "stopped",
)


@dataclass(frozen=True)
class RecoveryResult:
    batch_id: str
    task_id: str
    success: bool
    executed: bool
    commit_sha: str | None = None
    original_review_id: str | None = None
    recovery_id: str | None = None
    review_id: str | None = None
    decision: str | None = None
    error: str | None = None
    diagnostics: ReviewDiagnostics | None = None


def _duration(value: object) -> bool:
    return (type(value) in (float, int) and math.isfinite(value) and value >= 0)


def _tests(record: dict, definition: dict) -> TestRunResult:
    """Restore only complete, successful controller-owned runner evidence."""
    data = record.get("tests")
    if (not isinstance(data, dict) or data.get("success") is not True
            or data.get("error") is not None or data.get("failed_command") is not None
            or not _duration(data.get("duration_seconds"))
            or not isinstance(data.get("commands"), list)
            or len(data["commands"]) != len(definition["test_commands"])):
        raise ValueError("Original successful Python test evidence is missing or invalid")
    commands = []
    for item, arguments in zip(data["commands"], definition["test_commands"]):
        if (not isinstance(item, dict) or item.get("arguments") != arguments
                or item.get("success") is not True or type(item.get("exit_code")) is not int
                or item["exit_code"] != 0 or item.get("timed_out") is not False
                or item.get("error") is not None or item.get("cleanup_error") is not None
                or not _duration(item.get("duration_seconds"))
                or not isinstance(item.get("stdout"), str) or not isinstance(item.get("stderr"), str)):
            raise ValueError("Original approved test command evidence is incomplete or failed")
        commands.append(CommandResult(**{**item, "arguments": tuple(arguments)}))
    return TestRunResult(**{**data, "commands": tuple(commands)})


class ReviewRecovery:
    """Reuse BatchController dependencies/lock, never its coding/test/push loop."""

    def __init__(self, controller: BatchController):
        self.controller = controller
        self.store = controller.store
        self.config = controller.review_config
        if self.config.model != "openai/gpt-oss-120b" or self.config.reasoning_effort != "medium":
            raise ValueError("Review recovery requires openai/gpt-oss-120b with medium reasoning")

    def _inspect(self, batch_id: str, task_id: str, repository: GitRepository,
                 lock: WorkspaceLock, *, recovery: dict | None = None
                 ) -> tuple[FrozenPlan, Task, dict, dict, ReviewPacket, dict]:
        plan = self.store.get_plan(batch_id)
        if plan is None:
            raise ValueError("Approved frozen batch not found")
        self.store.validate_plan_tasks(plan)
        definitions = plan.to_dict()["tasks"]
        index = next((i for i, task in enumerate(definitions) if task["task_id"] == task_id), None)
        if index is None:
            raise ValueError("Exact task is not a member of the selected batch")
        definition = definitions[index]
        task = self.store.get_task(task_id)
        execution = self.store.executions.get(task_id)
        commit = self.store.get_commit(task_id)
        attempt = self.store.executions.recovery(task_id)
        if attempt is not None and (recovery is None or attempt["recovery_id"] != recovery["recovery_id"]
                                    or attempt["stage"] != "reviewing"):
            raise ValueError("Recovery already claimed or completed; interrupted attempts require manual inspection")
        if (task.status != TaskStatus.AWAITING_REVIEW or not execution or not commit
                or commit.push_state != PushState.VERIFIED or not SHA.fullmatch(commit.commit_sha)
                or execution.get("stage") != "stopped" or execution.get("failure_stage") != "review_finished"
                or execution.get("recovery_required") is not True
                or execution.get("review_status") != "UNCERTAIN"
                or execution.get("batch_id") != batch_id or execution.get("task_id") != task_id
                or execution.get("fingerprint") != plan.fingerprint
                or execution.get("local_sha") != commit.commit_sha
                or execution.get("remote_sha") != commit.commit_sha):
            raise ValueError("Task is not a verified pushed implementation stopped at technical review")
        if tuple(item.get("stage") for item in execution.get("checkpoints", [])) != STOPPED_STAGES:
            raise ValueError("Original execution checkpoint sequence is incomplete or ambiguous")
        if not definition.get("execution_limits") or any(
            execution.get(key) != definition.get(key)
            for key in ("model", "reasoning_effort", "execution_limits")
        ):
            raise ValueError("Original execution configuration differs from the frozen task")
        codex = execution.get("codex", {})
        if (codex.get("success") is not True or type(codex.get("exit_code")) is not int
                or codex["exit_code"] != 0 or codex.get("timed_out") is not False
                or codex.get("error") is not None or codex.get("model") != definition["model"]
                or not _duration(codex.get("duration_seconds"))):
            raise ValueError("Original successful Codex checkpoint is missing or invalid")
        original = self.store.reviews.get(execution.get("review_id", ""))
        if (not original or original["task_id"] != task_id or original["commit_sha"] != commit.commit_sha
                or original.get("provider") != "GroqCloud"
                or original.get("model") != self.config.model or original.get("reasoning_effort") != "medium"
                or original.get("decision") != "UNCERTAIN" or original.get("confidence") != "LOW"
                or original.get("coverage_complete") is not False
                or type(original.get("api_requests")) is not int or original["api_requests"] != 0
                or type(original.get("chunk_count")) is not int or original["chunk_count"] != 0
                or original.get("chunks") != []
                or original["requests"] or original.get("blocking_issues") != []
                or original.get("requirements_checked") != [] or original.get("missing_evidence") != []
                or original.get("needs_final_review") != [
                    "Not all approved acceptance criteria were demonstrably checked."]
                or original.get("error") not in TECHNICAL_FAILURES):
            raise ValueError("Previous review is not an eligible zero-request technical preparation failure")
        histories = self.store.reviews.list(task_id)
        expected_ids = {original["review_id"]}
        if recovery is not None:
            if (execution != recovery["original_execution"]
                    or original != recovery["original_review"]):
                raise ValueError("Original recovery evidence changed during review")
            expected_ids.add(attempt["review_id"])
        if len(histories) != len(expected_ids) or {r["review_id"] for r in histories} != expected_ids:
            raise ValueError("Additional or conflicting review history requires manual inspection")
        branch = plan.to_dict()["expected_branch"]
        origin = repository.verify_origin()
        binding = self.store.executions.binding(batch_id)
        if (repository.path != lock.workspace or not binding or binding["workspace"] != str(lock.workspace)
                or binding["fingerprint"] != plan.fingerprint
                or binding["origin"] != hashlib.sha256(origin.encode("utf-8")).hexdigest()
                or commit.branch != branch or repository.commit_url(commit.commit_sha) != commit.github_commit_url
                or not commit.github_commit_url):
            raise ValueError("Saved workspace, GitHub origin or approved branch identity differs")
        if recovery is not None and binding != recovery["original_binding"]:
            raise ValueError("Saved workspace binding changed during recovery")
        shas = []
        baseline = binding["initial_sha"]
        for earlier in definitions[:index]:
            saved = self.store.executions.get(earlier["task_id"])
            previous = self.store.get_commit(earlier["task_id"])
            if (self.store.get_task(earlier["task_id"]).status != TaskStatus.APPROVED
                    or not saved or saved.get("stage") != "finalized" or not previous
                    or previous.push_state != PushState.VERIFIED or previous.branch != branch
                    or saved.get("fingerprint") != plan.fingerprint or saved.get("baseline_sha") != baseline
                    or saved.get("local_sha") != previous.commit_sha or saved.get("remote_sha") != previous.commit_sha
                    or not _approval(self.store.reviews.get(saved.get("review_id", "")),
                                     earlier["task_id"], previous.commit_sha, earlier["acceptance_criteria"])):
                raise ValueError("Earlier tasks lack a consistent finalized approval chain")
            baseline = previous.commit_sha
            shas.append(baseline)
        if execution.get("baseline_sha") != baseline:
            raise ValueError("Implementation parent differs from the recorded prior state")
        for later in definitions[index + 1:]:
            later_id = later["task_id"]
            if (self.store.get_task(later_id).status != TaskStatus.QUEUED
                    or self.store.executions.get(later_id) or self.store.get_commit(later_id)):
                raise ValueError("Later task execution or unapproved history prevents recovery")
        repository.validate_execution(branch, definition["permitted_files"])
        repository.verify_linear_history(binding["initial_sha"], [*shas, commit.commit_sha])
        repository.verify_remote_commit(branch, commit.commit_sha, origin)
        tests = _tests(execution, definition)
        packet = build_review_packet(self.store, task_id, lock.workspace, commit.commit_sha, tests,
                                     tests_commit_sha=commit.commit_sha, safety_passed=True)
        return plan, task, execution, original, packet, binding

    def run(self, batch_id: str, directory: str | Path, *, task_id: str,
            execute: bool = False) -> RecoveryResult:
        claimed = None
        persistence_uncertain = False
        sha = original_id = review_id = None
        sizing = None
        try:
            if type(execute) is not bool:
                raise ValueError("Explicit boolean recovery authorization is required")
            repository = self.controller.single.repository_factory(directory)
            with self.controller.single.lock_factory(directory) as lock:
                try:
                    plan, task, execution, original, packet, binding = self._inspect(
                        batch_id, task_id, repository, lock)
                    sha, original_id = packet.commit_sha, original["review_id"]
                    try:
                        planned = plan_chunks(packet, self.config)
                        expected, sizing = planned.chunks, planned.diagnostics
                    except ReviewError as exc:
                        sizing = exc.diagnostics
                        if not execute:
                            raise
                        expected = None  # Reviewer records a new UNCERTAIN attempt, without I/O.
                    if not execute:
                        return RecoveryResult(batch_id, task_id, True, False, sha, original_id,
                                              diagnostics=sizing)
                    if (self.controller.reviewer_factory is GroqReviewer
                            and not os.environ.get("GROQ_API_KEY", "").strip()):
                        raise ValueError("GROQ_API_KEY is unavailable; no recovery was claimed")
                    claimed = self.store.claim_review_recovery(
                        plan, task, execution, original, asdict(self.config), binding)
                    with self.controller.reviewer_factory(self.store, config=self.config) as reviewer:
                        result = reviewer.review(packet, recovery_id=claimed["recovery_id"])
                    if not isinstance(result, ReviewResult):
                        raise ValueError("Recovery reviewer returned an invalid result")
                    review_id = result.review_id
                    persisted = self.store.reviews.get(review_id)
                    if (not persisted or persisted.get("recovery_id") != claimed["recovery_id"]
                            or any(encoded(persisted.get(key)) != encoded(value)
                                   for key, value in asdict(result).items())):
                        raise ValueError("Recovery result differs from durable review history")
                    if result.decision == "APPROVED" and (
                        not result.eligible_to_proceed or expected is None
                        or persisted.get("chunks") != [
                            {"chunk_id": chunk.chunk_id, "coverage": list(chunk.coverage)} for chunk in expected]
                        or not _approval(persisted, task_id, sha, task.acceptance_criteria)
                    ):
                        raise ValueError("Recovery approval lacks exact complete evidence coverage")
                    # Reverify source bytes, parent chain, remote SHA and all durable identity.
                    current = self._inspect(batch_id, task_id, repository, lock, recovery=claimed)
                    if current[0] != plan or current[4] != packet:
                        raise ValueError("Frozen plan or review evidence changed during recovery")
                    self.store.finish_review_recovery(task_id, claimed["recovery_id"], review_id, persisted)
                    return RecoveryResult(
                        batch_id, task_id, result.eligible_to_proceed, True, sha, original_id,
                        claimed["recovery_id"], review_id, result.decision,
                        None if result.eligible_to_proceed else result.error or "Recovery review did not approve; task blocked",
                        diagnostics=sizing,
                    )
                except BaseException as exc:
                    if claimed:
                        try:
                            self.store.block_review_recovery(task_id, claimed["recovery_id"])
                        except Exception:
                            persistence_uncertain = True
                            exc.add_note("Recovery audit persistence failed; inspect durable intent before any action")
                    raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            error = str(exc) if isinstance(exc, (ValueError, GitError, WorkspaceLockError)) else (
                f"Recovery failed ({type(exc).__name__}); inspect private history before any further action"
            )
            if persistence_uncertain:
                error += "; audit persistence failed; recovery state is uncertain"
            return RecoveryResult(batch_id, task_id, False, claimed is not None, sha, original_id,
                                  claimed["recovery_id"] if claimed else None, review_id, error=error,
                                  diagnostics=sizing)
