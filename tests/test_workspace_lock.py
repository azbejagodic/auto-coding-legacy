"""Real OS-lock tests: no Codex, network, or application-data writes."""

import os
from pathlib import Path
from queue import Queue
import subprocess
import sys
from threading import Thread

import pytest

from auto_coding.workspace_lock import WorkspaceBusyError, WorkspaceLock, WorkspaceLockError
from auto_coding import workspace_lock as lock_module


CHILD = """
import sys
from auto_coding.workspace_lock import WorkspaceLock, WorkspaceBusyError
try:
    with WorkspaceLock(sys.argv[1], lock_directory=sys.argv[2]):
        print('acquired', flush=True)
        if sys.argv[3] == 'hold':
            sys.stdin.readline()
except WorkspaceBusyError:
    print('busy', flush=True)
    sys.exit(2)
"""


@pytest.fixture
def paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace, tmp_path / "application-data" / "locks"


def attempt(workspace, root):
    return subprocess.run([sys.executable, "-c", CHILD, str(workspace), str(root), "once"],
                          capture_output=True, text=True, timeout=10, shell=False)


def test_acquire_contention_release_and_persistent_file(paths):
    workspace, root = paths
    lock = WorkspaceLock(workspace, lock_directory=root)
    with lock:
        assert lock.lock_path.is_file()
        with pytest.raises(WorkspaceBusyError):
            WorkspaceLock(workspace, lock_directory=root).acquire()
        with pytest.raises(WorkspaceLockError, match="already owns"):
            lock.acquire()
    assert lock.lock_path.exists()  # File existence is not lock ownership.
    with WorkspaceLock(workspace, lock_directory=root):
        pass
    lock.release()  # Repeated cleanup is safe.


def test_exception_releases_lock(paths):
    workspace, root = paths
    with pytest.raises(RuntimeError):
        with WorkspaceLock(workspace, lock_directory=root):
            raise RuntimeError("test")
    with WorkspaceLock(workspace, lock_directory=root):
        pass


def test_independent_process_contention_and_independent_workspace(paths):
    workspace, root = paths
    other = workspace.parent / "other"
    other.mkdir()
    with WorkspaceLock(workspace, lock_directory=root):
        busy = attempt(workspace, root)
        assert busy.returncode == 2 and busy.stdout.strip() == "busy", busy.stderr
        independent = attempt(other, root)
        assert independent.returncode == 0, independent.stderr
        assert independent.stdout.strip() == "acquired"
    released = attempt(workspace, root)
    assert released.returncode == 0, released.stderr


def test_terminated_process_releases_os_lock(paths):
    workspace, root = paths
    process = subprocess.Popen([sys.executable, "-u", "-c", CHILD, str(workspace), str(root), "hold"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, shell=False)
    messages = Queue()
    reader = Thread(target=lambda: messages.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        assert messages.get(timeout=10).strip() == "acquired"
        with pytest.raises(WorkspaceBusyError):
            WorkspaceLock(workspace, lock_directory=root).acquire()
        process.kill()  # Only our temporary test child; no cooperative cleanup.
        process.wait(timeout=10)
        with WorkspaceLock(workspace, lock_directory=root) as lock:
            assert lock.lock_path.exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
        reader.join(timeout=2)


def test_equivalent_paths_use_same_lock(paths, monkeypatch):
    workspace, root = paths
    monkeypatch.chdir(workspace.parent)
    aliases = [Path("workspace"), workspace / ".", workspace / ".." / "workspace"]
    if os.name == "nt":
        aliases.append(Path(str(workspace).swapcase()))
    with WorkspaceLock(workspace, lock_directory=root) as owner:
        for alias in aliases:
            contender = WorkspaceLock(alias, lock_directory=root)
            assert contender.lock_path == owner.lock_path
            with pytest.raises(WorkspaceBusyError):
                contender.acquire()


def test_directory_symlink_alias_uses_same_lock(paths):
    workspace, root = paths
    alias = workspace.parent / "alias"
    try:
        alias.symlink_to(workspace, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks require privileges on this Windows installation")
    with WorkspaceLock(workspace, lock_directory=root) as owner:
        contender = WorkspaceLock(alias, lock_directory=root)
        assert contender.lock_path == owner.lock_path
        with pytest.raises(WorkspaceBusyError):
            contender.acquire()


def test_invalid_workspace_and_in_repository_lock_directory_rejected(paths):
    workspace, root = paths
    with pytest.raises(WorkspaceLockError):
        WorkspaceLock(workspace / "missing", lock_directory=root)
    file = workspace / "file"
    file.touch()
    with pytest.raises(WorkspaceLockError, match="not a directory"):
        WorkspaceLock(file, lock_directory=root)
    with pytest.raises(WorkspaceLockError, match="outside"):
        WorkspaceLock(workspace, lock_directory=workspace / "locks")
    (workspace / ".git").mkdir()
    child = workspace / "child"
    child.mkdir()
    with pytest.raises(WorkspaceLockError, match="Git repository"):
        WorkspaceLock(child, lock_directory=workspace / "locks")


def test_acquisition_io_error_is_explicit(paths):
    workspace, root = paths
    root.parent.mkdir()
    root.touch()
    with pytest.raises(WorkspaceLockError, match="acquisition failed"):
        WorkspaceLock(workspace, lock_directory=root).acquire()


def test_release_error_is_explicit_and_preserves_original_exception(paths, monkeypatch):
    workspace, root = paths
    lock = WorkspaceLock(workspace, lock_directory=root)
    release = lock.release

    def fail_release():
        release()
        raise WorkspaceLockError("unlock failed")

    monkeypatch.setattr(lock, "release", fail_release)
    with pytest.raises(WorkspaceLockError, match="unlock failed"):
        with lock:
            pass
    with pytest.raises(KeyboardInterrupt) as error:
        with lock:
            raise KeyboardInterrupt()
    assert "unlock failed" in error.value.__notes__
    with WorkspaceLock(workspace, lock_directory=root):
        pass


def test_os_unlock_failure_closes_handle_and_reports_error(paths, monkeypatch):
    workspace, root = paths
    lock = WorkspaceLock(workspace, lock_directory=root)
    lock.acquire()
    if os.name == "nt":
        api, name = lock_module.msvcrt, "locking"
    else:
        api, name = lock_module.fcntl, "flock"
    with monkeypatch.context() as patch:
        def fail(*args):
            raise OSError("simulated unlock error")
        patch.setattr(api, name, fail)
        with pytest.raises(WorkspaceLockError, match="unlock failed"):
            lock.release()
    with WorkspaceLock(workspace, lock_directory=root):
        pass


def test_default_lock_location_uses_local_application_data(paths, monkeypatch):
    workspace, root = paths
    monkeypatch.setenv("LOCALAPPDATA", str(root))
    monkeypatch.setenv("XDG_DATA_HOME", str(root))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
    lock = WorkspaceLock(workspace)
    assert lock.lock_directory.name == "workspace-locks"
    assert lock.lock_directory.parent.name == "auto-coding"
    assert lock.lock_directory.is_relative_to(root)
    assert not lock.lock_directory.exists()  # Construction only validates paths.
    with lock:
        assert lock.lock_path.is_file()
