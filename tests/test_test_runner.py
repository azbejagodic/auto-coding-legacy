import logging
from pathlib import Path
import signal
import subprocess
import sys
from unittest.mock import Mock

import pytest

from auto_coding import test_runner as runner


@pytest.fixture
def popen(monkeypatch):
    process = Mock(pid=12345, returncode=0)
    process.communicate.return_value = ("captured stdout ž", "captured stderr")
    process.poll.return_value = 0
    factory = Mock(return_value=process)
    monkeypatch.setattr(runner.subprocess, "Popen", factory)
    return factory


def test_successful_command_captures_structured_output(tmp_path, popen):
    result = runner.run_tests(tmp_path, [["python", "-m", "pytest"]], timeout=20)
    assert result.success
    assert result.commands_executed == 1
    assert result.failed_command is None and result.error is None
    command = result.commands[0]
    assert command.arguments == ("python", "-m", "pytest")
    assert command.exit_code == 0 and command.success
    assert command.stdout == "captured stdout ž"
    assert command.stderr == "captured stderr"
    assert not command.timed_out
    assert 0 <= command.duration_seconds <= result.duration_seconds
    popen.return_value.communicate.assert_called_once_with(timeout=20)


@pytest.mark.parametrize("exit_code", [1, 2, 5, -9])
def test_nonzero_exit_is_failure_and_stops_plan(tmp_path, popen, exit_code):
    popen.return_value.returncode = exit_code
    result = runner.run_tests(tmp_path, [["cargo", "test"], ["npm", "test"]])
    assert not result.success
    assert result.commands_executed == 1
    assert result.failed_command == result.commands[0]
    assert result.failed_command.exit_code == exit_code
    assert str(exit_code) in result.error
    popen.assert_called_once()


def test_multiple_commands_run_sequentially_with_exact_arguments(tmp_path, popen):
    commands = [["python", "-m", "pytest"], ["npm", "test", "--", "--runInBand"], ["cargo", "test"]]
    result = runner.run_tests(tmp_path, commands)
    assert result.success and result.commands_executed == 3
    assert [call.args[0] for call in popen.call_args_list] == commands
    assert [item.arguments for item in result.commands] == [tuple(item) for item in commands]


def test_missing_executable(tmp_path, popen):
    popen.side_effect = FileNotFoundError("secret executable path")
    result = runner.run_tests(tmp_path, [["missing-tool"], ["never-run"]])
    assert not result.success and result.commands_executed == 1
    assert result.failed_command.exit_code is None
    assert "not found" in result.error
    assert "secret" not in result.error
    popen.assert_called_once()


@pytest.mark.parametrize("directory", ["missing", "file"])
def test_invalid_directory_never_launches(tmp_path, popen, directory):
    path = tmp_path / directory
    if directory == "file":
        path.touch()
    result = runner.run_tests(path, [["test-tool"]])
    assert not result.success and result.commands_executed == 0
    assert "directory" in result.error
    popen.assert_not_called()


@pytest.mark.parametrize("commands", [[], (), "python -m pytest", [[]], [[" "]], [["tool", None]], [["tool", "bad\x00arg"]]])
def test_invalid_or_empty_plan_is_not_success(tmp_path, popen, commands):
    result = runner.run_tests(tmp_path, commands)
    assert not result.success
    assert result.commands_executed == 0 and result.error
    popen.assert_not_called()


def test_entire_plan_validated_before_first_command(tmp_path, popen):
    result = runner.run_tests(tmp_path, [["valid"], "shell command"])
    assert not result.success
    popen.assert_not_called()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "1"])
def test_invalid_timeout(tmp_path, popen, timeout):
    result = runner.run_tests(tmp_path, [["test-tool"]], timeout=timeout)
    assert not result.success and "Timeout" in result.error
    popen.assert_not_called()


@pytest.mark.parametrize("windows", [True, False])
def test_platform_invocation_is_shell_free(tmp_path, popen, monkeypatch, windows):
    monkeypatch.setattr(runner, "_WINDOWS", windows)
    monkeypatch.setattr(runner.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    args = ["tool", "a b", "$(literal)", "quotes\"; &"]
    assert runner.run_tests(tmp_path, [args]).success
    options = popen.call_args.kwargs
    assert popen.call_args.args == (args,)
    assert options["shell"] is False
    assert options["cwd"] == tmp_path.resolve()
    assert options["stdin"] == subprocess.DEVNULL
    assert options["stdout"] == options["stderr"] == subprocess.PIPE
    if windows:
        assert options["creationflags"] == 512
        assert "start_new_session" not in options
    else:
        assert options["start_new_session"] is True
        assert "creationflags" not in options


def test_timeout_kills_and_captures_output(tmp_path, popen, monkeypatch):
    process = popen.return_value
    process.returncode = -9
    process.communicate.side_effect = [
        subprocess.TimeoutExpired(["tool"], 1, output=b"partial", stderr=b"partial error"),
        ("final stdout", "final stderr"),
    ]
    cleanup = Mock(return_value=None)
    monkeypatch.setattr(runner, "_cleanup", cleanup)
    result = runner.run_tests(tmp_path, [["tool"], ["never-run"]], timeout=1)
    assert not result.success and result.commands_executed == 1
    assert result.failed_command.timed_out
    assert result.failed_command.stdout == "final stdout"
    assert result.failed_command.stderr == "final stderr"
    assert result.failed_command.exit_code == -9
    assert "timed out" in result.error
    cleanup.assert_called_once_with(process)


def test_cleanup_failure_and_unclosed_pipes_are_explicit(tmp_path, popen, monkeypatch):
    timeout = subprocess.TimeoutExpired(["tool"], 1, output=b"partial stdout", stderr=b"partial stderr")
    popen.return_value.communicate.side_effect = timeout
    popen.return_value.returncode = None
    monkeypatch.setattr(runner, "_cleanup", Mock(return_value="Tree cleanup failed"))
    result = runner.run_tests(tmp_path, [["tool"]], timeout=1)
    assert not result.success
    assert "Tree cleanup failed" in result.error
    assert "pipes remained open" in result.error
    assert result.failed_command.stdout == "partial stdout"
    assert result.failed_command.stderr == "partial stderr"


@pytest.mark.parametrize("tree_exit", [0, 1])
def test_windows_cleanup_targets_only_launched_pid(monkeypatch, tree_exit):
    monkeypatch.setattr(runner, "_WINDOWS", True)
    monkeypatch.setenv("SystemRoot", "C:/Windows")
    process = Mock(pid=12345)
    process.poll.side_effect = [None, None]
    taskkill = Mock(return_value=subprocess.CompletedProcess([], tree_exit, "private", "private"))
    monkeypatch.setattr(runner.subprocess, "run", taskkill)
    error = runner._cleanup(process)
    assert bool(error) == (tree_exit != 0)
    args = taskkill.call_args.args[0]
    assert Path(args[0]).name == "taskkill.exe"
    assert args[1:] == ["/PID", "12345", "/T", "/F"]
    assert taskkill.call_args.kwargs["shell"] is False
    process.kill.assert_called_once()
    process.wait.assert_called_once_with(timeout=runner._CLEANUP_TIMEOUT)


def test_windows_exited_parent_does_not_target_unrelated_processes(monkeypatch):
    monkeypatch.setattr(runner, "_WINDOWS", True)
    process = Mock(pid=12345)
    process.poll.return_value = 0
    taskkill = Mock()
    monkeypatch.setattr(runner.subprocess, "run", taskkill)
    assert "cannot be safely identified" in runner._cleanup(process)
    taskkill.assert_not_called()
    process.kill.assert_not_called()


def test_posix_cleanup_targets_new_process_group(monkeypatch):
    monkeypatch.setattr(runner, "_WINDOWS", False)
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    killpg = Mock()
    monkeypatch.setattr(runner.os, "killpg", killpg, raising=False)
    process = Mock(pid=12345)
    process.poll.return_value = -9
    assert runner._cleanup(process) is None
    killpg.assert_called_once_with(12345, signal.SIGKILL)
    process.wait.assert_called_once()


def test_launch_error_does_not_expose_exception_content(tmp_path, popen, caplog):
    popen.side_effect = OSError(5, "private token")
    result = runner.run_tests(tmp_path, [["tool", "secret-argument"]])
    assert not result.success and "errno=5" in result.error
    assert "private token" not in caplog.text + result.error
    assert "secret-argument" not in caplog.text


def test_logging_excludes_arguments_and_outputs(tmp_path, popen, caplog):
    with caplog.at_level(logging.INFO):
        runner.run_tests(tmp_path, [["tool", "--token=private-token"]])
    assert "tool" in caplog.text and "passed" in caplog.text
    assert "private-token" not in caplog.text
    assert "captured stdout" not in caplog.text
    assert "captured stderr" not in caplog.text


def test_interruption_cleans_up_before_propagating(tmp_path, popen, monkeypatch):
    popen.return_value.communicate.side_effect = KeyboardInterrupt
    cleanup = Mock(return_value=None)
    monkeypatch.setattr(runner, "_cleanup", cleanup)
    with pytest.raises(KeyboardInterrupt):
        runner.run_tests(tmp_path, [["tool"]])
    cleanup.assert_called_once_with(popen.return_value)


def test_harmless_local_process(tmp_path):
    result = runner.run_tests(tmp_path, [[sys.executable, "-c", "import sys; print('local output'); print('local error', file=sys.stderr)"]])
    assert result.success
    assert result.commands[0].stdout.strip() == "local output"
    assert result.commands[0].stderr.strip() == "local error"


def test_harmless_local_timeout(tmp_path):
    result = runner.run_tests(tmp_path, [[sys.executable, "-c", "import time; time.sleep(30)"]], timeout=0.5)
    assert not result.success and result.failed_command.timed_out
    assert result.failed_command.exit_code is not None
    assert result.failed_command.cleanup_error is None
