"""Windows-only, creation-time job containment for the Python 3.12 runner.

The narrow Popen adapter retains stdlib pipe/communication/wait handling while
using CreateProcessW's JOB_LIST attribute (not attach-after-launch). No shell=True,
breakaway, elevation, or fallback to uncontained execution is supported. Windows
may interpret batch files through its command shell, as with stdlib Popen.
"""

from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import logging
import os
import subprocess
import sys
from threading import Thread
import time


_CLEANUP_TIMEOUT = 5.0
_HANDLE_LIST = 0x20002
_JOB_LIST = 0x2000D
logger = logging.getLogger(__name__)


class ProcessSafetyError(RuntimeError):
    """Containment or cleanup failed; never a successful execution."""

    def __init__(self, message: str, *, stdout: str | bytes = "", stderr: str | bytes = "",
                 timed_out: bool = False, exit_code: int | None = None,
                 cleanup_error: str | None = None) -> None:
        super().__init__(message)
        self.stdout, self.stderr = stdout, stderr
        self.timed_out, self.exit_code = timed_out, exit_code
        self.cleanup_error = cleanup_error


class _BasicLimits(C.Structure):
    _fields_ = [("PerProcessUserTimeLimit", C.c_int64), ("PerJobUserTimeLimit", C.c_int64),
                ("LimitFlags", W.DWORD), ("MinimumWorkingSetSize", C.c_size_t),
                ("MaximumWorkingSetSize", C.c_size_t), ("ActiveProcessLimit", W.DWORD),
                ("Affinity", C.c_size_t), ("PriorityClass", W.DWORD), ("SchedulingClass", W.DWORD)]


class _IoCounters(C.Structure):
    _fields_ = [(name, C.c_uint64) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(C.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", C.c_size_t), ("JobMemoryLimit", C.c_size_t),
                ("PeakProcessMemoryUsed", C.c_size_t), ("PeakJobMemoryUsed", C.c_size_t)]


class _Accounting(C.Structure):
    _fields_ = [(name, C.c_int64) for name in
                ("TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")] + [
                (name, W.DWORD) for name in
                ("TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]


class _StartupInfo(C.Structure):
    _fields_ = [("cb", W.DWORD), ("lpReserved", W.LPWSTR), ("lpDesktop", W.LPWSTR),
                ("lpTitle", W.LPWSTR)] + [(name, W.DWORD) for name in
                ("dwX", "dwY", "dwXSize", "dwYSize", "dwXCountChars", "dwYCountChars",
                 "dwFillAttribute", "dwFlags")] + [
                ("wShowWindow", W.WORD), ("cbReserved2", W.WORD), ("lpReserved2", C.c_void_p),
                ("hStdInput", W.HANDLE), ("hStdOutput", W.HANDLE), ("hStdError", W.HANDLE)]


class _StartupInfoEx(C.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", C.c_void_p)]


class _ProcessInfo(C.Structure):
    _fields_ = [("hProcess", W.HANDLE), ("hThread", W.HANDLE),
                ("dwProcessId", W.DWORD), ("dwThreadId", W.DWORD)]


def _kernel_api():
    api = C.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([C.c_void_p, W.LPCWSTR], W.HANDLE),
        "SetHandleInformation": ([W.HANDLE, W.DWORD, W.DWORD], W.BOOL),
        "SetInformationJobObject": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
        "QueryInformationJobObject": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p], W.BOOL),
        "TerminateJobObject": ([W.HANDLE, W.UINT], W.BOOL),
        "CloseHandle": ([W.HANDLE], W.BOOL),
        "InitializeProcThreadAttributeList": ([C.c_void_p, W.DWORD, W.DWORD, C.POINTER(C.c_size_t)], W.BOOL),
        "UpdateProcThreadAttribute": ([C.c_void_p, W.DWORD, C.c_size_t, C.c_void_p,
                                       C.c_size_t, C.c_void_p, C.c_void_p], W.BOOL),
        "DeleteProcThreadAttributeList": ([C.c_void_p], None),
        "CreateProcessW": ([W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL,
                             W.DWORD, C.c_void_p, W.LPCWSTR, C.POINTER(_StartupInfoEx),
                             C.POINTER(_ProcessInfo)], W.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


def _require(ok, operation: str) -> None:
    if not ok:
        raise ProcessSafetyError(f"Windows {operation} failed (winerror={C.get_last_error()})")


class _Job:
    def __init__(self) -> None:
        self.api = _kernel_api()
        self.handle = self.api.CreateJobObjectW(None, None)  # Unnamed, non-inheritable.
        _require(self.handle, "job creation")
        try:
            _require(self.api.SetHandleInformation(self.handle, 1, 0), "job inheritance protection")
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE, no breakaway.
            _require(self.api.SetInformationJobObject(self.handle, 9, C.byref(limits), C.sizeof(limits)),
                     "kill-on-close containment setup")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.handle:
            _require(self.api.CloseHandle(self.handle), "job handle close")
            self.handle = None

    def terminate_and_confirm(self) -> None:
        # Also terminate surviving descendants after a normally exiting root.
        _require(self.api.TerminateJobObject(self.handle, 1), "job tree termination")
        self.wait_until_empty(_CLEANUP_TIMEOUT)

    def wait_until_empty(self, timeout: float | None) -> BaseException | None:
        """Confirm emptiness; emergency waits defer errors/cancellation to stay locked.

        An unbounded safety wait is used only after test cleanup has failed. It
        retains the job (and the synchronous caller's workspace lock) rather
        than permitting known or possibly live members to outlive that lock.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        interruption = None
        while True:
            try:
                time.sleep(0.01)
                accounting = _Accounting()
                _require(self.api.QueryInformationJobObject(self.handle, 1, C.byref(accounting),
                                                           C.sizeof(accounting), None), "job cleanup verification")
                if accounting.ActiveProcesses == 0:
                    return interruption
                if deadline is not None and time.monotonic() >= deadline:
                    raise ProcessSafetyError("Windows job cleanup could not be confirmed before deadline")
            except (Exception, KeyboardInterrupt, SystemExit) as exc:
                if timeout is not None:
                    raise
                if not isinstance(exc, Exception):
                    interruption = exc


class _JobProcess(subprocess.Popen):
    """Narrow adapter to Python 3.12 Popen internals; only piped stdio supported."""

    def __init__(self, arguments: list[str], *, job: _Job, cwd) -> None:
        self.job = job
        super().__init__(arguments, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                         shell=False, close_fds=True)

    def _execute_child(self, args, executable, preexec_fn, close_fds, pass_fds, cwd, env,
                       startupinfo, creationflags, shell, p2cread, p2cwrite,
                       c2pread, c2pwrite, errread, errwrite, *unused):
        # Popen builds the pipes, then delegates here. Creation-time JOB_LIST
        # makes assignment atomic even if the controller dies inside CreateProcess.
        api = self.job.api
        initialized = False
        try:
            if (shell or env is not None or startupinfo is not None or creationflags
                    or preexec_fn or pass_fds or -1 in (p2cread, c2pwrite, errwrite)):
                raise ProcessSafetyError("Unsupported contained process startup configuration")
            size = C.c_size_t()
            api.InitializeProcThreadAttributeList(None, 2, 0, C.byref(size))
            _require(size.value and C.get_last_error() == 122, "startup attribute sizing")
            buffer = C.create_string_buffer(size.value)
            _require(api.InitializeProcThreadAttributeList(buffer, 2, 0, C.byref(size)),
                     "startup attribute initialization")
            initialized = True
            pipes = (W.HANDLE * 3)(int(p2cread), int(c2pwrite), int(errwrite))
            jobs = (W.HANDLE * 1)(self.job.handle)
            for attribute, values in ((_HANDLE_LIST, pipes), (_JOB_LIST, jobs)):
                _require(api.UpdateProcThreadAttribute(buffer, 0, attribute, values,
                                                        C.sizeof(values), None, None),
                         "creation-time containment attributes")
            info = _StartupInfoEx()
            info.StartupInfo.cb = C.sizeof(info)
            info.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
            info.StartupInfo.hStdInput, info.StartupInfo.hStdOutput, info.StartupInfo.hStdError = pipes
            info.lpAttributeList = C.cast(buffer, C.c_void_p)
            process = _ProcessInfo()
            command = subprocess.list2cmdline(args)
            # Match Popen's Windows batch dispatch/quoting. Passing an explicit
            # application name here breaks batch filenames containing spaces.
            # JOB_LIST still applies to the OS-created command-shell wrapper.
            application = None if os.path.splitext(args[0])[1].lower() in (".bat", ".cmd") else args[0]
            sys.audit("subprocess.Popen", args[0], command, cwd, None)
            _require(api.CreateProcessW(application, C.create_unicode_buffer(command), None, None, True,
                                         0x80000 | 0x08000000, None, os.fsdecode(cwd),
                                         C.byref(info), C.byref(process)), "contained process launch")
            self._child_created = True
            self._handle = subprocess.Handle(process.hProcess)
            self.pid = process.dwProcessId
            _require(api.CloseHandle(process.hThread), "primary thread handle close")
        finally:
            if initialized:
                api.DeleteProcThreadAttributeList(buffer)
            self._close_pipe_fds(p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)


def run_contained(arguments: list[str], *, input: str, cwd, timeout: float,
                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                  check=False, shell=False, wait_for_root_exit: bool = False) -> subprocess.CompletedProcess:
    """Run contained; test mode ends on root exit, not descendant-held pipe EOF.

    Test mode also fails closed on cleanup errors: no return/raise (and hence no
    workspace unlock by a synchronous caller) until job emptiness is verified.
    Codex's existing communication timeout and bounded cleanup stay unchanged.
    """
    if os.name != "nt" or sys.version_info[:2] != (3, 12):
        raise ProcessSafetyError("Windows containment requires Windows and Python 3.12")
    if not capture_output or not text or encoding != "utf-8" or errors != "replace" or check or shell:
        raise ProcessSafetyError("Unsupported contained process options")
    job = _Job()
    process = None
    communication = None
    outcome = []
    stdout = stderr = ""
    failure = None
    cleanup_errors = []
    try:
        process = _JobProcess(arguments, job=job, cwd=cwd)

        def communicate():
            try:
                outcome.append(process.communicate(input=input))
            except BaseException as exc:
                outcome.append(exc)

        # Windows Popen writes stdin synchronously, before its timeout waits.
        # Bound the whole communication, including a child that never reads stdin.
        communication = Thread(target=communicate, daemon=True)
        communication.start()
        if wait_for_root_exit:
            # Observe the root's actual exit code before terminating descendants.
            # Their inherited output handles must not turn a normal exit into a timeout.
            process.wait(timeout=timeout)
        else:
            communication.join(timeout)
            if communication.is_alive():
                raise subprocess.TimeoutExpired(arguments, timeout)
            if isinstance(outcome[0], BaseException):
                raise outcome[0]
            stdout, stderr = outcome[0]
    except BaseException as exc:
        failure = exc
        if isinstance(exc, subprocess.TimeoutExpired):
            stdout, stderr = exc.stdout or "", exc.stderr or ""
    finally:
        try:
            job.terminate_and_confirm()
        except (Exception, KeyboardInterrupt, SystemExit) as exc:
            if isinstance(exc, Exception):
                cleanup_errors.append(str(exc) if isinstance(exc, ProcessSafetyError)
                                      else f"Windows job cleanup failed ({type(exc).__name__})")
            else:
                failure = exc
            if wait_for_root_exit:
                try:
                    logger.critical("Test process cleanup failed; retaining containment and workspace ownership "
                                    "until Windows confirms no active job members")
                except Exception:
                    pass  # A failing log handler must not bypass the safety wait.
                interruption = job.wait_until_empty(None)
                if interruption is not None:
                    failure = interruption
        finally:
            try:
                job.close()  # OS kill-on-close fallback even if verification failed.
            except ProcessSafetyError as exc:
                cleanup_errors.append(str(exc))
        if process is not None:
            if communication is not None:
                communication.join(_CLEANUP_TIMEOUT)
                if communication.is_alive():
                    cleanup_errors.append("Contained output/input cleanup could not be confirmed")
                elif outcome and not isinstance(outcome[0], BaseException):
                    stdout, stderr = outcome[0]
                elif outcome and failure is None:
                    failure = outcome[0]
            try:
                process.wait(timeout=_CLEANUP_TIMEOUT)
            except (OSError, subprocess.TimeoutExpired):
                cleanup_errors.append("Contained process cleanup could not be confirmed")
            if communication is None or not communication.is_alive():
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        try:
                            stream.close()
                        except OSError:
                            cleanup_errors.append("Contained pipe close failed")
    if cleanup_errors:
        message = "; ".join(cleanup_errors)
        if failure is not None and not isinstance(failure, Exception):
            failure.add_note(message)
            raise failure
        prefix = "Process execution timed out; " if isinstance(failure, subprocess.TimeoutExpired) else ""
        raise ProcessSafetyError(prefix + message, stdout=stdout, stderr=stderr,
                                 timed_out=isinstance(failure, subprocess.TimeoutExpired),
                                 exit_code=process.returncode if process is not None else None,
                                 cleanup_error=message)
    if failure is not None:
        if isinstance(failure, subprocess.TimeoutExpired):
            failure.stdout, failure.stderr = stdout, stderr
            failure.exit_code = process.returncode if process is not None else None
        raise failure
    return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)
