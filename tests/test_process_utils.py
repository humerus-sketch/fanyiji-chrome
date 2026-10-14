"""Isolation and ownership of the browser project's native process helpers."""
from __future__ import annotations

import ctypes
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from fanyiji import process_utils


class NativeCall:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class FakeKernel:
    def __init__(self):
        self.directory = "C:/frozen/Qt"
        self.directories = []
        self.closed = []
        self.assigned = []
        self.limit_flags = None
        self.CreateJobObjectW = NativeCall(lambda *_: 123)
        self.SetInformationJobObject = NativeCall(self.set_limits)
        self.AssignProcessToJobObject = NativeCall(self.assign)
        self.CloseHandle = NativeCall(self.close)
        self.GetDllDirectoryW = NativeCall(self.get_directory)
        self.SetDllDirectoryW = NativeCall(self.set_directory)
        self.GetStdHandle = NativeCall(lambda *_: 0)

    def set_limits(self, handle, information_class, pointer, size):
        assert handle == 123 and information_class == 9 and size > 0
        self.limit_flags = pointer._obj.BasicLimitInformation.LimitFlags
        return True

    def assign(self, job_handle, process_handle):
        self.assigned.append((job_handle, process_handle))
        return True

    def close(self, handle):
        self.closed.append(handle)
        return True

    def get_directory(self, size, buffer):
        if buffer is not None:
            buffer.value = self.directory
        return len(self.directory)

    def set_directory(self, value):
        self.directories.append(value)
        self.directory = value or ""
        return True


@pytest.fixture
def kernel(monkeypatch):
    native = FakeKernel()
    monkeypatch.setattr(process_utils.ctypes, "WinDLL", lambda *_args, **_kwargs: native, raising=False)
    monkeypatch.setattr(process_utils.ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(process_utils.os, "name", "nt")
    return native


def test_windows_job_protects_only_explicitly_assigned_child(kernel):
    job = process_utils._WindowsJob()
    assert kernel.limit_flags == 0x2000
    assert not kernel.assigned
    job.assign(SimpleNamespace(_handle=456))
    assert kernel.assigned == [(123, 456)]
    job.close()
    job.close()
    assert kernel.closed == [123]
    assert job.handle is None


def test_failed_job_setup_closes_owned_handle(kernel):
    kernel.SetInformationJobObject = NativeCall(lambda *_: False)
    with pytest.raises(OSError, match="Cannot set helper Job Object limits"):
        process_utils._WindowsJob()
    assert kernel.closed == [123]


def test_failed_job_creation_reports_error_without_assigning_process(kernel):
    kernel.CreateJobObjectW = NativeCall(lambda *_: None)
    with pytest.raises(OSError, match="Cannot create helper Job Object"):
        process_utils._WindowsJob()
    assert kernel.closed == [] and kernel.assigned == []


def test_failed_assignment_does_not_close_an_unrelated_process_handle(kernel):
    job = process_utils._WindowsJob()
    kernel.AssignProcessToJobObject = NativeCall(lambda *_: False)
    with pytest.raises(OSError, match="Cannot protect helper process lifetime"):
        job.assign(SimpleNamespace(_handle=456))
    job.close()
    assert kernel.closed == [123]


def test_external_dll_directory_is_restored_after_child_spawn_failure(kernel, monkeypatch):
    monkeypatch.setattr(process_utils.sys, "frozen", True, raising=False)
    with pytest.raises(RuntimeError, match="spawn_failed"):
        with process_utils._external_dll_search():
            assert kernel.directories == [None]
            raise RuntimeError("spawn_failed")
    assert kernel.directories == [None, "C:/frozen/Qt"]


def test_external_dll_directory_has_no_change_for_source_runtime(kernel, monkeypatch):
    monkeypatch.setattr(process_utils.sys, "frozen", False, raising=False)
    with process_utils._external_dll_search():
        assert not kernel.directories
    assert not kernel.directories


def test_external_dll_directory_failure_prevents_child_creation(kernel, monkeypatch):
    monkeypatch.setattr(process_utils.sys, "frozen", True, raising=False)
    kernel.SetDllDirectoryW = NativeCall(lambda *_: False)
    executed = False
    with pytest.raises(OSError, match="Cannot isolate helper DLL search path"):
        with process_utils._external_dll_search():
            executed = True
    assert not executed


@pytest.mark.parametrize("mode,identifier", [("r", -10), ("w", -11)])
def test_provided_pipe_stream_is_preserved(mode, identifier):
    stream = io.StringIO()
    assert process_utils._pipe(stream, identifier, mode) is stream


@pytest.mark.skipif(os.name != "nt", reason="Windows inherited pipe API")
@pytest.mark.parametrize("handle", [0, ctypes.c_void_p(-1).value])
def test_missing_inherited_pipe_fails_without_opening_descriptor(monkeypatch, handle):
    kernel = FakeKernel()
    kernel.GetStdHandle = NativeCall(lambda *_: handle)
    monkeypatch.setattr(process_utils.ctypes, "windll", SimpleNamespace(kernel32=kernel))
    with pytest.raises(OSError, match="missing_pipe"):
        process_utils._pipe(None, -11, "w")


def _child_environment():
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(process_utils.__file__).resolve().parents[1])
    return environment


def _creation_flags():
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def test_import_is_independent_of_translation_engines_and_webview():
    script = (
        "import json,sys; from fanyiji import process_utils; "
        "print(json.dumps(sorted(name for name in sys.modules "
        "if name.startswith(('fanyiji.gguf_engine','fanyiji.engines',"
        "'fanyiji.browser_helper','ctranslate2','sherpa_onnx','webview')))))"
    )
    result = subprocess.run([sys.executable, "-c", script], env=_child_environment(),
                            capture_output=True, text=True, timeout=10,
                            creationflags=_creation_flags())
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows inherited pipe API")
def test_windowed_helper_writes_unicode_to_inherited_stdout():
    script = (
        "import sys; from fanyiji.process_utils import _pipe; "
        "sys.stdout=None; sink=_pipe(None,-11,'w'); "
        "sink.write('字幕读取正常\\n'); sink.flush()"
    )
    result = subprocess.run([sys.executable, "-c", script], env=_child_environment(),
                            capture_output=True, text=True, encoding="utf-8", timeout=10,
                            creationflags=_creation_flags())
    assert result.returncode == 0, result.stderr
    assert result.stdout == "字幕读取正常\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows process Job Object")
def test_closing_job_stops_only_its_owned_live_helper():
    job = process_utils._WindowsJob()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=_creation_flags())
    try:
        job.assign(process)
        assert process.poll() is None
        job.close()
        # Windows kill-on-close may report exit code 0. Completion before the
        # child's 30-second sleep is the lifetime guarantee being tested.
        process.wait(timeout=5)
        assert process.returncode is not None
    finally:
        job.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
