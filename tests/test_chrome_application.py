"""Independent lifecycle and presentation tests using local fixtures only."""
import json
import os
import sys
from concurrent.futures import Future
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QPushButton
from fanyiji.app import ChromeAppController
from fanyiji.config import AppConfig, ConfigError, load_config, save_config
from fanyiji.ui import MainWindow, SettingsDialog


class ImmediateExecutor:
    def submit(self, function, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except Exception as error:
            future.set_exception(error)
            raise
        return future

    def shutdown(self, **options):
        return


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def harness(application, tmp_path, monkeypatch):
    monkeypatch.setenv("FANYIJI_CHROME_DATA", str(tmp_path))
    monkeypatch.setattr("fanyiji.app.ThreadPoolExecutor", lambda **options: ImmediateExecutor())
    config = AppConfig()
    window = MainWindow(config)
    controller = ChromeAppController(window, config, tmp_path / "settings.json")
    yield SimpleNamespace(window=window, controller=controller, directory=tmp_path)
    controller.close()
    window.close()


def connect_fixture(value):
    browser = SimpleNamespace(generation=7, browser_pid=1234, is_browser_ready=True,
        source_ready=True, room="1698225", configure_audio_processing=Mock(), set_caption_hidden=Mock(),
        open_sync=Mock(return_value=1234), stop_browser=Mock(), close=Mock(), recover_caption=Mock())
    reader = SimpleNamespace(generation=4, reader_generation=2, start=Mock(), stop=Mock(), close=Mock())
    value.controller.chrome_browser_service = browser
    value.controller.chrome_caption_service = reader
    value.controller._ensure_owned_chrome = lambda: browser
    value.controller._ensure_chrome_caption_service = lambda: reader
    value.controller._ensure_chrome_danmaku = lambda: None
    return browser, reader


def test_startup_does_not_initialize_audio_browser_or_reader(harness):
    harness.controller.start()
    assert harness.controller.chrome_browser_service is None
    assert harness.controller.chrome_caption_service is None
    assert harness.controller.chrome_danmaku is None
    assert not hasattr(harness.controller, "pipeline")
    assert not hasattr(harness.controller, "live_source_service")
    assert not hasattr(harness.controller, "specialized")


@pytest.mark.parametrize("language", ["zh-CN", "ja", "en"])
@pytest.mark.parametrize("frozen", [False, True])
def test_smoke_checks_public_controls_without_browser_or_userdata(application, tmp_path, monkeypatch, capsys, language, frozen):
    from fanyiji import app as module, local_features
    from fanyiji.i18n import get_language, set_language
    previous_language = get_language()
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setenv("FANYIJI_CHROME_DATA", str(tmp_path))
    def forbidden(*args, **kwargs):
        raise AssertionError("UI smoke must not access browser or userdata")
    monkeypatch.setattr(module, "data_root", forbidden)
    monkeypatch.setattr(local_features, "data_root", forbidden)
    monkeypatch.setattr(local_features.platform, "node", forbidden)
    monkeypatch.setattr(module, "ChromeAppController", forbidden)
    try:
        assert module.smoke_test(language=language) == 0
        value = json.loads(capsys.readouterr().out)
        assert value == {"ui_smoke": "passed", "audio_capture": False, "settings_written": False,
                         "browser_opened": False, "project": "chrome", "danmaku_available": True,
                         "danmaku_controls_visible": True, "danmaku_controls_enabled": True,
                         "danmaku_default_enabled": False}
        assert not list(tmp_path.iterdir())
    finally:
        set_language(previous_language)


def test_smoke_does_not_report_passed_when_public_sender_is_unavailable(application, monkeypatch, capsys):
    from fanyiji import app as module, local_features
    from fanyiji.i18n import get_language, set_language
    previous_language = get_language()
    monkeypatch.setattr(local_features, "danmaku_available", lambda: False)
    try:
        with pytest.raises(RuntimeError, match="public_danmaku_smoke_failed"):
            module.smoke_test()
        assert not capsys.readouterr().out
    finally:
        set_language(previous_language)


def test_saved_enabled_push_and_room_do_not_auto_open(harness):
    (harness.directory / "chrome-caption-settings.json").write_text(json.dumps({
        "room_url": "https://live.bilibili.com/1698225", "browser_mode": "dedicated",
        "audio_processing": "gtcrn", "hide_caption": True,
        "push_preferences": {"enabled": True, "room": "1698225", "interval_seconds": 3.5}}), encoding="utf-8")
    harness.controller._load_preferences()
    harness.controller.start()
    assert harness.controller._chrome_active_url == ""
    assert harness.controller.chrome_browser_service is None
    assert harness.controller.chrome_caption_service is None


def test_explicit_dedicated_open_uses_only_exact_requested_room(harness):
    browser, reader = connect_fixture(harness)
    harness.controller._chrome_audio_processing = "gtcrn"
    harness.controller._chrome_hide_caption = True
    harness.controller._open_dedicated_chrome_room("https://live.bilibili.com/1698225?source=test")
    browser.open_sync.assert_called_once_with("https://live.bilibili.com/1698225")
    reader.start.assert_called_once_with("https://live.bilibili.com/1698225", session=1, owned_browser_pid=1234)
    browser.configure_audio_processing.assert_called_once_with("gtcrn", reload=True)
    browser.set_caption_hidden.assert_called_once_with(True)


def test_pause_restores_audio_and_bubble_then_resumes_same_source(harness):
    browser, reader = connect_fixture(harness)
    harness.controller._chrome_active_url = "https://live.bilibili.com/1698225"
    harness.controller._chrome_audio_processing = "gtcrn"
    harness.controller.set_paused(True)
    reader.stop.assert_called_once()
    browser.configure_audio_processing.assert_called_with("off")
    browser.set_caption_hidden.assert_called_with(False)
    browser.close.assert_not_called()
    harness.controller.set_paused(False)
    assert reader.start.call_args.kwargs["owned_browser_pid"] == 1234


def test_same_model_manual_retry_retains_subtitles_and_generation(harness):
    browser, reader = connect_fixture(harness)
    controller = harness.controller
    controller._chrome_audio_processing = "gtcrn"
    controller._chrome_active_url = "https://live.bilibili.com/1698225"
    original_protocol, push_protocol = object(), object()
    controller._chrome_protocol = original_protocol
    controller._chrome_push_protocol = push_protocol
    epoch = controller.epoch
    assert controller._configure_chrome_audio("gtcrn", persist=False)
    browser.configure_audio_processing.assert_called_once_with("gtcrn")
    assert controller.epoch == epoch
    assert controller._chrome_protocol is original_protocol
    assert controller._chrome_push_protocol is push_protocol
    reader.start.assert_not_called()


def test_old_reader_events_are_fenced_and_health_does_not_clear_text(harness):
    browser, reader = connect_fixture(harness)
    controller = harness.controller
    harness.window.set_translation("system", "当前有效字幕", "current")
    event = {"kind": "chrome_caption", "caption_kind": "health", "session": controller.epoch,
        "generation": reader.generation, "reader_generation": reader.reader_generation,
        "reader_alive": True, "caption_update_age_seconds": 25}
    controller._on_event({**event, "reader_generation": 1})
    assert controller._chrome_caption_health == {}
    controller._on_event(event)
    assert "距字幕文字更新 25 秒" in harness.window.source_status.text()
    assert harness.window._rows["current"]["label"].text() == "当前有效字幕"


def test_recover_bubble_cancels_hide_preference(harness):
    browser, reader = connect_fixture(harness)
    controller = harness.controller
    controller._chrome_hide_caption = True
    controller._chrome_active_url = "https://live.bilibili.com/1698225"
    controller._recover_chrome_caption()
    assert not controller._chrome_hide_caption
    saved = json.loads((harness.directory / "chrome-caption-settings.json").read_text(encoding="utf-8"))
    assert saved["hide_caption"] is False
    browser.set_caption_hidden.assert_called_once_with(False)
    browser.recover_caption.assert_called_once()


def test_caption_rows_revise_in_place_and_reject_stale_events(harness):
    window = harness.window
    window.set_translation("system", "第一句", "first", revision=1, context_generation=4)
    window.set_translation("system", "第二句", "second", revision=1, context_generation=4)
    window.set_translation("system", "修订后的第一句", "first", revision=2, context_generation=4)
    window.set_translation("system", "过期版本", "first", revision=1, context_generation=4)
    assert list(window._rows) == ["first", "second"]
    assert window._rows["first"]["label"].text() == "修订后的第一句"
    window.prune_chrome_captions(1)
    assert list(window._rows) == ["second"]


def test_caption_protocol_splits_sentences_and_keeps_latin_names_for_display(harness):
    browser, reader = connect_fixture(harness)
    controller = harness.controller
    controller._on_event({"kind": "chrome_caption", "caption_kind": "snapshot", "session": controller.epoch,
        "generation": reader.generation, "reader_generation": reader.reader_generation,
        "text": "今天天气很好。谢谢 Aya 的留言！我们开始下一局。", "window_id": "fixture-caption",
        "source_epoch": 3, "observed_at": 100.0, "bridge_generation": reader.generation,
        "room": "1698225", "connected": True})
    assert [row["label"].text() for row in harness.window._rows.values()] == [
        "今天天气很好。", "谢谢 Aya 的留言！", "我们开始下一局。"]
    assert controller.chrome_danmaku is None


def test_audio_model_dialog_has_only_gtcrn_and_does_not_access_live_source(application, tmp_path):
    from fanyiji.chrome_audio_dialog import ChromeAudioModelDialog
    dialog = ChromeAudioModelDialog(models_dir=tmp_path)
    assert dialog.mode.count() == 1
    assert dialog.mode.currentData() == "gtcrn"
    assert not hasattr(dialog, "service")
    assert not hasattr(dialog, "browser_service")
    assert not hasattr(dialog, "source_enabled")
    assert not dialog.is_busy
    dialog.close()


@pytest.mark.parametrize("value, expected", [
    ("1698225", "https://live.bilibili.com/1698225"),
    ("https://live.bilibili.com/1698225?broadcast_type=0", "https://live.bilibili.com/1698225"),
    ("000123", "https://live.bilibili.com/123"),
])
def test_room_url_normalization_is_pure(value, expected):
    from fanyiji.room_url import room_url
    assert room_url(value) == expected


@pytest.mark.parametrize("value", [None, 123, "0", "-1", "１２３", "https://example.com/123", "123/4"])
def test_room_url_rejects_invalid_reference(value):
    from fanyiji.room_url import room_url
    with pytest.raises(ValueError):
        room_url(value)


def test_ui_contains_only_chrome_controls_and_expected_defaults(harness):
    labels = {button.text() for button in harness.window.findChildren(QPushButton)}
    assert "Chrome 直播间" in labels
    assert not labels.intersection({"标准语音翻译", "模型管理", "专用模型", "国内 CLI 账号与模型"})
    assert harness.window.font_size.value() == 16
    assert harness.window.opacity_slider.value() == 66
    assert abs(harness.window.width() / harness.window.height() - 21 / 9) < .01


@pytest.mark.parametrize("language", ["zh-CN", "ja", "en"])
def test_language_preferences_round_trip(application, tmp_path, language):
    config = AppConfig(ui_language=language)
    path = tmp_path / "settings.json"
    save_config(path, config)
    assert load_config(path) == config
    dialog = SettingsDialog(config)
    assert dialog.language.currentData() == language
    dialog.close()


def test_legacy_standard_config_import_becomes_chrome_only(tmp_path):
    raw = asdict(AppConfig())
    raw.update(processing_mode="standard", audio_processing="uvr")
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_config(path)
    assert config.processing_mode == "chrome_caption"
    assert config.audio_processing == "gtcrn"
    with pytest.raises(ConfigError):
        save_config(path, replace(config, processing_mode="standard"))
