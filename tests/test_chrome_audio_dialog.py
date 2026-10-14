"""Chrome preprocessing controls are explicit and never enable browser or chat actions."""
import os
import sys
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QLabel

from fanyiji.chrome_caption_dialog import ChromeCaptionDialog
from fanyiji.i18n import get_language, set_language, tr


@pytest.fixture
def qt_app():
    app = QApplication.instance() or QApplication([])
    previous = get_language()
    set_language("zh-CN")
    yield app
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    app.processEvents()
    set_language(previous)


def actions(dialog):
    values = {name: [] for name in ("audio", "models", "dedicated", "connect", "push", "hide", "clear")}
    for name, signal in (("audio", dialog.audio_processing_requested),
                         ("dedicated", dialog.dedicated_room_requested),
                         ("connect", dialog.connect_room_requested),
                         ("push", dialog.push_preferences_requested),
                         ("hide", dialog.hide_caption_requested)):
        signal.connect(values[name].append)
    dialog.audio_models_requested.connect(lambda: values["models"].append(True))
    dialog.clear_pending_requested.connect(lambda: values["clear"].append(True))
    return values


@pytest.mark.parametrize("mode", ["attach", "dedicated"])
def test_saved_chrome_audio_choice_is_inert_and_survives_readonly_mode(qt_app, mode):
    dialog = ChromeCaptionDialog(mode=mode, audio_processing="gtcrn")
    recorded = actions(dialog)
    assert dialog.audio_processing_combo.currentData() == "gtcrn"
    assert dialog.audio_controls.isEnabled() is (mode == "dedicated")
    assert dialog.effective_audio_processing() == ("gtcrn" if mode == "dedicated" else "off")
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.result_preferences["audio_processing"] == "gtcrn"
    assert not dialog.result_preferences["push_preferences"]["enabled"]
    assert not any(recorded.values())


@pytest.mark.parametrize("mode", [None, "uvr", "raw", 1, [], {}])
def test_unavailable_audio_modes_fall_back_to_off_without_requests(qt_app, mode):
    dialog = ChromeCaptionDialog(mode="dedicated", audio_processing=mode)
    recorded = actions(dialog)
    dialog.accept()
    assert dialog.result_preferences["audio_processing"] == "off"
    assert dialog.effective_audio_processing() == "off"
    assert not any(recorded.values())


def test_explicit_apply_and_model_management_are_separate_from_browser_and_push(qt_app):
    with patch("fanyiji.local_features.danmaku_available", return_value=True):
        dialog = ChromeCaptionDialog(room_url="1698225", mode="dedicated")
    recorded = actions(dialog)
    dialog.audio_processing_combo.setCurrentIndex(dialog.audio_processing_combo.findData("gtcrn"))
    assert not any(recorded.values())
    dialog.audio_apply_button.click()
    dialog.audio_models_button.click()
    dialog.audio_processing_combo.setCurrentIndex(dialog.audio_processing_combo.findData("off"))
    dialog.audio_apply_button.click()
    assert recorded["audio"] == ["gtcrn", "off"]
    assert recorded["models"] == [True]
    assert not any(values for name, values in recorded.items() if name not in ("audio", "models"))
    assert not dialog.push_enabled.isChecked()


def test_readonly_attachment_ignores_direct_requests_and_preserves_dedicated_selection(qt_app):
    dialog = ChromeCaptionDialog(room_url="1698225", mode="dedicated", audio_processing="gtcrn")
    recorded = actions(dialog)
    dialog.connect_room_button.click()
    assert dialog.effective_audio_processing() == "off"
    assert not dialog.audio_controls.isEnabled()
    dialog.audio_apply_button.click()
    dialog.audio_models_button.click()
    dialog._request_audio_processing()
    dialog._request_audio_models()
    assert not recorded["audio"] and not recorded["models"]
    dialog.dedicated_room_button.click()
    assert dialog.effective_audio_processing() == "gtcrn"
    assert dialog.audio_controls.isEnabled()
    # Opening or connecting emits only that explicit action, never an audio or chat request.
    assert recorded["connect"] == recorded["dedicated"] == ["https://live.bilibili.com/1698225"]
    assert not recorded["audio"] and not recorded["push"]


@pytest.mark.parametrize("language", ["zh-CN", "ja", "en"])
def test_audio_controls_translate_capabilities_and_experimental_boundaries(qt_app, language):
    set_language(language)
    dialog = ChromeCaptionDialog(mode="dedicated")
    source = "GTCRN 主要减少噪声和部分背景声，不能锁定主播、分离游戏角色讲话或保证去除唱歌。Chrome 识别处理后声音的链路仍属实验，以实际状态为准。处理模型单独下载，不包含在程序包中。"
    labels = [label.text() for label in dialog.findChildren(QLabel)]
    assert tr(source) in labels
    assert dialog.audio_apply_button.text() == tr("应用 Chrome 音频处理")
    assert dialog.audio_models_button.text() == tr("管理音频处理模型")
    assert dialog.audio_processing_combo.itemText(1) == tr("GTCRN 语音增强（实验）")
    if language != "zh-CN":
        assert tr(source) != source
        assert dialog.audio_apply_button.text() != "应用 Chrome 音频处理"


@pytest.mark.parametrize("language", ["zh-CN", "ja", "en"])
@pytest.mark.parametrize("mode", ["attach", "dedicated"])
def test_public_frozen_package_shows_controls_but_requires_explicit_enable(qt_app, monkeypatch, tmp_path, language, mode):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("FANYIJI_CHROME_DATA", str(tmp_path))
    set_language(language)
    dialog = ChromeCaptionDialog(room_url="1698225", mode=mode)
    recorded = actions(dialog)
    assert dialog._danmaku_available
    assert not dialog.push_controls.isHidden()
    assert dialog.push_controls.isEnabled() is (mode == "dedicated")
    assert not dialog.push_enabled.isChecked()
    assert dialog.push_enabled.text() == tr("同步稳定的新中文字幕到直播弹幕")
    notice = "弹幕同步默认关闭。请先手动打开专用 Chrome 直播间并登录账号，再开启同步；现有 Chrome 只读连接不发送弹幕。"
    assert dialog.danmaku_access_note.text() == tr(notice)
    if language != "zh-CN":
        assert tr(notice) != notice
    assert not any(recorded.values())
    dialog.accept()
    assert not dialog.result_preferences["push_preferences"]["enabled"]
    assert not any(recorded.values())
    assert not (tmp_path / "local-features.json").exists()


@pytest.mark.parametrize("mode", ["attach", "dedicated"])
def test_public_sender_enable_request_still_requires_dedicated_mode(qt_app, monkeypatch, mode):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    dialog = ChromeCaptionDialog(room_url="1698225", mode=mode)
    recorded = actions(dialog)
    dialog.push_enabled.setChecked(True)
    dialog._request_push_preferences()
    dialog._request_clear_pending()
    if mode == "dedicated":
        assert recorded["push"] == [{"enabled": True, "room": "1698225", "interval_seconds": 3.5}]
        assert recorded["clear"] == [True]
    else:
        assert not recorded["push"] and not recorded["clear"]
    assert not recorded["dedicated"] and not recorded["connect"]
    dialog.accept()
    assert dialog.result_preferences["push_preferences"]["enabled"] is (mode == "dedicated")
