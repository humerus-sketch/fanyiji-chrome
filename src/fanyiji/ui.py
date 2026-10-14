"""Chrome-only caption UI; no microphone, ASR or translation model controls."""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import replace
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout,
    QLabel, QMainWindow, QPushButton, QScrollArea, QSlider, QSpinBox, QVBoxLayout, QWidget)
from .config import AppConfig
from .i18n import LANGUAGE_NAMES, recovery_action_text, tr
from .identity import APP_NAME


def _plain_label(text="", *, wrap=False):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(wrap)
    return label


class SettingsDialog(QDialog):
    def __init__(self, config=None, *, first_run=False, parent=None):
        super().__init__(parent)
        self._config = config or AppConfig()
        self.result_config = None
        self.setWindowTitle(APP_NAME + " · " + tr("更改设置"))
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.addWidget(_plain_label(tr("Chrome 仅提供当前译文，未提供日语原文；按句末标点分段。"), wrap=True))
        row = QHBoxLayout()
        row.addWidget(_plain_label(tr("界面语言")))
        self.language = QComboBox()
        for code, name in LANGUAGE_NAMES.items():
            self.language.addItem(name, code)
        self.language.setCurrentIndex(max(0, self.language.findData(self._config.ui_language)))
        row.addWidget(self.language, 1)
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(tr("保存设置"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("取消"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self):
        self.result_config = replace(self._config, ui_language=self.language.currentData())
        super().accept()


class MainWindow(QMainWindow):
    pause_toggled = Signal(bool)
    settings_requested = Signal()
    chrome_caption_requested = Signal()
    audio_models_requested = Signal()
    content_filter_requested = Signal()
    clear_pending_requested = Signal()
    closing = Signal()

    def __init__(self, config=None, parent=None):
        super().__init__(parent)
        self._processing_mode = "chrome_caption"
        self._paused = self._closing_emitted = False
        self._rows = OrderedDict()
        self._notices = deque(maxlen=2)
        self.close_guard = None
        self._danmaku_status_label = None
        self.setWindowTitle(APP_NAME)
        self.setFont(QFont("Microsoft YaHei UI", 10))
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumSize(610, 420)
        self.resize(round(self.minimumHeight() * 21 / 9), self.minimumHeight())
        self.setStyleSheet(
            "QMainWindow, QWidget { background: #121b29; color: #e8eef7; }"
            "QPushButton { background: #293c56; border: 1px solid #405779; border-radius: 5px; padding: 6px 12px; }"
            "QPushButton:hover { background: #365071; }"
            "QSpinBox { background: #24354c; padding: 3px; }"
            "QScrollBar:vertical { width: 8px; background: transparent; }"
            "QScrollBar::handle:vertical { background: #526781; min-height: 22px; }")
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        header = QHBoxLayout()
        title = _plain_label(APP_NAME)
        title.setFont(QFont("Microsoft YaHei UI", 14, QFont.Weight.Bold))
        header.addWidget(title)
        header.addStretch()
        self.session_state = _plain_label(tr("Chrome 字幕模式 · 等待连接现有浏览器"))
        header.addWidget(self.session_state)
        layout.addLayout(header)
        controls = QHBoxLayout()
        self.pause_button = QPushButton(tr("暂停"))
        self.pause_button.clicked.connect(self._toggle_pause)
        controls.addWidget(self.pause_button)
        for title, signal in (("更改设置", self.settings_requested), ("Chrome 直播间", self.chrome_caption_requested),
                              ("管理音频处理模型", self.audio_models_requested)):
            button = QPushButton(tr(title))
            button.clicked.connect(signal.emit)
            controls.addWidget(button)
        topmost = QCheckBox(tr("置顶"))
        topmost.setChecked(True)
        topmost.toggled.connect(self._set_topmost)
        controls.addWidget(topmost)
        controls.addStretch()
        exit_button = QPushButton(tr("退出"))
        exit_button.clicked.connect(self.close)
        controls.addWidget(exit_button)
        layout.addLayout(controls)
        appearance = QHBoxLayout()
        appearance.addWidget(_plain_label(tr("译文字号")))
        self.font_size = QSpinBox()
        self.font_size.setRange(16, 48)
        self.font_size.setValue(16)
        self.font_size.valueChanged.connect(self._set_font_size)
        appearance.addWidget(self.font_size)
        appearance.addWidget(_plain_label(tr("透明度")))
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(55, 100)
        self.opacity_slider.setFixedWidth(75)
        self.opacity_slider.valueChanged.connect(lambda value: self.setWindowOpacity(value / 100))
        self.opacity_slider.setValue(66)
        appearance.addWidget(self.opacity_slider)
        appearance.addStretch()
        layout.addLayout(appearance)
        self.chrome_caption_notice = _plain_label(tr(
            "Chrome 仅提供当前译文，未提供日语原文；按句末标点分段。"), wrap=True)
        layout.addWidget(self.chrome_caption_notice)
        from .local_features import danmaku_available
        if danmaku_available():
            features = QHBoxLayout()
            for title, signal in (("本地过滤词管理", self.content_filter_requested), ("清理待发队列", self.clear_pending_requested)):
                button = QPushButton(tr(title))
                button.clicked.connect(signal.emit)
                features.addWidget(button)
            features.addStretch()
            layout.addLayout(features)
        self.source_status = _plain_label(tr("等待 Chrome 字幕…"), wrap=True)
        self.source_status.setStyleSheet("color: #b6c8de;")
        layout.addWidget(self.source_status)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.rows_widget = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_widget)
        self.rows_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.rows_widget)
        self.scroll.verticalScrollBar().rangeChanged.connect(
            lambda _minimum, maximum: self.scroll.verticalScrollBar().setValue(maximum))
        layout.addWidget(self.scroll, 1)
        self.notice_label = _plain_label(wrap=True)
        self.notice_label.setStyleSheet("color: #ffd08d;")
        self.notice_label.hide()
        layout.addWidget(self.notice_label)
        self.update_config(config or AppConfig())

    def _toggle_pause(self):
        self._paused = not self._paused
        self.pause_button.setText(tr("继续" if self._paused else "暂停"))
        self.pause_toggled.emit(self._paused)

    def _set_topmost(self, enabled):
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
        self.show()

    def _set_font_size(self, size):
        for row in self._rows.values():
            row["label"].setFont(QFont("Microsoft YaHei UI", size))

    def set_session_state(self, state):
        self.session_state.setText(tr(state))

    def set_source_status(self, source_id, text):
        if source_id == "system":
            self.source_status.setText(text)

    def set_original(self, source_id, text, segment_id, **options):
        # Chrome exposes only its translated viewport, never Japanese source text.
        return

    def set_translation(self, source_id, text, segment_id, *, revision=1, context_generation=0, **options):
        if source_id != "system" or not isinstance(text, str):
            return
        row = self._rows.get(segment_id)
        if row is not None and (context_generation < row["context_generation"] or
            context_generation == row["context_generation"] and revision < row["revision"]):
            return
        if row is None:
            widget = QWidget()
            vertical = QVBoxLayout(widget)
            label = _plain_label(text, wrap=True)
            label.setFont(QFont("Microsoft YaHei UI", self.font_size.value()))
            stage = _plain_label(tr("Chrome 字幕快照 · 未提供终稿状态"))
            stage.setStyleSheet("color: #ffd08d;")
            vertical.addWidget(label)
            vertical.addWidget(stage)
            self.rows_layout.addWidget(widget)
            row = {"widget": widget, "label": label}
            self._rows[segment_id] = row
        row.update(revision=revision, context_generation=context_generation)
        row["label"].setText(text)

    def clear_subtitles(self):
        for row in self._rows.values():
            self.rows_layout.removeWidget(row["widget"])
            row["widget"].deleteLater()
        self._rows.clear()

    def prune_chrome_captions(self, max_rows=32):
        while len(self._rows) > max_rows:
            _, row = self._rows.popitem(last=False)
            self.rows_layout.removeWidget(row["widget"])
            row["widget"].deleteLater()

    def cancel_pending(self):
        return

    def update_config(self, config):
        self.config = config

    def append_notice(self, text):
        self._notices.append(text)
        self.notice_label.setText("\n".join(self._notices))
        self.notice_label.show()

    def closeEvent(self, event):
        if callable(self.close_guard) and not self.close_guard():
            event.ignore()
            return
        if not self._closing_emitted:
            self._closing_emitted = True
            self.closing.emit()
        super().closeEvent(event)

    def set_danmaku_status(self, value: dict) -> None:
        if self._danmaku_status_label is None:
            self._danmaku_status_label = _plain_label("")
            self._danmaku_status_label.setStyleSheet("color: #a9c7e5;")
            self.centralWidget().layout().insertWidget(3, self._danmaku_status_label)
        state = str(value.get("state", "disabled"))
        labels = {"disabled": "已关闭", "idle": "等待终稿", "ready": "等待终稿",
                  "empty": "等待终稿", "waiting": "等待队列调度",
                  "waiting_merge": "不足 2 个字符的片段已保存，等待下一句合并", "checking_login": "正在核实账号登录状态",
                  "sending": "正在发送", "checking": "正在核验", "verifying": "正在核验",
                  "waiting_browser": "等待浏览器连接", "waiting_connection": "等待浏览器连接",
                  "waiting_login": "等待登录", "login_required": "等待登录",
                  "queued_room_mismatch": "待发消息属于其他直播间，请核对设置",
                  "storage_error": "队列保存失败，请检查本机存储",
                  "filter_error": "过滤名单异常，待发消息保留",
                  "awaiting_verification": "接口回执已接收，尚未确认页面收到",
                  "auto_verifying": "发送结果未知，正在自动只读核验",
                  "auto_rechecking": "连续发送失败，正在自动复检账号和页面",
                  "auto_retry": "正在自动恢复当前条，保留队列顺序",
                  "ai_analyzing": "正在请求 Codex 分析失败正文",
                  "ai_repair_ready": "修改后正文已通过本地检查，等待独立测试验证",
                  "ai_retrying": "正在按发送间隔重试测试正文",
                  "waiting_validation": "修改后正文等待独立测试房间验证，尚未生成规则",
                  "validating": "正在独立测试房间验证修改后正文",
                  "validation_failed": "测试正文未通过，保留记录且不生成规则",
                  "waiting_validation_browser": "等待手动打开已登录的验证直播间",
                  "validation_storage_error": "验证记录暂存失败 自动重试保存",
                  "validation_passed": "独立测试房间验证通过，正式房间效果未确认",
                  "validation_idle": "等待需要验证的修改后正文",
                  "validation_configuration_changed": "待核验请求属于原测试配置，请恢复对应房间设置",
                  "failure_transferred": "失败已移入独立处理，后续队列继续发送",
                  "saving_failed_record": "失败记录保存异常，正在重试写入",
                  "sent_page_visible": "直播页面已收到弹幕",
                  "chrome_non_chinese": "含外文或无效字幕，未进入弹幕队列",
                  "chrome_too_short": "短片段待检查",
                  "too_few_han": "短片段待检查",
                  "minimum_han_unfit": "短片段待检查",
                  "chrome_revising": "字幕正在修订，等待重新稳定",
                  "platform_rejected": "平台拒绝，消息保留等待核实",
                  "platform_f": "平台返回 f，按发送间隔自动重试当前条",
                  "waiting_f_retry": "平台返回 f，等待自动重试当前条",
                  "retrying_f": "正在自动重试当前条，保留队列顺序",
                  "result_unknown": "发送结果未知，消息保留等待核实",
                  "cleared": "已清理未提交消息", "queue_cleared": "已清理未提交消息",
                  "cooldown": "发送间隔", "blocked": "待核实，队列保留"}
        label = labels.get(state, state)
        if self._processing_mode == "chrome_caption" and state in {"idle", "ready", "empty"}:
            label = "等待新的中文字幕"
        durable_queued = value.get("durable_queued")
        if self._processing_mode == "chrome_caption":
            if state == "waiting" and type(durable_queued) is int and durable_queued > 0:
                label = "译文已稳定，等待发送"
        if type(durable_queued) is int and durable_queued >= 0:
            message = tr("弹幕推送：{0} · 已写盘待发 {1} · 页面可见 {2}",
                tr(label), durable_queued, value.get("page_visible", 0))
        else:
            message = tr("弹幕推送：{0} · 待发 {1} · 页面可见 {2}",
                tr(label), value.get("queued", 0), value.get("page_visible", 0))
        for key, text in (("holding", " · 单字待合并 {0}"),
                          ("chrome_candidates", " · 尚未确认候选 {0}"),
                          ("accepted", " · 接口回执 {0}")):
            count = value.get(key)
            if type(count) is int and count >= 0:
                message += tr(text, count)
        rejected = value.get("chrome_rejected_count")
        if type(rejected) is int and rejected > 0:
            message += tr(" · 中文校验拦截 {0}", rejected)
            if state != "chrome_non_chinese" and value.get("chrome_last_rejection_reason") == "chrome_non_chinese":
                message += tr(" · 含外文或无效字幕，未进入弹幕队列")
        f_retries = value.get("f_retries", 0)
        if type(f_retries) is int and f_retries > 0:
            message += tr(" · f 自动重试累计 {0} 次", f_retries)
        rule_count, candidate_count = value.get("filter_rule_count"), value.get("filter_candidate_count")
        if type(rule_count) is int and rule_count >= 0 and type(candidate_count) is int and candidate_count >= 0:
            message += tr(" · 过滤规则 {0} · 未处理失败 {1}", rule_count, candidate_count)
        resolved_count = value.get("filter_resolved_count")
        if type(resolved_count) is int and resolved_count > 0:
            message += tr(" · 已处理历史 {0}", resolved_count)
        deferred = value.get("deferred_analysis")
        if type(deferred) is int and deferred >= 0:
            message += tr(" · 累计移入独立处理 {0} 段", deferred)
        if value.get("recovery_action"):
            message += tr(" · 处置 {0}", recovery_action_text(value["recovery_action"]))
        if value.get("filter_error") and state != "filter_error":
            message += tr(" · 过滤名单异常，待发消息保留")
        if value.get("filter_candidate_error"):
            message += tr(" · 失败候选保存失败，请检查本地文件")
        remaining = value.get("cooldown_remaining", value.get("wait_seconds", 0))
        if (value.get("f_retry_active", False) or state in {
                "platform_f", "waiting_f_retry", "retrying_f", "auto_rechecking", "auto_retry", "ai_retrying"}) and type(remaining) in {
                    int, float} and remaining > 0:
            message += tr(" · 还需等待 {0:.1f} 秒", remaining)
        self._danmaku_status_label.setText(message)
        self._danmaku_status_label.setVisible(value.get("enabled", state != "disabled") or bool(value.get("queued")) or state in {
            "ai_analyzing", "ai_repair_ready", "ai_retrying", "waiting_validation", "validating", "validation_failed",
            "waiting_validation_browser", "validation_storage_error", "validation_passed", "validation_configuration_changed",
            "failure_transferred", "saving_failed_record"})

