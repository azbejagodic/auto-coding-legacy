import json
import logging
import subprocess
from unittest.mock import Mock

import pytest

from auto_coding.integrations.codex import run_codex


def jsonl(*events):
    return "\n".join(json.dumps(event) for event in events)


SUCCESS = jsonl(
    {"type": "thread.started", "thread_id": "private-session"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "Done: ž"}},
    {"type": "turn.completed"},
)


@pytest.fixture(autouse=True)
def process(monkeypatch):
    # Every runner test is isolated from live Codex, including failure paths.
    monkeypatch.setattr("auto_coding.integrations.codex.shutil.which", lambda name: "codex.exe")
    mock = Mock(return_value=subprocess.CompletedProcess([], 0, SUCCESS, "private stderr"))
    monkeypatch.setattr("auto_coding.integrations.codex.subprocess.run", mock)
    return mock


def test_success_uses_exact_model_stdin_and_workspace_sandbox(tmp_path, process, caplog):
    prompt = "Private prompt: `echo danger`; $(secret) & \"quotes\"\nUnicode ž"
    with caplog.at_level(logging.INFO):
        result = run_codex(prompt, "requested-model", tmp_path, timeout=12)
    assert result.success
    assert result.model == "requested-model"
    assert result.exit_code == 0
    assert result.final_response == "Done: ž"
    assert result.stdout == SUCCESS
    assert result.stderr == "private stderr"
    assert len(result.events) == 3
    assert result.error is None
    assert not result.timed_out
    assert result.duration_seconds >= 0
    process.assert_called_once_with(
        ["codex.exe", "exec", "--model", "requested-model", "--sandbox", "workspace-write",
         "--json", "--color", "never", "-"],
        input=prompt, cwd=tmp_path.resolve(), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=12, check=False, shell=False,
    )
    assert prompt not in caplog.text
    assert "private-session" not in caplog.text
    assert "private stderr" not in caplog.text
    assert "Done: ž" not in caplog.text
    assert "requested-model" in caplog.text
    assert "completed" in caplog.text


def test_missing_executable(tmp_path, process, monkeypatch):
    monkeypatch.setattr("auto_coding.integrations.codex.shutil.which", lambda name: None)
    result = run_codex("prompt", "model", tmp_path)
    assert not result.success
    assert "not found on PATH" in result.error
    assert result.exit_code is None
    process.assert_not_called()


def test_windows_batch_shim_is_rejected(tmp_path, process, monkeypatch):
    monkeypatch.setattr("auto_coding.integrations.codex.shutil.which", lambda name: "codex.cmd")
    result = run_codex("prompt", "model & command", tmp_path)
    assert not result.success
    assert "native codex.exe" in result.error
    process.assert_not_called()


@pytest.mark.parametrize("is_file", [False, True])
def test_invalid_working_directory(tmp_path, process, is_file):
    directory = tmp_path / "invalid"
    if is_file:
        directory.touch()
    result = run_codex("prompt", "model", directory)
    assert not result.success
    assert "not a directory" in result.error
    process.assert_not_called()


def test_nonzero_exit_keeps_captured_output(tmp_path, process):
    process.return_value = subprocess.CompletedProcess([], 7, SUCCESS, "model unavailable")
    result = run_codex("prompt", "exact-model", tmp_path)
    assert not result.success
    assert result.exit_code == 7
    assert result.model == "exact-model"
    assert result.final_response == "Done: ž"
    assert result.stderr == "model unavailable"
    assert "code 7" in result.error
    process.assert_called_once()


def test_timeout_retains_partial_bytes_output(tmp_path, process):
    process.side_effect = subprocess.TimeoutExpired(
        ["codex"], 2, output=SUCCESS.encode("utf-8"), stderr=b"partial stderr",
    )
    result = run_codex("prompt", "model", tmp_path, timeout=2)
    assert not result.success
    assert result.timed_out
    assert result.exit_code is None
    assert result.stdout == SUCCESS
    assert result.stderr == "partial stderr"
    assert result.final_response == "Done: ž"
    assert "timed out" in result.error


def test_launch_failure_does_not_log_exception_content(tmp_path, process, caplog):
    process.side_effect = OSError(5, "secret-token")
    result = run_codex("prompt", "model", tmp_path)
    assert not result.success
    assert result.exit_code is None
    assert "errno=5" in result.error
    assert "secret-token" not in caplog.text


@pytest.mark.parametrize("output", [
    "not JSON", "[]", "null", '{"type": 42}',
    jsonl({"type": "item.completed", "item": None}),
    jsonl({"type": "item.completed", "item": {"type": "agent_message", "text": []}}),
])
def test_malformed_output_is_failure_without_crashing(tmp_path, process, output):
    process.return_value = subprocess.CompletedProcess([], 0, output, "")
    result = run_codex("prompt", "model", tmp_path)
    assert not result.success
    assert "malformed" in result.error
    assert result.stdout == output


@pytest.mark.parametrize("output", ["", jsonl({"type": "future.event"})])
def test_missing_completion_is_failure(tmp_path, process, output):
    process.return_value = subprocess.CompletedProcess([], 0, output, "")
    result = run_codex("prompt", "model", tmp_path)
    assert not result.success
    assert "completed turn" in result.error


@pytest.mark.parametrize("event_type", ["turn.failed", "error"])
def test_error_events_override_zero_exit(tmp_path, process, event_type):
    process.return_value = subprocess.CompletedProcess([], 0, SUCCESS + "\n" + jsonl({"type": event_type}), "")
    result = run_codex("prompt", "model", tmp_path)
    assert not result.success
    assert "failed turn event" in result.error


def test_unknown_events_are_retained_and_last_message_used(tmp_path, process):
    output = SUCCESS + "\n" + jsonl(
        {"type": "future.event", "data": 42},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Last response"}},
    )
    process.return_value = subprocess.CompletedProcess([], 0, output, "")
    result = run_codex("prompt", "model", tmp_path)
    assert result.success
    assert result.final_response == "Last response"
    assert result.events[-2]["type"] == "future.event"


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_invalid_timeout_does_not_launch(tmp_path, process, timeout):
    result = run_codex("prompt", "model", tmp_path, timeout=timeout)
    assert not result.success
    assert "Timeout" in result.error
    process.assert_not_called()


@pytest.mark.parametrize("prompt, model", [("", "model"), ("prompt", ""), ("prompt", "bad\x00model")])
def test_invalid_prompt_or_model_does_not_launch(tmp_path, process, prompt, model):
    assert not run_codex(prompt, model, tmp_path).success
    process.assert_not_called()
