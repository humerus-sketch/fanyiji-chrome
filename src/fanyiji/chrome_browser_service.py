"""One app-owned Chrome profile and the existing credential-free sender protocol."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import subprocess
import threading
import time

from PySide6.QtCore import QObject, Signal

from .chrome_caption_runtime import find_chrome, source_room, validate_source_url
from .chrome_cdp import CdpConnection, ChromeControlError, local_json, loopback_url
from .danmaku_protocol import (MIN_SEND_INTERVAL_SECONDS, login_script, presence_script, send_script,
                               sanitize_check, sanitize_login, sanitize_receipt, validate_danmaku_command)
from .local_features import danmaku_available
from .paths import data_root, models_root


def seed_caption_preferences(profile: Path) -> None:
    """Edit only our stopped profile, retaining account data and other preferences."""
    path = profile / "Default" / "Preferences"
    value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not isinstance(value, dict):
        raise ChromeControlError("profile_invalid")
    accessibility = value.setdefault("accessibility", {})
    captions = accessibility.setdefault("captions", {})
    captions.update(live_caption_enabled=True, live_caption_language="ja-JP",
                    live_translate_enabled=True, live_translate_target_language="zh-CN")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".fanyiji-pending")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    state_path = profile / "Local State"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    packs = state.setdefault("accessibility", {}).setdefault("captions", {})
    registered = packs.get("soda_registered_language_packs", [])
    if not isinstance(registered, list):
        raise ChromeControlError("profile_invalid")
    packs["soda_registered_language_packs"] = list(dict.fromkeys([*registered, "ja-JP"]))
    temporary = state_path.with_suffix(".fanyiji-pending")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, state_path)


class ChromeBrowserService(QObject):
    event = Signal(dict)
    danmaku_result = Signal(dict)
    danmaku_status = Signal(dict)

    def __init__(self, parent=None, *, profile=None, process_factory=None, chrome_finder=None,
                 connector=None, json_reader=None, protect=True):
        super().__init__(parent)
        self.profile = Path(profile) if profile else data_root() / "chrome-caption" / "dedicated-profile"
        self.preferences_path = data_root() / "chrome-caption-settings.json"
        self._factory, self._finder = process_factory or subprocess.Popen, chrome_finder or find_chrome
        self._connect, self._json = connector or CdpConnection, json_reader or local_json
        self._protect = protect
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="owned-chrome-send")
        self._operation = threading.RLock()
        self.process = self.job = self._browser = self._page = self._visibility = None
        self._port = 0
        self._generation = 0
        self._ready = self._closed = self._uncertain = self.source_ready = False
        self._hide_requested = False
        self._visibility_notified = None
        self._requests = set()
        self._next_send = 0.0
        self._unknown_send = None
        self.room = self.url = ""
        self.active_version = None
        self.danmaku_available = danmaku_available()
        self.audio_processing_mode = "off"
        self.audio_status = {"mode": "off", "state": "off", "reason": "disabled"}
        self._audio_runtime = None

    def configure_audio_processing(self, mode: str, *, reload=False):
        """Never open a browser; an explicit active-mode change restores the media graph by reload."""
        if mode not in ("off", "gtcrn"):
            raise ValueError("invalid_chrome_audio_processing")
        with self._operation:
            changed = mode != self.audio_processing_mode
            self.audio_processing_mode = mode
            if changed:
                self._stop_audio_processing()
            if changed and reload and self.is_browser_ready:
                self._room_target()
                self._page.call("Page.reload", {"ignoreCache": False})
                self._generation += 1
                self._emit("audio_source_changed")
            if self.is_browser_ready and self.audio_processing_mode == "gtcrn":
                self._start_audio_processing()
            elif mode == "off":
                self.audio_status = {"mode": "off", "state": "off", "reason": "disabled"}

    def _start_audio_processing(self):
        if self.audio_processing_mode != "gtcrn":
            return
        if self._audio_runtime is not None:
            if getattr(self._audio_runtime, "is_running", True):
                return
            # A finished worker must not block an explicit retry of the same model.
            self._stop_audio_processing()
        from .chrome_audio_runtime import ChromeAudioRuntime
        process, generation, url = self.process, self._generation, self.url
        last_check = [0.0, False]
        def guard():
            if (self._closed or self.process is not process or process.poll() is not None
                    or self._generation != generation or not self.is_browser_ready):
                return False
            now = time.monotonic()
            if now - last_check[0] < 0.2:
                return last_check[1]
            try:
                self._room_target()
                last_check[:] = [now, True]
                return True
            except Exception:
                last_check[:] = [now, False]
                return False
        def connection():
            return self._connect(self._room_target()["webSocketDebuggerUrl"], port=self._port)
        def status(value):
            if (self.process is not process or self._generation != generation or self._closed
                    or self._audio_runtime is not runtime or self.audio_processing_mode != "gtcrn"):
                return
            self.audio_status = dict(value)
            logging.info("CHROME_AUDIO mode=%s state=%s reason=%s frames=%s mean_ms=%.2f max_ms=%.2f "
                "fault=%s fault_state=%s recoveries=%s attempt=%s retry_seconds=%.2f "
                "captured=%s played=%s dropped=%s",
                value["mode"], value["state"], value["reason"], value["processed_frames"],
                value["processing_mean_ms"], value["processing_max_ms"],
                value.get("last_fault_reason", "none"), value.get("last_fault_state", "none"),
                value.get("recovery_count", 0), value.get("recovery_attempt", 0),
                value.get("retry_delay_seconds", 0), value.get("captured_frames", 0),
                value.get("played_frames", 0), value.get("dropped_frames", 0))
            self.event.emit({"kind": "audio_processing", "browser_generation": generation, **value})
        runtime = ChromeAudioRuntime(connection_factory=connection, source_guard=guard,
            generation=generation, url=url, models_dir=models_root(), on_status=status)
        self._audio_runtime = runtime
        runtime.start()

    def _stop_audio_processing(self):
        runtime, self._audio_runtime = self._audio_runtime, None
        if runtime:
            runtime.stop()

    @property
    def is_browser_ready(self):
        return self._ready and not self._closed and self.process is not None and self.process.poll() is None

    @property
    def browser_pid(self):
        return self.process.pid if self.is_browser_ready else 0

    @property
    def generation(self):
        return self._generation

    def _emit(self, kind, **fields):
        self.event.emit({"kind": kind, "browser_generation": self._generation, **fields})

    def open_sync(self, url: str):
        url = validate_source_url(url)
        with self._operation:
            if self._closed:
                raise ChromeControlError()
            if self.is_browser_ready and self.url == url:
                return self.browser_pid
            self.stop_browser()
            chrome = self._finder()
            if chrome is None:
                raise ChromeControlError("chrome_missing")
            self.profile.mkdir(parents=True, exist_ok=True)
            # An active foreign instance using this profile must never be adopted.
            lock = self.profile / "fanyiji-owner.lock"
            try:
                self._profile_lock = lock.open("a+b")
                if os.name == "nt":
                    import msvcrt
                    self._profile_lock.seek(0)
                    msvcrt.locking(self._profile_lock.fileno(), msvcrt.LK_NBLCK, 1)
                self._profile_lock.seek(0)
            except OSError:
                if getattr(self, "_profile_lock", None):
                    self._profile_lock.close()
                    self._profile_lock = None
                raise ChromeControlError("profile_busy") from None
            try:
                port_file = self.profile / "DevToolsActivePort"
                try:
                    old = port_file.read_text(encoding="utf-8").splitlines() if port_file.is_file() else []
                except (OSError, UnicodeError):
                    # Do not edit an existing profile while its control file may still be owned/written.
                    raise ChromeControlError("profile_busy") from None
                if old:
                    if len(old) == 2 and old[0].isdigit():
                        try:
                            version = self._json(int(old[0]), "/json/version")
                        except Exception:
                            version = {}
                        if version.get("webSocketDebuggerUrl", "").endswith(old[1]):
                            raise ChromeControlError("profile_busy")
                seed_caption_preferences(self.profile)
                if port_file.exists():
                    port_file.unlink()
                options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                           "stderr": subprocess.DEVNULL, "shell": False}
                args = [str(chrome), "--user-data-dir=" + str(self.profile.resolve()),
                        "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
                        "--no-first-run", "--no-default-browser-check", "--new-window", url]
                if self._protect and os.name == "nt":
                    from .process_utils import _WindowsJob, _external_dll_search
                    self.job = _WindowsJob()
                    with _external_dll_search():
                        self.process = self._factory(args, **options)
                    self.job.assign(self.process)
                else:
                    self.process = self._factory(args, **options)
                self._profile_lock.write(str(self.process.pid).encode("ascii"))
                self._profile_lock.flush()
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if self.process.poll() is not None:
                        raise ChromeControlError("browser_closed")
                    try:
                        lines = port_file.read_text(encoding="utf-8").splitlines() if port_file.is_file() else []
                    except (OSError, UnicodeError):
                        # Chrome publishes this file during startup; sharing locks and partial UTF-8 are transient.
                        lines = []
                    if (len(lines) == 2 and lines[0].isascii() and lines[0].isdigit()
                            and 1 <= int(lines[0]) <= 65535 and lines[1].startswith("/devtools/browser/")
                            and len(lines[1]) > len("/devtools/browser/")):
                        port = int(lines[0])
                        try:
                            endpoint = loopback_url(f"ws://127.0.0.1:{port}{lines[1]}", websocket=True, port=port)
                        except ChromeControlError:
                            endpoint = None
                        if endpoint is not None:
                            self._port = port
                            self._browser = self._connect(endpoint, port=self._port)
                            break
                    time.sleep(0.1)
                if self._browser is None:
                    raise ChromeControlError("browser_unavailable")
                self.url, self.room = url, source_room(url)
                self._page = self._connect(self._room_target()["webSocketDebuggerUrl"], port=self._port)
                self.active_version = self._json(self._port, "/json/version").get("Browser", "Chrome")
                # A new browser window cannot prove whether a prior POST was broadcast.
                self._ready, self.source_ready = True, True
                self._generation += 1
                self._emit("ready", active_version=self.active_version)
                self._start_audio_processing()
                threading.Thread(target=self._monitor, args=(self.process, self._generation), daemon=True,
                                 name="owned-chrome-caption-visibility").start()
                return self.browser_pid
            except Exception:
                self.stop_browser()
                raise

    def _room_target(self):
        targets = self._json(self._port, "/json/list")
        pages = [row for row in targets if row.get("type") == "page"]
        if len(pages) != 1 or pages[0].get("url", "").split("?", 1)[0].rstrip("/") != self.url:
            raise ChromeControlError("wrong_room")
        loopback_url(pages[0]["webSocketDebuggerUrl"], websocket=True, port=self._port)
        return pages[0]

    def _monitor(self, process, generation):
        while not self._closed and self.process is process and process.poll() is None:
            try:
                self._room_target()
                self.source_ready = True
            except Exception:
                self.source_ready = False
            if self.process is not process:
                return
            if self._hide_requested:
                with self._operation:
                    if self.process is not process or self._closed:
                        return
                    # A queued monitor decision must not restore an intent canceled by recovery.
                    if self._hide_requested:
                        try:
                            self.set_caption_hidden(True)
                        except Exception as error:
                            self._hide_requested = False
                            self._emit("caption_visibility_failed", error=getattr(error, "code", "visibility_unavailable"))
            time.sleep(0.5)
        if self.process is process:
            self._ready = self.source_ready = False
            self._emit("closed")

    def set_caption_hidden(self, hidden: bool):
        if type(hidden) is not bool:
            raise ValueError("invalid_visibility")
        with self._operation:
            self._hide_requested = hidden
            if not hidden:
                if self._visibility:
                    self._visibility.close()
                    self._visibility = None
                if self._visibility_notified != "visible":
                    self._emit("caption_visible")
                    self._visibility_notified = "visible"
                return
            if not self.is_browser_ready or self.job is None:
                return
            from .chrome_caption_visibility import CaptionVisibilityController, NativeCaptionVisibilityAdapter
            if self._visibility is None:
                adapter = NativeCaptionVisibilityAdapter(self.process, self.job)
                controller = CaptionVisibilityController(adapter, self.browser_pid)
                self._visibility = controller
            if not self._visibility.hide():
                return False
            if self._visibility_notified != "hidden":
                self._emit("caption_hidden")
                self._visibility_notified = "hidden"
            return True

    def recover_caption(self):
        with self._operation:
            if not self.is_browser_ready:
                raise ChromeControlError()
            # Recovery cancels automatic hiding; the monitor must not hide the rebuilt bubble again.
            self.set_caption_hidden(False)
            self._room_target()
            self._stop_audio_processing()
            self._page.call("Page.reload", {"ignoreCache": False})
            self._generation += 1
            self._emit("caption_recovering")
            self._start_audio_processing()

    def request_danmaku(self, operation, request_id, room, text=""):
        if not self.danmaku_available or not self.is_browser_ready or request_id in self._requests or len(self._requests) >= 64:
            return False
        command = {"op": "danmaku_" + operation, "request_id": request_id, "room": room}
        if operation != "login":
            command["text"] = text
        try:
            validate_danmaku_command(command)
        except (ValueError, TypeError):
            return False
        self._requests.add(request_id)
        self._executor.submit(self._send_command, command, self._generation)
        return True

    def _send_command(self, command, generation):
        operation = command["op"].removeprefix("danmaku_")
        started = False
        submission = None
        try:
            with self._operation:
                if not self.is_browser_ready or generation != self._generation or operation == "send" and self._uncertain:
                    raise ChromeControlError("window_closed")
                if command["room"] != self.room:
                    raise ChromeControlError("wrong_room")
                self._room_target()
                if operation == "send":
                    if time.monotonic() < self._next_send:
                        raise ChromeControlError("queue_full")
                    script = send_script(command["room"], command["text"])
                    # Establish a readonly occurrence baseline before any POST.
                    check = self._page.call("Runtime.evaluate", {"expression": presence_script(command["room"], command["text"]),
                        "returnByValue": True, "timeout": 3000}, timeout=5)
                    before = sanitize_check(check.get("result", {}).get("value", {})).get("occurrence_count")
                    submission = (command["room"], command["text"], before)
                    self._room_target()
                    if generation != self._generation or not self.is_browser_ready:
                        raise ChromeControlError("navigation_changed")
                elif operation == "login":
                    script = login_script(command["room"])
                else:
                    script = presence_script(command["room"], command["text"])
                started = operation == "send"
                if started:
                    # Only handing the send script to CDP can create an uncertain submission.
                    self._unknown_send = submission
                result = self._page.call("Runtime.evaluate", {"expression": script, "awaitPromise": True,
                    "returnByValue": True, "userGesture": True, "timeout": 18000}, timeout=20)
                if "exceptionDetails" in result:
                    raise ChromeControlError("script_unavailable")
                value = result.get("result", {}).get("value", {})
                self._room_target()
                if generation != self._generation:
                    raise ChromeControlError("navigation_changed")
        except Exception as error:
            if started:
                self._uncertain = True
            value = {"error": getattr(error, "code", "script_unavailable"), "request_started_epoch_ms": None} if not started else {
                "error": "script_unavailable"}
        finally:
            if started:
                self._next_send = time.monotonic() + MIN_SEND_INTERVAL_SECONDS
            self._requests.discard(command["request_id"])
        sanitize = sanitize_receipt if operation == "send" else sanitize_login if operation == "login" else sanitize_check
        result = sanitize(value)
        if operation == "send" and not started:
            result["outcome"] = "not_sent"
        with self._operation:
            if operation == "send" and started and self._unknown_send == submission:
                self._uncertain = result.get("outcome") == "result_unknown"
                if not self._uncertain:
                    self._unknown_send = None
            if operation == "check" and self._uncertain and self._unknown_send:
                room, text, before = self._unknown_send
                if (command["room"] == room and command["text"] == text and type(before) is int
                        and result.get("present") is True and type(result.get("occurrence_count")) is int
                        and result["occurrence_count"] > before):
                    self._uncertain = False
                    self._unknown_send = None
        self.danmaku_result.emit({"kind": "danmaku_result", "request_id": command["request_id"],
                                  "operation": operation, "result": result})

    def stop_browser(self):
        with self._operation:
            self._ready = self.source_ready = False
            self._generation += 1
            self._stop_audio_processing()
            visibility, self._visibility = self._visibility, None
            if visibility:
                try:
                    visibility.close()
                except Exception as error:
                    logging.warning("CHROME_CAPTION restore_before_close_failed type=%s", type(error).__name__)
            self._hide_requested = False
            self._visibility_notified = None
            if self._page:
                try:
                    self._page.close()
                except Exception:
                    pass
                self._page = None
            if self._browser:
                try:
                    self._browser.call("Browser.close", timeout=3)
                except Exception:
                    pass
                try:
                    self._browser.close()
                except Exception:
                    pass
                self._browser = None
            try:
                if self.process:
                    self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                try:
                    self.process.kill()
                    self.process.wait(timeout=3)
                except Exception as error:
                    logging.warning("CHROME_CAPTION owned_close_failed type=%s", type(error).__name__)
            except Exception as error:
                logging.warning("CHROME_CAPTION owned_wait_failed type=%s", type(error).__name__)
            finally:
                self.process = None
                try:
                    if self.job:
                        self.job.close()
                finally:
                    self.job = None
                    if visibility and hasattr(visibility, "adapter"):
                        try:
                            visibility.adapter.close()
                        except Exception as error:
                            logging.warning("CHROME_CAPTION adapter_close_failed type=%s", type(error).__name__)
                    lock = getattr(self, "_profile_lock", None)
                    if lock:
                        try:
                            if os.name == "nt":
                                import msvcrt
                                lock.seek(0)
                                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                        finally:
                            lock.close()
                            self._profile_lock = None
            self._emit("closed")

    def close(self):
        self._closed = True
        self.stop_browser()
        self._executor.shutdown(wait=False, cancel_futures=True)
