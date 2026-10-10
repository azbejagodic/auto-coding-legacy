"""Sacrificial Python trees only: never launch Codex or contact the network."""

import ctypes as C
from ctypes import wintypes as W
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from auto_coding.integrations import windows_process as windows
from auto_coding.integrations.codex import run_codex


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Job Object integration")

TREE = """
import json, os, pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(), child.pid]))
print('{"type":"turn.completed"}', flush=True)
if sys.argv[2] == 'hold':
    time.sleep(60)
"""

CONTROLLER = """
import pathlib, sys, time
from auto_coding.integrations import windows_process as windows
if sys.argv[3] == 'creation':
    original_api = windows._kernel_api
    def paused_api():
        api = original_api()
        create = api.CreateProcessW
        def paused_create(*args):
            result = create(*args)
            if result:
                time.sleep(60)  # Die before Popen even receives the process handle.
            return result
        api.CreateProcessW = paused_create
        return api
    windows._kernel_api = paused_api
windows.run_contained([sys.executable, '-c', sys.argv[2], sys.argv[1], 'hold'],
                      input='', cwd=pathlib.Path.cwd(), timeout=60)
"""


def wait_for_tree(path):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.02)
    pytest.fail("Sacrificial process tree did not become ready")


class ProcessHandle:
    """Retain a handle to avoid PID-reuse races in assertions and test cleanup."""

    def __init__(self, pid):
        self.api = C.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            ("WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD),
            ("TerminateProcess", [W.HANDLE, W.UINT], W.BOOL),
            ("CloseHandle", [W.HANDLE], W.BOOL),
        ):
            fn = getattr(self.api, name)
            fn.argtypes, fn.restype = args, result
        self.handle = self.api.OpenProcess(0x100001, False, pid)  # Synchronize + terminate, our test tree only.
        assert self.handle, f"Could not retain sacrificial process {pid}"

    def stopped(self, timeout=5000):
        return self.api.WaitForSingleObject(self.handle, timeout) == 0

    def close(self):
        if not self.stopped(0):
            self.api.TerminateProcess(self.handle, 1)
            assert self.stopped()
        self.api.CloseHandle(self.handle)


def test_normal_execution_forwards_stdin_and_returns_output(tmp_path):
    result = windows.run_contained([sys.executable, "-c", "import sys; print(sys.stdin.read()); print('err', file=sys.stderr)"],
                                   input="quotes & unicode ž\n", cwd=tmp_path, timeout=5)
    assert result.returncode == 0
    assert result.stdout == "quotes & unicode ž\n\n" and result.stderr == "err\n"


@pytest.mark.parametrize("mode", ["hold", "exit"])
def test_tree_termination_after_timeout_or_normal_root_exit(tmp_path, mode):
    path = tmp_path / "tree.json"
    handles = []
    # Keep the root alive briefly in 'exit' mode to retain the descendant handle.
    script = TREE.replace("if sys.argv[2] == 'hold':", "time.sleep(0.5)\nif sys.argv[2] == 'hold':")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(windows.run_contained,
                             [sys.executable, "-c", script, str(path), mode],
                             input="", cwd=tmp_path, timeout=2)
        try:
            pids = wait_for_tree(path)
            handles = [ProcessHandle(pid) for pid in pids]
            if mode == "hold":
                with pytest.raises(subprocess.TimeoutExpired):
                    future.result(timeout=15)
            else:
                assert future.result(timeout=15).returncode == 0
            assert all(handle.stopped() for handle in handles)
        finally:
            for handle in handles:
                handle.close()


@pytest.mark.parametrize("phase", ["running", "creation"])
def test_forced_controller_exit_kills_tree_but_not_unrelated_process(tmp_path, phase):
    path = tmp_path / "tree.json"
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    controller = subprocess.Popen([sys.executable, "-c", CONTROLLER, str(path), TREE, phase],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    handles = []
    try:
        handles = [ProcessHandle(pid) for pid in wait_for_tree(path)]
        controller.kill()  # Abrupt TerminateProcess; no Python finally handlers.
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


def test_runner_timeout_preserves_output_and_stops_children(tmp_path, monkeypatch):
    path = tmp_path / "tree.json"
    monkeypatch.setattr("auto_coding.integrations.codex.shutil.which", lambda _: sys.executable)

    def harmless(arguments, **options):
        assert arguments[2:4] == ["--model", "exact-model"]
        assert options["input"] == "private prompt"
        return windows.run_contained([sys.executable, "-c", TREE, str(path), "hold"], **options)

    monkeypatch.setattr("auto_coding.integrations.codex._run_process", harmless)
    handles = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_codex, "private prompt", "exact-model", tmp_path, 2)
        try:
            handles = [ProcessHandle(pid) for pid in wait_for_tree(path)]
            result = future.result(timeout=15)
            assert not result.success and result.timed_out
            assert "timed out" in result.error and '"turn.completed"' in result.stdout
            assert all(handle.stopped() for handle in handles)
        finally:
            for handle in handles:
                handle.close()


def test_timeout_covers_child_that_never_reads_large_prompt(tmp_path):
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        windows.run_contained([sys.executable, "-c", "import time; time.sleep(60)"],
                              input="x" * 2_000_000, cwd=tmp_path, timeout=0.3)
    assert time.monotonic() - started < 10


def test_containment_configuration_failure_does_not_launch(tmp_path, monkeypatch):
    api = windows._kernel_api()
    api.SetInformationJobObject = Mock(return_value=False)
    monkeypatch.setattr(windows, "_kernel_api", lambda: api)
    launch = Mock()
    monkeypatch.setattr(windows, "_JobProcess", launch)
    with pytest.raises(windows.ProcessSafetyError, match="containment setup"):
        windows.run_contained([sys.executable], input="", cwd=tmp_path, timeout=1)
    launch.assert_not_called()


def test_creation_attribute_failure_does_not_start_uncontained_process(tmp_path, monkeypatch):
    api = windows._kernel_api()
    api.UpdateProcThreadAttribute = Mock(return_value=False)
    api.CreateProcessW = Mock()
    monkeypatch.setattr(windows, "_kernel_api", lambda: api)
    with pytest.raises(windows.ProcessSafetyError, match="containment attributes"):
        windows.run_contained([sys.executable], input="", cwd=tmp_path, timeout=1)
    api.CreateProcessW.assert_not_called()


def test_cleanup_uncertainty_is_never_success(tmp_path, monkeypatch):
    original = windows._Job.terminate_and_confirm

    def uncertain(job):
        original(job)
        raise windows.ProcessSafetyError("cleanup verification failed")

    monkeypatch.setattr(windows._Job, "terminate_and_confirm", uncertain)
    with pytest.raises(windows.ProcessSafetyError, match="cleanup verification"):
        windows.run_contained([sys.executable, "-c", "print('done')"],
                              input="", cwd=tmp_path, timeout=5)


def test_normal_operation_with_unelevated_token(tmp_path):
    shell = C.WinDLL("shell32")
    shell.IsUserAnAdmin.argtypes, shell.IsUserAnAdmin.restype = [], W.BOOL
    if shell.IsUserAnAdmin():
        pytest.skip("This session is elevated; exercise non-admin operation in an unelevated session")
    result = windows.run_contained([sys.executable, "-c", "print('non-admin')"],
                                   input="", cwd=tmp_path, timeout=5)
    assert result.returncode == 0 and result.stdout.strip() == "non-admin"
