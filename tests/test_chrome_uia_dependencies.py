"""Dependency probes load CLR types only, and public packages retain their loader."""
import importlib.util
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tomllib
from types import ModuleType, SimpleNamespace

import pytest

from fanyiji import chrome_caption_worker as worker

ROOT = Path(__file__).resolve().parents[1]


def test_dependency_probe_uses_real_type_initialization_without_window_access(monkeypatch):
    clr = ModuleType("clr")
    system = ModuleType("System")
    automation = ModuleType("System.Windows.Automation")
    system.IntPtr = object()
    for name in ("AutomationElement", "ControlType", "TextPattern", "TreeWalker", "ValuePattern"):
        setattr(automation, name, object())
    for name, module in (("clr", clr), ("System", system), ("System.Windows.Automation", automation)):
        monkeypatch.setitem(sys.modules, name, module)
    calls = []
    monkeypatch.setattr(worker, "_load_uia_references", calls.append)
    def forbidden(*args, **kwargs):
        raise AssertionError("Dependency checks must not inspect Chrome or windows")
    monkeypatch.setattr(worker, "find_chrome", forbidden)
    monkeypatch.setattr(worker, "NativeUiaAdapter", forbidden)
    assert worker.check_uia_dependencies() == {
        "uia_dependencies": "passed", "windows_enumerated": False,
        "browser_opened": False, "profile_read": False}
    assert calls == [clr]


@pytest.mark.parametrize("error", [ModuleNotFoundError("private module/path/token"),
                                 ImportError("private account or assembly data")])
def test_missing_dependency_diagnostic_has_fixed_category_and_no_exception_content(error):
    assert worker.exception_diagnostic(error, "adapter_init") == {
        "stage": "adapter_init", "error_type": "dependency_missing"}


def test_worker_dependency_check_does_not_start_caption_reader(monkeypatch):
    sink = io.StringIO()
    monkeypatch.setattr(worker.sys, "stdout", sink)
    initialized = []
    monkeypatch.setattr(worker, "_initialize_uia_types", lambda: initialized.append(True))
    def forbidden(*args, **kwargs):
        raise AssertionError("Dependency checks must not create a native reader")
    monkeypatch.setattr(worker, "NativeUiaAdapter", forbidden)
    monkeypatch.setattr(worker, "AttachedCaptionReader", forbidden)
    assert worker.main(["--dependency-check", "--session", "7", "--generation", "12"]) == 0
    assert initialized == [True]
    assert json.loads(sink.getvalue()) == {
        "kind": "dependency_check", "uia_dependencies": "passed", "windows_enumerated": False,
        "browser_opened": False, "profile_read": False, "session": 7, "generation": 12}


@pytest.mark.parametrize("error,category", [(ModuleNotFoundError("private"), "dependency_missing"),
                                           (ImportError("private"), "dependency_missing"),
                                           (RuntimeError("private"), "runtime_error")])
def test_worker_dependency_check_returns_failure_without_browser_retry(monkeypatch, error, category):
    sink = io.StringIO()
    monkeypatch.setattr(worker.sys, "stdout", sink)
    def failed():
        raise error
    monkeypatch.setattr(worker, "_initialize_uia_types", failed)
    assert worker.main(["--dependency-check"]) == 1
    value = json.loads(sink.getvalue())
    assert value == {"kind": "diagnostic", "stage": "adapter_init", "error_type": category,
                     "session": 0, "generation": 0}
    assert "private" not in sink.getvalue()


def test_native_worker_reports_missing_dependency_as_terminal_status(monkeypatch):
    sink = io.StringIO()
    monkeypatch.setattr(worker.sys, "stdout", sink)
    def missing(*args, **kwargs):
        raise ModuleNotFoundError("private module or account data")
    monkeypatch.setattr(worker, "NativeUiaAdapter", missing)
    # The normal worker isolates provider stdout; restore these handles after the test.
    previous_stdout, previous_stderr = worker.sys.stdout, worker.sys.stderr
    try:
        assert worker.main(["--attach-existing", "--source-url", "https://live.bilibili.com/1698225"]) == 1
    finally:
        redirected = worker.sys.stdout
        worker.sys.stdout, worker.sys.stderr = previous_stdout, previous_stderr
        if redirected is not previous_stdout:
            redirected.close()
    events = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert events == [{"kind": "diagnostic", "stage": "adapter_init", "error_type": "dependency_missing",
                       "session": 0, "generation": 0},
                      {"kind": "status", "code": "dependency_missing", "session": 0, "generation": 0}]
    assert "private" not in sink.getvalue()


def test_packaging_keeps_clr_modules_and_collects_the_runtime_loader(monkeypatch):
    import PyInstaller.utils.hooks as hooks
    collected, analyses = [], []
    def collect_binaries(package):
        collected.append(package)
        return [(package + "/runtime.dll", package)]
    monkeypatch.setattr(hooks, "collect_dynamic_libs", collect_binaries)
    monkeypatch.setattr(hooks, "collect_data_files", lambda package, **kwargs: [])
    def analyze(*args, **kwargs):
        analyses.append(kwargs)
        return SimpleNamespace(pure=[], scripts=[], binaries=kwargs["binaries"], datas=kwargs["datas"])
    inert = lambda *args, **kwargs: None
    runpy.run_path(str(ROOT / "FanyiJi.spec"), init_globals={
        "SPECPATH": str(ROOT), "Analysis": analyze, "PYZ": inert, "EXE": inert, "COLLECT": inert})
    required = {"clr", "pythonnet", "clr_loader", "clr_loader.netfx", "cffi", "_cffi_backend", "pycparser"}
    assert required <= set(analyses[0]["hiddenimports"])
    assert not required.intersection(analyses[0]["excludes"])
    assert {"pythonnet", "clr_loader"} <= set(collected)
    assert "webview" in analyses[0]["excludes"]


def build_tool():
    spec = importlib.util.spec_from_file_location("chrome_uia_build_fixture", ROOT / "tools" / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_verifies_frozen_component_instead_of_synthetic_fixture(monkeypatch, tmp_path):
    module = build_tool()
    calls = []
    value = {"kind": "dependency_check", "uia_dependencies": "passed", "windows_enumerated": False,
             "browser_opened": False, "profile_read": False}
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")
    monkeypatch.setattr(module.subprocess, "run", run)
    executable = tmp_path / "app.exe"
    assert module._verify_uia_dependencies(executable, {"TEST": "1"}) == value
    assert calls[0][0] == [str(executable), "--chrome-caption-worker", "--dependency-check"]
    assert calls[0][1]["timeout"] == 30


@pytest.mark.parametrize("returncode,output", [(1, '{"error_type":"dependency_missing"}'),
    (0, '{"ui_smoke":"passed"}'), (0, "private raw child error"),
    (0, '{"kind":"dependency_check","uia_dependencies":"passed","windows_enumerated":true}')])
def test_build_rejects_failed_incomplete_or_unsafe_dependency_probe(monkeypatch, tmp_path, returncode, output):
    module = build_tool()
    monkeypatch.setattr(module.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, returncode, output, "private child details"))
    with pytest.raises(RuntimeError, match="Frozen Windows UI Automation dependency check failed") as error:
        module._verify_uia_dependencies(tmp_path / "app.exe", {})
    assert "private" not in str(error.value)


def test_clean_install_declares_dependencies_needed_by_the_uia_adapter():
    value = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {entry.split("==")[0] for entry in value["project"]["dependencies"]}
    assert {"pythonnet", "clr-loader", "cffi", "pycparser"} <= declared
