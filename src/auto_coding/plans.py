"""Offline task-plan validation and canonical, immutable definitions.

This module does not resolve paths, discover executables, expand placeholders,
or import any execution/integration module.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any


class PlanValidationError(ValueError):
    """Invalid input; messages identify fields, never echo private values."""


class PlanConflictError(ValueError):
    """An import conflicts with an existing batch or task."""


class PlanExecutionUnavailableError(ValueError):
    """The legacy controller cannot safely execute frozen plan tasks."""


@dataclass(frozen=True)
class FrozenPlan:
    """Canonical JSON is the snapshot; to_dict returns a detached copy."""

    canonical_json: str

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self.canonical_json)

    @property
    def batch_id(self) -> str:
        return self.to_dict()["batch_id"]

    @property
    def task_count(self) -> int:
        return len(self.to_dict()["tasks"])


def _error(location: str, detail: str) -> None:
    raise PlanValidationError(f"{location}: {detail}")


def _text(value: Any, location: str, *, multiline: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        _error(location, "a nonempty string is required")
    if any(ord(char) < 32 and (not multiline or char not in "\r\n\t") for char in value):
        _error(location, "control characters are forbidden")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _error(location, "invalid Unicode")
    return value


def _identifier(value: Any, location: str) -> str:
    value = _text(value, location)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        _error(location, "use a simple identifier of at most 128 characters")
    return value


def _fields(value: Any, required: set[str], optional: set[str], location: str) -> None:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        _error(location, "missing or unknown fields")


def _strings(value: Any, location: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        _error(location, "a list of strings is required")
    for index, item in enumerate(value):
        _text(item, f"{location}[{index}]", multiline=True)
    return value


def _branch(value: Any) -> None:
    branch = _text(value, "expected_branch")
    if (branch.casefold() in {"main", "master", "head", "@"} or branch.startswith(("-", "/"))
            or branch.endswith(("/", ".")) or ".." in branch or "@{" in branch
            or any(ord(char) == 127 or char.isspace() or char in "~^:?*[\\" for char in branch)
            or any(not part or part.startswith(".") or part.casefold().endswith(".lock")
                   for part in branch.split("/")) or branch.startswith("refs/")):
        _error("expected_branch", "a valid, unprotected short development branch is required")


def _file_path(value: Any, location: str) -> None:
    raw = _text(value, location)
    # Check Windows spelling even on POSIX. Preserve the supplied spelling.
    path = raw.replace("\\", "/")
    parts = path.split("/")
    if (path.startswith(("/", "-")) or any(char in path for char in ':*?[]<>"|{}')
            or any(part in {"", ".", ".."} or part.endswith((" ", ".")) for part in parts)
            or any(re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p) for p in parts)):
        _error(location, "an explicit portable repository-relative file path is required")
    # Match the existing Git protection conservatively, without filesystem/Git I/O.
    sensitive_parts = {".git", ".codex", ".aws", ".ssh", ".auth", "credentials", "secrets",
                       "browser-profile", "browser_profiles", "browser-profiles"}
    sensitive_names = {"auth.json", "credentials.json", "storage-state.json", "storage_state.json",
                       "cookies.json", "cookies", "login data", "token.json", "tokens.json",
                       "session.json", "sessions.json", ".netrc", "_netrc", ".npmrc", ".pypirc",
                       "credentials.yml", "credentials.yaml", "id_rsa", "id_ed25519", "config.toml"}
    if (any(part.casefold() in sensitive_parts for part in parts)
            or parts[-1].casefold() in sensitive_names or parts[-1].casefold().startswith(".env")
            or Path(parts[-1]).suffix.casefold() in {".pem", ".key", ".p12", ".pfx"}):
        _error(location, "protected or credential-sensitive paths are forbidden")


def _placeholders(prompt: str, approved: list[str], location: str) -> None:
    known = {"BRANCH", "LATEST_SHA"}
    if len(set(approved)) != len(approved) or any(item not in known for item in approved):
        _error(location, "only unique BRANCH and LATEST_SHA approvals are supported")
    names = re.findall(r"\{\{([A-Z][A-Z0-9_]*)\}\}", prompt)
    remainder = re.sub(r"\{\{([A-Z][A-Z0-9_]*)\}\}", "", prompt)
    if "{{" in remainder or "}}" in remainder or any(name not in approved for name in names):
        _error(location, "prompt contains malformed, unknown, or unapproved placeholders")


def validate_plan(data: Any) -> FrozenPlan:
    """Validate the entire batch without side effects and snapshot all fields."""
    # Validate a private snapshot, not caller-owned containers that could change
    # between individual field checks and final canonical serialization.
    data = deepcopy(data)
    _fields(data, {"schema_version", "batch_id", "expected_branch", "tasks"}, set(), "plan")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        _error("schema_version", "only integer version 1 is supported")
    _identifier(data["batch_id"], "batch_id")
    _branch(data["expected_branch"])
    tasks = data["tasks"]
    if not isinstance(tasks, list) or not tasks:
        _error("tasks", "a nonempty ordered list is required")
    required = {"task_id", "title", "prompt", "model", "reasoning_effort", "task_order",
                "acceptance_criteria", "dependencies", "permitted_files", "test_commands"}
    positions: dict[str, int] = {}
    previous_order = 0
    for index, task in enumerate(tasks):
        location = f"tasks[{index}]"
        _fields(task, required, {"approved_placeholders", "execution_limits"}, location)
        identity = _identifier(task["task_id"], location + ".task_id")
        if identity in positions:
            _error(location, "duplicate task ID")
        order = task["task_order"]
        if type(order) is not int or order <= previous_order or order > 2**63 - 1:
            _error(location + ".task_order", "strictly increasing positive SQLite integers are required")
        previous_order = order
        for field in ("title", "prompt", "model"):
            _text(task[field], location + "." + field, multiline=field == "prompt")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", task["model"]):
            _error(location + ".model", "a literal model identifier is required")
        if task["reasoning_effort"] not in ("minimal", "low", "medium", "high", "xhigh"):
            _error(location + ".reasoning_effort", "unsupported reasoning effort")
        _strings(task["acceptance_criteria"], location + ".acceptance_criteria", nonempty=True)
        dependencies = _strings(task["dependencies"], location + ".dependencies")
        if len(set(dependencies)) != len(dependencies) or any(dep not in positions for dep in dependencies):
            _error(location + ".dependencies", "dependencies must uniquely reference earlier tasks in this batch")
        positions[identity] = index  # Forward/self references (and therefore cycles) are forbidden.
        files = _strings(task["permitted_files"], location + ".permitted_files", nonempty=True)
        for path in files:
            _file_path(path, location + ".permitted_files")
        if len({path.replace("\\", "/").casefold() for path in files}) != len(files):
            _error(location + ".permitted_files", "duplicate or Windows-equivalent paths")
        commands = task["test_commands"]
        if not isinstance(commands, list) or not commands:
            _error(location + ".test_commands", "a nonempty list of argument arrays is required")
        for command in commands:
            if (not isinstance(command, list) or not command or any(
                    not isinstance(arg, str) or "\x00" in arg for arg in command)
                    or not command[0].strip()):
                _error(location + ".test_commands", "invalid test argument array")
            for arg in command:
                if "{{" in arg or "}}" in arg:
                    _error(location + ".test_commands", "placeholders are supported only in prompts")
        approved = _strings(task.get("approved_placeholders", []), location + ".approved_placeholders")
        _placeholders(task["prompt"], approved, location + ".approved_placeholders")
        if "execution_limits" in task:
            limits = task["execution_limits"]
            _fields(limits, {"codex_timeout", "test_timeout"}, set(), location + ".execution_limits")
            for value in limits.values():
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not 0 < value <= 86400 or not math.isfinite(value)):
                    _error(location + ".execution_limits", "timeouts must be finite seconds in (0, 86400]")
    try:
        canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        canonical.encode("utf-8")
    except (ValueError, TypeError, UnicodeEncodeError):
        _error("plan", "definition cannot be serialized as UTF-8 JSON")
    return FrozenPlan(canonical)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            _error("JSON", "duplicate object keys are forbidden")
        result[key] = value
    return result


def load_plan(path: str | Path) -> FrozenPlan:
    """Read UTF-8 JSON only. No database, log, directory, or process creation."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
        def invalid_constant(_value: str) -> None:
            _error("JSON", "non-finite numbers are forbidden")
        data = json.loads(text, object_pairs_hook=_object, parse_constant=invalid_constant)
        return validate_plan(data)
    except PlanValidationError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise PlanValidationError("Plan file cannot be read as valid UTF-8 task-plan JSON") from None
