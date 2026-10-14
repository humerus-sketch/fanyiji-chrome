"""Owned Windows child-process and pipe helpers without translation dependencies."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import io
import logging
import os
import subprocess
import sys
import threading

LOGGER = logging.getLogger(__name__)
_DLL_LAUNCH_LOCK = threading.Lock()


class _WindowsJob:
    """Only app-owned children are assigned to this kill-on-close job."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Owned browser helpers currently require Windows")
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSet", ctypes.c_size_t),
                        ("MaximumWorkingSet", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperations", "WriteOperations", "OtherOperations", "ReadBytes", "WriteBytes", "OtherBytes")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "Cannot create helper Job Object")
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise OSError(error, "Cannot set helper Job Object limits")

    def assign(self, process: subprocess.Popen) -> None:
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise OSError(ctypes.get_last_error(), "Cannot protect helper process lifetime")

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle and not self.kernel.CloseHandle(handle):
            raise OSError(ctypes.get_last_error(), "Cannot close helper Job Object")


@contextmanager
def _external_dll_search():
    """Do not let a frozen Qt DLL directory contaminate native Chrome."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        yield
        return
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetDllDirectoryW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
    kernel.GetDllDirectoryW.restype = wintypes.DWORD
    kernel.SetDllDirectoryW.argtypes = [wintypes.LPCWSTR]
    kernel.SetDllDirectoryW.restype = wintypes.BOOL
    # This setting is process-wide; serialize and limit it to child creation.
    with _DLL_LAUNCH_LOCK:
        size = kernel.GetDllDirectoryW(0, None)
        buffer = ctypes.create_unicode_buffer(size + 1)
        kernel.GetDllDirectoryW(len(buffer), buffer)
        previous = buffer.value or None
        if not kernel.SetDllDirectoryW(None):
            raise OSError(ctypes.get_last_error(), "Cannot isolate helper DLL search path")
        try:
            yield
        finally:
            if not kernel.SetDllDirectoryW(previous):
                LOGGER.error("Could not restore application DLL search directory")


def _pipe(stream, identifier: int, mode: str):
    if stream is not None:
        return stream
    # Windowed PyInstaller executables expose inherited handles without sys.std*.
    import msvcrt
    from ctypes import wintypes
    getter = ctypes.windll.kernel32.GetStdHandle
    getter.argtypes, getter.restype = [wintypes.DWORD], wintypes.HANDLE
    handle = getter(identifier & 0xFFFFFFFF)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise OSError("missing_pipe")
    descriptor = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDONLY if mode == "r" else os.O_WRONLY))
    return io.TextIOWrapper(os.fdopen(descriptor, mode + "b", buffering=0), encoding="utf-8")
