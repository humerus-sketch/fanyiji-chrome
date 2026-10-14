"""Controller error recovery with real candidate persistence and fake transport."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fanyiji.danmaku_service import DanmakuService
from test_chrome_application import application, connect_fixture, harness
from test_danmaku_chrome_candidates import Browser


@pytest.fixture
def connected(harness):
    _old_browser, reader = connect_fixture(harness)
    controller = harness.controller
    browser = Browser()
    browser.generation, browser.browser_pid = 7, 1234
    browser.source_ready, browser.room = True, "1698225"
    browser.configure_audio_processing = Mock()
    browser.set_caption_hidden = Mock()
    browser.open_sync = Mock(return_value=1234)
    browser.stop_browser, browser.close, browser.recover_caption = Mock(), Mock(), Mock()
    browser.restart_danmaku_transport = Mock(return_value=4321)
    controller.chrome_browser_service = browser
    controller._ensure_owned_chrome = lambda: browser
    controller._chrome_browser_mode = "dedicated"
    controller._chrome_active_url = "https://live.bilibili.com/1698225"
    controller._chrome_initial_capture_room = "1698225"
    controller._chrome_hide_caption = True
    sender = DanmakuService(browser, store_path=harness.directory / "test-engine-queue.sqlite3")
    sender.begin_session(controller.epoch)
    sender.configure(True, "1698225")
    sender.timer.stop()
    sender._login_room = "1698225"
    controller.chrome_danmaku = sender
    return SimpleNamespace(**vars(harness), browser=browser, reader=reader, sender=sender)


def event(value, caption_kind, **fields):
    return {"kind": "chrome_caption", "caption_kind": caption_kind, "session": value.controller.epoch,
            "generation": value.reader.generation, "reader_generation": value.reader.reader_generation, **fields}


def snapshot(value, text, *, at=100.0, epoch=1):
    value.controller._on_event(event(value, "snapshot", text=text, window_id="101", source_epoch=epoch,
        observed_at=at, bridge_generation=value.reader.generation, room="", connected=True))


def error(value, **fields):
    value.controller._on_event(event(value, "status", code="caption_engine_unavailable", **fields))


def assert_no_restarts(value):
    value.reader.start.assert_not_called()
    value.reader.stop.assert_not_called()
    value.browser.open_sync.assert_not_called()
    value.browser.stop_browser.assert_not_called()
    value.browser.recover_caption.assert_not_called()
    value.browser.restart_danmaku_transport.assert_not_called()


def test_native_engine_error_invalidates_old_mutable_candidate_without_publishing_it(connected):
    value = connected
    snapshot(value, "尚未完整的上一句话")
    snapshot(value, "尚未完整的上一句话。", at=100.5)
    assert value.window._rows and value.sender._chrome_revisions
    assert value.controller._chrome_protocol is not None and value.controller._chrome_push_protocol is not None
    assert value.sender.store.head() is None
    value.controller._chrome_initial_capture_room = "1698225"
    error(value, text="暂时无法使用实时字幕功能")
    assert value.controller._chrome_protocol is None and value.controller._chrome_push_protocol is None
    assert value.controller._chrome_initial_capture_room == ""
    assert value.sender._chrome_candidates == {} and value.sender._chrome_revisions == {}
    assert value.window._rows == {}
    value.sender.tick()
    assert value.sender.store.head() is None and value.browser.requests == []
    assert value.sender.enabled and value.controller._chrome_hide_caption
    assert "Chrome 实时字幕引擎暂不可用" in value.window.source_status.text()
    assert_no_restarts(value)


def test_engine_error_preserves_already_durable_caption_without_an_extra_send(connected):
    value = connected
    snapshot(value, "已经稳定保存的完整句子。")
    snapshot(value, "已经稳定保存的完整句子。", at=101.4)
    saved = value.sender.store.head()
    assert saved is not None and saved["state"] == "pending"
    error(value)
    assert value.sender.store.head()["id"] == saved["id"]
    assert value.sender.store.head()["text"] == saved["text"]
    assert value.sender.snapshot()["durable_queued"] == 1
    assert value.browser.requests == []
    assert_no_restarts(value)


def test_repeated_engine_error_and_healthy_heartbeats_do_not_restart_or_send(connected):
    value = connected
    for _ in range(4):
        error(value)
        value.controller._on_event(event(value, "health", reader_alive=True, heartbeat_age_seconds=0.0,
            snapshot_age_seconds=None, caption_update_age_seconds=None, recovery_attempts=0,
            reader_recovery_state="healthy", caption_state="caption_engine_unavailable"))
        value.sender.tick()
    assert value.controller._chrome_caption_health["reader_recovery_state"] == "healthy"
    assert value.controller._chrome_caption_health["caption_state"] == "caption_engine_unavailable"
    assert value.browser.requests == [] and value.sender.store.head() is None
    assert "Chrome 实时字幕引擎暂不可用" in value.window.source_status.text()
    assert_no_restarts(value)


def test_true_snapshots_resume_after_engine_error_without_replaying_history(connected):
    value = connected
    snapshot(value, "尚未完整的上一句话")
    error(value)
    value.controller._on_event(event(value, "status", code="caption_found_unverified"))
    baseline = "恢复时已经显示的历史句子。"
    snapshot(value, baseline, at=200.0, epoch=2)
    snapshot(value, baseline, at=202.0, epoch=2)
    assert value.window._rows and value.controller._chrome_protocol is not None
    assert value.sender.store.head() is None
    current = baseline + "接着介绍新的游戏地图。"
    snapshot(value, current, at=203.0, epoch=2)
    assert value.sender.store.head() is None
    snapshot(value, current, at=204.4, epoch=2)
    saved = value.sender.store.head()
    assert saved is not None and saved["text"] == "【接着介绍新的游戏地图】"
    assert saved["state"] == "pending" and saved["channel"] == "chrome_caption"
    assert value.sender.snapshot()["durable_queued"] == 1
    assert value.browser.requests == []
    assert "暂不可用" not in value.window.source_status.text()
    assert_no_restarts(value)


@pytest.mark.parametrize("stale", [{"session": 99}, {"generation": 99}, {"reader_generation": 99}])
def test_stale_engine_error_does_not_clear_current_candidates(connected, stale):
    value = connected
    snapshot(value, "当前仍在继续的这一句话")
    snapshot(value, "当前仍在继续的这一句话。", at=100.5)
    protocol, push_protocol = value.controller._chrome_protocol, value.controller._chrome_push_protocol
    revisions = dict(value.sender._chrome_revisions)
    assert revisions
    rows = dict(value.window._rows)
    error(value, **stale)
    assert value.controller._chrome_protocol is protocol and value.controller._chrome_push_protocol is push_protocol
    assert value.sender._chrome_revisions == revisions and value.window._rows == rows
    assert value.browser.requests == []
    assert_no_restarts(value)
