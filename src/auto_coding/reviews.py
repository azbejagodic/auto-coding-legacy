"""Pure review contracts, response validation and lossless request budgeting."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from importlib.resources import files
import hashlib
import json
import math
import re
from typing import Any, Callable, Iterator

from .review_security import sanitize, validate_review_path


TEMPLATE_VERSION = "2"
_HUNK_HEADER = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n?")
SHA = re.compile(r"[0-9a-f]{40}\Z")
DECISIONS = ("APPROVED", "REJECTED", "UNCERTAIN")
CONFIDENCES = ("LOW", "MEDIUM", "HIGH")
ARRAY_FIELDS = ("requirements_checked", "blocking_issues", "needs_final_review")
RESPONSE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "task_id": {"type": "string"}, "commit_sha": {"type": "string"},
        "decision": {"type": "string", "enum": list(DECISIONS)},
        "confidence": {"type": "string", "enum": list(CONFIDENCES)},
        **{key: {"type": "array", "items": {"type": "string"}} for key in ARRAY_FIELDS},
    },
    "required": ["task_id", "commit_sha", "decision", "confidence", *ARRAY_FIELDS],
}


class ReviewError(ValueError):
    """Sanitized, predictable review failure."""

    def __init__(self, message: str, *, diagnostics: ReviewDiagnostics | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def template() -> str:
    return files("auto_coding").joinpath("templates/groq-review.md").read_text(encoding="utf-8")


@dataclass(frozen=True)
class ReviewConfig:
    model: str = "openai/gpt-oss-120b"
    reasoning_effort: str = "medium"
    timeout: float = 45.0
    # One practical commit can use the existing total allowance without paying
    # repeated essential evidence overhead. Explicit lower limits still apply.
    request_budget: int = 30000
    output_tokens: int = 1536
    headroom: int = 512
    max_chunks: int = 16
    max_review_tokens: int = 30000

    def __post_init__(self) -> None:
        if (not isinstance(self.model, str) or not re.fullmatch(r"[A-Za-z0-9._/-]+", self.model)
                or self.reasoning_effort not in ("low", "medium", "high")):
            raise ReviewError("Invalid review model or reasoning effort")
        if (isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float))
                or not math.isfinite(self.timeout) or self.timeout <= 0):
            raise ReviewError("Timeout must be finite and positive")
        for value in (self.request_budget, self.output_tokens, self.headroom,
                      self.max_chunks, self.max_review_tokens):
            if type(value) is not int or value <= 0:
                raise ReviewError("Review budgets must be positive integers")
        if self.request_budget <= self.output_tokens + self.headroom:
            raise ReviewError("Request budget leaves no input capacity")
        if sanitize(asdict(self))[1]:
            raise ReviewError("Sensitive review configuration refused")


@dataclass(frozen=True)
class FileChange:
    path: str
    change_type: str
    patch: str
    old_path: str | None = None


@dataclass(frozen=True)
class ReviewPacket:
    task_id: str
    commit_sha: str
    branch: str
    requirements: str
    acceptance_criteria: tuple[str, ...]
    changes: tuple[FileChange, ...]
    tests: dict[str, Any] | None
    safety_passed: bool
    missing_evidence: tuple[str, ...] = ()
    template_version: str = TEMPLATE_VERSION
    plan_fingerprint: str | None = None
    approved_configuration: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReviewChunk:
    chunk_id: str
    coverage: tuple[str, ...]
    content: str
    estimated_tokens: int


@dataclass(frozen=True)
class ReviewDiagnostics:
    """Size-only diagnostics: conservative units, never actual API usage."""

    essential_json_bytes: int
    essential_request_units: int
    diff_bytes: int
    chunk_count: int | None
    estimated_total_units: int | None
    repeated_overhead_units: int | None
    largest_request_units: int
    request_budget: int
    max_chunks: int
    total_budget: int
    limiting_factor: str | None = None


@dataclass(frozen=True)
class ReviewPlan:
    chunks: tuple[ReviewChunk, ...]
    diagnostics: ReviewDiagnostics


@dataclass(frozen=True)
class ReviewResult:
    review_id: str
    decision: str
    confidence: str
    coverage_complete: bool
    requirements_checked: tuple[str, ...]
    blocking_issues: tuple[str, ...]
    needs_final_review: tuple[str, ...]
    error: str | None = None

    @property
    def eligible_to_proceed(self) -> bool:
        # This result never changes a task status or controls the queue.
        return self.decision == "APPROVED" and self.coverage_complete and self.error is None


def validate_packet(packet: ReviewPacket) -> None:
    if (not isinstance(packet.task_id, str) or not packet.task_id.strip()
            or not isinstance(packet.commit_sha, str) or not SHA.fullmatch(packet.commit_sha)
            or not isinstance(packet.branch, str) or not packet.branch
            or packet.branch.casefold() in ("main", "master")
            or packet.template_version != TEMPLATE_VERSION):
        raise ReviewError("Invalid task, commit, branch or template binding")
    if (not isinstance(packet.requirements, str) or not packet.requirements.strip()
            or not isinstance(packet.acceptance_criteria, (list, tuple)) or not packet.acceptance_criteria
            or any(not isinstance(item, str) or not item.strip() for item in packet.acceptance_criteria)):
        raise ReviewError("Missing approved requirements")
    if packet.missing_evidence or not isinstance(packet.changes, (list, tuple)) or not packet.changes:
        raise ReviewError("Missing code or other required evidence")
    tests = packet.tests
    if (not isinstance(tests, dict) or tests.get("commit_sha") != packet.commit_sha
            or tests.get("success") is not True
            or not isinstance(tests.get("commands"), (list, tuple)) or not tests["commands"]
            or tests.get("error") or tests.get("failed_command")
            or packet.safety_passed is not True):
        raise ReviewError("Missing or failed Python-controlled tests or safety checks")
    for command in tests["commands"]:
        if (not isinstance(command, dict) or type(command.get("exit_code")) is not int
                or command["exit_code"] != 0
                or command.get("success") is not True or command.get("error")
                or command.get("timed_out") or command.get("cleanup_error")):
            raise ReviewError("Invalid Python-controlled test evidence")
        arguments = command.get("arguments")
        if (not isinstance(arguments, (list, tuple)) or not arguments
                or any(not isinstance(arg, str) or "\x00" in arg for arg in arguments)
                or not arguments[0].strip()):
            raise ReviewError("Missing Python-controlled test command arguments")
    for change in packet.changes:
        if not isinstance(change, FileChange) or not isinstance(change.patch, str):
            raise ReviewError("Invalid changed-file evidence")
        validate_review_path(change.path)
        if change.old_path is not None:
            validate_review_path(change.old_path)
        if change.change_type not in ("A", "M", "D", "R"):
            raise ReviewError("Unsupported change type")
        if (not change.patch.startswith("diff --git ") or "\ufffd" in change.patch
                or any(marker in change.patch for marker in
                       ("GIT binary patch", "Binary files ", "Submodule ", "diff --cc "))):
            raise ReviewError("Unsupported or incomplete patch")
    _, changed = sanitize(asdict(packet))
    if changed:
        raise ReviewError("Sensitive evidence withheld; review refused")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReviewError("Duplicate JSON response field")
        result[key] = value
    return result


def validate_response(content: str, packet: ReviewPacket) -> dict:
    try:
        data = json.loads(content, object_pairs_hook=_unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(data, dict) or set(data) != set(RESPONSE_SCHEMA["required"]):
            raise ReviewError("Invalid review response fields")
        if (data["task_id"] != packet.task_id or not isinstance(data["commit_sha"], str)
                or not SHA.fullmatch(data["commit_sha"]) or data["commit_sha"] != packet.commit_sha):
            raise ReviewError("Review task or full commit SHA mismatch")
        if data["decision"] not in DECISIONS or data["confidence"] not in CONFIDENCES:
            raise ReviewError("Invalid review decision or confidence")
        for field in ARRAY_FIELDS:
            if not isinstance(data[field], list) or any(
                not isinstance(item, str) or not item.strip() for item in data[field]
            ):
                raise ReviewError("Invalid review finding types")
        if any(item not in packet.acceptance_criteria for item in data["requirements_checked"]):
            raise ReviewError("Response claims unknown requirements")
        if data["decision"] == "APPROVED":
            validate_packet(packet)
            if data["blocking_issues"] or not data["requirements_checked"]:
                raise ReviewError("Contradictory or unsupported approval")
        if data["decision"] == "REJECTED" and not data["blocking_issues"]:
            raise ReviewError("Rejection must identify a blocking issue")
        _, changed = sanitize(data)
        if changed:
            raise ReviewError("Sensitive provider response withheld")
        return data
    except (TypeError, ValueError, RecursionError) as exc:
        if isinstance(exc, ReviewError):
            raise
        raise ReviewError("Malformed review JSON") from None


def request_arguments(config: ReviewConfig, instructions: str, content: str) -> dict:
    return {
        "model": config.model, "reasoning_effort": config.reasoning_effort,
        "messages": [{"role": "system", "content": instructions},
                     {"role": "user", "content": content}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "quick_review", "strict": True, "schema": RESPONSE_SCHEMA,
        }},
        "max_completion_tokens": config.output_tokens, "stream": False,
        "include_reasoning": False, "store": False,
    }


def _diff_lines(text: str) -> list[str]:
    # Git delimits lines with LF, not Unicode separators or bare CR. Retain bytes.
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _split_hunk(section: dict, fits: Callable[[list[dict]], bool], *,
                first_fits: Callable[[list[dict]], bool] | None = None) -> list[dict]:
    """Partition primary evidence; repeated neighbors are explicitly context only."""
    lines = _diff_lines(section["body"])
    match = _HUNK_HEADER.fullmatch(lines[0]) if lines else None
    if match is None:
        raise ReviewError("Diff metadata exceeds the safe request budget")
    hunk_header, body = lines[0], lines[1:]
    old_start, _, new_start, _ = match.groups()
    old_offsets, new_offsets = [0], [0]
    # Keep a no-newline marker attached to the preceding source line.
    boundaries = [0]
    for index, line in enumerate(body):
        old_offsets.append(old_offsets[-1] + int(line.startswith((" ", "-"))))
        new_offsets.append(new_offsets[-1] + int(line.startswith((" ", "+"))))
        if index + 1 == len(body) or not body[index + 1].startswith("\\"):
            boundaries.append(index + 1)

    def fragment(first: int, last: int) -> dict:
        start, end = boundaries[first], boundaries[last]
        before = boundaries[max(0, first - 3)]
        after = boundaries[min(len(boundaries) - 1, last + 3)]
        return {
            **section, "section_id": f'{section["section_id"]}:{start}-{end}',
            "body": "".join(body[start:end]),
            "fragment": {
                "parent_section_id": section["section_id"], "hunk_header": hunk_header,
                "hunk_body_lines": len(body), "primary_body_lines": [start, end],
                "old_range": {"start": int(old_start) + old_offsets[start],
                              "count": old_offsets[end] - old_offsets[start]},
                "new_range": {"start": int(new_start) + new_offsets[start],
                              "count": new_offsets[end] - new_offsets[start]},
                "context_only": {
                    "before": {"body_lines": [before, start], "body": "".join(body[before:start])},
                    "after": {"body_lines": [end, after], "body": "".join(body[end:after])},
                },
            },
        }

    result = []
    first = 0
    while first < len(boundaries) - 1:
        # With a fixed first boundary, primary+following context grows
        # monotonically. Measure the actual serialized request, not raw text.
        low, high = first + 1, len(boundaries) - 1
        # An already oversized hunk can fill the previous chunk's spare room.
        # If even its first complete line cannot fit there, start a new chunk.
        fits_here = (first_fits if first == 0 and first_fits is not None
                     and first_fits([fragment(first, low)]) else fits)
        if not fits_here([fragment(first, low)]):
            raise ReviewError("A diff line or required hunk context exceeds the safe request budget")
        best = low
        while low <= high:
            middle = (low + high) // 2
            if fits_here([fragment(first, middle)]):
                best, low = middle, middle + 1
            else:
                high = middle - 1
        result.append(fragment(first, best))
        first = best
    # Check exact reconstruction, not merely a count of generated identifiers.
    if not result or hunk_header + "".join(item["body"] for item in result) != section["body"]:
        raise ReviewError("Incomplete diff fragment coverage")
    return result


def build_chunks(packet: ReviewPacket, config: ReviewConfig, *,
                 instructions: str | None = None) -> tuple[ReviewChunk, ...]:
    """Backward-compatible chunk interface, using the same diagnosed planner."""
    return plan_chunks(packet, config, instructions=instructions).chunks


def plan_chunks(packet: ReviewPacket, config: ReviewConfig, *,
                instructions: str | None = None) -> ReviewPlan:
    """Byte upper-bound estimate, including schema/messages and output reserve.

    Preserve files/hunks where possible, then partition oversized hunks at
    complete diff lines with separately labeled context and exact coverage.
    """
    validate_packet(packet)
    instructions = template() if instructions is None else instructions
    if sanitize(instructions)[1]:
        raise ReviewError("Sensitive review instructions withheld")
    essential = asdict(packet)
    del essential["changes"]
    manifest = [{"path": c.path, "old_path": c.old_path, "change_type": c.change_type}
                for c in packet.changes]
    sections: list[dict] = []
    for index, change in enumerate(packet.changes):
        # Actual hunk lines start @@; source content starts space/+/-.
        lines = _diff_lines(change.patch)
        starts = [i for i, line in enumerate(lines) if line.startswith("@@ ")]
        if starts:
            header = "".join(lines[:starts[0]])
            for hunk, start in enumerate(starts):
                end = starts[hunk + 1] if hunk + 1 < len(starts) else len(lines)
                match = _HUNK_HEADER.fullmatch(lines[start])
                if match is None:
                    raise ReviewError("Unsupported diff hunk header")
                body = lines[start + 1:end]
                for position, line in enumerate(body):
                    if line.startswith((" ", "+", "-")):
                        continue
                    if (line.rstrip("\r\n") != "\\ No newline at end of file"
                            or position == 0 or body[position - 1].startswith("\\")):
                        raise ReviewError("Unsupported diff hunk body")
                old = sum(line.startswith((" ", "-")) for line in body)
                new = sum(line.startswith((" ", "+")) for line in body)
                if (old, new) != tuple(int(count) if count is not None else 1
                                       for count in (match[2], match[4])):
                    raise ReviewError("Incomplete diff hunk coverage")
                sections.append({"section_id": f"{index}:{hunk}", **manifest[index],
                                 "header": header, "body": "".join(lines[start:end])})
        elif change.change_type == "R" and "rename from " in change.patch and "rename to " in change.patch:
            sections.append({"section_id": f"{index}:0", **manifest[index],
                             "header": change.patch, "body": ""})
        elif change.change_type in ("A", "D") and "index " in change.patch:
            # Empty regular files have metadata but no text hunks.
            sections.append({"section_id": f"{index}:0", **manifest[index],
                             "header": change.patch, "body": ""})
        else:
            raise ReviewError("Patch has insufficient reviewable context")
    if len({c.path for c in packet.changes}) != len(packet.changes):
        raise ReviewError("Ambiguous changed-file coverage")

    def make(items: list[dict], count: int | None = None, index: int | None = None) -> tuple[str, int]:
        count = config.max_chunks if count is None else count
        index = config.max_chunks if index is None else index
        content = encoded({**essential, "changed_files": manifest, "sections": items,
                           "chunk_index": index, "chunk_count": count,
                           "partial_diff": count != 1})
        # UTF-8 bytes deliberately overestimate typical tokens; not a tokenizer.
        estimate = len(encoded(request_arguments(config, instructions, content)).encode("utf-8"))
        return content, estimate + config.output_tokens + config.headroom

    _, whole_size = make(sections, 1, 1)

    def diagnostics(groups: list[list[dict]] | None, limit: str | None = None) -> ReviewDiagnostics:
        sizes = ([make(group, len(groups), i)[1] for i, group in enumerate(groups, 1)]
                 if groups else [])
        overhead = ([make([], len(groups), i)[1] for i in range(1, len(groups) + 1)]
                    if groups else [])
        return ReviewDiagnostics(
            len(encoded(essential).encode("utf-8")), make([], 1, 1)[1],
            sum(len(change.patch.encode("utf-8")) for change in packet.changes),
            len(groups) if groups else None, sum(sizes) if sizes else None,
            sum(overhead[1:]) if overhead else None, max(sizes, default=whole_size),
            config.request_budget, config.max_chunks, config.max_review_tokens, limit,
        )

    groups: list[list[dict]] = []
    expected_sections = sections
    if whole_size <= config.request_budget:
        groups = [sections]
    else:
        if make([])[1] > config.request_budget:
            raise ReviewError("Essential review evidence exceeds the safe request budget",
                              diagnostics=diagnostics(None, "essential_request_budget"))
        expected_sections = []
        group: list[dict] = []

        def units(file_sections: list[dict]) -> Iterator[list[dict]]:
            if make(file_sections)[1] <= config.request_budget:
                yield file_sections
                return
            for section in file_sections:
                if make([section])[1] <= config.request_budget:
                    yield [section]
                    continue
                try:
                    parts = _split_hunk(
                        section, lambda items: make(items)[1] <= config.request_budget,
                        first_fits=lambda items: make([*group, *items])[1] <= config.request_budget,
                    )
                except ReviewError as exc:
                    raise ReviewError(str(exc), diagnostics=diagnostics(None, "request_budget")) from None
                for part in parts:
                    yield [part]

        # Keep whole files together where possible, then fall back to hunks.
        for file_index in range(len(packet.changes)):
            file_sections = [s for s in sections if s["section_id"].startswith(f"{file_index}:")]
            for unit in units(file_sections):
                expected_sections.extend(unit)
                if group and make([*group, *unit])[1] > config.request_budget:
                    groups.append(group)
                    group = []
                group.extend(unit)
        if group:
            groups.append(group)
    if not groups or len(groups) > config.max_chunks:
        raise ReviewError("Excessive review chunks", diagnostics=diagnostics(groups, "max_chunks"))
    chunks = []
    for index, group in enumerate(groups, 1):
        content, size = make(group, len(groups), index)
        if size > config.request_budget:
            raise ReviewError("Final review request exceeds the safe request budget",
                              diagnostics=diagnostics(groups, "request_budget"))
        identity = hashlib.sha256(content.encode("utf-8")).hexdigest()
        chunks.append(ReviewChunk(identity, tuple(s["section_id"] for s in group), content, size))
    coverage = [section for chunk in chunks for section in chunk.coverage]
    if coverage != [section["section_id"] for section in expected_sections] or len(set(coverage)) != len(coverage):
        raise ReviewError("Incomplete or duplicate diff coverage")
    if sum(chunk.estimated_tokens for chunk in chunks) > config.max_review_tokens:
        raise ReviewError("Total review allowance would be exceeded",
                          diagnostics=diagnostics(groups, "max_review_tokens"))
    return ReviewPlan(tuple(chunks), diagnostics(groups))
