"""Explicit Chrome caption controls; browser operations belong to the controller."""
from __future__ import annotations

import math

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFrame,
                              QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .room_url import room_url as normalize_room_url
from .i18n import tr
from . import local_features


def _label(text=""):
    label = QLabel(tr(text))
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class ChromeCaptionDialog(QDialog):
    dedicated_room_requested = Signal(str)
    connect_room_requested = Signal(str)
    copy_room_requested = Signal(str)
    # Older controllers may still know this signal; no UI action emits it.
    open_room_requested = Signal(str)
    caption_settings_requested = Signal()
    hide_caption_requested = Signal(bool)
    recover_caption_requested = Signal()
    push_preferences_requested = Signal(dict)
    clear_pending_requested = Signal()
    audio_processing_requested = Signal(str)
    audio_models_requested = Signal()

    def __init__(self, parent=None, *, room_url="", mode="dedicated", hide_caption=False, push_preferences=None,
                 audio_processing="off"):
        super().__init__(parent)
        self.result_room_url = None
        self.result_preferences = None
        self._mode = mode if mode in ("attach", "dedicated") else "attach"
        self._danmaku_available = local_features.danmaku_available()
        self.setWindowTitle(tr("Chrome 直播间与字幕设置"))
        self.resize(690, 660)
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        layout = QVBoxLayout(body)
        layout.addWidget(_label("推荐手动打开专用 Chrome 直播间，使用独立资料目录；不复制你的现有登录资料。也可只读连接现有 Chrome。保存设置及重启不会自动打开或连接直播间；现有 Chrome 不会被关闭。"))
        row = QHBoxLayout()
        row.addWidget(_label("直播间"))
        self.room_input = QLineEdit(room_url)
        self.room_input.setMaxLength(2048)
        self.room_input.setPlaceholderText(tr("直播间号或 HTTPS 直播间地址"))
        self.room_input.textChanged.connect(self._clear_error)
        row.addWidget(self.room_input, 1)
        layout.addLayout(row)
        self.dedicated_room_button = QPushButton(tr("打开专用 Chrome 直播间"))
        self.dedicated_room_button.clicked.connect(self._open_dedicated_room)
        layout.addWidget(self.dedicated_room_button)
        self.connect_room_button = QPushButton(tr("连接现有 Chrome 直播间"))
        self.connect_room_button.setAutoDefault(False)
        self.connect_room_button.clicked.connect(self._connect_room)
        layout.addWidget(self.connect_room_button)
        self.copy_room_button = QPushButton(tr("复制直播间地址"))
        self.copy_room_button.setAutoDefault(False)
        self.copy_room_button.clicked.connect(self._copy_room)
        self.open_room_button = self.copy_room_button
        layout.addWidget(self.copy_room_button)
        self.caption_settings_button = QPushButton(tr("Chrome 字幕设置说明"))
        self.caption_settings_button.setAutoDefault(False)
        self.caption_settings_button.clicked.connect(self.caption_settings_requested.emit)
        layout.addWidget(self.caption_settings_button)
        self.mode_label = _label()
        layout.addWidget(self.mode_label)
        self.caption_controls = QWidget()
        caption_layout = QHBoxLayout(self.caption_controls)
        caption_layout.setContentsMargins(0, 0, 0, 0)
        self.hide_caption_checkbox = QCheckBox(tr("隐藏原生字幕（取消勾选后显示）"))
        self.hide_caption_checkbox.setChecked(hide_caption is True)
        self.hide_caption_checkbox.toggled.connect(self._request_hide_caption)
        caption_layout.addWidget(self.hide_caption_checkbox)
        self.recover_caption_button = QPushButton(tr("恢复原生字幕"))
        self.recover_caption_button.setAutoDefault(False)
        self.recover_caption_button.clicked.connect(self._request_recover_caption)
        caption_layout.addWidget(self.recover_caption_button)
        layout.addWidget(self.caption_controls)
        layout.addWidget(_label("隐藏和恢复仅用于翻译姬打开的专用 Chrome；恢复会取消隐藏并重启专用 Chrome，重新打开当前直播间，保留登录状态。等待人声和字幕气泡出现后恢复读取。"))
        self.audio_controls = QWidget()
        audio_layout = QVBoxLayout(self.audio_controls)
        audio_layout.setContentsMargins(0, 0, 0, 0)
        audio_row = QHBoxLayout()
        audio_row.addWidget(_label("Chrome 前置音频处理（实验）"))
        self.audio_processing_combo = QComboBox()
        self.audio_processing_combo.addItem(tr("关闭前置音频处理"), "off")
        self.audio_processing_combo.addItem(tr("GTCRN 语音增强（实验）"), "gtcrn")
        saved_audio_processing = audio_processing if audio_processing in ("off", "gtcrn") else "off"
        self.audio_processing_combo.setCurrentIndex(self.audio_processing_combo.findData(saved_audio_processing))
        audio_row.addWidget(self.audio_processing_combo, 1)
        self.audio_apply_button = QPushButton(tr("应用 Chrome 音频处理"))
        self.audio_apply_button.setAutoDefault(False)
        self.audio_apply_button.clicked.connect(self._request_audio_processing)
        audio_row.addWidget(self.audio_apply_button)
        audio_layout.addLayout(audio_row)
        self.audio_models_button = QPushButton(tr("管理音频处理模型"))
        self.audio_models_button.setAutoDefault(False)
        self.audio_models_button.clicked.connect(self._request_audio_models)
        audio_layout.addWidget(self.audio_models_button)
        audio_layout.addWidget(_label("仅用于已打开的专用 Chrome 直播页；保存或应用不会自动打开浏览器，也不会开启弹幕同步。与标准语音模式的处理设置独立。"))
        audio_layout.addWidget(_label("GTCRN 主要减少噪声和部分背景声，不能锁定主播、分离游戏角色讲话或保证去除唱歌。Chrome 识别处理后声音的链路仍属实验，以实际状态为准。处理模型单独下载，不包含在程序包中。"))
        layout.addWidget(self.audio_controls)
        self.push_controls = QWidget()
        push_layout = QVBoxLayout(self.push_controls)
        push_layout.setContentsMargins(0, 0, 0, 0)
        self.push_enabled = QCheckBox(tr("同步稳定的新中文字幕到直播弹幕"))
        preferences = push_preferences if isinstance(push_preferences, dict) else {}
        self._push_room_uses_source = not preferences.get("room")
        self.push_enabled.setChecked(preferences.get("enabled") is True)
        push_layout.addWidget(self.push_enabled)
        push_row = QHBoxLayout()
        push_row.addWidget(_label("发送目标直播间"))
        source_room = self._normalize_saved_room(room_url)
        self.push_room_input = QLineEdit(preferences.get("room") if isinstance(preferences.get("room"), str)
                                         and preferences["room"] else source_room)
        self.push_room_input.setMaxLength(2048)
        self.push_room_input.setPlaceholderText(tr("直播间号或 HTTPS 直播间地址"))
        self.push_room_input.textEdited.connect(self._mark_push_room_explicit)
        push_row.addWidget(self.push_room_input, 1)
        push_row.addWidget(_label("发送间隔"))
        self.push_interval = QDoubleSpinBox()
        self.push_interval.setRange(3.5, 60.0)
        self.push_interval.setDecimals(1)
        self.push_interval.setSingleStep(0.5)
        self.push_interval.setSuffix(tr(" 秒"))
        interval = preferences.get("interval_seconds", 3.5)
        self.push_interval.setValue(interval if type(interval) in (int, float) and math.isfinite(interval) else 3.5)
        push_row.addWidget(self.push_interval)
        push_layout.addLayout(push_row)
        push_buttons = QHBoxLayout()
        self.push_save_button = QPushButton(tr("保存弹幕设置"))
        self.push_save_button.setAutoDefault(False)
        self.push_save_button.clicked.connect(self._request_push_preferences)
        push_buttons.addWidget(self.push_save_button)
        self.clear_pending_button = QPushButton(tr("清空待发队列"))
        self.clear_pending_button.setAutoDefault(False)
        self.clear_pending_button.clicked.connect(self._request_clear_pending)
        push_buttons.addWidget(self.clear_pending_button)
        push_layout.addLayout(push_buttons)
        push_layout.addWidget(_label("明确打开专用直播间或开启同步时，当前完整中文句稳定 1.2 秒后可进入待发；自动恢复或重连不重复扫历史。正文满 2 字独立发送，单字保留与下一句合并；含【】加权最多 40 单位。发送间隔至少 3.5 秒，正文只允许中文和数字。保存设置不会自动打开直播间或提交弹幕。"))
        self.push_controls.setVisible(self._danmaku_available)
        layout.addWidget(self.push_controls)
        self.danmaku_access_note = _label("弹幕同步默认关闭。请先手动打开专用 Chrome 直播间并登录账号，再开启同步；现有 Chrome 只读连接不发送弹幕。")
        layout.addWidget(self.danmaku_access_note)
        layout.addWidget(_label("首次使用请在 Chrome 中启用实时字幕、添加日语，开启实时翻译并选择中文。待原生字幕出现并确认能读取后，可在专用 Chrome 模式隐藏气泡；现有 Chrome 气泡需保持可见。"))
        layout.addWidget(_label("专用 Chrome 仅保留一个目标直播标签页，推送目标必须与专用 Chrome 打开的直播间相同。连接现有 Chrome 可保留其他标签页，但目标房间需为当前标签页。字幕音源由 Chrome 自行选择，其他媒体可能切换字幕来源，不能锁定主播或分离游戏角色讲话。"))
        layout.addWidget(_label("直播间未开播时，轮播中有讲话也可采集 Chrome 产生的字幕；没有讲话且尚未出现字幕气泡时，等待字幕属于正常状态。"))
        layout.addWidget(_label("Chrome 译文可能继续修订，稳定状态由程序判断。只读连接现有 Chrome 不推送弹幕；Chrome 译文不用于专用模型训练。"))
        layout.addWidget(_label("Chrome 仅提供当前译文，未提供日语原文；按句末标点分段。需要中日对照请切换“标准语音翻译”。"))
        layout.addWidget(_label("字幕设置说明只显示操作指引，不打开设置页面，也不切换标签页。专用 Chrome 首次登录由你自行完成；只读连接可沿用现有 Chrome 的登录和字幕设置。"))
        self.status_label = _label("填写直播间后手动打开专用 Chrome，或先在现有 Chrome 打开目标标签页再连接。保存设置不会自动打开或连接。")
        layout.addWidget(self.status_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self._update_mode_controls()
        self.dedicated_room_button.setDefault(True)

    def _clear_error(self):
        self.status_label.setText(tr("填写直播间后手动打开专用 Chrome，或先在现有 Chrome 打开目标标签页再连接。保存设置不会自动打开或连接。"))
        if getattr(self, "_push_room_uses_source", False):
            self.push_room_input.setText(self._normalize_saved_room(self.room_input.text()))

    def _mark_push_room_explicit(self):
        self._push_room_uses_source = False

    @staticmethod
    def _normalize_saved_room(value):
        try:
            return normalize_room_url(value).rsplit("/", 1)[-1]
        except (ValueError, TypeError, AttributeError):
            return ""

    def _update_mode_controls(self):
        dedicated = self._mode == "dedicated"
        self.mode_label.setText(tr("当前选择：专用 Chrome") if dedicated else tr("当前选择：现有 Chrome 只读连接"))
        self.caption_controls.setEnabled(dedicated)
        self.push_controls.setEnabled(dedicated and self._danmaku_available)
        self.audio_controls.setEnabled(dedicated)

    def _open_dedicated_room(self):
        url = self._normalized_room()
        if url is None:
            return
        self._mode = "dedicated"
        self._update_mode_controls()
        self.room_input.setText(url)
        self.status_label.setText(tr("已请求打开专用 Chrome 直播间，等待原生字幕连接。"))
        self.dedicated_room_requested.emit(url)

    def _normalized_room(self, *, allow_empty=False):
        value = self.room_input.text().strip()
        if allow_empty and not value:
            return ""
        try:
            return normalize_room_url(value)
        except (ValueError, TypeError):
            self.status_label.setText(tr("直播间格式无效，请填写房间号或哔哩哔哩 HTTPS 直播链接。"))
            return None

    def _connect_room(self):
        url = self._normalized_room()
        if url is None:
            return
        self._mode = "attach"
        self._update_mode_controls()
        self.room_input.setText(url)
        self.status_label.setText(tr("已请求连接现有 Chrome 直播间，等待目标页面和字幕气泡可读取。"))
        self.connect_room_requested.emit(url)

    def _copy_room(self):
        url = self._normalized_room()
        if url is None:
            return
        self.room_input.setText(url)
        self.status_label.setText(tr("已请求复制直播间地址，请在你现有的 Chrome 中手动粘贴打开。"))
        self.copy_room_requested.emit(url)

    def _request_hide_caption(self, hidden: bool):
        if self._mode == "dedicated":
            self.hide_caption_requested.emit(hidden)

    def _request_recover_caption(self):
        if self._mode == "dedicated":
            with QSignalBlocker(self.hide_caption_checkbox):
                self.hide_caption_checkbox.setChecked(False)
            self.recover_caption_requested.emit()

    def effective_audio_processing(self):
        """Keep the saved choice while read-only attachment bypasses audio processing."""
        return self.audio_processing_combo.currentData() if self._mode == "dedicated" else "off"

    def _request_audio_processing(self):
        if self._mode == "dedicated":
            self.audio_processing_requested.emit(self.effective_audio_processing())

    def _request_audio_models(self):
        if self._mode == "dedicated":
            self.audio_models_requested.emit()

    def _push_values(self):
        enabled = self._mode == "dedicated" and self._danmaku_available and self.push_enabled.isChecked()
        value = self.push_room_input.text().strip()
        if not value and not enabled:
            room = ""
        else:
            try:
                room = normalize_room_url(value).rsplit("/", 1)[-1]
            except (ValueError, TypeError):
                if enabled:
                    self.status_label.setText(tr("弹幕目标直播间格式无效，请填写房间号或 HTTPS 直播链接。"))
                    return None
                room = ""
        return {"enabled": enabled, "room": room, "interval_seconds": self.push_interval.value()}

    def _request_push_preferences(self):
        if self._mode != "dedicated" or not self._danmaku_available:
            return
        preferences = self._push_values()
        if preferences is not None:
            self.push_preferences_requested.emit(preferences)

    def _request_clear_pending(self):
        if self._mode == "dedicated" and self._danmaku_available:
            self.clear_pending_requested.emit()

    def set_status(self, text: str):
        self.status_label.setText(tr(text))

    def accept(self):
        url = self._normalized_room(allow_empty=True)
        if url is None:
            return
        preferences = self._push_values()
        if preferences is None:
            return
        self.result_room_url = url
        self.result_preferences = {"browser_mode": self._mode,
                                   "hide_caption": self._mode == "dedicated" and self.hide_caption_checkbox.isChecked(),
                                   "audio_processing": self.audio_processing_combo.currentData(),
                                   "push_preferences": preferences}
        super().accept()
