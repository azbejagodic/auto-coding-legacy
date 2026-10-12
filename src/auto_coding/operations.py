"""Allowlisted operational events; logging never controls application results."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import inspect
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

from .review_security import redact


logger = logging.getLogger("auto_coding.operations")
logger.addHandler(logging.NullHandler())
_context: ContextVar[dict] = ContextVar("operations_context", default={})
_TEXT = {"batch_id", "task_id", "operation_id", "span_id", "parent_span_id", "stage", "outcome",
         "commit_sha", "local_sha", "remote_sha", "review_id", "original_review_id", "recovery_id",
         "chunk_id", "model", "reasoning_effort", "fingerprint", "decision", "final_status", "error_type"}
_NUMBERS = {"duration_seconds", "chunk_index", "chunk_count", "command_index", "command_count",
            "exit_code", "http_status", "estimated_input_tokens", "estimated_output_tokens",
            "estimated_total_tokens", "wire_units", "input_tokens", "output_tokens", "total_tokens",
            "unknown_usage_requests", "blocking_issue_count", "unchecked_requirement_count"}
_FLAGS = {"success", "completed", "executed", "timed_out", "state_uncertain", "coverage_complete",
          "push_verified", "workspace_busy", "cleanup_failed"}
# Diagnostic text must be a source-owned literal, never provider findings or an
# exception containing user paths/prompts. Unknown text is deliberately withheld.
_REASONS = {
    "Review incomplete", "Unsafe or inconsistent review evidence",
    "Provider rate limit/reset prevents another request", "Groq authentication configuration unavailable",
    "Task or commit state changed during review", "Sensitive provider response withheld",
    "Provider returned a different model", "Missing or ambiguous response choices",
    "Truncated, refused or unexpected review response", "Invalid review response fields",
    "Review task or full commit SHA mismatch", "Invalid review decision or confidence",
    "Invalid review finding types", "Response claims unknown requirements",
    "Contradictory or unsupported approval", "Rejection must identify a blocking issue", "Malformed review JSON",
    "Duplicate JSON response field", "Invalid task, commit, branch or template binding",
    "Missing approved requirements", "Missing code or other required evidence",
    "Missing or failed Python-controlled tests or safety checks", "Invalid Python-controlled test evidence",
    "Missing Python-controlled test command arguments", "Invalid changed-file evidence",
    "Unsupported change type", "Unsupported or incomplete patch", "Sensitive evidence withheld; review refused",
    "Sensitive review instructions withheld", "Unsupported diff hunk header", "Unsupported diff hunk body",
    "Incomplete diff hunk coverage", "Patch has insufficient reviewable context", "Ambiguous changed-file coverage",
    "Essential review evidence exceeds the safe request budget", "Excessive review chunks",
    "Final review request exceeds the safe request budget", "Incomplete or duplicate diff coverage",
    "Total review allowance would be exceeded", "Diff metadata exceeds the safe request budget",
    "A diff line or required hunk context exceeds the safe request budget", "Incomplete diff fragment coverage",
    "Approved task definition or task state differs from evidence", "Frozen plan fingerprint differs from review evidence",
    "Frozen task configuration differs from review evidence", "Provider token capacity is invalid",
    "Provider token capacity is unavailable", "Local tokenizer vocabulary integrity check failed",
    "No verified local tokenizer for the selected review model",
    "authentication", "permission", "quota_exhausted", "rate_limit", "timeout", "network",
    "provider_http_error", "unexpected_provider_error", "Incomplete review coverage",
    "Provider returned UNCERTAIN", "Approved acceptance criteria were not all checked",
    "Unrecognized diagnostic withheld",
    "Git command timed out", "Git launch failed", "Git command failed",
    "Authentication or permission failure", "Network or remote access failure",
    "Codex CLI was not found on PATH", "Codex execution timed out",
    "Prompt must be a nonempty string", "Model must be a nonempty identifier without NUL characters",
    "Timeout must be a finite positive number of seconds", "Unsupported reasoning effort",
    "Working directory does not exist or is not a directory", "Working directory could not be accessed",
    "Codex resolves to a Windows batch shim; put the native codex.exe on PATH",
    "Codex emitted an error or failed turn event", "Codex output contained malformed JSONL events",
    "Codex output did not contain a completed turn", "Test command timed out", "Test executable was not found",
    "Task does not exist", "Task already produced a commit; manual recovery/review is required",
    "Only a queued task may be executed; manual recovery is required for interrupted tasks",
    "Task is no longer queued or another execution already claimed it",
}


def _label(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 160 or not re.fullmatch(r"[A-Za-z0-9._/:-]+", value):
        return "[WITHHELD]"
    safe, changed = redact(value)
    if any(secret and secret in value for key, secret in os.environ.items()
           if re.search(r"(?i)(?:key|token|password|passwd|secret|credential)", key)):
        changed = True
    return "[WITHHELD]" if changed else safe


def safe_reason(value: object) -> str | None:
    return None if value is None else value if isinstance(value, str) and value in _REASONS else "Unrecognized diagnostic withheld"


def _safe(fields: dict) -> dict:
    result = {}
    for key, value in fields.items():
        if key in _TEXT:
            result[key] = _label(value)
        elif key in _NUMBERS and (value is None or type(value) in (int, float) and math.isfinite(value)):
            result[key] = value
        elif key in _FLAGS and type(value) is bool:
            result[key] = value
        elif key == "reason":
            result[key] = safe_reason(value)
        elif key in ("provider_error_type", "provider_error_code", "rate_limits", "rate_limits_observed_at"):
            # Reuse the provider's strict header/code allowlists without importing
            # integrations during module initialization (or logging raw bodies).
            from .integrations.groq import _error_label, rate_headers
            if key == "rate_limits":
                result[key] = rate_headers(value) if isinstance(value, dict) else {}
            elif key == "rate_limits_observed_at":
                try:
                    observed = datetime.fromisoformat(value)
                    result[key] = observed.astimezone(timezone.utc).isoformat() if observed.utcoffset() is not None else None
                except (ValueError, TypeError):
                    result[key] = None
            else:
                result[key] = _error_label(value)
    return result


def event(name: str, *, stage: str, outcome: str, duration_seconds: float = 0.0, **fields) -> None:
    """No arbitrary messages, payloads or tracebacks; handler failures are inert."""
    try:
        data = _safe({**_context.get(), **fields, "stage": stage, "outcome": outcome,
                      "duration_seconds": duration_seconds})
        data.update(timestamp=datetime.now(timezone.utc).isoformat(), event=_label(name))
        for key in ("batch_id", "task_id"):
            data.setdefault(key, None)
        logger.info("operational event", extra={"operation_event": data})
    except Exception:
        # Observability must not change safety decisions, state or retries.
        pass


def correlate(**fields) -> None:
    """Enrich the current observed call; its enclosing context resets on exit."""
    try:
        _context.set({**_context.get(), **_safe(fields)})
    except Exception:
        pass


@contextmanager
def operation_context(**fields):
    try:
        safe = _safe(fields)
    except Exception:
        safe = {}
    token = _context.set({**_context.get(), **safe})
    try:
        yield
    finally:
        _context.reset(token)


def observed(name: str, **bindings: str):
    """Observe existing synchronous entry points without changing their results."""
    def decorate(function):
        signature = inspect.signature(function)
        @wraps(function)
        def wrapped(*args, **kwargs):
            values = {}
            # Instrumentation failures must never prevent the original call.
            try:
                arguments = signature.bind_partial(*args, **kwargs).arguments
                for field, path in bindings.items():
                    root, *attributes = path.split(".")
                    value = arguments.get(root)
                    for attribute in attributes:
                        value = getattr(value, attribute, None)
                    if value is not None:
                        values[field] = value
            except Exception:
                pass
            parent = _context.get()
            values.update(operation_id=parent.get("operation_id", uuid4().hex), span_id=uuid4().hex,
                          parent_span_id=parent.get("span_id"))
            started = time.monotonic()
            with operation_context(**values):
                event(name + ".started", stage=name, outcome="started")
                try:
                    result = function(*args, **kwargs)
                except BaseException as exc:
                    event(name + ".failed", stage=name, outcome="interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed",
                          duration_seconds=time.monotonic() - started, error_type=type(exc).__name__)
                    raise
                fields, outcome = {}, "unknown"
                try:
                    fields = {key: value for key in
                              ("commit_sha", "review_id", "original_review_id", "recovery_id", "final_status", "exit_code",
                               "success", "completed", "executed", "timed_out", "state_uncertain", "push_verified", "workspace_busy")
                              if (value := getattr(result, key, None)) is not None}
                    fields["reason"] = safe_reason(getattr(result, "error", None))
                    task_id = getattr(result, "task_id", None) or getattr(result, "current_task", None)
                    if task_id is not None:
                        fields["task_id"] = task_id
                    outcome = getattr(result, "decision", None) or ("completed" if getattr(result, "success", True) else "failed")
                except Exception:
                    pass
                event(name + ".finished", stage=name, outcome=outcome,
                      duration_seconds=time.monotonic() - started, **fields)
                return result
        return wrapped
    return decorate


class _JSON(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(record.operation_event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class _Progress(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        item = record.operation_event
        return f"[{item['batch_id'] or '-'} / {item['task_id'] or '-'}] {item['stage']}: {item['outcome']} ({item['duration_seconds']:.2f}s)"


class _QuietErrors:
    def handleError(self, record: logging.LogRecord) -> None:
        # logging's default error report includes record data. Never emit it.
        try:
            if not getattr(self, "_warned", False):
                self._warned = True
                sys.stderr.write("Operational logging write failed; execution continues.\n")
        except Exception:
            pass


class _File(_QuietErrors, RotatingFileHandler):
    pass


class _Console(_QuietErrors, logging.StreamHandler):
    pass


def configure_operations(log_directory: str | Path = "runtime/logs", *, max_bytes: int = 5_000_000,
                         backup_count: int = 3) -> None:
    """Opt-in setup for execution; read-only commands never create log files.

    File failures leave terminal progress available and never block execution.
    The dedicated logger never captures free-form library/root messages.
    """
    try:
        if any(isinstance(handler, _Console) for handler in logger.handlers):
            return
        logger.setLevel(logging.INFO)
        logger.propagate = False
        console = _Console()
        console.setFormatter(_Progress())
        logger.addHandler(console)
        try:
            directory = Path(log_directory)
            directory.mkdir(parents=True, exist_ok=True)
            handler = _File(directory / "operations.jsonl", maxBytes=max_bytes, backupCount=backup_count,
                            encoding="utf-8")
            handler.setFormatter(_JSON())
            logger.addHandler(handler)
        except Exception:
            console.stream.write("Operational file logging unavailable; continuing with terminal progress.\n")
    except Exception:
        pass
