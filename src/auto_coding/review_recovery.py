"""Explicit, one-attempt recovery of a verified commit's technical review stop."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import math
import os
from pathlib import Path
import re
from functools import partial
from typing import TYPE_CHECKING

from .batch_controller import _approval
from .operations import event, observed, operation_context
from .integrations.git import GitError, GitRepository
from .integrations.groq import GroqReviewer, rate_blocked
from .integrations.groq_limits import provider_plan, request_tokens
from .plans import FrozenPlan
from .review_evidence import build_review_packet
from .reviews import (
    ReviewConfig, ReviewDiagnostics, ReviewError, ReviewPacket, ReviewResult, SHA,
    encoded, plan_chunks, request_arguments, template, validate_response,
)
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
LOCAL_RATE_BLOCK = "Provider rate limit/reset prevents another request"


@dataclass(frozen=True)
class CompatibilityRetry:
    """Explicit binding for ONE second recovery after the legacy store error."""

    failed_review_id: str
    fingerprint: str
    commit_sha: str
    branch: str

    def __post_init__(self) -> None:
        if (not all(isinstance(value, str) for value in (
                    self.failed_review_id, self.fingerprint, self.commit_sha, self.branch))
                or not re.fullmatch(r"[0-9a-f]{32}", self.failed_review_id)
                or not re.fullmatch(r"[0-9a-f]{64}", self.fingerprint)
                or not SHA.fullmatch(self.commit_sha) or not self.branch
                or self.branch.casefold() in ("main", "master")):
            raise ValueError("Exact failed review, fingerprint, commit and development branch are required")


class TechnicalContinuation(CompatibilityRetry):
    """Explicit identity binding to one completed, locally blocked review."""


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


def _compatibility_failure(previous: dict, execution: dict, failed: dict,
                           packet: ReviewPacket) -> None:
    """Fail closed: one completed non-review HTTP failure of the old request."""
    base = previous["original_execution"]
    mutable = {"review_id", "review_status", "recovery_id", "failure_stage", "error",
               "ended_at", "updated_at"}
    if ({k: v for k, v in execution.items() if k not in mutable | {"checkpoints"}}
            != {k: v for k, v in base.items() if k not in mutable | {"checkpoints"}}
            or execution.get("checkpoints", [])[:-1] != base["checkpoints"]
            or execution["checkpoints"][-1].get("stage") != "stopped"
            or execution.get("review_id") != previous["review_id"]
            or execution.get("recovery_id") != previous["recovery_id"]
            or execution.get("review_status") != "UNCERTAIN"
            or execution.get("failure_stage") != "review_recovery"
            or execution.get("error") != "Recovery review did not provide complete approval"):
        raise ValueError("The completed first recovery checkpoint is inconsistent")
    if (failed.get("provider") != "GroqCloud" or failed.get("model") != "openai/gpt-oss-120b"
            or failed.get("reasoning_effort") != "medium"
            or failed.get("decision") != "UNCERTAIN" or failed.get("confidence") != "LOW"
            or failed.get("coverage_complete") is not False or failed.get("error") != "provider_http_error"
            or failed.get("recovery_id") != previous["recovery_id"]
            or failed.get("original_review_id") != previous["original_review_id"]
            or failed.get("recovery_timestamp") != previous["started_at"]
            or failed.get("config") != previous["config"]
            or type(failed.get("api_requests")) is not int or failed["api_requests"] != 1
            or type(failed.get("chunk_count")) is not int or failed["chunk_count"] != 1
            or len(failed.get("requests", [])) != 1
            or failed.get("requirements_checked") != [] or failed.get("blocking_issues") != []
            or failed.get("missing_evidence") != []
            or failed.get("needs_final_review") != ["Not all approved acceptance criteria were demonstrably checked."]
            or failed.get("usage") != {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            or type(failed.get("unknown_usage_requests")) is not int or failed["unknown_usage_requests"] != 1):
        raise ValueError("Previous recovery is not the single-request legacy API compatibility failure")
    config = ReviewConfig(**previous["config"])
    instructions = template()
    chunks = plan_chunks(packet, config).chunks
    if (len(chunks) != 1 or failed.get("template_sha256") != hashlib.sha256(instructions.encode()).hexdigest()
            or failed.get("chunks") != [{"chunk_id": chunks[0].chunk_id, "coverage": list(chunks[0].coverage)}]):
        raise ValueError("Failed API request is not bound to the exact complete frozen evidence")
    request = failed["requests"][0]
    legacy_arguments = {**request_arguments(config, instructions, chunks[0].content), "store": False}
    status = request.get("http_status")
    if (request.get("submitted") != legacy_arguments or request.get("completed") is not True
            or request.get("error") != "provider_http_error" or request.get("response") is not None
            or request.get("response_text") is not None or request.get("usage") is not None
            or request.get("decision") != "UNCERTAIN" or request.get("confidence") != "LOW"
            or request.get("chunk_id") != chunks[0].chunk_id or request.get("coverage") != list(chunks[0].coverage)
            or request.get("max_retries") != 0 or request.get("request_timeout") != config.timeout
            or (status is not None and (type(status) is not int or status not in (400, 422)))):
        raise ValueError("Failed request is ambiguous, not the completed unsupported-store request")


def _local_failure(record: dict, packet: ReviewPacket) -> None:
    """Only unsent work after a local block; completed approving prefixes are safe.

    A new explicitly authorized review rechecks ALL chunks. No approval is copied
    and no uncertain delivery, provider judgment or unresolved finding is retried.
    """
    if (record.get("task_id") != packet.task_id or record.get("commit_sha") != packet.commit_sha
            or record.get("error") != LOCAL_RATE_BLOCK or record.get("decision") != "UNCERTAIN"
            or record.get("confidence") != "LOW" or record.get("coverage_complete") is not False
            or record.get("provider") != "GroqCloud" or record.get("model") != "openai/gpt-oss-120b"
            or record.get("reasoning_effort") != "medium" or record.get("missing_evidence") != []
            or record.get("blocking_issues") != []
            or record.get("template_sha256") != hashlib.sha256(template().encode()).hexdigest()):
        raise ValueError("Review is not a completed local capacity block without findings")
    config = ReviewConfig(**record["config"])
    if config.model != "openai/gpt-oss-120b" or config.reasoning_effort != "medium":
        raise ValueError("Historical review configuration differs from the pinned model/effort")
    cap = record.get("budget_diagnostics", {}).get("provider_token_limit")
    planned = plan_chunks(packet, config,
                          token_counter=partial(request_tokens, headroom=config.headroom) if cap else None,
                          token_limit=cap)
    chunks = planned.chunks
    requests = record["requests"]
    if (record.get("chunks") != [{"chunk_id": c.chunk_id, "coverage": list(c.coverage)} for c in chunks]
            or type(record.get("chunk_count")) is not int or record["chunk_count"] != len(chunks)
            or type(record.get("api_requests")) is not int or record["api_requests"] != len(requests)
            or len(requests) >= len(chunks)):
        raise ValueError("Local block coverage or request history is inconsistent")
    checked = []
    for index, (request, chunk) in enumerate(zip(requests, chunks), 1):
        if (request.get("completed") is not True or request.get("error") is not None
                or request.get("decision") != "APPROVED"
                or request.get("response_redacted") is True
                or request.get("request_index") != index or request.get("max_retries") != 0
                or request.get("request_timeout") != config.timeout
                or request.get("chunk_id") != chunk.chunk_id or request.get("coverage") != list(chunk.coverage)
                or request.get("submitted") != request_arguments(config, template(), chunk.content)):
            raise ValueError("Previous request delivery or approval is uncertain")
        response = request.get("response") or {}
        choices = response.get("choices", [])
        if response.get("model") != config.model or len(choices) != 1 or choices[0].get("finish_reason") != "stop":
            raise ValueError("Previous request has no complete verified model response")
        message = choices[0]["message"]
        if any(message.get(key) for key in ("refusal", "tool_calls", "function_call", "executed_tools")):
            raise ValueError("Previous model response contains unsupported evidence")
        data = validate_response(choices[0]["message"]["content"], packet)
        if data["decision"] != "APPROVED" or data["blocking_issues"] or data["needs_final_review"]:
            raise ValueError("Unresolved model findings cannot be retried")
        checked.extend(data["requirements_checked"])
    checked = list(dict.fromkeys(checked))
    expected_notes = (["Multi-chunk approval does not guarantee cross-file architectural correctness."]
                      if len(chunks) > 1 else [])
    if any(item not in checked for item in packet.acceptance_criteria):
        expected_notes.append("Not all approved acceptance criteria were demonstrably checked.")
    if record.get("requirements_checked") != checked or record.get("needs_final_review") != expected_notes:
        raise ValueError("Unresolved or inconsistent local-block findings require manual inspection")


def _stopped_after(event: dict, current: dict) -> None:
    base = event["original_execution"]
    mutable = {"review_id", "review_status", "recovery_id", "failure_stage", "error", "ended_at", "updated_at", "checkpoints"}
    if ({k: v for k, v in current.items() if k not in mutable}
            != {k: v for k, v in base.items() if k not in mutable}
            or current.get("checkpoints", []) != [*base["checkpoints"], current.get("checkpoints", [{}])[-1]]
            or current["checkpoints"][-1].get("stage") != "stopped"
            or current.get("review_id") != event["review_id"] or current.get("recovery_id") != event["recovery_id"]
            or current.get("review_status") != "UNCERTAIN" or current.get("failure_stage") != "review_recovery"
            or current.get("error") != "Recovery review did not provide complete approval"):
        raise ValueError("Completed continuation execution checkpoints are inconsistent")


class ReviewRecovery:
    """Reuse BatchController dependencies/lock, never its coding/test/push loop."""

    def __init__(self, controller: BatchController):
        self.controller = controller
        self.store = controller.store
        self.config = controller.review_config
        if self.config.model != "openai/gpt-oss-120b" or self.config.reasoning_effort != "medium":
            raise ValueError("Review recovery requires openai/gpt-oss-120b with medium reasoning")

    def _inspect(self, batch_id: str, task_id: str, repository: GitRepository,
                 lock: WorkspaceLock, *, recovery: dict | None = None,
                 compatibility: CompatibilityRetry | None = None,
                 continuation: TechnicalContinuation | None = None
                 ) -> tuple[FrozenPlan, Task, dict, dict, ReviewPacket, dict, dict | None]:
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
        persisted_execution = execution
        commit = self.store.get_commit(task_id)
        previous = self.store.executions.recovery(task_id)
        second = self.store.executions.recovery(task_id, compatibility=True)
        failed = None
        context = None
        if continuation is not None:
            audits = self.store.executions.recoveries(task_id)
            if recovery is not None:
                if not audits or audits[-1]["recovery_id"] != recovery["recovery_id"] or audits[-1]["stage"] != "reviewing":
                    raise ValueError("Continuation claim differs from the active review")
                audits = audits[:-1]
            histories = self.store.reviews.list(task_id)
            prior_reviews = histories[:-1] if recovery is not None else histories
            if (not commit or not persisted_execution or not prior_reviews
                    or continuation.fingerprint != plan.fingerprint or continuation.commit_sha != commit.commit_sha
                    or continuation.branch != plan.to_dict()["expected_branch"]
                    or continuation.failed_review_id != persisted_execution.get("review_id")
                    or prior_reviews[-1]["review_id"] != continuation.failed_review_id
                    or len(prior_reviews) != len(audits) + 1):
                raise ValueError("Continuation authorization or review chain differs")
            for position, event in enumerate(audits):
                if (event.get("stage") != "finished" or event.get("decision") != "UNCERTAIN"
                        or event.get("task_id") != task_id or event.get("batch_id") != batch_id
                        or event.get("commit_sha") != commit.commit_sha
                        or event.get("original_task") != asdict(task) or event.get("original_commit") != asdict(commit)
                        or event.get("original_review") != prior_reviews[position]
                        or event.get("original_review_id") != prior_reviews[position]["review_id"]
                        or event.get("review_id") != prior_reviews[position + 1]["review_id"]
                        or prior_reviews[position + 1].get("recovery_id") != event["recovery_id"]
                        or prior_reviews[position + 1].get("config") != event["config"]
                        or prior_reviews[position + 1].get("recovery_timestamp") != event["started_at"]
                        or (event.get("technical_continuation") and
                            (event["prior_recoveries"] != self.store.executions.signatures(audits[:position])
                             or event["previous_reviews"] != prior_reviews[:position + 1]))
                        or (event.get("compatibility_retry") and event["previous_recovery"] != audits[position - 1])):
                    raise ValueError("Previous recovery audit or review history is inconsistent")
                _stopped_after(event, audits[position + 1]["original_execution"]
                               if position + 1 < len(audits) else persisted_execution)
            context = {"prior_recoveries": self.store.executions.signatures(audits), "previous_reviews": prior_reviews}
            failed = prior_reviews[-1]
            execution = audits[0]["original_execution"] if audits else persisted_execution
            attempt = self.store.executions.recovery_by_id(task_id, recovery["recovery_id"]) if recovery else None
        elif compatibility is not None:
            if (compatibility.fingerprint != plan.fingerprint or not commit
                    or compatibility.commit_sha != commit.commit_sha
                    or compatibility.branch != plan.to_dict()["expected_branch"]
                    or not previous or previous.get("stage") != "finished"
                    or previous.get("decision") != "UNCERTAIN"
                    or previous.get("review_id") != compatibility.failed_review_id
                    or previous.get("batch_id") != batch_id or previous.get("task_id") != task_id
                    or previous.get("commit_sha") != commit.commit_sha
                    or asdict(task) != previous["original_task"]
                    or asdict(commit) != previous["original_commit"]):
                raise ValueError("Second recovery authorization or completed first attempt does not match")
            failed = self.store.reviews.get(compatibility.failed_review_id)
            if not failed or failed["task_id"] != task_id or failed["commit_sha"] != commit.commit_sha:
                raise ValueError("Exact failed review not found for the existing commit")
            execution = previous["original_execution"]
        elif second is not None or self.store.executions.continuations(task_id):
            raise ValueError("Second technical recovery already claimed; no further retries are permitted")
        if continuation is None:
            attempt = second if compatibility is not None else previous
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
        root_local = continuation is not None and original and original.get("error") == LOCAL_RATE_BLOCK
        if not root_local and (not original or original["task_id"] != task_id or original["commit_sha"] != commit.commit_sha
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
        if context is not None:
            if original != context["previous_reviews"][0]:
                raise ValueError("Original implementation review differs from continuation history")
            expected_ids = {r["review_id"] for r in context["previous_reviews"]}
        if compatibility is not None:
            if original != previous["original_review"]:
                raise ValueError("Original preparation failure changed after the first recovery")
            expected_ids.add(failed["review_id"])
        if recovery is not None:
            if (persisted_execution != recovery["original_execution"]
                    or (failed if compatibility is not None or continuation is not None else original) != recovery["original_review"]
                    or (compatibility is not None and previous != recovery["previous_recovery"])):
                raise ValueError("Original recovery evidence changed during review")
            if context is not None and any(context[k] != recovery[k] for k in context):
                raise ValueError("Continuation history changed during review")
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
        if compatibility is not None and binding != previous["original_binding"]:
            raise ValueError("Saved workspace binding changed after the first recovery")
        if context is not None and any(event["original_binding"] != binding for event in audits):
            raise ValueError("Saved workspace binding changed during continuation history")
        shas = []
        baseline = binding["initial_sha"]
        for earlier in definitions[:index]:
            saved = self.store.executions.get(earlier["task_id"])
            previous_commit = self.store.get_commit(earlier["task_id"])
            if (self.store.get_task(earlier["task_id"]).status != TaskStatus.APPROVED
                    or not saved or saved.get("stage") != "finalized" or not previous_commit
                    or previous_commit.push_state != PushState.VERIFIED or previous_commit.branch != branch
                    or saved.get("fingerprint") != plan.fingerprint or saved.get("baseline_sha") != baseline
                    or saved.get("local_sha") != previous_commit.commit_sha or saved.get("remote_sha") != previous_commit.commit_sha
                    or not _approval(self.store.reviews.get(saved.get("review_id", "")),
                                     earlier["task_id"], previous_commit.commit_sha, earlier["acceptance_criteria"])):
                raise ValueError("Earlier tasks lack a consistent finalized approval chain")
            baseline = previous_commit.commit_sha
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
        if compatibility is not None:
            _compatibility_failure(previous, persisted_execution, failed, packet)
        if context is not None:
            for position, review in enumerate(context["previous_reviews"]):
                if review.get("error") == LOCAL_RATE_BLOCK:
                    _local_failure(review, packet)
                elif position == 0:
                    continue  # The strict original zero-request preparation check above.
                elif position == 1 and len(audits) > 1 and audits[1].get("compatibility_retry"):
                    _compatibility_failure(audits[0], audits[1]["original_execution"], review, packet)
                else:
                    raise ValueError("A provider error or substantive judgment cannot be continued")
        return plan, task, persisted_execution, failed or original, packet, binding, context or (previous if compatibility else None)

    @observed("recovery", batch_id="batch_id", task_id="task_id")
    def run(self, batch_id: str, directory: str | Path, *, task_id: str,
            execute: bool = False, compatibility: CompatibilityRetry | None = None,
            continuation: TechnicalContinuation | None = None) -> RecoveryResult:
        claimed = None
        persistence_uncertain = False
        sha = original_id = review_id = None
        sizing = None
        try:
            if type(execute) is not bool:
                raise ValueError("Explicit boolean recovery authorization is required")
            if compatibility is not None and continuation is not None:
                raise ValueError("Choose either legacy compatibility recovery or technical continuation")
            repository = self.controller.single.repository_factory(directory)
            with self.controller.single.lock_factory(directory) as lock:
                try:
                    plan, task, execution, original, packet, binding, previous = self._inspect(
                        batch_id, task_id, repository, lock, compatibility=compatibility, continuation=continuation)
                    sha, original_id = packet.commit_sha, original["review_id"]
                    try:
                        planned = provider_plan(packet, self.config, self.store.reviews.latest_limits(self.config.model))
                        expected, sizing = planned.chunks, planned.diagnostics
                    except ReviewError as exc:
                        sizing = exc.diagnostics
                        if not execute:
                            raise
                        expected = None  # Reviewer records a new UNCERTAIN attempt, without I/O.
                    if expected and rate_blocked(self.store.reviews.latest_limits(self.config.model),
                            request_tokens(request_arguments(self.config, template(), expected[0].content),
                                           headroom=self.config.headroom)):
                        raise ValueError(LOCAL_RATE_BLOCK)
                    if not execute:
                        event("recovery.preflight", stage="recovery_preflight", outcome="eligible",
                              commit_sha=sha, original_review_id=original_id)
                        return RecoveryResult(batch_id, task_id, True, False, sha, original_id,
                                              diagnostics=sizing)
                    if (self.controller.reviewer_factory is GroqReviewer
                            and not os.environ.get("GROQ_API_KEY", "").strip()):
                        raise ValueError("GROQ_API_KEY is unavailable; no recovery was claimed")
                    claimed = self.store.claim_review_recovery(
                        plan, task, execution, original, asdict(self.config), binding,
                        previous_recovery=previous if continuation is None else None,
                        continuation=previous if continuation is not None else None)
                    event("recovery.claimed", stage="recovery_claim", outcome="completed",
                          recovery_id=claimed["recovery_id"], original_review_id=original_id, commit_sha=sha)
                    with operation_context(recovery_id=claimed["recovery_id"], original_review_id=original_id), self.controller.reviewer_factory(self.store, config=self.config) as reviewer:
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
                    current = self._inspect(batch_id, task_id, repository, lock, recovery=claimed,
                                            compatibility=compatibility, continuation=continuation)
                    if current[0] != plan or current[4] != packet:
                        raise ValueError("Frozen plan or review evidence changed during recovery")
                    self.store.finish_review_recovery(task_id, claimed["recovery_id"], review_id, persisted)
                    event("recovery.finalized", stage="recovery_finalization", outcome=result.decision,
                          recovery_id=claimed["recovery_id"], review_id=review_id, commit_sha=sha)
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
