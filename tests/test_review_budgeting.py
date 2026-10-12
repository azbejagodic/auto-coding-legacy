"""Practical commit budgeting; synthetic evidence and no external I/O."""

from dataclasses import replace
import hashlib
import json

import pytest

from auto_coding.cli import build_parser
from auto_coding.reviews import (
    ReviewConfig, ReviewError, build_chunks, encoded, plan_chunks, request_arguments, template,
)

from test_review_fragments import addition, sections
from test_reviews import packet


@pytest.fixture
def practical_packet(packet):
    # Match the reported six-file / 623-insertion commit, with ~3.5 KB of
    # approved prompt and full test/configuration evidence. No private data.
    kinds = (
        ("README.md", 31, 1071, lambda n: f"- Tile {n}: ready"),
        ("index.html", 59, 1565, lambda n: f"<li>{n}</li>"),
        ("board.js", 99, 2759, lambda n: f"const r{n}=0;"),
        ("test_board.py", 68, 2601, lambda n: f"assert row[{n}]==0"),
        ("board.css", 268, 5048, lambda n: f".t{n}{{gap:0}}"),
        ("layout.css", 98, 3085, lambda n: f".r{n}{{display:grid}}"),
    )
    changes = []
    for path, count, size, line in kinds:
        lines = [line(n) + "\n" for n in range(count)]
        padding = size - len(addition(path, lines).patch.encode())
        assert padding >= 0
        lines = [text[:-1] + " " * (padding // count + (n < padding % count)) + "\n"
                 for n, text in enumerate(lines)]
        changes.append(addition(path, lines))
        assert len(changes[-1].patch.encode()) == size
    tests = {
        **packet.tests, "error": None, "failed_command": None, "duration_seconds": 0.1,
        "commands": [{**packet.tests["commands"][0], "duration_seconds": 0.1,
                      "stdout": "test result: passed\n" * 29, "stderr": "",
                      "timed_out": False, "error": None, "cleanup_error": None}],
    }
    return replace(
        packet, changes=tuple(changes), requirements="Approved visual board scope. " * 122,
        acceptance_criteria=tuple(f"Requirement {n}: implement the approved board behavior." for n in range(8)),
        tests=tests, plan_fingerprint="f" * 64,
        approved_configuration={
            "model": "codex-test", "reasoning_effort": "high", "task_order": 1,
            "dependencies": [], "permitted_files": [c.path for c in changes],
            "test_commands": [["python", "-m", "pytest"]], "approved_placeholders": [],
            "execution_limits": {"codex_timeout": 300, "test_timeout": 60},
        },
    )


def assert_complete(packet, chunks, config):
    items = sections(chunks)
    for change in packet.changes:
        matched = [item for item in items if item["path"] == change.path]
        assert matched
        body = "".join(item["body"] for item in matched)
        if "fragment" in matched[0]:
            body = matched[0]["fragment"]["hunk_header"] + body
        assert matched[0]["header"] + body == change.patch
    coverage = [identity for chunk in chunks for identity in chunk.coverage]
    assert coverage == [item["section_id"] for item in items]
    assert len(coverage) == len(set(coverage))
    for chunk in chunks:
        content = json.loads(chunk.content)
        assert content["requirements"] == packet.requirements
        assert content["acceptance_criteria"] == list(packet.acceptance_criteria)
        assert content["approved_configuration"] == packet.approved_configuration
        assert content["tests"] == packet.tests
        assert content["plan_fingerprint"] == packet.plan_fingerprint
        wire = request_arguments(config, template(), chunk.content)
        assert wire["reasoning_effort"] == "medium"
        assert chunk.estimated_tokens == len(encoded(wire).encode()) + config.output_tokens + config.headroom
        assert chunk.estimated_tokens <= config.request_budget
        assert chunk.chunk_id == hashlib.sha256(chunk.content.encode()).hexdigest()
    assert sum(chunk.estimated_tokens for chunk in chunks) <= config.max_review_tokens


def test_practical_six_file_commit_fits_default_without_repeating_requirements(practical_packet):
    config = ReviewConfig()
    planned = plan_chunks(practical_packet, config)
    assert len(planned.chunks) == 1
    assert config.request_budget == config.max_review_tokens == 30000
    assert planned == plan_chunks(practical_packet, config)
    assert_complete(practical_packet, planned.chunks, config)
    size = planned.diagnostics
    assert size.chunk_count == 1 and size.limiting_factor is None
    assert size.repeated_overhead_units == 0
    assert size.estimated_total_units == sum(c.estimated_tokens for c in planned.chunks)
    assert size.diff_bytes == sum(len(c.patch.encode()) for c in practical_packet.changes)
    assert 10000 < size.essential_request_units < 12000


@pytest.mark.parametrize("budget,total,factor", [
    (12000, 30000, "max_chunks"), (16000, 30000, "max_review_tokens"),
])
def test_old_limits_reproduce_failures_and_have_numeric_diagnostics(practical_packet, budget, total, factor):
    config = ReviewConfig(request_budget=budget, max_review_tokens=total)
    with pytest.raises(ReviewError) as failed:
        plan_chunks(practical_packet, config)
    size = failed.value.diagnostics
    assert size.limiting_factor == factor
    assert size.request_budget == budget and size.total_budget == total
    assert size.chunk_count > 1 and size.estimated_total_units > total
    assert size.repeated_overhead_units > size.essential_request_units
    assert size.largest_request_units <= budget
    if factor == "max_chunks":
        assert size.chunk_count > size.max_chunks


def test_spare_capacity_packing_resolves_the_80000_unit_failure(practical_packet, monkeypatch):
    from auto_coding import reviews
    config = ReviewConfig(request_budget=16000, max_review_tokens=80000)
    split = reviews._split_hunk
    # Reproduce the old isolated-hunk packing, not a copy of private evidence.
    with monkeypatch.context() as old:
        old.setattr(reviews, "_split_hunk", lambda section, fits, **kwargs: split(section, fits))
        with pytest.raises(ReviewError) as failed:
            plan_chunks(practical_packet, config)
    assert failed.value.diagnostics.limiting_factor == "max_review_tokens"
    planned = plan_chunks(practical_packet, config)
    assert planned.diagnostics.chunk_count < failed.value.diagnostics.chunk_count
    assert planned.diagnostics.estimated_total_units < 80000
    assert_complete(practical_packet, planned.chunks, config)


def test_chunk_packing_fills_spare_room_before_splitting_oversized_hunk(packet):
    small = addition("small.css", [".small { gap: 0; }\n"] * 50)
    large = addition("large.css", [f".t{n} {{ padding: {n % 10}px; }}\n" for n in range(320)])
    packet = replace(packet, changes=(small, large))
    config = ReviewConfig(request_budget=12000)
    chunks = build_chunks(packet, config)
    first = json.loads(chunks[0].content)["sections"]
    assert first[0]["path"] == "small.css" and "fragment" not in first[0]
    assert first[1]["path"] == "large.css" and "fragment" in first[1]
    assert_complete(packet, chunks, config)


@pytest.mark.parametrize("command", ["run", "review-retry"])
def test_cli_limits_match_reviewer_defaults_and_preserve_overrides(command):
    arguments = ["batch", command, "BATCH", "--db", "tasks.sqlite3", "--repo", "repository"]
    if command == "review-retry":
        arguments += ["--task", "TASK"]
    default = build_parser().parse_args(arguments)
    assert default.review_budget == ReviewConfig().request_budget
    assert default.review_token_limit == ReviewConfig().max_review_tokens
    explicit = build_parser().parse_args(arguments + ["--review-budget", "16000", "--review-token-limit", "80000"])
    assert explicit.review_budget == 16000 and explicit.review_token_limit == 80000


@pytest.mark.parametrize("field,config,limit", [
    ("requirements", ReviewConfig(), "essential_request_budget"),
    ("changes", ReviewConfig(), "request_budget"),
])
def test_impossible_evidence_still_fails_closed_with_size_diagnostics(packet, field, config, limit):
    changed = ("Approved requirements. " * 2000 if field == "requirements" else
               (addition("minified.js", ["x" * 40000 + "\n"]),))
    with pytest.raises(ReviewError) as failed:
        plan_chunks(replace(packet, **{field: changed}), config)
    size = failed.value.diagnostics
    assert size.limiting_factor == limit
    assert size.chunk_count is None and size.estimated_total_units is None
    assert size.largest_request_units > size.request_budget


def test_total_cap_is_not_increased_for_larger_commits(practical_packet):
    with pytest.raises(ReviewError) as failed:
        plan_chunks(replace(practical_packet, changes=practical_packet.changes + (
            addition("extra.css", [".extra { padding: 10px; }\n"] * 500),
        )), ReviewConfig())
    assert failed.value.diagnostics.limiting_factor == "max_review_tokens"
    assert failed.value.diagnostics.total_budget == 30000


def test_mocked_review_uses_one_full_request_and_only_provider_usage_is_counted(practical_packet, tmp_path, monkeypatch):
    from unittest.mock import Mock
    import httpx
    from auto_coding.integrations.groq import GroqReviewer
    from auto_coding.storage import TaskStore
    from test_groq_reviewer import client_for, raw_reply

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", Mock(side_effect=AssertionError("Offline only")))
    packet = practical_packet
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        store.add_task(task_id=packet.task_id, title="Synthetic board", prompt=packet.requirements,
                       model="codex-test", task_order=1, acceptance_criteria=list(packet.acceptance_criteria),
                       status="awaiting_review")
        store.record_commit(packet.task_id, packet.commit_sha, packet.branch, None)
        client = client_for(raw_reply(packet))
        result = GroqReviewer(store, client=client).review(packet)
        assert result.eligible_to_proceed and result.coverage_complete
        client.chat.completions.with_raw_response.create.assert_called_once()
        record = store.reviews.get(result.review_id)
        assert record["budget_diagnostics"]["estimated_total_units"] > 28000
        assert record["usage"]["total_tokens"] == store.reviews.usage()["total_tokens"] == 125
        assert record["api_requests"] == record["chunk_count"] == 1
        assert store.get_task(packet.task_id).status == "awaiting_review"
