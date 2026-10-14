"""Exercise the service's real lifecycle methods against a synthetic desktop.

No QObject is instantiated, no browser starts, and no native/UIA code executes.
"""
from types import SimpleNamespace
import os
import subprocess
import threading

import pytest

from fanyiji.chrome_browser_service import ChromeBrowserService
from fanyiji.chrome_caption_visibility import CaptionVisibilityController, CaptionVisibilityError, LWA_ALPHA, WS_EX_LAYERED
from fanyiji.chrome_caption_worker import NativeWindow
from test_chrome_caption_visibility import FakeVisibility


class FakeService:
    set_caption_hidden = ChromeBrowserService.set_caption_hidden
    stop_browser = ChromeBrowserService.stop_browser
    recover_caption = ChromeBrowserService.recover_caption
    close = ChromeBrowserService.close
    _monitor = ChromeBrowserService._monitor
    _stop_audio_processing = ChromeBrowserService._stop_audio_processing
    _start_audio_processing = ChromeBrowserService._start_audio_processing
    is_browser_ready = ChromeBrowserService.is_browser_ready
    browser_pid = ChromeBrowserService.browser_pid
    generation = ChromeBrowserService.generation

    def __init__(self):
        self.actions = []
        self._operation = threading.RLock()
        self._ready = self.source_ready = True
        self._closed = self._hide_requested = False
        self._visibility = None
        self._visibility_notified = None
        self._uncertain = True
        self._audio_runtime = None
        self.audio_processing_mode = "off"
        self._generation = 4
        self.process = FakeProcess(self.actions)
        self.job = SimpleNamespace(handle=777, close=self.close_job, owner_process=self.process)
        self._page = SimpleNamespace(call=self.page_call, close=lambda: self.actions.append(("page_close",)))
        self._browser = SimpleNamespace(call=self.browser_call, close=lambda: self.actions.append(("connection_close",)))
        self._executor = SimpleNamespace(shutdown=lambda **kwargs: self.actions.append(("executor_close", kwargs)))

    def close_job(self):
        self.actions.append(("job_close",))
        self.job.owner_process.exited = True
        self.job.handle = None

    def _room_target(self):
        self.actions.append(("room_check",))
        return {"type": "page", "url": "https://live.bilibili.com/1698225"}

    def _emit(self, kind, **fields):
        self.actions.append(("event", kind, fields))

    def page_call(self, method, parameters=None):
        self.actions.append(("page_call", method, parameters))

    def browser_call(self, method, **kwargs):
        self.actions.append(("browser_call", method))


class FakeProcess:
    pid = 42

    def __init__(self, actions):
        self.actions, self.exited = actions, False

    def poll(self):
        return 0 if self.exited else None

    def wait(self, timeout):
        self.actions.append(("process_wait", timeout))
        self.exited = True
        return 0

    def kill(self):
        self.actions.append(("process_kill",))
        self.exited = True


class EndMonitorTick(Exception):
    pass


def one_monitor_tick(service, monkeypatch):
    def end(_seconds):
        raise EndMonitorTick
    monkeypatch.setattr("fanyiji.chrome_browser_service.time.sleep", end)
    with pytest.raises(EndMonitorTick):
        service._monitor(service.process, service.generation)


@pytest.fixture
def lifecycle(monkeypatch):
    service, adapter = FakeService(), FakeVisibility()
    adapter.operations = service.actions

    def factory(process, job):
        assert process is service.process and process.poll() is None
        assert job is service.job and job.handle == 777
        return adapter

    monkeypatch.setattr("fanyiji.chrome_caption_visibility.NativeCaptionVisibilityAdapter", factory)
    return service, adapter


def test_pending_caption_does_not_claim_hidden_and_adapter_remains_closeable(lifecycle):
    service, adapter = lifecycle
    adapter.items = [adapter.items[1]]
    assert service.set_caption_hidden(True) is False
    assert service._hide_requested and service._visibility is not None and not service._visibility.hidden
    assert not any(row[:2] == ("event", "caption_hidden") for row in service.actions)
    service.set_caption_hidden(False)
    assert service._visibility is None and ("close",) in adapter.operations


def test_monitor_hides_caption_that_arrives_after_initial_empty_result(lifecycle, monkeypatch):
    service, adapter = lifecycle
    adapter.items = [adapter.items[1]]
    service.set_caption_hidden(True)
    adapter.items.append(NativeWindow(101, 42, "实时字幕", "Chrome_WidgetWin_1"))
    one_monitor_tick(service, monkeypatch)
    assert service._visibility is not None and service._visibility.hidden
    assert adapter.attributes.alpha == 0 and adapter.attributes.flags & LWA_ALPHA


def test_shutdown_restores_before_closing_cdp_process_and_job(lifecycle):
    service, adapter = lifecycle
    assert service.set_caption_hidden(True)
    service.stop_browser()
    names = [row[0] for row in service.actions]
    assert names.index("unmark") < names.index("page_close") < names.index("process_wait") < names.index("job_close")
    assert adapter.style == 0x80 and adapter.attributes is None
    assert service._visibility is service.process is service.job is None


def test_show_restore_failure_retains_live_job_and_controller_until_retry(lifecycle):
    service, adapter = lifecycle
    assert service.set_caption_hidden(True)
    controller, process, job = service._visibility, service.process, service.job
    adapter.fail_style = 1
    with pytest.raises(CaptionVisibilityError, match="restore_failed"):
        service.set_caption_hidden(False)
    assert service._visibility is controller and controller.hidden
    assert service.process is process and process.poll() is None and service.job is job and job.handle == 777
    assert not any(row[0] in ("page_close", "process_wait", "process_kill", "job_close") for row in service.actions)
    service.set_caption_hidden(False)
    assert adapter.style == 0x80 and service._visibility is None and service.job is job


def test_terminal_shutdown_closes_owned_job_even_when_window_restore_fails(lifecycle):
    service, adapter = lifecycle
    service.set_caption_hidden(True)
    process, job = service.process, service.job
    adapter.fail_style = 1
    service.stop_browser()
    assert job.handle is None and process.exited and service.job is None
    assert ("job_close",) in service.actions


def test_failed_initial_hide_and_rollback_keep_controller_accessible_to_restore(lifecycle):
    service, adapter = lifecycle
    original = adapter.set_layered_attributes
    adapter.fail_alpha = 1

    def fail_opacity_and_both_restoration_attempts(identity, attributes):
        adapter.fail_style = 2
        return original(identity, attributes)

    adapter.set_layered_attributes = fail_opacity_and_both_restoration_attempts
    with pytest.raises(CaptionVisibilityError):
        service.set_caption_hidden(True)
    assert service._visibility is not None and service._visibility.hidden, "Original state must remain reachable after rollback failure"
    assert adapter.style & WS_EX_LAYERED and adapter.marked is not None
    adapter.fail_style = 0
    service.set_caption_hidden(False)
    assert service._visibility is None and adapter.style == 0x80 and adapter.marked is None


def test_monitor_revalidates_hidden_controller_and_hides_new_caption_hwnd(lifecycle, monkeypatch):
    service, adapter = lifecycle
    service.set_caption_hidden(True)
    adapter.items[0] = NativeWindow(102, 42, "实时字幕", "Chrome_WidgetWin_1")
    adapter.serial += 1
    adapter.marked = None
    adapter.style, adapter.attributes = 0x80, None
    one_monitor_tick(service, monkeypatch)
    assert adapter.marked is not None and adapter.marked.handle == 102
    assert adapter.attributes.alpha == 0 and service._visibility.hidden


def test_recovery_restores_before_reload_and_cancels_hide_request(lifecycle):
    service, adapter = lifecycle
    service.set_caption_hidden(True)
    previous = service.generation
    service.recover_caption()
    names = [row[0] for row in service.actions]
    assert names.index("unmark") < names.index("page_call")
    assert any(row[:2] == ("page_call", "Page.reload") for row in service.actions)
    assert service.generation > previous and not service._hide_requested and service._visibility is None
    assert adapter.style == 0x80


def test_monitor_after_recovery_keeps_caption_visible_and_source_ready(lifecycle, monkeypatch):
    service, adapter = lifecycle
    service.set_caption_hidden(True)
    unknown = ("1698225", "synthetic pending record", 0)
    service._unknown_send = unknown
    service._uncertain = True
    service.recover_caption()
    previous_writes = [row for row in service.actions if row[0] in {"alpha", "style", "mark"}]
    one_monitor_tick(service, monkeypatch)
    assert [row for row in service.actions if row[0] in {"alpha", "style", "mark"}] == previous_writes
    assert service._visibility is None and not service._hide_requested
    assert adapter.style == 0x80 and adapter.attributes is None
    assert service.is_browser_ready and service.source_ready
    assert service._uncertain and service._unknown_send == unknown


class BeforeMonitorLock:
    """Run another operation after the old decision, before acquiring its lock."""
    def __init__(self, action):
        self.action, self.lock = action, threading.RLock()

    def __enter__(self):
        action, self.action = self.action, None
        if action:
            action()
        self.lock.acquire()
        return self

    def __exit__(self, *_args):
        self.lock.release()


def test_queued_monitor_hide_decision_cannot_override_recovery_that_acquires_lock_first(lifecycle, monkeypatch):
    service, adapter = lifecycle
    service.set_caption_hidden(True)
    service._operation = BeforeMonitorLock(service.recover_caption)
    one_monitor_tick(service, monkeypatch)
    assert service._visibility is None and not service._hide_requested
    assert adapter.style == 0x80 and adapter.attributes is None
    writes = [row for row in service.actions if row[0] == "alpha"]
    assert len(writes) == 1, "The monitor must not submit a second alpha=0 after recovery"
    assert service.is_browser_ready and service.source_ready


def test_queued_monitor_hide_decision_cannot_mutate_replacement_browser(lifecycle):
    service, adapter = lifecycle
    service._hide_requested = True
    process = service.process

    def replace():
        service.process = FakeProcess(service.actions)
        service._generation += 1

    service._operation = BeforeMonitorLock(replace)
    service._monitor(process, service.generation)
    assert service._visibility is None
    assert not any(row[0] in {"alpha", "style", "mark"} for row in adapter.operations)


def test_recovery_restore_failure_does_not_reload_or_discard_state(lifecycle):
    service, adapter = lifecycle
    service.set_caption_hidden(True)
    controller, generation = service._visibility, service.generation
    adapter.fail_style = 1
    with pytest.raises(CaptionVisibilityError, match="restore_failed"):
        service.recover_caption()
    assert service._visibility is controller and controller.hidden
    assert service.generation == generation and service._uncertain
    assert not any(row[0] == "page_call" for row in service.actions)


def test_browser_exit_after_recovery_still_emits_closed_and_clears_source(lifecycle):
    service, _adapter = lifecycle
    initial_monitor_generation = service.generation
    service.recover_caption()
    service.process.exited = True
    service._monitor(service.process, initial_monitor_generation)
    assert not service._ready and not service.source_ready
    assert any(row[:2] == ("event", "closed") for row in service.actions)


def test_old_monitor_cannot_hide_replacement_browser_after_target_lookup(lifecycle, monkeypatch):
    service, adapter = lifecycle
    service._hide_requested = True
    old_process = service.process

    def replace_browser():
        service.process = FakeProcess(service.actions)
        service._generation += 1
        return {}

    service._room_target = replace_browser
    monkeypatch.setattr("fanyiji.chrome_browser_service.time.sleep", lambda _seconds: None)
    service._monitor(old_process, service.generation)
    assert service._visibility is None and adapter.operations == [], "A stale monitor may not mutate the replacement instance"


@pytest.mark.parametrize("failure", ["page_close", "connection_close", "process_wait", "kill_after_timeout", "adapter_close"])
def test_terminal_shutdown_finally_closes_job_and_releases_profile_lock_after_cleanup_errors(lifecycle, tmp_path, failure, caplog):
    service, adapter = lifecycle
    service.set_caption_hidden(True)
    process, job = service.process, service.job
    lock = (tmp_path / "owned-profile.lock").open("w+b")
    lock.write(b"0")
    lock.flush()
    lock.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    service._profile_lock = lock

    def fail(*_args, **_kwargs):
        raise OSError("private cleanup error must not enter logs")

    if failure == "page_close":
        service._page.close = fail
    elif failure == "connection_close":
        service._browser.close = fail
    elif failure == "process_wait":
        process.wait = fail
    elif failure == "kill_after_timeout":
        process.wait = lambda **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("owned fake", 1))
        process.kill = fail
    else:
        adapter.close = fail
    try:
        service.stop_browser()
    except (OSError, subprocess.TimeoutExpired):
        pass  # The cleanup strategy may return a fixed failure, but its owned Job must be closed.
    assert job.handle is None and process.exited
    assert service.job is None and lock.closed and service._profile_lock is None
    assert "private cleanup" not in caplog.text
