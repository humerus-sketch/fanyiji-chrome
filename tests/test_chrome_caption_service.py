"""Read-only Chrome attachment, using fake worker processes and isolated data paths."""
import io
import json
from pathlib import Path

import pytest

from fanyiji.chrome_caption_service import ChromeCaptionError, ChromeCaptionService


class FakeProcess:
    def __init__(self, pid, *, output="", actions=None):
        self.pid = pid
        self.stdout = io.StringIO(output)
        self.killed = False
        self.exited = False
        self.actions = actions if actions is not None else []

    def poll(self):
        return 0 if self.exited else None

    def kill(self):
        self.killed = self.exited = True
        self.actions.append(("kill", self.pid))

    def wait(self, timeout):
        self.actions.append(("wait", self.pid))
        return 0


class FakeThread:
    """A fake pipe thread must never invoke an OS reader or native UIA."""
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start(self):
        pass


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("FANYIJI_DATA", str(tmp_path / "isolated-data"))
    monkeypatch.setattr("fanyiji.chrome_caption_service.threading.Thread", FakeThread)
    calls, processes, events, actions = [], [], [], []

    def factory(argv, **kwargs):
        assert "--attach-existing" in argv, "Connecting must never launch Chrome"
        process = FakeProcess(100 + len(processes), actions=actions)
        processes.append(process)
        calls.append((argv, kwargs))
        actions.append(("start", process.pid))
        return process

    service = ChromeCaptionService(events.append, profile=tmp_path / "unused-legacy-profile",
        process_factory=factory, chrome_finder=lambda: Path("C:/Chrome/chrome.exe"), auto_restart=False)
    yield service, calls, processes, events, actions
    service.close()


def packet(service, *, text="测试字幕", **changes):
    value = {"kind": "snapshot", "generation": service.generation, "session": service._session,
             "revision": 1, "text": text, "window_id": "444", "source_epoch": 1,
             "observed_at": 1.2, "bridge_generation": service.generation, "room": "",
             "requested_room": "1698225", "source_verified": False, "connected": True,
             "source_lang": "unknown", "target_lang": "zh"}
    value.update(changes)
    return value


def read_packets(service, data, *, worker=None, generation=None):
    worker = worker or service._worker or FakeProcess(777)
    worker.stdout = io.StringIO("".join(json.dumps(value) + "\n" for value in data))
    service._read(worker, service.generation if generation is None else generation)
    return worker


def test_connect_owns_only_worker_and_uses_utf8_pipe(harness):
    service, calls, processes, events, _ = harness
    service.start("https://live.bilibili.com/1698225", session=4)
    assert len(calls) == len(processes) == 1
    argv, options = calls[0]
    assert "--attach-existing" in argv and "--browser-pid" not in argv
    assert argv[argv.index("--source-url") + 1] == "https://live.bilibili.com/1698225"
    assert argv[argv.index("--session") + 1] == "4"
    assert options["shell"] is False and options["text"] is True
    assert options["env"]["PYTHONUTF8"] == "1"
    assert options["env"]["PYTHONIOENCODING"] == "utf-8"
    assert not service.profile.exists()
    assert events == [{"kind": "status", "code": "starting", "session": 4, "generation": 1, "reader_generation": 0}]


def test_restart_retires_only_old_worker_before_new_attach_and_preserves_login_marker(harness):
    service, _calls, processes, _events, actions = harness
    # This marker belongs to a synthetic existing browser, not a profile the service creates.
    service.profile.mkdir()
    marker = service.profile / "synthetic-login-state"
    marker.write_text("preserved", encoding="utf-8")
    external_browser = FakeProcess(999)
    service.start("https://live.bilibili.com/1698225")
    service.start("https://live.bilibili.com/21399689", session=1)
    assert actions.index(("kill", 100)) < actions.index(("start", 101))
    assert processes[0].killed and not processes[1].killed
    assert not external_browser.killed
    service.stop()
    assert processes[1].killed
    assert marker.read_text(encoding="utf-8") == "preserved"


def test_late_pipe_event_is_fenced_after_stop(harness):
    service, _calls, _processes, events, _actions = harness
    service.start("https://live.bilibili.com/1698225", session=8)
    stale_generation = service.generation
    data = packet(service, text="过期字幕")
    service.stop()
    before = list(events)
    read_packets(service, [data], generation=stale_generation)
    assert events == before


def test_pipe_whitelists_fields_and_never_promotes_unverified_global_caption(harness):
    service, _calls, _processes, events, _actions = harness
    service.start("https://live.bilibili.com/1698225", session=2)
    generation = service.generation
    data = [
        {"kind": "status", "generation": generation, "session": 2,
         "code": "caption_found_unverified", "cookie": "not-forwarded", "url": "not-forwarded"},
        packet(service, revision=3, is_final=True, is_official_final=True, allow_push=True,
               cookie="not-forwarded", url="not-forwarded"),
        {"kind": "status", "generation": generation, "session": 2, "code": "arbitrary-diagnostic"},
        packet(service, session=8, text="错误会话"),
    ]
    read_packets(service, data)
    assert events[1] == {"kind": "status", "code": "caption_found_unverified",
                         "session": 2, "generation": generation, "reader_generation": 1}
    assert events[2] == {**{key: value for key, value in data[1].items()
                            if key not in ("cookie", "url", "is_final", "is_official_final", "allow_push")},
                         "is_final": False, "is_official_final": False, "allow_push": False, "reader_generation": 1}
    assert events[2]["room"] == "" and events[2]["requested_room"] == "1698225"
    assert events[2]["source_verified"] is False
    assert events[3]["code"] == "reader_failed" and len(events) == 4
    assert "not-forwarded" not in json.dumps(events)


@pytest.mark.parametrize("output", ["{not-json}\n", "[]\n", "x" * 65537 + "\n", "{}"],
                         ids=["invalid-json", "invalid-shape", "too-large", "partial-line"])
def test_malformed_or_unbounded_pipe_stops_with_fixed_status(harness, output):
    service, _calls, processes, events, _actions = harness
    service.start("https://live.bilibili.com/1698225")
    worker = processes[0]
    worker.stdout = io.StringIO(output)
    service._read(worker, service.generation)
    assert events[-1]["code"] == "reader_failed" and len(events) == 2
    assert worker.killed and service._worker is None


def test_worker_launch_failure_redacts_errors_without_starting_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("FANYIJI_DATA", str(tmp_path / "isolated-data"))
    monkeypatch.setattr("fanyiji.chrome_caption_service.threading.Thread", FakeThread)
    calls = []

    def factory(argv, **_kwargs):
        calls.append(argv)
        assert "--attach-existing" in argv
        raise RuntimeError("private-url-and-cookie")

    service = ChromeCaptionService(profile=tmp_path / "unused-profile", process_factory=factory,
        chrome_finder=lambda: Path("C:/Chrome/chrome.exe"))
    with pytest.raises(ChromeCaptionError) as error:
        service.start("https://live.bilibili.com/1698225")
    assert len(calls) == 1 and "private" not in str(error.value)
    assert not service.profile.exists()
    service.close()


def test_missing_chrome_and_closed_service_never_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("FANYIJI_DATA", str(tmp_path / "isolated-data"))
    calls, events = [], []
    service = ChromeCaptionService(events.append, profile=tmp_path / "unused-profile",
        process_factory=lambda *args, **kwargs: calls.append(args), chrome_finder=lambda: None)
    with pytest.raises(ChromeCaptionError) as error:
        service.start("https://live.bilibili.com/1698225")
    assert error.value.code == "chrome_missing" and events[-1]["code"] == "chrome_missing"
    service.close()
    with pytest.raises(ChromeCaptionError):
        service.start("https://live.bilibili.com/1698225")
    assert not calls and not service.profile.exists()


def test_invalid_source_and_session_never_launch_or_create_profile(harness):
    service, calls, _processes, _events, _actions = harness
    with pytest.raises(ValueError):
        service.start("file:///C:/private")
    with pytest.raises(ValueError):
        service.start("https://live.bilibili.com/1698225", session=True)
    assert not calls and not service.profile.exists()


def test_start_without_source_waits_without_worker_or_browser(harness):
    service, calls, _processes, events, _actions = harness
    service.start("", session=11)
    assert events == [{"kind": "status", "code": "waiting_browser", "session": 11, "generation": 1, "reader_generation": 0}]
    assert not calls and not service.profile.exists()
    service.open_browser("https://live.bilibili.com/1698225")
    assert len(calls) == 1 and events[-1]["session"] == 11
    assert "--attach-existing" in calls[0][0]


def test_callback_stop_prevents_late_attach(harness):
    service, calls, _processes, _events, _actions = harness
    service.callback = lambda _event: service.stop()
    service.start("https://live.bilibili.com/1698225")
    assert not calls


@pytest.mark.parametrize("field,value", [
    ("observed_at", float("nan")), ("observed_at", True), ("source_epoch", True),
    ("bridge_generation", True), ("window_id", "non-window"), ("room", "1698225"),
    ("requested_room", "21399689"), ("source_verified", True), ("source_verified", 0),
    ("connected", 1), ("text", "x" * 4001), ("source_lang", "ja"), ("target_lang", "ja"),
], ids=["nan-time", "bool-time", "bool-epoch", "bool-generation", "invalid-window",
         "forged-room-source", "wrong-requested-room", "forged-source-verification",
         "numeric-source-verification", "invalid-connection", "oversized-text",
         "forged-language", "invalid-target"])
def test_bad_snapshot_identity_or_types_are_not_forwarded(harness, field, value):
    service, _calls, _processes, events, _actions = harness
    service.start("https://live.bilibili.com/1698225", session=5)
    read_packets(service, [packet(service, **{field: value})])
    assert all(event["kind"] != "snapshot" for event in events)


def test_malformed_status_does_not_escape_reader_or_echo_source(harness):
    service, _calls, _processes, events, _actions = harness
    service.start("https://live.bilibili.com/1698225", session=5)
    read_packets(service, [{"kind": "status", "generation": service.generation,
                           "session": 5, "code": {"private": "not-forwarded"}}])
    assert events[-1]["code"] == "reader_failed"
    assert "private" not in json.dumps(events)


def test_generation_is_readonly_and_terminal_exit_is_not_overwritten(harness):
    service, _calls, _processes, events, _actions = harness
    service.start("https://live.bilibili.com/1698225", session=5)
    with pytest.raises(AttributeError):
        service.generation = 900
    read_packets(service, [{"kind": "status", "generation": service.generation,
                           "session": 5, "code": "browser_closed"}])
    assert events[-1]["code"] == "browser_closed"


def test_callbacks_run_outside_service_state_lock(harness):
    service, _calls, _processes, _events, _actions = harness
    captured = []
    service.callback = lambda _event: captured.append(service._lock._is_owned())
    service.start("", session=6)
    service.open_browser("https://live.bilibili.com/1698225")
    assert captured == [False, False]


def test_broken_pipe_retires_only_current_worker(harness):
    service, _calls, processes, events, _actions = harness
    service.start("https://live.bilibili.com/1698225", session=6)
    worker = processes[0]
    worker.stdout = io.StringIO("broken-json\n")
    service._read(worker, service.generation)
    assert worker.killed and service._worker is None
    assert events[-1]["code"] == "reader_failed"


def test_late_broken_pipe_does_not_retire_new_generation_worker(harness):
    service, _calls, processes, _events, _actions = harness
    service.start("https://live.bilibili.com/1698225", session=6)
    old_generation, old_worker = service.generation, processes[0]
    service.open_browser("https://live.bilibili.com/21399689", session=7)
    old_worker.stdout = io.StringIO("broken-json\n")
    service._read(old_worker, old_generation)
    assert service._worker is processes[1] and not processes[1].killed


def test_reader_diagnostics_log_only_fixed_metadata_not_caption_or_url(harness, caplog):
    service, _calls, _processes, _events, _actions = harness
    service.start("https://live.bilibili.com/1698225", session=6)
    value = {"kind": "diagnostic", "generation": service.generation, "session": 6,
             "stage": "address_walk", "error_type": "tree_limit", "node_count": 257,
             "class_kind": "TabStrip", "control_kind": "Button",
             "text": "PRIVATE_CAPTION_MARKER", "url": "PRIVATE_URL_MARKER",
             "exception": "PRIVATE_EXCEPTION_MARKER"}
    read_packets(service, [value])
    assert "tree_limit" in caplog.text and "257" in caplog.text
    assert "PRIVATE_" not in caplog.text


def test_unknown_diagnostic_fields_and_stale_generation_are_not_logged(harness, caplog):
    service, _calls, _processes, _events, _actions = harness
    service.start("https://live.bilibili.com/1698225", session=6)
    read_packets(service, [{"kind": "diagnostic", "generation": service.generation,
        "session": 6, "stage": "PRIVATE_STAGE", "error_type": "PRIVATE_ERROR"},
        {"kind": "diagnostic", "generation": service.generation - 1,
         "session": 6, "stage": "address_walk", "error_type": "tree_limit"}])
    assert "CHROME_CAPTION reader_diagnostic" not in caplog.text
