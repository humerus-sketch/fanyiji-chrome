"""Independent Chrome caption application; no recognition or translation pipeline."""
from __future__ import annotations
import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from logging.handlers import RotatingFileHandler
from pathlib import Path
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
from .config import AppConfig, ConfigError, load_config, save_config
from .i18n import set_language, tr
from .identity import APP_NAME, INSTANCE_NAME
from .paths import data_root, models_root

class EventBridge(QObject):
    event = Signal(object)

class ChromeAppController(QObject):
    def __init__(self, window, config: AppConfig, config_path: Path):
        super().__init__(window)
        self.window, self.config, self.config_path = window, config, config_path
        self.epoch, self.paused, self.closed = 0, False, False
        self.chrome_caption_service = self.chrome_caption_dialog = None
        self.chrome_browser_service = self.chrome_danmaku = None
        self._chrome_browser_mode = "dedicated"
        self._chrome_hide_caption = False
        self._chrome_audio_processing = "off"
        self._chrome_audio_model_dialog = None
        self._chrome_audio_notice_state = None
        self._chrome_audio_effective_state = "off"
        self._chrome_caption_status_text = ""
        self._chrome_caption_health = {}
        self._chrome_health_log_at = 0.0
        self._chrome_push_preferences = {"enabled": False, "room": "", "interval_seconds": 3.5}
        self._chrome_push_protocol = self._chrome_protocol = None
        self._chrome_initial_capture_room = self._chrome_active_url = ""
        self._chrome_push_restart_after_generation = -1
        self._chrome_open_settings = False
        self._danmaku_store_initialized = False
        self._danmaku_startup_checked = False
        self.bridge = EventBridge(self)
        self.bridge.event.connect(self._on_event)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chrome-session-control")
        window.close_guard = self._may_close_window
        window.pause_toggled.connect(self.set_paused)
        window.settings_requested.connect(self.change_settings)
        window.chrome_caption_requested.connect(self.open_chrome_caption)
        window.audio_models_requested.connect(self._open_chrome_audio_models)
        window.content_filter_requested.connect(self.open_content_filter)
        window.clear_pending_requested.connect(self._clear_chrome_pending)
        window.closing.connect(self.close)
        self._load_preferences()

    def _load_preferences(self):
        try:
            value = json.loads((data_root() / "chrome-caption-settings.json").read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return
            self._chrome_browser_mode = value.get("browser_mode") if value.get("browser_mode") in {"attach", "dedicated"} else "dedicated"
            self._chrome_hide_caption = value.get("hide_caption") is True
            self._chrome_audio_processing = value.get("audio_processing") if value.get("audio_processing") in {"off", "gtcrn"} else "off"
            preferences = value.get("push_preferences")
            if isinstance(preferences, dict):
                self._chrome_push_preferences = preferences
        except (OSError, ValueError, TypeError):
            pass
        # A saved room is a preference, never authorization to open it on launch.

    def _open_chrome_audio_models(self):
        if self.closed:
            return
        from .chrome_audio_dialog import ChromeAudioModelDialog
        if self._chrome_audio_model_dialog is None:
            self._chrome_audio_model_dialog = ChromeAudioModelDialog(self.window, models_dir=models_root())
        self._chrome_audio_model_dialog.exec()

    def open_content_filter(self):
        if self.closed:
            return
        service = self._ensure_chrome_danmaku()
        if service is None:
            self.window.append_notice(tr("弹幕队列暂不可用，字幕翻译继续运行。"))
            return
        from .danmaku_filter_dialog import DanmakuFilterDialog
        dialog = DanmakuFilterDialog(service.content_filter, service.room, self.window)
        try:
            dialog.exec()
            service.refresh_content_filter()
        finally:
            dialog.deleteLater()

    def _ensure_chrome_caption_service(self):
        if self.chrome_caption_service is None:
            from .chrome_caption_service import ChromeCaptionService
            self.chrome_caption_service = ChromeCaptionService(self._chrome_caption_event)
        return self.chrome_caption_service


    def _chrome_caption_event(self, event: dict) -> None:
        self.bridge.event.emit({**event, "caption_kind": event.get("kind"), "kind": "chrome_caption"})


    def _ensure_owned_chrome(self):
        if self.chrome_browser_service is None:
            from .chrome_browser_service import ChromeBrowserService
            self.chrome_browser_service = ChromeBrowserService(self)
            self.chrome_browser_service.event.connect(self._owned_chrome_event)
        return self.chrome_browser_service


    def _configure_chrome_audio(self, mode, *, persist=True):
        if self.closed or self.config.processing_mode != "chrome_caption" or mode not in ("off", "gtcrn"):
            return False
        if persist:
            try:
                self._save_chrome_preferences({"audio_processing": mode})
            except (OSError, ValueError, TypeError):
                self.window.append_notice(tr("Chrome 音频设置保存失败，请检查目录权限。"))
                return False
        changed = mode != self._chrome_audio_processing
        self._chrome_audio_processing = mode
        if changed:
            self._chrome_audio_notice_state = None
        if (changed and self._chrome_browser_mode == "dedicated" and self._chrome_active_url
                and self.chrome_browser_service is not None and self.chrome_browser_service.is_browser_ready):
            # No initial-history grant: rebuilding an audio graph must not resend older captions.
            self._chrome_initial_capture_room = ""
            self._chrome_push_protocol = self._chrome_protocol = None
            if self.chrome_danmaku:
                self.chrome_danmaku.end_chrome_source()
            self.start()
        elif (not changed and mode == "gtcrn" and self._chrome_browser_mode == "dedicated"
                and self.chrome_browser_service is not None and self.chrome_browser_service.is_browser_ready):
            # An explicit same-model retry uses the current page. It grants no history replay.
            self.executor.submit(self.chrome_browser_service.configure_audio_processing, mode)
        return True


    def _refresh_chrome_runtime_status(self):
        if self.closed or self.paused or self.config.processing_mode != "chrome_caption":
            return
        text = self._chrome_caption_status_text or tr("等待 Chrome 字幕…")
        if self._chrome_browser_mode == "dedicated" and self._chrome_audio_processing == "gtcrn":
            states = {"processing": "GTCRN：运行", "recovering": "GTCRN：恢复中，暂用原音",
                      "loading": "GTCRN：加载模型", "waiting": "GTCRN：等待播放器",
                      "buffering": "GTCRN：缓冲中", "bypassed": "原音：GTCRN 已停止"}
            text += " · " + tr(states.get(self._chrome_audio_effective_state, "GTCRN：尚未运行"))
        health = self._chrome_caption_health
        age = health.get("caption_update_age_seconds")
        if (health.get("reader_alive") is True and type(age) in (float, int)
                and 10 <= age <= 86400):
            text += " · " + tr("距字幕文字更新") + f" {int(age)} " + tr("秒")
        self.window.set_source_status("system", text)


    def _ensure_chrome_danmaku(self):
        browser = self._ensure_owned_chrome()
        if self.chrome_danmaku is None and browser.danmaku_available:
            from .danmaku_service import DanmakuService
            self.chrome_danmaku = DanmakuService(browser, self,
                store_path=data_root() / "danmaku-queue.sqlite3", filter_path=data_root() / "danmaku-content-filter.json",
                reset_on_start=not self._danmaku_store_initialized)
            self._danmaku_store_initialized = True
            self._danmaku_startup_checked = True
            if hasattr(self.window, "set_danmaku_status"):
                self.chrome_danmaku.status.connect(self.window.set_danmaku_status)
            self.chrome_danmaku.begin_session(self.epoch)
        return self.chrome_danmaku


    def _owned_chrome_event(self, event):
        if self.closed or self.config.processing_mode != "chrome_caption" or self._chrome_browser_mode != "dedicated":
            return
        if self.chrome_browser_service and event.get("browser_generation") != self.chrome_browser_service.generation:
            return
        if event.get("kind") == "audio_processing":
            state = event.get("state")
            self._chrome_audio_effective_state = state
            self._refresh_chrome_runtime_status()
            if state == self._chrome_audio_notice_state:
                return
            self._chrome_audio_notice_state = state
            if state == "processing":
                message = tr("GTCRN 前置音频增强已运行（实验），不能区分主播和游戏人物讲话。")
            elif state == "bypassed":
                message = tr("Chrome 前置音频处理未生效，已恢复原音。请检查处理模型和播放器状态。")
            elif state == "recovering":
                message = tr("GTCRN 音频处理暂时中断，正在自动恢复；期间播放原音。")
            else:
                return
            self.window.append_notice(message)
            if self.chrome_caption_dialog is not None:
                self.chrome_caption_dialog.set_status(message)
            return
        messages = {"caption_hidden": "原生字幕已透明隐藏，翻译姬继续读取。",
                    "caption_visible": "原生字幕已恢复显示。",
                    "caption_recovering": "正在刷新专用直播页恢复字幕，请保持播放。",
                    "caption_visibility_failed": "当前 Chrome 气泡无法安全隐藏，原生字幕保留显示。",
                    "closed": "专用 Chrome 已关闭，请重新打开直播间。"}
        if event.get("kind") in messages:
            self.window.append_notice(tr(messages[event["kind"]]))
        if event.get("kind") in {"closed", "caption_recovering", "audio_source_changed"}:
            self._chrome_push_protocol = None
            self._chrome_initial_capture_room = ""
            if self.chrome_danmaku:
                self.chrome_danmaku.end_chrome_source()


    def _open_dedicated_chrome_room(self, url):
        if self.closed or self.config.processing_mode != "chrome_caption":
            return
        from .chrome_caption_runtime import validate_source_url
        try:
            url = validate_source_url(url)
        except (TypeError, ValueError):
            return
        was_dedicated = self._chrome_browser_mode == "dedicated"
        self._chrome_browser_mode = "dedicated"
        browser = self._ensure_owned_chrome()
        first_open = not (was_dedicated and browser.is_browser_ready and self._chrome_active_url == url)
        service = self._ensure_chrome_danmaku()
        if service:
            self._configure_chrome_push(self._chrome_push_preferences, persist=False)
        self._chrome_initial_capture_room = (
            url.rsplit("/", 1)[-1] if first_open and service and service.enabled else "")
        self._connect_chrome_room(url, dedicated=True)


    def _save_chrome_preferences(self, changes):
        path = data_root() / "chrome-caption-settings.json"
        previous = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(previous, dict):
            raise ValueError("invalid_chrome_preferences")
        context = ({"browser_mode": self._chrome_browser_mode, "room_url": self._chrome_active_url}
                   if self._chrome_active_url else {})
        value = {**previous, **context, **changes}
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)


    def _set_chrome_caption_hidden(self, hidden, *, persist=True):
        if self.closed or self.config.processing_mode != "chrome_caption":
            return
        browser, epoch = self.chrome_browser_service, self.epoch
        desired = hidden is True
        logging.info("CHROME_CAPTION visibility_requested hidden=%s epoch=%s generation=%s",
                     desired, epoch, browser.generation if browser else -1)
        if persist:
            try:
                self._save_chrome_preferences({"hide_caption": hidden is True})
            except (OSError, TypeError, ValueError):
                self.window.append_notice(tr("Chrome 字幕设置保存失败，请检查目录权限。"))
                return
        self._chrome_hide_caption = hidden is True
        if self._chrome_browser_mode == "dedicated" and self.chrome_browser_service:
            def visibility():
                if self.closed or epoch != self.epoch or browser is not self.chrome_browser_service:
                    return
                try:
                    browser.set_caption_hidden(desired)
                except Exception as failure:
                    self._chrome_caption_action_failed("visibility", "visibility", failure, epoch, browser, hidden=desired)
            try:
                self.executor.submit(visibility)
            except Exception as failure:
                self._chrome_caption_action_failed("visibility", "submit", failure, epoch, browser, hidden=desired)


    def _recover_chrome_caption(self):
        if (self.closed or self.config.processing_mode != "chrome_caption"
                or self._chrome_browser_mode != "dedicated" or not self.chrome_browser_service):
            return
        browser, epoch = self.chrome_browser_service, self.epoch
        logging.info("CHROME_CAPTION recovery_requested epoch=%s generation=%s", epoch, browser.generation)
        # Recovery is an explicit request to show the bubble. Cancel the saved
        # hide preference too, or refresh/resume would immediately hide it again.
        self._chrome_hide_caption = False
        try:
            self._save_chrome_preferences({"hide_caption": False})
        except (OSError, TypeError, ValueError) as failure:
            self._chrome_caption_action_failed("recovery", "preferences", failure, epoch, browser)
        self._chrome_protocol = self._chrome_push_protocol = None
        self._chrome_initial_capture_room = ""
        if self.chrome_danmaku:
            self.chrome_danmaku.end_chrome_source()
        def recover():
            if self.closed or epoch != self.epoch or browser is not self.chrome_browser_service:
                return
            stage = "visibility"
            try:
                browser.set_caption_hidden(False)
                stage = "refresh"
                browser.recover_caption()
                if not self.closed and epoch == self.epoch and browser is self.chrome_browser_service:
                    stage = "reader_restart"
                    self._ensure_chrome_caption_service().start(self._chrome_active_url, session=epoch,
                        owned_browser_pid=browser.browser_pid)
                    logging.info("CHROME_CAPTION recovery_completed epoch=%s generation=%s", epoch, browser.generation)
            except Exception as failure:
                self._chrome_caption_action_failed("recovery", stage, failure, epoch, browser)
        try:
            self.executor.submit(recover)
        except Exception as failure:
            self._chrome_caption_action_failed("recovery", "submit", failure, epoch, browser)


    def _chrome_caption_action_failed(self, action, stage, failure, epoch, browser, *, hidden=None):
        # Exception messages may contain page/script/account data. Publish only
        # fixed error codes and the exception class, never str(failure)/traceback.
        known = {"browser_unavailable", "browser_closed", "window_closed", "wrong_room", "navigation_changed",
                 "script_unavailable", "script_timeout", "invalid_response", "ownership_required", "caption_ambiguous",
                 "identity_changed", "unsupported_layered_state", "visibility_failed", "restore_failed", "closed",
                 "unsupported_platform", "chrome_missing", "reader_failed", "uia_unavailable"}
        code = getattr(failure, "code", None)
        code = code if isinstance(code, str) and code in known else "action_failed"
        error_type = type(failure).__name__
        if len(error_type) > 80 or not error_type.isascii() or not error_type.isidentifier():
            error_type = "Exception"
        generation = browser.generation if browser else -1
        logging.warning("CHROME_CAPTION action_failed action=%s stage=%s code=%s error_type=%s epoch=%s generation=%s",
                        action, stage, code, error_type, epoch, generation)
        if not self.closed and epoch == self.epoch and browser is self.chrome_browser_service:
            self.bridge.event.emit({"kind": "chrome_caption_action_failed", "session": epoch, "action": action,
                "stage": stage, "code": code, "error_type": error_type, "browser_generation": generation,
                "hidden": hidden, "browser": browser})


    def _configure_chrome_push(self, preferences, *, persist=True, admit_current=False):
        from .room_url import room_url
        from .local_features import danmaku_available
        if not isinstance(preferences, dict) or not danmaku_available():
            return
        try:
            room = room_url(preferences["room"]).rsplit("/", 1)[-1] if preferences.get("room") else ""
            interval = preferences.get("interval_seconds", 3.5)
            if type(interval) not in (int, float) or not 3.5 <= interval <= 60:
                return
        except (KeyError, ValueError, TypeError):
            return
        normalized = {"enabled": preferences.get("enabled") is True, "room": room, "interval_seconds": interval}
        if persist:
            try:
                self._save_chrome_preferences({"push_preferences": normalized})
            except (OSError, TypeError, ValueError):
                self.window.append_notice(tr("Chrome 直播间设置保存失败，请检查目录权限。"))
                return
        previous = self._chrome_push_preferences
        explicit_enable = ((persist or admit_current) and normalized["enabled"]
            and (not previous.get("enabled") or previous.get("room") != room))
        self._chrome_push_preferences = normalized
        if not normalized["enabled"]:
            self._chrome_initial_capture_room = ""
        elif explicit_enable:
            self._chrome_initial_capture_room = room
            self._chrome_push_protocol = None
        if self._chrome_browser_mode == "dedicated":
            service = self._ensure_chrome_danmaku()
            if service:
                service.configure(enabled=self._chrome_push_preferences["enabled"], room=room, source="system",
                                  interval_seconds=interval, repair_enabled=False)
            browser = self.chrome_browser_service
            if (explicit_enable and browser and browser.is_browser_ready and browser.room == room
                    and self._chrome_active_url):
                reader, epoch, url = self._ensure_chrome_caption_service(), self.epoch, self._chrome_active_url
                # In-flight snapshots from the old helper cannot consume the
                # user's one-shot admission before this asynchronous restart.
                self._chrome_push_restart_after_generation = reader.generation
                def restart_after_enable():
                    if (not self.closed and not self.paused and epoch == self.epoch
                            and browser is self.chrome_browser_service and browser.is_browser_ready
                            and self._chrome_initial_capture_room == room):
                        reader.start(url, session=epoch, owned_browser_pid=browser.browser_pid)
                self.executor.submit(restart_after_enable)


    def _clear_chrome_pending(self):
        if self.chrome_danmaku and self._chrome_browser_mode == "dedicated":
            self.chrome_danmaku.end_chrome_source()
            self.chrome_danmaku.clear_pending()
            self._chrome_push_protocol = None
            self._chrome_initial_capture_room = ""
            epoch = self.epoch
            def restart_reader():
                if not self.closed and epoch == self.epoch and self.chrome_browser_service.is_browser_ready:
                    self._ensure_chrome_caption_service().start(self._chrome_active_url, session=epoch,
                        owned_browser_pid=self.chrome_browser_service.browser_pid)
            self.executor.submit(restart_reader)


    def open_chrome_caption(self) -> None:
        if self.closed or self.config.processing_mode != "chrome_caption":
            return
        from .chrome_caption_dialog import ChromeCaptionDialog
        from .chrome_caption_runtime import validate_source_url
        path = data_root() / "chrome-caption-settings.json"
        room = ""
        saved = {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            saved = value if isinstance(value, dict) else {}
            if isinstance(value, dict) and value.get("room_url"):
                room = validate_source_url(value["room_url"])
        except (OSError, ValueError, TypeError, UnicodeError):
            pass
        options = {}
        self._chrome_audio_processing = (saved.get("audio_processing", "off")
                                         if saved.get("audio_processing", "off") in ("off", "gtcrn") else "off")
        if hasattr(ChromeCaptionDialog, "audio_processing_requested"):
            options["audio_processing"] = self._chrome_audio_processing
        if saved.get("browser_mode") in {"attach", "dedicated"}:
            options.update(mode=saved["browser_mode"], hide_caption=saved.get("hide_caption") is True,
                           push_preferences=saved.get("push_preferences", {}))
            self._chrome_hide_caption = options["hide_caption"]
            if isinstance(options["push_preferences"], dict):
                self._chrome_push_preferences = options["push_preferences"]
        dialog = ChromeCaptionDialog(self.window, room_url=room, **options)
        self.chrome_caption_dialog = dialog
        dialog.connect_room_requested.connect(self._connect_chrome_room)
        dialog.copy_room_requested.connect(self._copy_chrome_room)
        dialog.caption_settings_requested.connect(self._open_chrome_caption_settings)
        if hasattr(dialog, "dedicated_room_requested"):
            dialog.dedicated_room_requested.connect(self._open_dedicated_chrome_room)
            dialog.hide_caption_requested.connect(self._set_chrome_caption_hidden)
            dialog.recover_caption_requested.connect(self._recover_chrome_caption)
            dialog.push_preferences_requested.connect(self._configure_chrome_push)
            dialog.clear_pending_requested.connect(self._clear_chrome_pending)
        if hasattr(dialog, "audio_processing_requested"):
            dialog.audio_processing_requested.connect(self._configure_chrome_audio)
            dialog.audio_models_requested.connect(self._open_chrome_audio_models)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_room_url is not None:
                value = dialog.result_room_url
                if value:
                    value = validate_source_url(value)
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_name(path.name + ".tmp")
                with temp.open("w", encoding="utf-8", newline="\n") as stream:
                    settings = {**saved, "room_url": value}
                    if hasattr(dialog, "result_preferences") and isinstance(dialog.result_preferences, dict):
                        settings.update(dialog.result_preferences)
                    json.dump(settings, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, path)
                if isinstance(getattr(dialog, "result_preferences", None), dict):
                    preferences = dialog.result_preferences
                    self._configure_chrome_push(preferences.get("push_preferences", {}), persist=False, admit_current=True)
                    self._set_chrome_caption_hidden(preferences.get("hide_caption") is True, persist=False)
                    self._configure_chrome_audio(preferences.get("audio_processing", "off"), persist=False)
        except (OSError, ValueError, TypeError):
            QMessageBox.warning(self.window, tr("无法保存"), tr("Chrome 直播间设置保存失败，请检查目录权限。"))
        finally:
            self.chrome_caption_dialog = None
            dialog.deleteLater()


    def _connect_chrome_room(self, url: str, *, dedicated=False) -> None:
        if self.closed or self.config.processing_mode != "chrome_caption":
            return
        from .chrome_caption_runtime import validate_source_url
        try:
            url = validate_source_url(url)
        except (ValueError, TypeError):
            self.window.append_notice(tr("直播间格式无效，请填写房间号或哔哩哔哩 HTTPS 直播链接。"))
            return
        self._chrome_active_url = url
        if not dedicated:
            self._chrome_browser_mode = "attach"
            self._chrome_initial_capture_room = ""
            if self.chrome_danmaku:
                self.chrome_danmaku.configure(enabled=False, room="", source="system", interval_seconds=3.5, repair_enabled=False)
            if self.chrome_browser_service:
                self.executor.submit(self.chrome_browser_service.stop_browser)
        self._chrome_open_settings = False
        self.paused = False
        self.window.set_session_state("running")
        self.window.cancel_pending()
        if hasattr(self.window, "clear_subtitles"):
            self.window.clear_subtitles()
        self.start()


    def _copy_chrome_room(self, url: str) -> None:
        if self.closed or self.config.processing_mode != "chrome_caption":
            return
        from .chrome_caption_runtime import validate_source_url
        try:
            url = validate_source_url(url)
        except (ValueError, TypeError):
            return
        QApplication.clipboard().setText(url)
        if self.chrome_caption_dialog is not None:
            self.chrome_caption_dialog.set_status(tr("直播间地址已复制，请在现有 Chrome 打开后点击连接。"))


    def _open_chrome_caption_settings(self) -> None:
        if self.closed or self.config.processing_mode != "chrome_caption":
            return
        QMessageBox.information(self.chrome_caption_dialog or self.window, tr("Chrome 字幕设置说明"), tr(
            "请在已打开的 Chrome 地址栏输入 chrome://settings/accessibility，开启实时字幕、添加日语，再开启实时翻译并选择中文。"
            "设置完成后切回直播间，保持字幕气泡可见，再点击连接。翻译姬不会启动或关闭 Chrome。"))


    def _on_chrome_caption_event(self, event: dict) -> None:
        service = self.chrome_caption_service
        generation = event.get("generation")
        if (self.closed or self.paused or self.config.processing_mode != "chrome_caption"
                or event.get("session") != self.epoch or service is None
                or type(generation) is not int or generation != service.generation):
            return
        reader_generation = event.get("reader_generation")
        if (reader_generation is not None and reader_generation != getattr(service, "reader_generation", reader_generation)):
            return
        if event.get("caption_kind") == "health":
            self._chrome_caption_health = {key: event.get(key) for key in (
                "reader_alive", "heartbeat_age_seconds", "snapshot_age_seconds", "caption_update_age_seconds",
                "recovery_attempts", "reader_recovery_state", "caption_state")}
            self._refresh_chrome_runtime_status()
            now = time.monotonic()
            if now - self._chrome_health_log_at >= 10:
                self._chrome_health_log_at = now
                logging.info("CHROME_CAPTION_HEALTH %s", json.dumps(self._chrome_caption_health, ensure_ascii=True))
            return
        if event.get("caption_kind") == "status":
            messages = {
                "starting": "正在连接现有 Chrome 字幕…",
                "waiting_browser": "Chrome 字幕模式 · 等待连接现有浏览器",
                "waiting_caption": "等待 Chrome 原生字幕气泡，请开启实时字幕及中文翻译。",
                "caption_found": "正在显示 Chrome 字幕快照 · 未提供终稿状态",
                "caption_found_unverified": "Chrome 当前字幕 · 音源由 Chrome 选择，尚未确认属于指定直播间",
                "source_unverified": "请在现有 Chrome 的当前标签页打开指定直播间后连接。",
                "source_ambiguous": "发现多个 Chrome 字幕气泡或目标窗口，请保留一个可识别的字幕来源。",
                "browser_missing": "未找到已打开的 Chrome，请先打开 Chrome 后连接。",
                "chrome_missing": "未找到 Google Chrome，请先安装官方 Chrome。",
                "uia_unavailable": "Chrome 字幕读取暂时失败，正在重试；无需重新登录。",
                "unsupported_platform": "Chrome 字幕桥目前仅支持 Windows。",
                "reader_failed": "Chrome 字幕连接已中断，请重新打开直播间。",
                "reader_recovering": "字幕读取进程失联，正在自动恢复；现有字幕保留。",
                "browser_closed": "连接的 Chrome 窗口已关闭。",
                "manual_caption_settings": "请在现有 Chrome 打开 chrome://settings/accessibility 完成字幕设置。",
                "stopped": "Chrome 字幕读取已停止。",
            }
            text = messages.get(event.get("code"), "Chrome 字幕连接已中断，请重新打开直播间。")
            self._chrome_caption_status_text = tr(text)
            self.window.set_session_state(tr(text))
            self._refresh_chrome_runtime_status()
            if self.chrome_caption_dialog is not None:
                self.chrome_caption_dialog.set_status(text)
            if event.get("code") in {"source_unverified", "source_ambiguous", "browser_missing", "browser_closed", "reader_failed", "uia_unavailable"}:
                self._chrome_protocol = None
                self._chrome_push_protocol = None
                self._chrome_initial_capture_room = ""
                if self.chrome_danmaku:
                    self.chrome_danmaku.suspend_chrome_candidates()
                if hasattr(self.window, "clear_subtitles"):
                    self.window.clear_subtitles()
            return
        if event.get("caption_kind") != "snapshot":
            return
        from .chrome_caption_protocol import CaptionDeltaProtocol, CaptionSnapshot
        if self._chrome_protocol is None or self._chrome_protocol.generation != generation:
            # Chrome can retain Latin names in an otherwise Chinese caption.
            # These display-only snapshots never reach the posting boundary.
            self._chrome_protocol = CaptionDeltaProtocol(generation, max_caption_characters=80, allow_latin_names=True)
        try:
            snapshot = CaptionSnapshot(**{key: event[key] for key in (
                "text", "window_id", "source_epoch", "observed_at", "bridge_generation", "room", "connected")},
                source_lang="unknown", target_lang="zh")
            deltas = self._chrome_protocol.observe(snapshot)
        except (KeyError, TypeError, ValueError):
            return
        browser = self.chrome_browser_service
        from .chrome_caption_runtime import source_room
        if (self._chrome_browser_mode == "dedicated" and browser and browser.source_ready
                and self._chrome_active_url and browser.room == source_room(self._chrome_active_url)
                and self.chrome_danmaku and self.chrome_danmaku.enabled
                and self.chrome_danmaku.room == browser.room
                and generation > self._chrome_push_restart_after_generation):
            self._chrome_push_restart_after_generation = -1
            self.chrome_danmaku.begin_chrome_source(session=self.epoch, bridge_generation=generation,
                                                   source_epoch=snapshot.source_epoch)
            if self._chrome_push_protocol is None or self._chrome_push_protocol.generation != generation:
                # A user opening a new source or enabling sync may admit the
                # current complete sentences once. Recovery never replays history.
                include_initial = self._chrome_initial_capture_room == browser.room
                self._chrome_initial_capture_room = ""
                self._chrome_push_protocol = CaptionDeltaProtocol(generation,
                    max_caption_characters=4000, max_candidates=4000, allow_latin_names=True,
                    rolling_publication=True, include_initial_completed=include_initial)
            for candidate in self._chrome_push_protocol.observe(snapshot):
                if candidate["kind"] == "rejected":
                    # A corrupt/replaced display snapshot invalidates mutable
                    # candidates too; it is not evidence of a failed POST.
                    self.chrome_danmaku.suspend_chrome_candidates()
                elif candidate["kind"] in {"append", "revise", "reset", "retract", "stable_candidate"}:
                    self.chrome_danmaku.handle_chrome_candidate({**candidate, "session": self.epoch})
        elif self.chrome_danmaku and self._chrome_push_protocol is not None:
            self.chrome_danmaku.suspend_chrome_candidates()
            self._chrome_push_protocol = None
            self._chrome_initial_capture_room = ""
        for delta in deltas:
            kind = delta["kind"]
            if kind in ("reset", "rejected"):
                if hasattr(self.window, "clear_subtitles"):
                    self.window.clear_subtitles()
                if kind == "rejected" and delta.get("reason") == "non_chinese_output":
                    self.window.set_session_state(tr("字幕尚非中文，请在 Chrome 开启实时翻译并选择中文。"))
            elif kind in ("baseline", "append", "revise", "stable_candidate"):
                # UIA provides a viewport snapshot, not an audio start timestamp
                # or a final signal. Do not route it to danmaku/training/latency.
                self.window.set_original("system", "", delta["segment_id"], revision=delta["revision"],
                    provisional=True, context_generation=delta["source_epoch"])
                self.window.set_translation("system", delta["text"], delta["segment_id"],
                    revision=delta["revision"], provisional=True, translation_stage="draft",
                    translation_provider="chrome_caption", context_generation=delta["source_epoch"])
                if hasattr(self.window, "prune_chrome_captions"):
                    self.window.prune_chrome_captions()


    def start(self):
        if self.closed:
            return
        self.epoch += 1
        self._chrome_caption_health = {}
        self._chrome_caption_status_text = ""
        self._chrome_protocol = self._chrome_push_protocol = None
        if self.paused:
            self._chrome_initial_capture_room = ""
        if self.chrome_danmaku is not None:
            self.chrome_danmaku.begin_session(self.epoch)
            preferences = self._chrome_push_preferences
            self.chrome_danmaku.configure(enabled=(preferences.get("enabled") is True and not self.paused
                and self._chrome_browser_mode == "dedicated"), room=preferences.get("room", ""), source="system",
                interval_seconds=preferences.get("interval_seconds", 3.5), repair_enabled=False)
        self.window.set_session_state(tr("已暂停") if self.paused else tr("Chrome 字幕模式 · 等待连接现有浏览器"))
        # Initial startup only presents controls. The explicit open/connect action sets this URL.
        if self._chrome_active_url or self.chrome_caption_service is not None:
            self.executor.submit(self._transition, self.epoch, self.paused)

    def _transition(self, epoch, paused):
        if self.closed or epoch != self.epoch:
            return
        service = self._ensure_chrome_caption_service()
        if paused:
            service.stop()
            if self.chrome_browser_service:
                self.chrome_browser_service.configure_audio_processing("off")
                self.chrome_browser_service.set_caption_hidden(False)
            return
        try:
            if self._chrome_browser_mode == "dedicated" and self._chrome_active_url:
                browser = self._ensure_owned_chrome()
                browser.configure_audio_processing(self._chrome_audio_processing, reload=True)
                pid = browser.open_sync(self._chrome_active_url)
                if self.closed or epoch != self.epoch:
                    return
                service.start(self._chrome_active_url, session=epoch, owned_browser_pid=pid)
                if self._chrome_hide_caption:
                    browser.set_caption_hidden(True)
            elif self._chrome_active_url:
                service.start(self._chrome_active_url, session=epoch)
        except Exception as failure:
            logging.warning("CHROME_CAPTION start_failed type=%s", type(failure).__name__)
            self._chrome_caption_event({"session": epoch, "generation": service.generation,
                                       "kind": "status", "code": getattr(failure, "code", "reader_failed")})

    def set_paused(self, paused):
        if self.closed:
            return
        self.paused = paused is True
        self.start()

    def change_settings(self):
        from .ui import SettingsDialog
        dialog = SettingsDialog(self.config, parent=self.window)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_config is not None:
            self.apply_config(dialog.result_config)
        dialog.deleteLater()

    def apply_config(self, config):
        if self.closed:
            return False
        try:
            save_config(self.config_path, config)
        except (OSError, ConfigError):
            QMessageBox.warning(self.window, tr("无法保存"), tr("设置未改变，请检查输入内容和设置目录权限。"))
            return False
        language_changed = config.ui_language != self.config.ui_language
        self.config = config
        self.window.update_config(config)
        if language_changed:
            self.window.append_notice(tr("界面语言将在下次启动时生效。"))
        return True

    def _on_event(self, event):
        if self.closed or event.get("session") != self.epoch:
            return
        if event.get("kind") == "chrome_caption":
            self._on_chrome_caption_event(event)
        elif (event.get("kind") == "chrome_caption_action_failed"
              and event.get("browser") is self.chrome_browser_service
              and self.chrome_browser_service
              and event.get("browser_generation") == self.chrome_browser_service.generation):
            if event.get("action") == "recovery" and event.get("stage") == "preferences":
                message = tr("Chrome 字幕设置保存失败，仍会恢复原生字幕，请查看日志。")
            else:
                operation = ("恢复原生字幕失败" if event.get("action") == "recovery" else
                             "隐藏原生字幕失败" if event.get("hidden") is True else "显示原生字幕失败")
                message = tr(operation) + tr("，请查看日志。")
            self.window.append_notice(message)
            if self.chrome_caption_dialog is not None:
                self.chrome_caption_dialog.set_status(message)

    def _may_close_window(self):
        dialog = self._chrome_audio_model_dialog
        if dialog is not None and dialog.is_busy:
            dialog.reject()
            self.window.append_notice(tr("正在取消处理模型操作…"))
            return False
        return True

    def close(self):
        if self.closed:
            return
        self.closed, self.epoch = True, self.epoch + 1
        if self.chrome_danmaku is not None:
            self.chrome_danmaku.close()
        if self.chrome_caption_service is not None:
            self.executor.submit(self.chrome_caption_service.close)
        if self.chrome_browser_service is not None:
            self.executor.submit(self.chrome_browser_service.close)
        self.executor.shutdown(wait=False, cancel_futures=False)


def configure_logging() -> None:
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(root / "diagnostic.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s")


def restore_main_window(window) -> None:
    # A launcher's SW_HIDE can hide the native window while Qt considers it
    # visible. Force a visibility transition instead of repeating show().
    window.hide()
    window.showNormal()
    window.raise_()
    window.activateWindow()


def activate_existing_window(app: QApplication) -> None:
    modal = app.activeModalWidget()
    if modal is not None:
        # Hiding a QDialog can end its exec() and discard unfinished settings.
        modal.showNormal()
        modal.raise_()
        modal.activateWindow()
        return
    window = getattr(app, "_fanyiji_main_window", None)
    if window is not None:
        if not getattr(window, "_closing_emitted", False):
            restore_main_window(window)
        return
    window = app.activeWindow()
    if window is None:
        windows = [widget for widget in app.topLevelWidgets() if widget.isVisible()]
        window = windows[0] if windows else None
    if window is not None:
        window.showNormal()
        window.raise_()
        window.activateWindow()


def configure_application_identity(app: QApplication) -> None:
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(APP_NAME)


def single_instance(app):
    socket = QLocalSocket()
    socket.connectToServer(INSTANCE_NAME)
    if socket.waitForConnected(200):
        socket.write(b"activate")
        socket.waitForBytesWritten(200)
        return None
    server = QLocalServer(app)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(INSTANCE_NAME):
        QMessageBox.warning(None, tr("无法启动"), tr("已有一个字幕程序正在启动，请稍后再试。"))
        return None
    def activate():
        connection = server.nextPendingConnection()
        if connection:
            connection.close()
            connection.deleteLater()
        activate_existing_window(app)
    server.newConnection.connect(activate)
    return server


def smoke_test(screenshot=None, language="zh-CN"):
    set_language(language)
    from .ui import MainWindow, SettingsDialog
    app = QApplication.instance() or QApplication(sys.argv[:1])
    configure_application_identity(app)
    config = AppConfig(ui_language=language)
    window = MainWindow(config)
    dialog = SettingsDialog(config, first_run=True)
    window.set_session_state("界面预览 · 未采集声音")
    window.set_source_status("system", "界面预览")
    window.set_translation("system", "这只是界面预览 实际使用时显示 Chrome 字幕快照", "preview-caption")
    window.show()
    app.processEvents()
    if screenshot:
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        window.grab().save(str(screenshot))
        dialog.show()
        app.processEvents()
        dialog.grab().save(str(screenshot.with_name("first-run.png")))
    dialog.close()
    window.close()
    print(json.dumps({"ui_smoke": "passed", "audio_capture": False, "settings_written": False,
                      "browser_opened": False, "project": "chrome"}))
    return 0


def main():
    parser = argparse.ArgumentParser(description=APP_NAME + " · Chrome 实时字幕翻译")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--smoke-language", choices=("zh-CN", "ja", "en"), default="zh-CN")
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    if args.smoke_test:
        return smoke_test(args.screenshot, args.smoke_language)
    app = QApplication(sys.argv[:1])
    configure_application_identity(app)
    server = single_instance(app)
    if server is None:
        return 0
    from .migration import ensure_initial_migration
    ensure_initial_migration()
    configure_logging()
    config_path = data_root() / "settings.json"
    try:
        config = load_config(config_path) or AppConfig()
    except ConfigError:
        QMessageBox.warning(None, tr("需要重新设置"), tr("无法读取设置文件，请重新设置"))
        config = AppConfig()
    set_language(config.ui_language)
    from .ui import MainWindow
    window = MainWindow(config)
    controller = ChromeAppController(window, config, config_path)
    app.aboutToQuit.connect(controller.close)
    app._fanyiji_main_window = window
    window.show()
    restore_main_window(window)
    QTimer.singleShot(0, controller.start)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
