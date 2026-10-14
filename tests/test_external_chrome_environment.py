"""External Chrome receives a sanitized copy; no browser or real profile runs."""
from contextlib import nullcontext
import os
from types import SimpleNamespace

import pytest

from fanyiji import process_utils
from test_chrome_browser_service import harness, open_browser


@pytest.mark.parametrize("key", ["PATH", "Path", "path"])
def test_frozen_external_path_removes_only_bundle_and_children(monkeypatch, key):
    bundled = [r"D:\应用\_internal", r"d:/应用/_internal/PySide6", r'"D:\应用\_internal\plugins"']
    retained = [r"C:\Windows\System32", r"D:\应用\_internal-other", r"D:\应用", "", r"E:\工具\bin", "relative-bin"]
    original = {key: ";".join(bundled + retained), "FIXTURE_SETTING": "preserve"}
    monkeypatch.setattr(process_utils.os, "environ", original)
    monkeypatch.setattr(process_utils.sys, "frozen", True, raising=False)
    monkeypatch.setattr(process_utils.sys, "_MEIPASS", r"D:\应用\_internal", raising=False)
    result = process_utils.external_chrome_environment()
    assert result[key] == ";".join(retained)
    assert result["FIXTURE_SETTING"] == "preserve"
    assert original[key] == ";".join(bundled + retained)
    assert result is not original


def test_unc_bundle_subdirectories_are_removed_without_affecting_other_shares(monkeypatch):
    original = {"PATH": r"\\server\share\bundle;\\SERVER\SHARE\bundle\Qt;\\server\share\bundle-other;\\other\share\bundle"}
    monkeypatch.setattr(process_utils.os, "environ", original)
    monkeypatch.setattr(process_utils.sys, "frozen", True, raising=False)
    monkeypatch.setattr(process_utils.sys, "_MEIPASS", r"\\server\share\bundle", raising=False)
    result = process_utils.external_chrome_environment()
    assert result["PATH"] == r"\\server\share\bundle-other;\\other\share\bundle"
    assert original["PATH"].startswith(r"\\server\share\bundle;")


@pytest.mark.parametrize("frozen,bundle", [(False, r"D:\bundle"), (True, None), (True, ""), (True, "relative-bundle")])
def test_source_or_missing_bundle_preserves_environment_copy(monkeypatch, frozen, bundle):
    original = {"PATH": r"D:\bundle;C:\Windows\System32", "FIXTURE_SETTING": "preserve"}
    monkeypatch.setattr(process_utils.os, "environ", original)
    monkeypatch.setattr(process_utils.sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(process_utils.sys, "_MEIPASS", bundle, raising=False)
    result = process_utils.external_chrome_environment()
    assert result == original
    assert result is not original
    result["FIXTURE_SETTING"] = "child-only"
    assert original["FIXTURE_SETTING"] == "preserve"


def test_bundle_path_expansion_and_dot_segments_do_not_remove_unrelated_directories(monkeypatch):
    original = {"PATH": r"%FIXTURE_BUNDLE%;%FIXTURE_BUNDLE%\Qt;D:\bundle\..\user-tools;C:\Windows\System32",
                "FIXTURE_BUNDLE": r"D:\bundle"}
    monkeypatch.setattr(process_utils.os, "environ", original)
    monkeypatch.setattr(process_utils.sys, "frozen", True, raising=False)
    monkeypatch.setattr(process_utils.sys, "_MEIPASS", "D:\\bundle\\", raising=False)
    assert process_utils.external_chrome_environment()["PATH"] == r"D:\bundle\..\user-tools;C:\Windows\System32"
    assert original["PATH"].startswith("%FIXTURE_BUNDLE%;")


def test_no_path_key_does_not_create_or_modify_path(monkeypatch):
    original = {"FIXTURE_SETTING": "preserve"}
    monkeypatch.setattr(process_utils.os, "environ", original)
    monkeypatch.setattr(process_utils.sys, "frozen", True, raising=False)
    monkeypatch.setattr(process_utils.sys, "_MEIPASS", r"D:\bundle", raising=False)
    assert process_utils.external_chrome_environment() == original


@pytest.mark.parametrize("protect", [False, pytest.param(True, marks=pytest.mark.skipif(os.name != "nt", reason="Windows owned browser"))])
def test_owned_browser_factory_receives_child_environment_without_global_mutation(harness, monkeypatch, protect):
    original = {"PATH": r"D:\bundle;D:\bundle\PySide6;C:\Windows\System32;E:\user-tools", "FIXTURE_SETTING": "preserve"}
    monkeypatch.setattr(process_utils.os, "environ", original)
    monkeypatch.setattr(process_utils.sys, "frozen", True, raising=False)
    monkeypatch.setattr(process_utils.sys, "_MEIPASS", r"D:\bundle", raising=False)
    assigned = []
    job = SimpleNamespace(assign=assigned.append, close=lambda: None)
    monkeypatch.setattr(process_utils, "_WindowsJob", lambda: job)
    monkeypatch.setattr(process_utils, "_external_dll_search", nullcontext)
    harness.service._protect = protect
    open_browser(harness)
    arguments, options, _process = harness.launched[0]
    assert options["env"]["PATH"] == r"C:\Windows\System32;E:\user-tools"
    assert options["env"]["FIXTURE_SETTING"] == "preserve"
    assert original["PATH"] == r"D:\bundle;D:\bundle\PySide6;C:\Windows\System32;E:\user-tools"
    assert "--no-sandbox" not in arguments
    assert options["shell"] is False
    if protect:
        assert harness.service.job is job
        assert assigned == [_process]
    else:
        assert not assigned
