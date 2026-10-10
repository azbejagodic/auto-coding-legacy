"""Fail-fast OS file locks for local workspaces, independent of task IDs."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
from pathlib import Path
import sys
from types import TracebackType
from typing import BinaryIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


logger = logging.getLogger(__name__)


class WorkspaceLockError(RuntimeError):
    """Workspace validation or lock I/O failed."""


class WorkspaceBusyError(WorkspaceLockError):
    """Another owner holds the workspace lock."""


def _application_lock_directory() -> Path:
    if os.name == "nt":
        local_data = os.environ.get("LOCALAPPDATA")
        root = (Path(local_data) if local_data and Path(local_data).is_absolute()
                else Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        configured = os.environ.get("XDG_DATA_HOME")
        root = (Path(configured) if configured and Path(configured).is_absolute()
                else Path.home() / ".local" / "share")
    return root / "auto-coding" / "workspace-locks"


class WorkspaceLock:
    """Lock a canonical workspace, without deleting or treating files as leases.

    An optional lock_directory permits isolated tests or a shared local data
    location. All cooperating controllers must use the same lock directory.
    """

    def __init__(self, workspace: str | Path, *, lock_directory: str | Path | None = None) -> None:
        try:
            self.workspace = Path(workspace).resolve(strict=True)
            if not self.workspace.is_dir():
                raise WorkspaceLockError("Project workspace is not a directory")
            root = Path(lock_directory) if lock_directory is not None else _application_lock_directory()
            self.lock_directory = root.resolve()
            if self.lock_directory.is_relative_to(self.workspace):
                raise WorkspaceLockError("Lock directory must be outside the project workspace")
            for parent in (self.workspace, *self.workspace.parents):
                if (parent / ".git").exists():
                    if self.lock_directory.is_relative_to(parent):
                        raise WorkspaceLockError("Lock directory must be outside the target Git repository")
                    break
            identity = os.path.normcase(str(self.workspace))
            digest = hashlib.sha256(os.fsencode(identity)).hexdigest()
            self.lock_path = self.lock_directory / f"{digest}.lock"
        except (OSError, ValueError, TypeError):
            raise WorkspaceLockError("Project workspace or lock directory is invalid or inaccessible") from None
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        if self._file is not None:
            raise WorkspaceLockError("This lock object already owns a workspace lock")
        handle = None
        try:
            self.lock_directory.mkdir(parents=True, exist_ok=True)
            handle = self.lock_path.open("a+b")
            os.set_inheritable(handle.fileno(), False)
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise WorkspaceBusyError("Workspace is busy; another controller holds its lock") from None
                raise
        except (OSError, WorkspaceLockError) as exc:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    raise WorkspaceLockError("Lock acquisition failed and the lock handle could not close") from None
            if isinstance(exc, WorkspaceLockError):
                raise
            raise WorkspaceLockError(f"Workspace lock acquisition failed (errno={exc.errno})") from None
        self._file = handle

    def release(self) -> None:
        if self._file is None:
            return
        handle = self._file
        self._file = None
        errors = []
        try:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            errors.append("Workspace lock unlock failed")
        finally:
            try:
                handle.close()  # Closing also releases an OS lock.
            except OSError:
                errors.append("Workspace lock handle close failed; lock ownership is uncertain")
        if errors:
            raise WorkspaceLockError("; ".join(errors))

    def __enter__(self) -> WorkspaceLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc_value: BaseException | None, traceback: TracebackType | None) -> None:
        try:
            self.release()
        except WorkspaceLockError as error:
            if exc_value is None:
                raise
            # Preserve interruption semantics, but do not conceal cleanup failure.
            exc_value.add_note(str(error))
            logger.error("Workspace lock release failed during interruption/exception")
