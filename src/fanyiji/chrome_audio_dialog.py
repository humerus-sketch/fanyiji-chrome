"""GTCRN-only model management for the Chrome audio graph."""
from __future__ import annotations
import threading
from PySide6.QtCore import QObject, QThread, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                              QProgressBar, QPushButton, QVBoxLayout)
from .i18n import tr
from .identity import APP_NAME
from .paths import models_root

def _label(text=""):
    result = QLabel(tr(text))
    result.setTextFormat(Qt.TextFormat.PlainText)
    result.setWordWrap(True)
    return result

class _ProcessingModelWorker(QObject):
    progress = Signal(int)
    result = Signal(object)
    finished = Signal()

    def __init__(self, directory, mode, action, cancel):
        super().__init__()
        self.directory, self.mode, self.action, self.cancel = directory, mode, action, cancel

    def _progress(self, _phase, done, total):
        if isinstance(done, (int, float)) and isinstance(total, (int, float)) and total > 0:
            self.progress.emit(max(0, min(100, int(done * 100 / total))))

    @Slot()
    def run(self):
        try:
            from .audio_processing_models import download_processing_model, verify_processing_model
            if self.action == "download":
                download_processing_model(self.directory, self.mode, cancel=self.cancel, progress=self._progress)
            verified = verify_processing_model(self.directory, self.mode, cancel=self.cancel)
            self.result.emit({"mode": self.mode, "ready": verified.ready, "error": ""})
        except Exception as error:
            self.result.emit({"mode": self.mode, "ready": False, "error": type(error).__name__,
                              "cancelled": self.cancel.is_set()})
        finally:
            self.finished.emit()


class ChromeAudioModelDialog(QDialog):
    def __init__(self, parent=None, *, models_dir=None):
        super().__init__(parent)
        self.models_dir = models_root() if models_dir is None else models_dir
        self._thread = self._worker = None
        self._cancel = threading.Event()
        self._close_requested = False
        self._verified = {}
        self.setWindowTitle(APP_NAME + " · " + tr("管理音频处理模型"))
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)
        self.mode = QComboBox()
        self.mode.addItem("GTCRN", "gtcrn")
        layout.addWidget(self.mode)
        self.model_status = _label("点击校验以检查本地处理模型")
        layout.addWidget(self.model_status)
        row = QHBoxLayout()
        self.download_button = QPushButton(tr("下载处理模型"))
        self.download_button.clicked.connect(lambda: self._start_task("download"))
        row.addWidget(self.download_button)
        self.verify_button = QPushButton(tr("校验本地处理模型"))
        self.verify_button.clicked.connect(lambda: self._start_task("verify"))
        row.addWidget(self.verify_button)
        self.model_link_button = QPushButton(tr("官方模型链接"))
        self.model_link_button.clicked.connect(self._open_model_link)
        row.addWidget(self.model_link_button)
        layout.addLayout(row)
        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)
        layout.addWidget(_label("GTCRN 主要减少噪声和部分背景声，不能锁定主播、分离游戏角色讲话或保证去除唱歌。Chrome 识别处理后声音的链路仍属实验，以实际状态为准。处理模型单独下载，不包含在程序包中。"))
        self.status = _label()
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.buttons.button(QDialogButtonBox.StandardButton.Close).setText(tr("关闭"))
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    @property
    def is_busy(self):
        return self._thread is not None

    def _mode_changed(self, *_):
        ready = self._verified.get("gtcrn")
        self.model_status.setText(tr("本地处理模型校验通过" if ready is True else
                                    "本地处理模型缺失或校验未通过" if ready is False else "点击校验以检查本地处理模型"))
        busy = self.is_busy
        self.download_button.setEnabled(not busy)
        self.verify_button.setEnabled(not busy)

    def _open_model_link(self):
        if self.mode.currentData() == "off":
            return
        from .audio_processing_models import processing_model_entry
        entry = processing_model_entry(self.mode.currentData())
        if entry is not None:
            url = entry.info_url
            if isinstance(url, str) and url.startswith("https://"):
                QDesktopServices.openUrl(QUrl(url))


    def _start_task(self, action):
        if self.is_busy or self.mode.currentData() == "off" or self._close_requested:
            return
        self._cancel.clear()
        thread = QThread(self)
        worker = _ProcessingModelWorker(self.models_dir, self.mode.currentData(), action, self._cancel)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.progress.setValue)
        worker.result.connect(self._on_result)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(self._task_finished)
        thread.finished.connect(thread.deleteLater)
        self._thread, self._worker = thread, worker
        self.progress.setValue(0)
        self.progress.show()
        self.status.setText(tr("正在下载处理模型…" if action == "download" else "正在校验本地处理模型…"))
        self._mode_changed()
        thread.start()


    def _on_result(self, result):
        self._verified[result["mode"]] = result.get("ready") is True
        self.status.setText(tr("操作已取消" if result.get("cancelled") else "本地处理模型校验通过" if result.get("ready")
                               else "处理模型操作失败，请检查本地文件或网络")
                           + (" · " + result["error"] if result.get("error") and not result.get("cancelled") else ""))


    def _task_finished(self):
        self._thread = self._worker = None
        self.progress.hide()
        self._mode_changed()
        if self._close_requested:
            super().reject()


    def reject(self):
        if self.is_busy:
            self._close_requested = True
            self._cancel.set()
            self.status.setText(tr("正在取消处理模型操作…"))
        else:
            super().reject()


    def closeEvent(self, event):
        if self.is_busy:
            event.ignore()
            self.reject()
        else:
            super().closeEvent(event)


