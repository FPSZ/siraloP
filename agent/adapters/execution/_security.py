"""Narrow process protections, not a filesystem/network or OS sandbox."""
from __future__ import annotations

import ctypes
import sys
from typing import Any


def protect_controller() -> None:
    """Protect the credential owner and make inherited execution non-escalating.

    These flags are applied in the spawning controller thread and inherited by
    children, avoiding unsafe Python preexec hooks. Linux resets dumpability on
    exec; no_new_privs does not reset. Thus tools may debug their own dumpable
    children, but cannot regain CAP_SYS_PTRACE through setuid or file capabilities.
    An already ptrace-capable spawning thread is rejected, not assumed safe.

    This does not protect unrelated ancestors or same-user credential files.
    """
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)

        class CapabilityHeader(ctypes.Structure):
            _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

        class CapabilityData(ctypes.Structure):
            _fields_ = [
                ("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32),
                ("inheritable", ctypes.c_uint32),
            ]

        header = CapabilityHeader(0x20080522, 0)  # _LINUX_CAPABILITY_VERSION_3
        data = (CapabilityData * 2)()
        libc.capget.argtypes = [ctypes.POINTER(CapabilityHeader), ctypes.POINTER(CapabilityData)]
        libc.capget.restype = ctypes.c_int
        if libc.capget(ctypes.byref(header), data) != 0:
            raise OSError(ctypes.get_errno(), "cannot inspect execution capabilities")
        # Ambient capabilities are a subset of permitted+inheritable. Checking
        # all three sets also rejects capabilities temporarily absent from Eff.
        if (data[0].effective | data[0].permitted | data[0].inheritable) & (1 << 19):
            raise RuntimeError(
                "Unsafe execution environment: CAP_SYS_PTRACE must be absent "
                "from effective, permitted and inheritable capabilities"
            )
        if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
            raise OSError(ctypes.get_errno(), "cannot disable execution privilege gains")
        if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
            raise OSError(ctypes.get_errno(), "cannot protect controller /proc access")


def _windows_library(name: str) -> Any:
    """Bind Windows-only APIs without requiring them on other platforms."""
    if sys.platform == "win32":
        return ctypes.WinDLL(name, use_last_error=True)
    raise OSError("Windows native execution helpers require Windows")


def _windows_error() -> OSError:
    if sys.platform == "win32":
        return ctypes.WinError(ctypes.get_last_error())
    raise OSError("Windows native execution helpers require Windows")


class WindowsDirectory:
    """Pin an execution directory against rename/reparse replacement."""

    def __init__(self, path: str) -> None:
        from ctypes import wintypes

        kernel = _windows_library("kernel32")
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self._kernel = kernel
        # Read-data access is required for Windows to enforce share restrictions;
        # a metadata-only handle (desired access 0) does not pin the directory.
        # Deny write/delete sharing while still allowing ordinary child files.
        self._handle = kernel.CreateFileW(path, 0x1, 0x1, None, 3, 0x02000000, None)
        if self._handle == ctypes.c_void_p(-1).value:
            self._handle = None
            raise _windows_error()

    def close(self) -> None:
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


class WindowsJob:
    """Keep descendants killable even after the command shell has exited."""

    def __init__(self) -> None:
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("minimum_ws", ctypes.c_size_t),
                ("maximum_ws", ctypes.c_size_t),
                ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes"
            )]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits), ("io", IOCounters),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
            ]

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("user_time", ctypes.c_int64), ("kernel_time", ctypes.c_int64),
                ("period_user_time", ctypes.c_int64), ("period_kernel_time", ctypes.c_int64),
                ("page_faults", wintypes.DWORD), ("total", wintypes.DWORD),
                ("active", wintypes.DWORD), ("terminated", wintypes.DWORD),
            ]

        self._accounting_type = Accounting

        kernel = _windows_library("kernel32")
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
        ]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self._kernel = kernel
        self._handle = kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise _windows_error()
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(
            self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits),
        ):
            error = _windows_error()
            self.close()
            raise error

    def assign(self, pid: int) -> None:
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE; no inheritable control handles.
        process = self._kernel.OpenProcess(0x0100 | 0x0001, False, pid)
        if not process:
            raise _windows_error()
        try:
            if not self._kernel.AssignProcessToJobObject(self._handle, process):
                raise _windows_error()
        finally:
            self._kernel.CloseHandle(process)

    def resume(self, pid: int) -> None:
        """Resume only after assign; Popen has already closed its thread handle."""
        process = self._kernel.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
        if not process:
            raise _windows_error()
        try:
            ntdll = _windows_library("ntdll")
            ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
            ntdll.NtResumeProcess.restype = ctypes.c_int32
            status = ntdll.NtResumeProcess(process)
            if status < 0:
                raise OSError(f"Cannot resume job-assigned execution process: NTSTATUS {status:#x}")
        finally:
            self._kernel.CloseHandle(process)

    def active(self) -> bool:
        accounting = self._accounting_type()
        if not self._kernel.QueryInformationJobObject(
            self._handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
        ):
            # Keep an unqueryable handle for fail-closed cleanup.
            return True
        return bool(accounting.active)

    def close(self) -> None:
        if self._handle:
            self._kernel.TerminateJobObject(self._handle, 1)
            self._kernel.CloseHandle(self._handle)
            self._handle = None
