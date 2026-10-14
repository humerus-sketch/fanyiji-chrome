"""Read-only attachment ownership; no public action may launch actual Chrome."""
import io
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

import fanyiji.chrome_caption_service as service_module
from fanyiji.chrome_caption_service import ChromeCaptionService


ROOM_URL = "https://live.bilibili.com/1698225"


class WorkerProcess:
    def __init__(self, pid):
        self.pid, self.stdout, self.killed = pid, io.StringIO(), False

    def poll(self):
        return 0 if self.killed else None

    def kill(self):
        self.killed = True

    def wait(self, timeout):
        return 0


@pytest.fixture
def attach_harness(tmp_path, monkeypatch):
    monkeypatch.setenv("FANYIJI_DATA", str(tmp_path / "never-created-data"))
    monkeypatch.setattr(service_module.threading, "Thread", lambda **_kwargs: Mock(start=Mock()))
    calls, workers, events = [], [], []

    def factory(argv, **options):
        assert "--attach-existing" in argv
        assert Path(argv[0]).name.lower() != "chrome.exe"
        worker = WorkerProcess(200 + len(workers))
        workers.append(worker)
        calls.append((argv, options))
        return worker

    closer = Mock(side_effect=AssertionError("Existing Chrome must never be closed"))
    service = ChromeCaptionService(events.append, profile=tmp_path / "legacy-unused-profile",
        process_factory=factory, chrome_finder=lambda: Path("C:/Chrome/chrome.exe"), browser_closer=closer)
    yield service, calls, workers, events, closer
    service.close()
    closer.assert_not_called()


def test_default_start_is_disconnected_and_performs_no_process_or_profile_operations(attach_harness, monkeypatch):
    service, calls, workers, events, _ = attach_harness
    monkeypatch.setattr(Path, "mkdir", Mock(side_effect=AssertionError("Connect must not create a profile")))
    service.start(session=4)
    assert not calls and not workers
    assert events[-1]["code"] == "waiting_browser"
    assert not service.profile.exists()


def test_explicit_connect_starts_single_reader_without_touching_profile(attach_harness, monkeypatch):
    service, calls, workers, _events, _ = attach_harness
    monkeypatch.setattr(Path, "mkdir", Mock(side_effect=AssertionError("Connect must not create a profile")))
    service.start(ROOM_URL, session=4)
    assert len(calls) == len(workers) == 1
    argv, options = calls[0]
    assert argv[:3] == [sys.executable, "-m", "fanyiji.chrome_caption_worker"]
    assert "--attach-existing" in argv
    assert "--browser-pid" not in argv
    assert not any(argument.startswith(("--user-data-dir", "--new-window")) for argument in argv)
    assert options["shell"] is False
    assert options["env"]["PYTHONUTF8"] == "1"
    assert options["env"]["PYTHONIOENCODING"] == "utf-8"
    assert not service.profile.exists()


def test_frozen_entry_dispatch_is_only_caption_reader_and_keeps_attach_flag(attach_harness, monkeypatch):
    service, calls, _workers, _events, _ = attach_harness
    monkeypatch.setattr(service_module.sys, "frozen", True, raising=False)
    service.start(ROOM_URL, session=7)
    argv, _options = calls[0]
    assert argv[:2] == [sys.executable, "--chrome-caption-worker"]
    assert "--attach-existing" in argv
    assert "-m" not in argv and "--browser-pid" not in argv
    assert argv[argv.index("--generation") + 1] == str(service.generation)
    assert argv[argv.index("--session") + 1] == "7"


@pytest.mark.parametrize("method", ["stop", "close"])
def test_exit_disposes_only_worker_without_closing_browser_or_changing_login_files(attach_harness, tmp_path, method):
    service, _calls, workers, _events, closer = attach_harness
    existing_profile = tmp_path / "existing-browser-profile"
    existing_profile.mkdir()
    marker = existing_profile / "synthetic-login-state"
    marker.write_bytes(b"unchanged synthetic state")
    service.start(ROOM_URL)
    getattr(service, method)()
    assert workers[0].killed and service._worker is None
    closer.assert_not_called()
    assert marker.read_bytes() == b"unchanged synthetic state"
    assert not service.profile.exists()


def test_open_browser_compatibility_alias_only_connects_existing_chrome(attach_harness):
    service, calls, _workers, _events, _ = attach_harness
    service.open_browser(ROOM_URL, session=8)
    assert len(calls) == 1 and "--attach-existing" in calls[0][0]


def test_open_room_tab_compatibility_alias_replaces_only_reader_and_never_navigates(attach_harness):
    service, calls, workers, _events, _ = attach_harness
    service.start(ROOM_URL)
    generation, worker = service.generation, workers[0]
    service.open_room_tab("https://live.bilibili.com/21399689")
    assert len(calls) == 2 and worker.killed
    assert service._worker is workers[1] and not workers[1].killed
    assert service.generation > generation
    argv = calls[1][0]
    assert "--attach-existing" in argv
    assert argv[argv.index("--source-url") + 1] == "https://live.bilibili.com/21399689"


def test_caption_settings_is_manual_instruction_and_preserves_attached_reader(attach_harness):
    service, calls, workers, events, closer = attach_harness
    service.start(ROOM_URL)
    generation, worker = service.generation, workers[0]
    service.open_caption_settings()
    assert len(calls) == 1 and not worker.killed
    assert service._worker is worker and service.generation == generation
    assert events[-1]["kind"] == "status" and events[-1]["code"] == "manual_caption_settings"
    closer.assert_not_called()
    assert not service.profile.exists()


def test_manual_settings_from_waiting_state_does_not_find_or_launch_chrome(attach_harness, monkeypatch):
    service, calls, _workers, events, _ = attach_harness
    finder = Mock(side_effect=AssertionError("Manual instructions must not discover Chrome"))
    monkeypatch.setattr(service, "_finder", finder)
    monkeypatch.setattr(Path, "mkdir", Mock(side_effect=AssertionError("Instructions must not create a profile")))
    service.start("", session=9)
    service.open_caption_settings(session=99)
    finder.assert_not_called()
    assert events[-1]["code"] == "manual_caption_settings" and events[-1]["session"] == 9
    assert not calls and not service.profile.exists()


def test_missing_chrome_on_compatibility_room_action_never_falls_back_to_launching_browser(attach_harness, monkeypatch):
    service, calls, _workers, _events, _ = attach_harness
    monkeypatch.setattr(service, "_finder", lambda: None)
    with pytest.raises(service_module.ChromeCaptionError) as error:
        service.open_room_tab(ROOM_URL)
    assert error.value.code == "chrome_missing"
    assert not calls


@pytest.mark.parametrize("url", ["file:///C:/private", "https://example.com/1698225", "javascript:alert(1)"])
def test_invalid_compatibility_room_action_never_starts_reader_or_browser(attach_harness, url):
    service, calls, _workers, _events, _ = attach_harness
    with pytest.raises(ValueError):
        service.open_room_tab(url)
    assert not calls
