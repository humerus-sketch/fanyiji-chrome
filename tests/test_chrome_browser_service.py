"""Owned Chrome controls with fake CDP and processes; no browser or website calls."""
from collections import deque
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

import lifecycle_support
from lifecycle_support import FakeProcess
from fanyiji import chrome_browser_service as module
from fanyiji.chrome_browser_service import ChromeBrowserService, seed_caption_preferences
from fanyiji.chrome_cdp import ChromeControlError
from fanyiji.danmaku_protocol import login_script, presence_script, send_script


ROOM = "1698225"
URL = "https://live.bilibili.com/1698225"
TEXT = "【现在开始进行字幕同步测试】"
PORT = 9222


def accepted_receipt(**extra):
    return {"error": "none", "http_status": 200, "code": 0, "msg_class": "empty", "message_class": "empty",
            "request_started_epoch_ms": 1000, "before_count": 0, **extra}


def check_receipt(**extra):
    return {"error": "none", "room_matches": True, "chat_ready": True, "present": True,
            "occurrence_count": 1, **extra}




class FakeConnection:
    def __init__(self, name, trace):
        self.name, self.trace = name, trace
        self.calls = []
        self.values = deque()
        self.baseline_values = deque()
        self.before_result = None
        self.before_baseline_result = None
        self.closed = False

    def call(self, method, params=None, *, timeout=20):
        self.calls.append((method, params, timeout))
        self.trace.append((self.name + ".call", method))
        baseline = method == "Runtime.evaluate" and params and params.get("timeout") == 3000
        if baseline and self.before_baseline_result:
            action, self.before_baseline_result = self.before_baseline_result, None
            action()
        if not baseline and self.before_result:
            action, self.before_result = self.before_result, None
            action()
        if baseline:
            value = self.baseline_values.popleft() if self.baseline_values else {
                "result": {"value": check_receipt(present=False, occurrence_count=0)}}
        elif self.values:
            value = self.values.popleft()
        else:
            value = {"result": {"value": accepted_receipt()}}
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        self.trace.append((self.name + ".close",))
        self.closed = True


@pytest.fixture
def harness(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    trace, launched, connections, results, events = [], [], [], [], []
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"synthetic executable, never launched")
    profile = tmp_path / "owned-profile"
    state = SimpleNamespace(targets=[{"type": "page", "url": URL,
                                     "webSocketDebuggerUrl": f"ws://127.0.0.1:{PORT}/devtools/page/test"}],
                            now=100.0)

    def factory(argv, **options):
        process = FakeProcess(trace, 4000 + len(launched))
        launched.append((list(argv), options, process))
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "DevToolsActivePort").write_text(f"{PORT}\n/devtools/browser/test\n", encoding="utf-8")
        trace.append(("process.launch", process.pid))
        return process

    def connector(url, *, port):
        assert url.startswith(f"ws://127.0.0.1:{PORT}/devtools/") and port == PORT
        connection = FakeConnection("page" if "/page/" in url else "browser", trace)
        connections.append(connection)
        return connection

    def read_json(port, route):
        assert port == PORT
        if route == "/json/list":
            return state.targets
        assert route == "/json/version"
        return {"Browser": "Chrome/123.0", "private_field": "should_not_escape"}

    monkeypatch.setattr(module, "data_root", lambda: tmp_path)
    monkeypatch.setattr(module, "danmaku_available", lambda: True)
    monkeypatch.setattr(module, "ThreadPoolExecutor", lifecycle_support._ControlledExecutor)
    monkeypatch.setattr(module.threading, "Thread", Mock())
    monkeypatch.setattr(module.time, "monotonic", lambda: state.now)
    service = ChromeBrowserService(profile=profile, process_factory=factory, chrome_finder=lambda: chrome,
                                   connector=connector, json_reader=read_json, protect=False)
    service.event.connect(events.append)
    service.danmaku_result.connect(results.append)
    value = SimpleNamespace(service=service, state=state, trace=trace, launched=launched,
                            connections=connections, results=results, events=events, profile=profile, root=tmp_path)
    try:
        yield value
    finally:
        service.close()
        service._executor.drain()
        service.deleteLater()
        app.processEvents()


def open_browser(harness):
    pid = harness.service.open_sync(URL)
    assert pid == harness.launched[-1][2].pid
    return harness.service._page


def submit(harness, operation="send", *, request_id="test-1", room=ROOM, text=TEXT):
    accepted = harness.service.request_danmaku(operation, request_id, room, text)
    harness.service._executor.drain()
    return accepted


def send_evaluations(page):
    return [row for row in page.calls if row[0] == "Runtime.evaluate"
            and row[1].get("expression") == send_script(ROOM, TEXT)]


def test_constructor_has_no_browser_process_connections_or_profile_side_effects(harness):
    assert not harness.launched and not harness.connections and not harness.profile.exists()
    assert not harness.service.is_browser_ready and harness.service.browser_pid == 0


def test_open_creates_one_loopback_debugger_and_same_url_reuses_owned_window(harness):
    page = open_browser(harness)
    generation = harness.service.generation
    assert harness.service.open_sync(URL) == harness.launched[0][2].pid
    assert len(harness.launched) == 1 and len(harness.connections) == 2
    assert harness.service._page is page and harness.service.generation == generation
    argv, options, _process = harness.launched[0]
    assert "--remote-debugging-address=127.0.0.1" in argv and "--remote-debugging-port=0" in argv
    assert "--user-data-dir=" + str(harness.profile.resolve()) in argv
    assert argv.count("--new-window") == 1 and argv[-1] == URL
    assert options["shell"] is False
    assert all(options[key] == subprocess.DEVNULL for key in ("stdin", "stdout", "stderr"))
    assert "should_not_escape" not in json.dumps(harness.events)


@pytest.mark.parametrize("url", ["http://live.bilibili.com/1698225", "https://foreign.invalid/123", "1698225", ""])
def test_invalid_live_url_never_launches_process(harness, url):
    with pytest.raises(ValueError):
        harness.service.open_sync(url)
    assert not harness.launched and not harness.profile.exists()


@pytest.mark.parametrize("first_read", [
    PermissionError("synthetic sharing lock"),
    FileNotFoundError("synthetic publish race"),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "partial file"),
    "9222\n", "9222\n/devtools/browser/", "9222\n/devtools/brow",
    "9222\n/devtools/browser/test?unfinished=1",
])
def test_startup_port_file_transient_lock_or_partial_content_is_retried(harness, monkeypatch, first_read):
    original = Path.read_text
    reads = []

    def reading(path, *args, **kwargs):
        if path.name == "DevToolsActivePort" and not reads:
            reads.append(True)
            if isinstance(first_read, BaseException):
                raise first_read
            return first_read
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reading)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: setattr(harness.state, "now", harness.state.now + seconds))
    assert open_browser(harness) is harness.service._page
    assert len(harness.launched) == 1 and len(harness.connections) == 2 and harness.state.now > 100
    assert "synthetic sharing" not in json.dumps(harness.events)


def test_permanent_startup_port_lock_has_bounded_deadline_and_owned_cleanup(harness, monkeypatch):
    original, reads = Path.read_text, []

    def reading(path, *args, **kwargs):
        if path.name == "DevToolsActivePort":
            reads.append(True)
            raise PermissionError("synthetic sharing lock")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reading)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: setattr(harness.state, "now", harness.state.now + seconds))
    with pytest.raises(ChromeControlError, match="browser_unavailable"):
        harness.service.open_sync(URL)
    assert 1 <= len(reads) <= 202 and 120 <= harness.state.now < 121
    assert len(harness.launched) == 1 and not harness.connections
    assert harness.service.process is None and harness.service._profile_lock is None


def test_process_exit_is_detected_while_startup_port_file_remains_locked(harness, monkeypatch):
    original = Path.read_text

    def reading(path, *args, **kwargs):
        if path.name == "DevToolsActivePort":
            harness.launched[0][2].returncode = 1
            raise PermissionError("synthetic sharing lock")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reading)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: setattr(harness.state, "now", harness.state.now + seconds))
    with pytest.raises(ChromeControlError, match="browser_closed"):
        harness.service.open_sync(URL)
    assert harness.state.now < 101 and harness.service.process is None


@pytest.mark.parametrize("error", [PermissionError("private path must not escape"),
                                  FileNotFoundError("private path must not escape"),
                                  UnicodeDecodeError("utf-8", b"\xff", 0, 1, "partial file")])
def test_old_port_file_read_failure_returns_fixed_busy_before_profile_seed_or_launch(harness, monkeypatch, error):
    harness.profile.mkdir()
    port_file = harness.profile / "DevToolsActivePort"
    port_file.write_text(f"{PORT}\n/devtools/browser/old\n", encoding="utf-8")
    original = Path.read_text

    def reading(path, *args, **kwargs):
        if path.name == "DevToolsActivePort":
            raise error
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reading)
    with pytest.raises(ChromeControlError) as failure:
        harness.service.open_sync(URL)
    assert failure.value.code == "profile_busy" and "private" not in str(failure.value)
    assert not harness.launched and not harness.connections
    assert not (harness.profile / "Default" / "Preferences").exists()
    assert harness.service._profile_lock is None and port_file.exists()


@pytest.mark.parametrize("targets", [
    [], [{"type": "page", "url": "https://live.bilibili.com/21399689"}],
    [{"type": "page", "url": URL}, {"type": "page", "url": URL}],
    [{"type": "page", "url": "https://live.bilibili.com/1698225#other"}],
])
def test_room_validation_before_send_blocks_empty_wrong_or_ambiguous_pages(harness, targets):
    page = open_browser(harness)
    harness.state.targets = targets
    assert submit(harness)
    assert not page.calls
    assert harness.results[-1]["result"]["error"] == "wrong_room"
    assert harness.results[-1]["result"]["outcome"] == "not_sent"


def test_foreign_websocket_page_endpoint_is_rejected_before_script_execution(harness):
    page = open_browser(harness)
    harness.state.targets = [{"type": "page", "url": URL,
                              "webSocketDebuggerUrl": "ws://foreign.invalid:9222/devtools/page/secret"}]
    assert submit(harness)
    assert not page.calls
    assert "secret" not in json.dumps(harness.results)


def test_wrong_command_room_is_rejected_before_runtime_evaluate(harness):
    page = open_browser(harness)
    assert submit(harness, room="21399689")
    assert not page.calls
    assert harness.results[-1]["result"]["outcome"] == "not_sent"


@pytest.mark.parametrize("operation", ["send", "check", "login"])
def test_script_operation_has_fixed_cdp_options_and_sanitized_public_result(harness, operation):
    page = open_browser(harness)
    raw = accepted_receipt(cookie="synthetic_secret", csrf="synthetic_secret", text=TEXT, raw_response="synthetic_secret")
    if operation == "check":
        raw = check_receipt(cookie="synthetic_secret", text=TEXT)
    elif operation == "login":
        raw = {"error": "none", "code": 0, "is_login": True, "cookie": "synthetic_secret", "user_name": "private_test"}
    page.values.append({"result": {"value": raw}})
    assert submit(harness, operation)
    method, params, timeout = page.calls[-1]
    expected = {"send": lambda: send_script(ROOM, TEXT), "check": lambda: presence_script(ROOM, TEXT),
                "login": lambda: login_script(ROOM)}[operation]()
    assert method == "Runtime.evaluate" and timeout == 20
    assert params == {"expression": expected, "awaitPromise": True, "returnByValue": True,
                      "userGesture": True, "timeout": 18000}
    if operation == "send":
        assert page.calls[0] == ("Runtime.evaluate", {"expression": presence_script(ROOM, TEXT),
                               "returnByValue": True, "timeout": 3000}, 5)
    result = harness.results[-1]
    assert result["operation"] == operation and result["request_id"] == "test-1"
    assert "synthetic_secret" not in json.dumps(result) and "private_test" not in json.dumps(result)
    assert TEXT not in json.dumps(result, ensure_ascii=False)


def test_global_request_gap_is_at_least_three_point_five_seconds(harness):
    page = open_browser(harness)
    assert submit(harness, request_id="first")
    assert len(send_evaluations(page)) == 1
    harness.state.now += 3.49
    assert submit(harness, request_id="too-soon")
    assert len(send_evaluations(page)) == 1
    assert harness.results[-1]["result"]["error"] == "queue_full"
    harness.state.now += 0.01
    assert submit(harness, request_id="next")
    assert len(send_evaluations(page)) == 2


def test_duplicate_request_id_cannot_queue_or_submit_twice(harness):
    page = open_browser(harness)
    assert harness.service.request_danmaku("send", "pending-id", ROOM, TEXT)
    assert not harness.service.request_danmaku("send", "pending-id", ROOM, TEXT)
    harness.service._executor.drain()
    assert len(send_evaluations(page)) == 1 and not harness.service._requests


@pytest.mark.parametrize("operation,room,text", [("unknown", ROOM, TEXT), ("send", "bad-room", TEXT),
                                                ("send", ROOM, "【Aya英文名字不能发送】")])
def test_invalid_or_non_chinese_send_command_never_queues_cdp(harness, operation, room, text):
    page = open_browser(harness)
    assert not submit(harness, operation, room=room, text=text)
    assert not page.calls and not harness.results


def test_timeout_emits_unknown_stops_blind_send_but_allows_readonly_check(harness):
    page = open_browser(harness)
    page.values.append(TimeoutError("synthetic_secret"))
    assert submit(harness, request_id="lost")
    assert harness.results[-1]["result"]["outcome"] == "result_unknown"
    assert "synthetic_secret" not in json.dumps(harness.results)
    harness.state.now += 4
    assert submit(harness, request_id="blind-retry")
    assert len(send_evaluations(page)) == 1 and len(page.calls) == 2
    page.values.append({"result": {"value": check_receipt()}})
    assert submit(harness, "check", request_id="verify")
    assert len(page.calls) == 3 and harness.results[-1]["result"]["present"] is True
    assert not harness.service._uncertain
    assert submit(harness, request_id="after-confirmation")
    assert len(send_evaluations(page)) == 2


def test_unknown_send_is_only_released_by_matching_message_with_a_new_occurrence(harness):
    page = open_browser(harness)
    page.baseline_values.append({"result": {"value": check_receipt(occurrence_count=2)}})
    page.values.append(TimeoutError("synthetic_secret"))
    assert submit(harness, request_id="lost")
    harness.state.now += 4
    page.values.append({"result": {"value": check_receipt(occurrence_count=3)}})
    assert submit(harness, "check", request_id="different-message", text="【这是另一条不同字幕内容】")
    assert harness.service._uncertain
    page.values.append({"result": {"value": check_receipt(occurrence_count=2)}})
    assert submit(harness, "check", request_id="old-occurrence")
    assert harness.service._uncertain
    page.values.append({"result": {"value": check_receipt(occurrence_count=3)}})
    assert submit(harness, "check", request_id="new-occurrence")
    assert not harness.service._uncertain
    assert len(send_evaluations(page)) == 1


@pytest.mark.parametrize("receipt", [
    {"error": "request_timeout", "request_started_epoch_ms": 1000, "before_count": 0},
    {"error": "invalid_response", "request_started_epoch_ms": 1000, "before_count": 0},
    {},
])
def test_returned_unknown_receipt_keeps_submission_guard_without_python_exception(harness, receipt):
    page = open_browser(harness)
    page.values.append({"result": {"value": receipt}})
    assert submit(harness, request_id="returned-unknown")
    assert harness.results[-1]["result"]["outcome"] == "result_unknown"
    assert harness.service._uncertain and harness.service._unknown_send == (ROOM, TEXT, 0)
    harness.state.now += 4
    assert submit(harness, request_id="cannot-blind-retry")
    assert len(send_evaluations(page)) == 1
    assert harness.results[-1]["result"]["outcome"] == "not_sent"
    assert harness.service._unknown_send == (ROOM, TEXT, 0)


def test_caption_recovery_does_not_release_unknown_submission_guard(harness):
    page = open_browser(harness)
    page.values.append(TimeoutError("synthetic_secret"))
    submit(harness, request_id="lost-before-recovery")
    unknown = harness.service._unknown_send
    generation = harness.service.generation
    harness.service.recover_caption()
    assert harness.service.generation > generation
    assert harness.service._uncertain and harness.service._unknown_send == unknown
    harness.state.now += 4
    submit(harness, request_id="after-recovery")
    assert len(send_evaluations(page)) == 1 and harness.service._uncertain
    page.values.append({"result": {"value": check_receipt()}})
    submit(harness, "check", request_id="verify-after-recovery")
    assert not harness.service._uncertain and harness.service._unknown_send is None


@pytest.mark.parametrize("next_room", [ROOM, "21399689"])
def test_stop_and_reopen_do_not_clear_or_overwrite_prior_unknown_submission(harness, next_room):
    page = open_browser(harness)
    page.values.append(TimeoutError("synthetic_secret"))
    submit(harness, request_id="lost-before-stop")
    unknown = harness.service._unknown_send
    harness.service.stop_browser()
    next_url = "https://live.bilibili.com/" + next_room
    harness.state.targets = [{"type": "page", "url": next_url,
                              "webSocketDebuggerUrl": f"ws://127.0.0.1:{PORT}/devtools/page/reopened"}]
    harness.service.open_sync(next_url)
    new_page = harness.service._page
    assert new_page is not page and harness.service.is_browser_ready
    assert harness.service._uncertain and harness.service._unknown_send == unknown
    harness.state.now += 4
    submit(harness, room=next_room, request_id="after-reopening")
    assert not new_page.calls and harness.results[-1]["result"]["outcome"] == "not_sent"
    assert harness.service._unknown_send == unknown


@pytest.mark.parametrize("preflight", ["generation", "room", "script_not_sent"])
def test_confirmed_not_sent_does_not_create_an_unknown_submission_record(harness, preflight):
    page = open_browser(harness)
    if preflight == "generation":
        page.before_baseline_result = lambda: setattr(harness.service, "_generation", harness.service.generation + 1)
    elif preflight == "room":
        harness.state.targets = []
    else:
        page.values.append({"result": {"value": {"error": "not_logged_in", "request_started_epoch_ms": None}}})
    submit(harness, request_id="known-not-sent")
    assert harness.results[-1]["result"]["outcome"] == "not_sent"
    assert not harness.service._uncertain and harness.service._unknown_send is None


def test_queued_preflight_not_sent_cannot_overwrite_protected_unknown_record(harness):
    page = open_browser(harness)
    page.values.append(TimeoutError("synthetic_secret"))
    submit(harness, request_id="first-unknown")
    unknown = harness.service._unknown_send
    submit(harness, request_id="different-text-not-submitted", text="【这是另一条未实际推送的测试内容】")
    assert harness.results[-1]["result"]["outcome"] == "not_sent"
    assert harness.service._uncertain and harness.service._unknown_send == unknown


@pytest.mark.parametrize("response", [check_receipt(occurrence_count=0), check_receipt(occurrence_count=True),
                                       check_receipt(room_matches=False), check_receipt(chat_ready=False)])
def test_invalid_or_nonready_occurrence_cannot_release_unknown_submission(harness, response):
    page = open_browser(harness)
    page.values.append(TimeoutError("synthetic_secret"))
    submit(harness, request_id="protected")
    unknown = harness.service._unknown_send
    page.values.append({"result": {"value": response}})
    submit(harness, "check", request_id="invalid-proof")
    assert harness.service._uncertain and harness.service._unknown_send == unknown


@pytest.mark.parametrize("change", ["generation", "navigation"])
def test_readonly_preflight_race_prevents_post_and_remains_known_not_sent(harness, change):
    page = open_browser(harness)
    def switch():
        if change == "generation":
            harness.service._generation += 1
        else:
            harness.state.targets = [{"type": "page", "url": "https://live.bilibili.com/21399689"}]
    page.before_baseline_result = switch
    assert submit(harness)
    assert not send_evaluations(page)
    assert harness.results[-1]["result"]["outcome"] == "not_sent"


def test_preflight_generation_change_prevents_post_and_is_known_not_sent(harness):
    page = open_browser(harness)
    assert harness.service.request_danmaku("send", "old-generation", ROOM, TEXT)
    harness.service._generation += 1
    harness.service._executor.drain()
    assert not page.calls
    assert harness.results[-1]["result"]["outcome"] == "not_sent"


@pytest.mark.parametrize("change", ["generation", "navigation"])
def test_generation_or_navigation_change_after_evaluation_keeps_result_unknown(harness, change):
    page = open_browser(harness)
    def switch():
        if change == "generation":
            harness.service._generation += 1
        else:
            harness.state.targets = [{"type": "page", "url": "https://live.bilibili.com/21399689"}]
    page.before_result = switch
    assert submit(harness)
    assert len(send_evaluations(page)) == 1 and harness.results[-1]["result"]["outcome"] == "result_unknown"


def test_close_restores_caption_opacity_before_browser_and_job_close(harness):
    open_browser(harness)
    harness.service._visibility = SimpleNamespace(close=lambda: harness.trace.append(("visibility.restore",)))
    harness.service.job = SimpleNamespace(close=lambda: harness.trace.append(("job.close",)))
    harness.service.stop_browser()
    names = [row[0] for row in harness.trace]
    assert names.index("visibility.restore") < names.index("browser.call") < names.index("job.close")
    assert not harness.service.is_browser_ready and harness.service.process is None and harness.service.job is None


def test_restore_failure_does_not_skip_owned_process_and_job_cleanup(harness):
    open_browser(harness)
    def failed_restore():
        harness.trace.append(("visibility.restore",))
        raise RuntimeError("synthetic_secret")
    harness.service._visibility = SimpleNamespace(close=failed_restore)
    harness.service.job = SimpleNamespace(close=lambda: harness.trace.append(("job.close",)))
    harness.service.stop_browser()
    assert harness.service.process is None and harness.service.job is None
    assert harness.trace.index(("visibility.restore",)) < harness.trace.index(("job.close",))
    assert "synthetic_secret" not in json.dumps(harness.events)


def test_recovery_restores_visibility_then_refreshes_page_and_fences_old_generation(harness):
    page = open_browser(harness)
    generation = harness.service.generation
    harness.service._hide_requested = True
    harness.service._visibility = SimpleNamespace(close=lambda: harness.trace.append(("visibility.restore",)))
    harness.service.recover_caption()
    assert page.calls[-1] == ("Page.reload", {"ignoreCache": False}, 20)
    assert harness.trace.index(("visibility.restore",)) < harness.trace.index(("page.call", "Page.reload"))
    assert harness.service.generation > generation and not harness.service._hide_requested
    assert not any(row[0] == "Runtime.evaluate" for row in page.calls)


def test_recovering_wrong_room_does_not_refresh_or_send(harness):
    page = open_browser(harness)
    harness.state.targets = [{"type": "page", "url": "https://live.bilibili.com/21399689"}]
    with pytest.raises(ChromeControlError):
        harness.service.recover_caption()
    assert not page.calls


def test_profile_caption_seed_retains_user_and_noncaption_data(tmp_path):
    profile = tmp_path / "profile"
    path = profile / "Default" / "Preferences"
    path.parent.mkdir(parents=True)
    original = {"account_info": [{"id": "synthetic-account-marker"}], "homepage": "https://example.invalid",
                "accessibility": {"other_setting": 42, "captions": {"font_size": 19, "custom_color": "white"}},
                "unrelated": {"retain": [1, 2, 3]}}
    path.write_text(json.dumps(original), encoding="utf-8")
    cookie_file = path.parent / "Cookies"
    cookie_file.write_bytes(b"synthetic cookie database, do not read or replace")
    seed_caption_preferences(profile)
    actual = json.loads(path.read_text(encoding="utf-8"))
    assert actual["account_info"] == original["account_info"] and actual["homepage"] == original["homepage"]
    assert actual["unrelated"] == original["unrelated"] and actual["accessibility"]["other_setting"] == 42
    captions = actual["accessibility"]["captions"]
    assert captions["font_size"] == 19 and captions["custom_color"] == "white"
    assert captions["live_caption_enabled"] is True and captions["live_caption_language"] == "ja-JP"
    assert captions["live_translate_enabled"] is True and captions["live_translate_target_language"] == "zh-CN"
    assert cookie_file.read_bytes() == b"synthetic cookie database, do not read or replace"


def test_caption_preference_atomic_write_failure_keeps_original_profile_data(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    path = profile / "Default" / "Preferences"
    path.parent.mkdir(parents=True)
    original = b'{"unrelated":{"retain":true},"accessibility":{"captions":{"font_size":19}}}'
    path.write_bytes(original)
    monkeypatch.setattr(module.os, "replace", Mock(side_effect=OSError("synthetic write failure")))
    with pytest.raises(OSError):
        seed_caption_preferences(profile)
    assert path.read_bytes() == original
