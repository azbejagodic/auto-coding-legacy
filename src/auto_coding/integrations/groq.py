"""Bounded, fail-closed Groq quick reviews; no workflow or repository mutation."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import logging
import math
from pathlib import Path
import re
from typing import Any, TYPE_CHECKING

import groq

from ..review_history import utc_now
from ..review_security import sanitize
from ..reviews import (
    ARRAY_FIELDS, CONFIDENCES, ReviewConfig, ReviewError, ReviewPacket, ReviewResult,
    TEMPLATE_VERSION, plan_chunks, request_arguments, template, validate_response,
)

if TYPE_CHECKING:
    from ..storage import TaskStore
    from ..test_runner import TestRunResult

logger = logging.getLogger(__name__)
_LIMIT_HEADERS = {
    "retry-after", "x-ratelimit-limit-requests", "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens",
}
_ERROR_LABELS = frozenset({
    "invalid_request_error", "invalid_request", "invalid_api_key", "authentication_error",
    "permission_error", "permission_denied", "rate_limit_exceeded", "rate_limit_error",
    "insufficient_quota", "quota_exceeded", "server_error", "internal_server_error",
    "unsupported_parameter", "unknown_parameter", "invalid_parameter", "invalid_value",
    "model_not_found", "invalid_json_schema",
})


def _error_label(value: Any) -> str | int | None:
    # Error fields are untrusted too. Never persist arbitrary provider text,
    # even when disguised as a short identifier rather than a message/body.
    if value is None:
        return None
    if type(value) is int and 0 <= value <= 999999:
        return value
    if isinstance(value, str) and value in _ERROR_LABELS and not sanitize(value)[1]:
        return value
    return "unrecognized"


def rate_headers(headers: Any) -> dict[str, str]:
    values = {str(key).lower(): str(value) for key, value in headers.items()
              if str(key).lower() in _LIMIT_HEADERS}
    safe, changed = sanitize(values)
    # Never retain suspicious headers, even under an allowed header name.
    if changed:
        return {}
    for key, value in safe.items():
        if "reset" in key or key == "retry-after":
            valid = len(value) <= 64 and _seconds(value) is not None
            if not valid and key == "retry-after" and re.fullmatch(
                r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} "
                r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
                r"\d{4} \d{2}:\d{2}:\d{2} GMT", value
            ):
                try:
                    parsedate_to_datetime(value)
                    valid = True
                except (ValueError, TypeError, OverflowError):
                    pass
        else:
            valid = re.fullmatch(r"[0-9]{1,20}", value) is not None
        if not valid:
            # Keep exhaustion checks fail-closed, without saving arbitrary text.
            safe[key] = "[WITHHELD: invalid rate limit]"
    return safe


def _seconds(value: str) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except ValueError:
        pass
    if re.fullmatch(r"(?:\d+(?:\.\d+)?[dhms])+", value):
        return sum(float(number) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
                   for number, unit in re.findall(r"(\d+(?:\.\d+)?)([dhms])", value))
    return None


def rate_blocked(limits: dict, required_tokens: int) -> bool:
    """Do not retry or sleep: stop until supplied provider limits have reset."""
    if not limits:
        return False
    try:
        observed = datetime.fromisoformat(limits["observed_at"])
        now = datetime.now(timezone.utc)
        checks = []
        if "retry-after" in limits:
            value = limits["retry-after"]
            seconds = _seconds(value)
            deadline = (observed + timedelta(seconds=seconds) if seconds is not None
                        else parsedate_to_datetime(value))
            checks.append(deadline)
        for kind, required in (("requests", 1), ("tokens", required_tokens)):
            remaining = limits.get(f"x-ratelimit-remaining-{kind}")
            if remaining is not None and int(remaining) < required:
                reset = limits.get(f"x-ratelimit-reset-{kind}")
                seconds = _seconds(reset) if reset is not None else None
                if seconds is None:
                    return True  # Known exhaustion, unknown reset: human inspection.
                checks.append(observed + timedelta(seconds=seconds))
        return any(deadline > now for deadline in checks)
    except (ValueError, TypeError, KeyError, OverflowError):
        return True  # Uninterpretable provider limits are not permission to spend.


def _provider_error(exc: Exception) -> tuple[str, dict, dict]:
    limits = rate_headers(exc.response.headers) if isinstance(exc, groq.APIStatusError) else {}
    diagnostics = {}
    if isinstance(exc, groq.APIStatusError):
        body = exc.body if isinstance(exc.body, dict) else {}
        error = body.get("error", body)
        error = error if isinstance(error, dict) else {}
        status = exc.status_code
        diagnostics = {
            "http_status": status if type(status) is int and 100 <= status <= 599 else None,
            "provider_error_type": _error_label(error.get("type")),
            "provider_error_code": _error_label(error.get("code")),
        }
    if isinstance(exc, groq.AuthenticationError):
        category = "authentication"
    elif isinstance(exc, groq.PermissionDeniedError):
        category = "permission"
    elif isinstance(exc, groq.RateLimitError):
        body = exc.body if isinstance(exc.body, dict) else {}
        error = body.get("error", body)
        code = error.get("code") if isinstance(error, dict) else None
        category = "quota_exhausted" if code in ("insufficient_quota", "quota_exceeded") else "rate_limit"
    elif isinstance(exc, groq.APITimeoutError):
        category = "timeout"
    elif isinstance(exc, groq.APIConnectionError):
        category = "network"
    elif isinstance(exc, groq.APIStatusError):
        category = "provider_http_error"
    else:
        category = "unexpected_provider_error"
    return category, limits, diagnostics


def token_usage(completion: Any) -> dict | None:
    usage = getattr(completion, "usage", None)
    if usage is None:
        return None
    values = [getattr(usage, name, None) for name in
              ("prompt_tokens", "completion_tokens", "total_tokens")]
    if any(type(value) is not int or value < 0 for value in values) or values[2] != sum(values[:2]):
        return None
    return dict(zip(("input_tokens", "output_tokens", "total_tokens"), values))


class GroqReviewer:
    """One task/commit review, with a caller-owned TaskStore and SDK client.

    The default SDK is created lazily only after evidence validation. Inject a
    mocked Groq client for tests. No retries, tools, provider fallback or billing
    configuration. Persistence failures propagate: no durable approval is returned.
    """

    def __init__(self, store: TaskStore, config: ReviewConfig | None = None, *, client: Any = None):
        self.store = store
        self.config = config or ReviewConfig()
        self._client = client
        self._owns_client = client is None

    def __enter__(self) -> GroqReviewer:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def review_task(self, task_id: str, repository_path: str | Path, commit_sha: str,
                    test_results: TestRunResult | None, *, tests_commit_sha: str | None,
                    safety_passed: bool) -> ReviewResult:
        """Preferred interface: collect Git evidence, then review exactly once."""
        from .git import GitError
        from ..review_evidence import build_review_packet

        try:
            packet = build_review_packet(
                self.store, task_id, repository_path, commit_sha, test_results,
                tests_commit_sha=tests_commit_sha, safety_passed=safety_passed,
            )
        except (GitError, ValueError):
            task = self.store.get_task(task_id)
            if task is None:
                raise ReviewError("Review task does not exist") from None
            commit = self.store.get_commit(task_id)
            if commit is None or commit.commit_sha != commit_sha:
                raise ReviewError("Review commit evidence does not match") from None
            packet = ReviewPacket(
                task_id, commit_sha, commit.branch, task.prompt, tuple(task.acceptance_criteria),
                (), None, False, ("Local evidence unavailable, incomplete or unsafe",),
            )
        return self.review(packet)

    def review(self, packet: ReviewPacket, *, recovery_id: str | None = None) -> ReviewResult:
        packet = deepcopy(packet)
        task = self.store.get_task(packet.task_id)
        commit = self.store.get_commit(packet.task_id)
        instructions = template()  # One immutable snapshot for budgeting and all requests.
        # Bind the caller-owned packet to durable task/commit records.
        if (task is None or commit is None or commit.commit_sha != packet.commit_sha
                or commit.branch != packet.branch):
            raise ReviewError("Review is not bound to a persisted task and commit")
        record = {
            "provider": "GroqCloud", "model": self.config.model,
            "reasoning_effort": self.config.reasoning_effort,
            "template_version": TEMPLATE_VERSION, "config": asdict(self.config),
            "template_sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
            "missing_evidence": list(packet.missing_evidence),
            "decision": "UNCERTAIN", "confidence": "LOW", "coverage_complete": False,
            "chunk_count": 0, "error": "Review incomplete",
        }
        if recovery_id is not None:
            recovery = self.store.executions.recovery_by_id(packet.task_id, recovery_id)
            if (not recovery or recovery["recovery_id"] != recovery_id or recovery["stage"] != "claimed"
                    or recovery["commit_sha"] != packet.commit_sha or recovery["config"] != asdict(self.config)):
                raise ReviewError("Review does not match the claimed recovery")
            record.update(recovery_id=recovery_id, original_review_id=recovery["original_review_id"],
                          recovery_timestamp=recovery["started_at"])
            if recovery.get("compatibility_retry"):
                record.update(recovery_attempt=2,
                              previous_recovery_id=recovery["previous_recovery"]["recovery_id"])
        review_id = self.store.reviews.create(packet.task_id, packet.commit_sha, record)
        if recovery_id is not None:
            self.store.attach_recovery_review(packet.task_id, recovery_id, review_id)
        findings: dict[str, list[str]] = {key: [] for key in ARRAY_FIELDS}
        responses: list[dict] = []
        error = None
        chunks = ()
        requests = []
        try:
            if (task.status != "awaiting_review" or task.prompt != packet.requirements
                    or tuple(task.acceptance_criteria) != packet.acceptance_criteria):
                raise ReviewError("Approved task definition or task state differs from evidence")
            approved = self.store.get_task_plan(packet.task_id)
            if approved is not None and approved[0].fingerprint != packet.plan_fingerprint:
                raise ReviewError("Frozen plan fingerprint differs from review evidence")
            if approved is not None:
                definition = {key: value for key, value in approved[1].items()
                              if key not in ("prompt", "title", "acceptance_criteria")}
                if packet.approved_configuration != definition:
                    raise ReviewError("Frozen task configuration differs from review evidence")
            planned = plan_chunks(packet, self.config, instructions=instructions)
            chunks = planned.chunks
            record["budget_diagnostics"] = asdict(planned.diagnostics)
        except (ReviewError, ValueError) as exc:
            # Only known sanitized validation messages may be retained.
            error = str(exc) if isinstance(exc, ReviewError) else "Unsafe or inconsistent review evidence"
            if isinstance(exc, ReviewError) and exc.diagnostics is not None:
                record["budget_diagnostics"] = asdict(exc.diagnostics)

        logger.info("Groq review start: model=%s chunks=%d", self.config.model, len(chunks))
        for index, chunk in enumerate(chunks, 1):
            if error:
                break
            if rate_blocked(self.store.reviews.latest_limits(self.config.model), chunk.estimated_tokens):
                error = "Provider rate limit/reset prevents another request"
                break
            arguments = request_arguments(self.config, instructions, chunk.content)
            request = {
                "model": self.config.model, "chunk_id": chunk.chunk_id,
                "coverage": list(chunk.coverage), "estimated_tokens": chunk.estimated_tokens,
                "submitted": arguments, "response": None, "usage": None,
                "request_timeout": self.config.timeout, "max_retries": 0,
                "completed": False, "error": "Response missing", "rate_limits": {},
                "decision": "UNCERTAIN", "confidence": "LOW",
            }
            # Auth configuration failure does not count as an API request.
            if self._client is None:
                try:
                    self._client = groq.Groq(
                        timeout=self.config.timeout, max_retries=0,
                        base_url="https://api.groq.com",
                    )
                except groq.GroqError:
                    error = "Groq authentication configuration unavailable"
                    break
            self.store.reviews.start_request(review_id, index, request)
            requests.append(request)
            try:
                client = self._client.with_options(timeout=self.config.timeout, max_retries=0)
                raw = client.chat.completions.with_raw_response.create(**arguments)
                request["rate_limits"] = rate_headers(raw.headers)
                response_text = raw.text() if callable(raw.text) else raw.text
                if isinstance(response_text, str):
                    request["response_text"], request["response_redacted"] = sanitize(response_text)
                completion = raw.parse()
                request["usage"] = token_usage(completion)
                raw_data = completion.model_dump(mode="json")
                safe, redacted = sanitize(raw_data)
                request["response"] = safe
                request["response_redacted"] = redacted or request.get("response_redacted", False)
                if request["response_redacted"]:
                    raise ReviewError("Sensitive provider response withheld")
                if completion.model != self.config.model:
                    raise ReviewError("Provider returned a different model")
                if len(completion.choices) != 1:
                    raise ReviewError("Missing or ambiguous response choices")
                choice = completion.choices[0]
                if (choice.finish_reason != "stop" or getattr(choice.message, "refusal", None)
                        or choice.message.tool_calls or choice.message.function_call
                        or getattr(choice.message, "executed_tools", None) or not choice.message.content):
                    raise ReviewError("Truncated, refused or unexpected review response")
                response = validate_response(choice.message.content, packet)
                request.update({key: response[key] for key in ("decision", "confidence")})
                responses.append(response)
                for key in findings:
                    findings[key].extend(response[key])
                request["error"] = None
            except ReviewError as exc:
                error = str(exc)
                request["error"] = error
            except Exception as exc:
                category, limits, diagnostics = _provider_error(exc)
                request["rate_limits"] = limits or request["rate_limits"]
                request.update(diagnostics)
                request["error"] = error = category
            request["completed"] = True
            request["completed_at"] = utc_now()
            self.store.reviews.finish_request(review_id, index, request)
            # Rejection is final, never retry or seek an approving replacement.
            if responses and responses[-1]["decision"] == "REJECTED":
                break

        complete = bool(chunks) and len(responses) == len(chunks) and error is None
        # Never return eligible approval after durable task/commit state drift.
        current_task = self.store.get_task(packet.task_id)
        current_plan = self.store.get_task_plan(packet.task_id)
        if (self.store.get_commit(packet.task_id) != commit or current_task != task
                or (current_plan is not None and current_plan[0].fingerprint != packet.plan_fingerprint)):
            complete = False
            error = "Task or commit state changed during review"
        rejected = any(response["decision"] == "REJECTED" for response in responses)
        checked = tuple(dict.fromkeys(findings["requirements_checked"]))
        if rejected:
            decision = "REJECTED"
        elif (not complete or any(response["decision"] != "APPROVED" for response in responses)
              or any(criterion not in checked for criterion in packet.acceptance_criteria)):
            decision = "UNCERTAIN"
        else:
            decision = "APPROVED"
        confidence = min((response["confidence"] for response in responses),
                         key=CONFIDENCES.index, default="LOW")
        if not complete:
            confidence = "LOW"
        suggestions = list(dict.fromkeys(findings["needs_final_review"]))
        if len(chunks) > 1:
            suggestions.append("Multi-chunk approval does not guarantee cross-file architectural correctness.")
        if any(criterion not in checked for criterion in packet.acceptance_criteria):
            suggestions.append("Not all approved acceptance criteria were demonstrably checked.")
        result = ReviewResult(
            review_id, decision, confidence, complete, checked,
            tuple(dict.fromkeys(findings["blocking_issues"])), tuple(suggestions), error,
        )
        totals = {key: sum(request["usage"][key] for request in requests if request["usage"])
                  for key in ("input_tokens", "output_tokens", "total_tokens")}
        record.update(asdict(result))
        record.update({
            "chunk_count": len(chunks), "chunks": [
                {"chunk_id": chunk.chunk_id, "coverage": list(chunk.coverage)}
                for chunk in chunks
            ], "usage": totals, "api_requests": len(requests),
            "unknown_usage_requests": sum(request["usage"] is None for request in requests),
        })
        self.store.reviews.finish(review_id, record)
        logger.info("Groq review complete: decision=%s requests=%d error=%s",
                    decision, len(requests), error)
        return result
