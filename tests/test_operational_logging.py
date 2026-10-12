"""Operational logging is offline, bounded, secret-free and behavior-neutral."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import subprocess
from threading import Barrier
from unittest.mock import Mock

import groq
import httpx
import pytest

from auto_coding import operations
from auto_coding.cli import main
from auto_coding.integrations.groq import GroqReviewer
from auto_coding.integrations.git import GitError
from auto_coding.integrations.codex import run_codex
from auto_coding.reviews import ReviewConfig

from test_batch_controller import offline, workflow, run
from test_git import repository
from test_groq_reviewer import store, packet, client_for, raw_reply
from test_review_recovery import stopped, retry


@pytest.fixture(autouse=True)
def isolate_logger():
    logger = operations.logger
    handlers, level, propagate = logger.handlers[:], logger.level, logger.propagate
    logger.handlers.clear()
    try:
        yield
    finally:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        logger.handlers.extend(handlers)
        logger.setLevel(level)
        logger.propagate = propagate


@pytest.fixture
def journal(tmp_path):
    directory = tmp_path / "logs"
    operations.configure_operations(directory)
    return directory / "operations.jsonl"


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_rotation_json_schema_and_concise_terminal(tmp_path, capsys):
    directory = tmp_path / "logs"
    operations.configure_operations(directory, max_bytes=1100, backup_count=2)
    operations.configure_operations(directory)  # No duplicate handlers/events.
    with operations.operation_context(batch_id="BATCH", task_id="TASK-1"):
        for _ in range(20):
            operations.event("tests.completed", stage="tests", outcome="completed", duration_seconds=.25,
                             total_tokens=123, reason="Malformed review JSON")
    assert len(operations.logger.handlers) == 2
    paths = list(directory.glob("operations.jsonl*"))
    assert len(paths) == 3 and all(path.stat().st_size <= 1100 for path in paths)
    for path in paths:
        for item in records(path):
            assert datetime.fromisoformat(item["timestamp"]).utcoffset().total_seconds() == 0
            assert item["batch_id"] == "BATCH" and item["task_id"] == "TASK-1"
            assert item["stage"] == "tests" and item["duration_seconds"] == .25
    terminal = capsys.readouterr().err
    assert terminal.count("tests: completed") == 20
    assert "123" not in terminal and "Malformed" not in terminal


def test_allowlists_exclude_secrets_payloads_and_exception_messages(journal, monkeypatch, capsys):
    secret = "opaque-credential-for-test"
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    operations.event("security.test", stage="test", outcome="failed", model=secret,
                     prompt="private plain prompt", diff="private plain diff", response="private plain response",
                     request_body={"password": secret}, reason="private plain failure",
                     provider_error_code="PRIVATE_PROMPT", rate_limits={
                         "Authorization": secret, "x-ratelimit-limit-tokens": "PRIVATE_PROMPT"})
    @operations.observed("failure")
    def fail():
        raise ValueError("private plain exception " + secret)
    with pytest.raises(ValueError, match=secret):
        fail()
    text = journal.read_text(encoding="utf-8") + capsys.readouterr().err
    for forbidden in (secret, "private plain", "PRIVATE_PROMPT", "Authorization", "request_body"):
        assert forbidden not in text
    item = records(journal)[0]
    assert item["model"] == "[WITHHELD]" and item["provider_error_code"] == "unrecognized"
    assert item["reason"] == "Unrecognized diagnostic withheld"
    assert records(journal)[-1]["error_type"] == "ValueError"


@pytest.mark.parametrize("failure", ["emit", "stream"])
def test_logger_write_failure_does_not_change_result_or_exception(journal, monkeypatch, capsys, failure):
    handler = next(handler for handler in operations.logger.handlers if isinstance(handler, operations._File))
    if failure == "emit":
        monkeypatch.setattr(handler, "emit", Mock(side_effect=OSError("secret disk error")))
    else:
        monkeypatch.setattr(handler.stream, "write", Mock(side_effect=OSError("secret disk error")))
    answer = object()
    @operations.observed("success")
    def succeed():
        return answer
    assert succeed() is answer
    error = RuntimeError("original failure")
    @operations.observed("failure")
    def fail():
        raise error
    with pytest.raises(RuntimeError) as caught:
        fail()
    assert caught.value is error
    terminal = capsys.readouterr().err
    assert "secret disk error" not in terminal and "Traceback" not in terminal
    if failure == "stream":
        assert terminal.count("Operational logging write failed") == 1


def test_unavailable_directory_keeps_execution_available(tmp_path, capsys):
    target = tmp_path / "not-a-directory"
    target.write_text("existing file")
    operations.configure_operations(target)
    operations.event("test.completed", stage="test", outcome="completed")
    assert target.read_text() == "existing file"
    assert "file logging unavailable" in capsys.readouterr().err


def test_context_isolated_across_threads_and_reset(journal):
    barrier = Barrier(2)
    @operations.observed("thread", task_id="task_id")
    def work(task_id):
        barrier.wait(timeout=10)
        operations.event("thread.checkpoint", stage="test", outcome="completed")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(work, ("TASK-1", "TASK-2")))
    items = records(journal)
    for task in ("TASK-1", "TASK-2"):
        group = [item for item in items if item["task_id"] == task]
        assert len(group) == 3 and len({item["operation_id"] for item in group}) == 1
    operations.event("outside", stage="test", outcome="completed")
    assert records(journal)[-1]["task_id"] is None


@pytest.mark.parametrize("kind", ["success", "local_block", "http_error", "malformed", "uncertain"])
def test_groq_request_states_usage_diagnostics_and_reasons(journal, store, packet, monkeypatch, kind):
    reply = raw_reply(packet, data="{" if kind == "malformed" else None,
                      headers={"x-ratelimit-limit-tokens": "8000", "Authorization": "private"})
    if kind == "uncertain":
        from test_reviews import response
        reply = raw_reply(packet, data=response(packet, decision="UNCERTAIN", needs_final_review=["private model finding"]))
    elif kind == "http_error":
        reply = groq.RateLimitError("private body", response=httpx.Response(429,
            request=httpx.Request("POST", "https://api.groq.com"), headers={"retry-after": "2"}),
            body={"error": {"type": "rate_limit_error", "code": "rate_limit_exceeded", "message": "private body"}})
    elif kind == "local_block":
        monkeypatch.setattr("auto_coding.integrations.groq.rate_blocked", lambda *args: True)
    client = client_for(reply)
    result = GroqReviewer(store, client=client).review(packet)
    items = records(journal)
    decision = next(item for item in items if item["event"] == "groq.review.decision")
    assert decision["review_id"] == result.review_id and decision["outcome"] == result.decision
    assert decision["model"] == "openai/gpt-oss-120b" and decision["reasoning_effort"] == "medium"
    request = next(item for item in items if item["event"] == ("groq.request.blocked" if kind == "local_block" else "groq.request.submitted"))
    assert request["chunk_index"] == request["chunk_count"] == 1
    assert request["estimated_input_tokens"] + request["estimated_output_tokens"] == request["estimated_total_tokens"]
    assert request["estimated_total_tokens"] <= 8000
    assert all(item["operation_id"] == items[0]["operation_id"] for item in items)
    if kind == "success":
        assert result.eligible_to_proceed and decision["total_tokens"] == 125
        assert any(item["event"] == "groq.request.completed" and item["input_tokens"] == 100 for item in items)
    else:
        assert not result.eligible_to_proceed and decision["reason"] == (result.error or "Provider returned UNCERTAIN")
    if kind == "http_error":
        failed = next(item for item in items if item["event"] == "groq.request.failed")
        assert failed["http_status"] == 429 and failed["provider_error_code"] == "rate_limit_exceeded"
        assert failed["rate_limits"] == {"retry-after": "2"} and failed["rate_limits_observed_at"]
    if kind == "local_block":
        client.chat.completions.with_raw_response.create.assert_not_called()
    else:
        client.chat.completions.with_raw_response.create.assert_called_once()
    text = journal.read_text(encoding="utf-8")
    assert packet.requirements not in text and "private" not in text and "Authorization" not in text


@pytest.mark.parametrize("failed", [False, True])
def test_controller_stage_logging_preserves_execution_and_git_behavior(journal, workflow, failed):
    w = workflow
    if failed:
        # Explicit result; no real test process is launched.
        from auto_coding.test_runner import TestRunResult
        w.tests.side_effect = lambda **kwargs: TestRunResult(False, (), None, .1, "private test output")
    result = run(w, max_tasks=1)
    assert result.success is not failed
    items = records(journal)
    stages = {item["stage"] for item in items}
    assert {"batch", "claimed", "codex_started", "tests_started"} <= stages
    w.codex.assert_called_once()
    w.tests.assert_called_once()
    if failed:
        w.repo.push_branch.assert_not_called()
        w.client.chat.completions.with_raw_response.create.assert_not_called()
        assert "private test output" not in journal.read_text(encoding="utf-8")
    else:
        assert {"git.commit", "git.push", "git.remote_verify", "review_started", "finalized"} <= stages
        final = next(item for item in items if item["event"] == "task.finalized")
        assert final["batch_id"] == "BATCH" and final["task_id"] == "TASK-1"
        assert final["commit_sha"] == result.commit_sha and final["review_id"]
        w.repo.push_branch.assert_called_once()


def test_recovery_logging_preserves_prior_history_and_readonly_preflight(journal, stopped):
    w = stopped
    before = w.database.read_bytes()
    assert retry(w).success
    assert w.database.read_bytes() == before
    result = retry(w, execute=True)
    assert result.success and w.store.reviews.get(w.original["review_id"]) == w.original
    items = records(journal)
    assert any(item["event"] == "recovery.preflight" for item in items)
    final = next(item for item in items if item["event"] == "recovery.finalized")
    assert final["recovery_id"] == result.recovery_id and final["review_id"] == result.review_id
    submitted = next(item for item in items if item["event"] == "groq.request.submitted")
    assert submitted["recovery_id"] == result.recovery_id and submitted["original_review_id"] == w.original["review_id"]


def test_readonly_cli_does_not_configure_or_create_operations_logs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    setup = Mock(side_effect=AssertionError("Read-only diagnostics must not configure file logging"))
    monkeypatch.setattr("auto_coding.cli.configure_operations", setup)
    monkeypatch.setattr("auto_coding.cli.run_doctor", lambda: [])
    assert main(["doctor"]) == 0
    assert main(["batch", "run", "BATCH", "--db", "missing.sqlite3", "--repo", "."]) == 1
    assert main(["batch", "status", "--db", "missing.sqlite3"]) == 1
    assert main(["batch", "review-retry", "BATCH", "--db", "missing.sqlite3", "--repo", ".", "--task", "TASK-1"]) == 1
    setup.assert_not_called()
    assert not (tmp_path / "runtime").exists()


def test_cli_execution_enables_default_jsonl_without_touching_missing_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = main(["batch", "review-retry", "BATCH", "--db", "missing.sqlite3",
                   "--repo", ".", "--task", "TASK-1", "--execute"])
    assert result == 1 and not (tmp_path / "missing.sqlite3").exists()
    items = records(tmp_path / "runtime/logs/operations.jsonl")
    assert items[-1]["event"] == "cli.failed" and items[-1]["task_id"] == "TASK-1"


@pytest.mark.parametrize("failure", ["timeout", "launch", "exit"])
def test_internal_git_failure_is_diagnosable_without_arguments_or_output(journal, repository, monkeypatch, failure):
    secret = "private git argument and output"
    runner = Mock()
    if failure == "timeout":
        runner.side_effect = subprocess.TimeoutExpired([secret], 60, output=secret, stderr=secret)
    elif failure == "launch":
        runner.side_effect = OSError(secret)
    else:
        runner.return_value = subprocess.CompletedProcess([secret], 128, secret, "authentication " + secret)
    monkeypatch.setattr("auto_coding.integrations.git.subprocess.run", runner)
    with pytest.raises(GitError):
        repository._run("remote", "get-url", secret)
    item = records(journal)[-1]
    assert item["event"] == "git.command.failed" and item["stage"] == "git.remote"
    assert item["reason"] == {"timeout": "Git command timed out", "launch": "Git launch failed",
                              "exit": "Authentication or permission failure"}[failure]
    assert secret not in journal.read_text(encoding="utf-8")
    runner.assert_called_once()


def test_missing_codex_has_a_safe_specific_diagnostic_without_launch(journal, tmp_path, monkeypatch):
    monkeypatch.setattr("auto_coding.integrations.codex.shutil.which", lambda _: None)
    result = run_codex("private original prompt", "exact-model", tmp_path)
    assert not result.success
    item = records(journal)[-1]
    assert item["reason"] == result.error == "Codex CLI was not found on PATH"
    assert item["model"] == "exact-model" and item["outcome"] == "failed"
    assert "private original prompt" not in journal.read_text(encoding="utf-8")
