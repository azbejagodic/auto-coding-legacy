"""Offline review contracts and lossless splitting; no Groq requests."""

from dataclasses import replace
import json

import pytest

from auto_coding.review_security import redact, sanitize
from auto_coding.reviews import (
    FileChange, ReviewConfig, ReviewError, ReviewPacket, build_chunks,
    encoded, request_arguments, template, validate_packet, validate_response,
)


SHA = "a" * 40
PATCH = "diff --git a/file.py b/file.py\nindex abc..def 100644\n--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"


@pytest.fixture
def packet():
    return ReviewPacket(
        "TASK-TEST", SHA, "feature/test", "Implement the approved change",
        ("Works correctly",), (FileChange("file.py", "M", PATCH),),
        {"commit_sha": SHA, "success": True, "commands": [
            {"arguments": ["python", "-m", "pytest"], "exit_code": 0, "success": True},
        ]}, True,
    )


def response(packet, **changes):
    return {
        "task_id": packet.task_id, "commit_sha": packet.commit_sha,
        "decision": "APPROVED", "confidence": "MEDIUM",
        "requirements_checked": list(packet.acceptance_criteria),
        "blocking_issues": [], "needs_final_review": [], **changes,
    }


@pytest.mark.parametrize("decision", ["APPROVED", "REJECTED", "UNCERTAIN"])
def test_valid_responses(packet, decision):
    data = response(packet, decision=decision,
                    blocking_issues=["Missing implementation"] if decision == "REJECTED" else [])
    assert validate_response(encoded(data), packet) == data


@pytest.mark.parametrize("changes", [
    {"task_id": "OTHER"}, {"commit_sha": "a" * 12},
    {"commit_sha": "a" * 12 + "b" * 28}, {"commit_sha": "A" * 40},
    {"decision": "YES"}, {"confidence": "99%"},
    {"decision": None}, {"confidence": ["HIGH"]}, {"blocking_issues": "none"},
    {"blocking_issues": ["Defect"]}, {"requirements_checked": []},
    {"requirements_checked": ["invented requirement"]},
    {"needs_final_review": [False]}, {"needs_final_review": [""]},
    {"decision": "REJECTED", "blocking_issues": []}, {"extra": 1},
])
def test_response_fail_closed(packet, changes):
    with pytest.raises(ReviewError):
        validate_response(encoded(response(packet, **changes)), packet)


@pytest.mark.parametrize("content", ["", "{}", "[]", "null", "```json\n{}\n```",
                                      '{"task_id":"one","task_id":"two"}', '{"a":NaN}', '{"task_id":'])
def test_bad_json(packet, content):
    with pytest.raises(ReviewError):
        validate_response(content, packet)


@pytest.mark.parametrize("changes", [
    {"requirements": ""}, {"acceptance_criteria": ()}, {"changes": ()},
    {"tests": None}, {"missing_evidence": ("Tests unavailable",)},
    {"safety_passed": False}, {"commit_sha": "bad"}, {"branch": "main"},
    {"template_version": "unknown"},
])
def test_missing_evidence_never_approves(packet, changes):
    modified = replace(packet, **changes)
    with pytest.raises(ReviewError):
        build_chunks(modified, ReviewConfig())
    with pytest.raises(ReviewError):
        validate_response(encoded(response(modified)), modified)


@pytest.mark.parametrize("changes", [
    {"commit_sha": "b" * 40}, {"success": False}, {"error": "cleanup failed"},
    {"failed_command": {"exit_code": 1}}, {"commands": []},
    {"commands": [{"success": True, "exit_code": 1}]},
    {"commands": [{"success": True, "exit_code": 0, "cleanup_error": "failed"}]},
    {"commands": [{"success": True, "exit_code": False}]},
    {"commands": [{"success": True, "exit_code": 0}]},
])
def test_test_evidence_validation(packet, changes):
    with pytest.raises(ReviewError):
        validate_packet(replace(packet, tests={**packet.tests, **changes}))


def test_small_packet_one_request_and_budget(packet):
    config = ReviewConfig()
    chunks = build_chunks(packet, config)
    assert len(chunks) == 1
    assert chunks[0].coverage == ("0:0",)
    content = json.loads(chunks[0].content)
    assert content["partial_diff"] is False
    assert content["sections"][0]["header"] + content["sections"][0]["body"] == PATCH
    arguments = request_arguments(config, template(), chunks[0].content)
    assert chunks[0].estimated_tokens == (
        len(encoded(arguments).encode("utf-8")) + config.output_tokens + config.headroom
    )
    assert chunks[0].estimated_tokens <= config.request_budget


def test_large_hunks_split_losslessly_and_repeat_essentials(packet):
    header = PATCH[:PATCH.index("@@")]
    hunks = [f"@@ -{n},1 +{n},1 @@\n-" + "a" * 500 + "\n+" + "b" * 500 + "\n"
             for n in range(1, 6)]
    packet = replace(packet, changes=(FileChange("file.py", "M", header + "".join(hunks)),))
    chunks = build_chunks(packet, ReviewConfig(request_budget=8000, max_review_tokens=100000))
    assert len(chunks) > 1
    sections = [section for chunk in chunks for section in json.loads(chunk.content)["sections"]]
    assert "".join(section["body"] for section in sections) == "".join(hunks)
    assert [section["section_id"] for section in sections] == [f"0:{n}" for n in range(5)]
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)
    for chunk in chunks:
        content = json.loads(chunk.content)
        assert content["task_id"] == packet.task_id and content["commit_sha"] == SHA
        assert content["requirements"] == packet.requirements
        assert content["acceptance_criteria"] == list(packet.acceptance_criteria)
        assert content["chunk_count"] == len(chunks)
        assert content["partial_diff"]
        assert chunk.estimated_tokens <= 8000


@pytest.mark.parametrize("config", [
    ReviewConfig(request_budget=2200, output_tokens=1536, headroom=512),
    ReviewConfig(max_review_tokens=100),
])
def test_insufficient_budget_refuses_not_truncates(packet, config):
    with pytest.raises(ReviewError):
        build_chunks(packet, config)


def test_excessive_chunks_and_unsplittable_hunk(packet):
    big = replace(packet, changes=(FileChange("file.py", "M", PATCH + "+" + "x" * 10000),))
    with pytest.raises(ReviewError, match="hunk"):
        build_chunks(big, ReviewConfig())
    many = replace(packet, changes=tuple(
        FileChange(f"file{n}.py", "M", PATCH.replace("+new", "+" + "x" * 1000)) for n in range(10)
    ))
    with pytest.raises(ReviewError, match="chunks"):
        build_chunks(many, ReviewConfig(request_budget=10000, max_chunks=1, max_review_tokens=100000))


@pytest.mark.parametrize("patch,kind", [
    ("diff --git a/f b/f\nBinary files a/f and b/f differ\n", "M"),
    ("diff --git a/f b/f\nGIT binary patch\n", "A"),
    ("diff --git a/f b/f\nSubmodule f changed\n", "M"),
    ("diff --cc f\n@@@ bad\n", "M"),
    ("diff --git a/f b/f\nmode change only\n", "M"),
    (PATCH.replace("new", "\ufffd"), "M"),
])
def test_unreviewable_changes(packet, patch, kind):
    with pytest.raises(ReviewError):
        build_chunks(replace(packet, changes=(FileChange("f", kind, patch),)), ReviewConfig())


def test_rename_and_empty_deletion_metadata_covered(packet):
    rename = "diff --git a/old b/new\nsimilarity index 100%\nrename from old\nrename to new\n"
    deletion = "diff --git a/empty b/empty\ndeleted file mode 100644\nindex abc..000\n"
    chunks = build_chunks(replace(packet, changes=(
        FileChange("new", "R", rename, "old"), FileChange("empty", "D", deletion),
    )), ReviewConfig())
    sections = [s for chunk in chunks for s in json.loads(chunk.content)["sections"]]
    assert [s["change_type"] for s in sections] == ["R", "D"]
    assert sections[0]["old_path"] == "old"
    assert [s["header"] for s in sections] == [rename, deletion]


@pytest.mark.parametrize("text", [
    "gsk_" + "a" * 40, "sk-" + "b" * 30, "ghp_" + "c" * 40,
    "AKIA" + "X" * 16, "Authorization: Bearer " + "x" * 30,
    'api_key = "not-a-real-key"', "https://user:password@example.com/path",
    'GROQ_API_KEY = "synthetic-value"', 'AWS_SECRET_ACCESS_KEY = "synthetic-value"',
    "-----BEGIN PRIVATE KEY-----\nprivate\n", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
])
def test_secret_redaction_refuses_evidence(packet, text):
    safe, changed = redact(text)
    assert changed and text not in safe
    assert sanitize({"nested": [text]})[1]
    with pytest.raises(ReviewError, match="Sensitive"):
        build_chunks(replace(packet, requirements=text), ReviewConfig())


def test_environment_key_exact_match(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "short-test-key")
    assert redact("value short-test-key")[0] == "value [REDACTED]"


@pytest.mark.parametrize("path", [
    "../f", "C:\\secret", "//server/file", ":(glob)*", "f*.py", ".env",
    ".aws/credentials", "runtime/history.sqlite3", "config.toml", "a/../b", ".ssh/id_rsa",
])
def test_sensitive_and_unsafe_paths(packet, path):
    with pytest.raises(ValueError):
        build_chunks(replace(packet, changes=(FileChange(path, "M", PATCH),)), ReviewConfig())


@pytest.mark.parametrize("kwargs", [
    {"reasoning_effort": "max"}, {"timeout": 0}, {"timeout": True}, {"timeout": float("inf")},
    {"max_chunks": False}, {"output_tokens": -1}, {"request_budget": 20},
    {"model": "bad\nmodel"},
])
def test_invalid_config(kwargs):
    with pytest.raises(ReviewError):
        ReviewConfig(**kwargs)


@pytest.mark.parametrize("changes", [
    {"commit_sha": None}, {"branch": 1}, {"requirements": None},
    {"acceptance_criteria": "not an array"}, {"changes": ("not a change",)},
    {"tests": "not a report"},
])
def test_malformed_packet_types_are_predictable(packet, changes):
    with pytest.raises(ReviewError):
        build_chunks(replace(packet, **changes), ReviewConfig())


@pytest.mark.parametrize("patch", [
    PATCH.replace("@@ -1 +1 @@", "@@ malformed @@"),
    PATCH.replace("-old\n", ""), PATCH.replace("+new\n", ""),
    PATCH + "unprefixed text\n",
])
def test_incomplete_or_unsupported_hunks_never_count_as_reviewed(packet, patch):
    with pytest.raises(ReviewError):
        build_chunks(replace(packet, changes=(FileChange("file.py", "M", patch),)), ReviewConfig())
