"""Real harmless Windows test trees; no Codex, network, or GitHub operations."""

from concurrent.futures import ThreadPoolExecutor
import ctypes as C
import os
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from auto_coding.integrations import windows_process as windows
from auto_coding.storage import TaskStatus
from auto_coding.test_runner import run_tests
from auto_coding.workspace_lock import WorkspaceBusyError
from test_controller import repository_factory, setup
from test_windows_process import ProcessHandle, wait_for_tree


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows test Job Object integration")

TREE = """
import json, os, pathlib, subprocess, sys, time
flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if sys.argv[2] == 'detached' else 0
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                         stdin=subprocess.DEVNULL, creationflags=flags)
pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(), child.pid]))
print('captured output', flush=True)
print('captured error', file=sys.stderr, flush=True)
time.sleep(0.5)  # Let the test retain process handles before the root exits.
if sys.argv[3] == 'hold':
    time.sleep(60)
sys.exit(int(sys.argv[4]))
"""

CONTROLLER = """
import pathlib, sys, time
from auto_coding.integrations import windows_process as windows
from auto_coding.test_runner import run_tests
from auto_coding.workspace_lock import WorkspaceLock
if sys.argv[3] == 'creation':
    original_api = windows._kernel_api
    def paused_api():
        api = original_api()
        create = api.CreateProcessW
        def paused_create(*args):
            result = create(*args)
            if result:
                time.sleep(60)
            return result
        api.CreateProcessW = paused_create
        return api
    windows._kernel_api = paused_api
with WorkspaceLock(pathlib.Path.cwd(), lock_directory=sys.argv[4]):
    run_tests(pathlib.Path.cwd(), [[sys.executable, '-c', sys.argv[2], sys.argv[1], 'detached', 'hold', '0']], timeout=60)
"""


@pytest.mark.parametrize("child_kind", ["normal", "detached"])
@pytest.mark.parametrize("mode,exit_code", [("exit", 0), ("exit", 7), ("hold", 0)])
def test_normal_exit_and_timeout_stop_descendants_and_preserve_output(tmp_path, child_kind, mode, exit_code):
    path = tmp_path / "tree.json"
    commands = [[sys.executable, "-c", TREE, str(path), child_kind, mode, str(exit_code)]]
    handles = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_tests, tmp_path, commands, timeout=2)
        try:
            handles = [ProcessHandle(pid) for pid in wait_for_tree(path)]
            result = future.result(timeout=15)
            command = result.commands[0]
            assert result.success == (mode == "exit" and exit_code == 0)
            assert command.timed_out == (mode == "hold")
            assert command.exit_code == (1 if mode == "hold" else exit_code)
            assert command.stdout == "captured output\n" and command.stderr == "captured error\n"
            assert command.cleanup_error is None
            # Descendant-held pipes must not delay a normal root exit until timeout.
            if mode == "exit":
                assert command.duration_seconds < 2
            assert all(handle.stopped(0) for handle in handles)
        finally:
            for handle in handles:
                handle.close()


@pytest.mark.parametrize("phase", ["running", "creation"])
def test_forced_controller_death_kills_detached_tree_without_touching_unrelated(tmp_path, phase):
    path = tmp_path / "tree.json"
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    controller = subprocess.Popen(
        [sys.executable, "-c", CONTROLLER, str(path), TREE, phase, str(tmp_path.parent / "locks")],
        cwd=tmp_path, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    handles = []
    try:
        handles = [ProcessHandle(pid) for pid in wait_for_tree(path)]
        controller.kill()  # No Python cleanup handlers run.
        controller.communicate(timeout=10)
        assert all(handle.stopped() for handle in handles)
        assert unrelated.poll() is None
    finally:
        if controller.poll() is None:
            controller.kill()
        controller.communicate(timeout=10)
        for handle in handles:
            handle.close()
        unrelated.kill()
        unrelated.wait(timeout=5)


def test_sequential_commands_start_only_after_previous_descendants_stop(tmp_path, monkeypatch):
    original = windows._Job.terminate_and_confirm
    calls = []
    handles = []

    def cleanup(job):
        path = tmp_path / f"tree{len(calls)}.json"
        tree = [ProcessHandle(pid) for pid in wait_for_tree(path)]
        handles.extend(tree)
        assert not tree[1].stopped(0)
        original(job)
        assert all(handle.stopped(0) for handle in tree)
        calls.append(path)

    monkeypatch.setattr(windows._Job, "terminate_and_confirm", cleanup)
    commands = [[sys.executable, "-c", TREE, str(tmp_path / f"tree{i}.json"), "detached", "exit", "0"]
                for i in range(2)]
    try:
        result = run_tests(tmp_path, commands, timeout=5)
        assert result.success and result.commands_executed == 2
        assert len(calls) == 2
        assert all(handle.stopped(0) for handle in handles)
    finally:
        for handle in handles:
            handle.close()


@pytest.mark.parametrize("exit_code", [0, 7])
@pytest.mark.parametrize("batch_name", ["approved.cmd", "approved test.cmd"])
def test_windows_batch_commands_preserve_compatibility_and_containment(tmp_path, exit_code, batch_name):
    # Windows may dispatch batch files through cmd.exe even with shell=False.
    # Its wrapper and Python descendants must all remain inside the same job.
    script = tmp_path / "harmless test.py"
    script.write_text(TREE, encoding="utf-8")
    command = tmp_path / batch_name
    # Keep batch source ASCII: Unicode paths travel through CreateProcessW's
    # arguments rather than depending on cmd.exe's current source code page.
    command.write_text(f'@echo off\n"%~2" "%~3" "%~1" detached exit {exit_code}\n'
                       'exit /b %errorlevel%\n', encoding="ascii")
    path = tmp_path / "tree.json"
    handles = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_tests, tmp_path, [[str(command), str(path), sys.executable, str(script)]], timeout=5)
        try:
            handles = [ProcessHandle(pid) for pid in wait_for_tree(path)]
            result = future.result(timeout=15)
            assert result.success == (exit_code == 0)
            assert result.commands[0].exit_code == exit_code
            assert result.commands[0].stdout == "captured output\n"
            assert result.commands[0].stderr == "captured error\n"
            assert all(handle.stopped(0) for handle in handles)
        finally:
            for handle in handles:
                handle.close()


@pytest.mark.parametrize("failure", ["setup", "attributes"])
def test_failed_containment_never_launches_or_commits(setup, monkeypatch, failure):
    _, store, controller, _, _, directory = setup
    repo = repository_factory(directory)
    controller.repository_factory = lambda _: repo
    controller.test_runner = run_tests
    api = windows._kernel_api()
    if failure == "setup":
        api.SetInformationJobObject = Mock(return_value=False)
    else:
        api.UpdateProcThreadAttribute = Mock(return_value=False)
    api.CreateProcessW = Mock()
    monkeypatch.setattr(windows, "_kernel_api", lambda: api)
    result = controller.execute("TASK-A", directory, [[sys.executable, "-c", "print('never')"]])
    assert not result.success and result.final_status == TaskStatus.FAILED
    assert "containment" in result.error
    assert store.get_task("TASK-A").status == TaskStatus.FAILED
    assert store.get_commit("TASK-A") is None
    api.CreateProcessW.assert_not_called()
    repo.commit_files.assert_not_called()
    repo.push_branch.assert_not_called()
    with controller.lock_factory(directory):
        pass


@pytest.mark.parametrize("mode,cleanup_failure", [("exit", False), ("exit", True), ("hold", True)])
def test_controller_keeps_lock_until_actual_cleanup_and_blocks_uncertain_commits(setup, monkeypatch, mode, cleanup_failure):
    _, store, controller, _, _, directory = setup
    controller.test_runner = run_tests
    repo = repository_factory(directory)
    controller.repository_factory = lambda _: repo
    path = directory / "tree.json"
    handles = []
    original = windows._Job.terminate_and_confirm

    def cleanup(job):
        handles.extend(ProcessHandle(pid) for pid in wait_for_tree(path))
        assert not handles[1].stopped(0)
        with pytest.raises(WorkspaceBusyError):
            with controller.lock_factory(directory):
                pass
        repo.commit_files.assert_not_called()
        original(job)
        assert all(handle.stopped(0) for handle in handles)
        with pytest.raises(WorkspaceBusyError):
            with controller.lock_factory(directory):
                pass
        if cleanup_failure:
            raise windows.ProcessSafetyError("cleanup verification failed")

    monkeypatch.setattr(windows._Job, "terminate_and_confirm", cleanup)
    try:
        result = controller.execute("TASK-A", directory,
                                    [[sys.executable, "-c", TREE, str(path), "detached", mode, "0"]],
                                    test_timeout=2)
        assert result.success == (not cleanup_failure)
        assert all(handle.stopped(0) for handle in handles)
        if cleanup_failure:
            assert result.test_result.failed_command.exit_code == (1 if mode == "hold" else 0)
            assert result.test_result.failed_command.timed_out == (mode == "hold")
            assert result.test_result.failed_command.cleanup_error == "cleanup verification failed"
            assert result.final_status == TaskStatus.FAILED and "cleanup verification failed" in result.error
            assert store.get_commit("TASK-A") is None
            repo.commit_files.assert_not_called()
            repo.push_branch.assert_not_called()
        with controller.lock_factory(directory):
            pass
    finally:
        for handle in handles:
            handle.close()


@pytest.mark.parametrize("interrupt_query", [False, True])
@pytest.mark.parametrize("cleanup_error", [windows.ProcessSafetyError, OSError])
def test_failed_termination_retains_lock_while_known_descendant_is_active(setup, monkeypatch, caplog,
                                                                        interrupt_query, cleanup_error):
    _, store, controller, _, _, directory = setup
    controller.test_runner = run_tests
    repo = repository_factory(directory)
    controller.repository_factory = lambda _: repo
    path = directory / "tree.json"
    # A bounded sacrificial child exits naturally if termination fails.
    script = TREE.replace("time.sleep(60)", "time.sleep(1.5)")
    handles = []
    original = windows._Job.wait_until_empty

    def terminate(job):
        handles.extend(ProcessHandle(pid) for pid in wait_for_tree(path))
        assert not handles[1].stopped(0)
        raise cleanup_error("job tree termination failed" if cleanup_error is windows.ProcessSafetyError
                            else "private cleanup detail")

    def wait(job, timeout):
        assert timeout is None
        with pytest.raises(WorkspaceBusyError):
            with controller.lock_factory(directory):
                pass
        assert not handles[1].stopped(0)
        # An accounting failure and cancellation during emergency cleanup must
        # not escape the safety wait and unlock while the child is still alive.
        query = job.api.QueryInformationJobObject
        failures = [False, KeyboardInterrupt() if interrupt_query else OSError("private accounting detail")]

        def transient(*args):
            if failures:
                outcome = failures.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome
            return query(*args)

        job.api.QueryInformationJobObject = transient
        started = time.monotonic()
        interruption = original(job, timeout)
        assert time.monotonic() - started > 0.3
        assert all(handle.stopped(0) for handle in handles)
        assert isinstance(interruption, KeyboardInterrupt) if interrupt_query else interruption is None
        return interruption

    monkeypatch.setattr(windows._Job, "terminate_and_confirm", terminate)
    monkeypatch.setattr(windows._Job, "wait_until_empty", wait)
    try:
        commands = [[sys.executable, "-c", script, str(path), "detached", "exit", "0"]]
        if interrupt_query:
            with pytest.raises(KeyboardInterrupt):
                controller.execute("TASK-A", directory, commands)
            assert store.get_task("TASK-A").status == TaskStatus.RUNNING
        else:
            result = controller.execute("TASK-A", directory, commands)
            assert not result.success and result.final_status == TaskStatus.FAILED
            assert "failed" in result.error and "private" not in result.error
        assert "retaining containment" in caplog.text and "private" not in caplog.text
        repo.commit_files.assert_not_called()
        repo.push_branch.assert_not_called()
        with controller.lock_factory(directory):
            pass
    finally:
        for handle in handles:
            handle.close()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_cleanup_interruption_cannot_unlock_before_known_child_exits(setup, monkeypatch, interruption):
    _, store, controller, _, _, directory = setup
    controller.test_runner = run_tests
    repo = repository_factory(directory)
    controller.repository_factory = lambda _: repo
    path = directory / "tree.json"
    script = TREE.replace("time.sleep(60)", "time.sleep(1.2)")
    handles = []
    original = windows._Job.wait_until_empty

    def interrupt(job):
        handles.extend(ProcessHandle(pid) for pid in wait_for_tree(path))
        assert not handles[1].stopped(0)
        raise interruption

    def wait(job, timeout):
        assert timeout is None
        with pytest.raises(WorkspaceBusyError):
            with controller.lock_factory(directory):
                pass
        result = original(job, timeout)
        assert all(handle.stopped(0) for handle in handles)
        return result

    monkeypatch.setattr(windows._Job, "terminate_and_confirm", interrupt)
    monkeypatch.setattr(windows._Job, "wait_until_empty", wait)
    try:
        with pytest.raises(interruption):
            controller.execute("TASK-A", directory,
                               [[sys.executable, "-c", script, str(path), "detached", "exit", "0"]])
        assert store.get_task("TASK-A").status == TaskStatus.RUNNING
        repo.commit_files.assert_not_called()
        repo.push_branch.assert_not_called()
        with controller.lock_factory(directory):
            pass
    finally:
        for handle in handles:
            handle.close()


def test_real_test_failure_prevents_commit_and_review(setup):
    _, store, controller, _, _, directory = setup
    controller.test_runner = run_tests
    repo = repository_factory(directory)
    controller.repository_factory = lambda _: repo
    result = controller.execute("TASK-A", directory, [[sys.executable, "-c", "import sys; sys.exit(7)"]])
    assert not result.success and result.final_status == TaskStatus.FAILED
    assert result.test_result.failed_command.exit_code == 7
    assert store.get_commit("TASK-A") is None
    repo.commit_files.assert_not_called()
    repo.push_branch.assert_not_called()


def test_test_runner_operates_without_administrator_privileges(tmp_path):
    shell = C.WinDLL("shell32")
    if shell.IsUserAnAdmin():
        pytest.skip("Exercise non-admin operation in an unelevated session")
    result = run_tests(tmp_path, [[sys.executable, "-c", "print('non-admin')"]])
    assert result.success and result.commands[0].stdout == "non-admin\n"
