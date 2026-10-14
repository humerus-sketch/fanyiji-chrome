"""One app-owned Chrome profile and the existing credential-free sender protocol."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ctypes
import json
import logging
import os
from pathlib import Path
import subprocess
import stat
import threading
import time

from PySide6.QtCore import QObject, Signal

from .chrome_caption_runtime import find_chrome, source_room, validate_source_url
from .chrome_cdp import CdpConnection, ChromeControlError, local_json, loopback_url
from .danmaku_protocol import (MIN_SEND_INTERVAL_SECONDS, login_script, presence_script, send_script,
                               sanitize_check, sanitize_login, sanitize_receipt, validate_danmaku_command)
from .local_features import danmaku_available
from .identity import APP_ID
from .paths import data_root, models_root, path_override


def _existing_short_profile(physical: Path) -> Path:
    """Use an existing verified 8.3 alias; never create names or expand the result."""
    if os.name != "nt":
        return physical
    try:
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        get_short = kernel.GetShortPathNameW
        get_short.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_short.restype = wintypes.DWORD
        size = get_short(str(physical), None, 0)
        if not 0 < size <= 32768:
            return physical
        buffer = ctypes.create_unicode_buffer(size)
        written = get_short(str(physical), buffer, size)
        if not 0 < written < size or written != len(buffer.value):
            return physical
        candidate = Path(buffer.value)
        if not candidate.is_absolute() or len(str(candidate)) >= len(str(physical)):
            return physical
        # Real 8.3 names are ordinary directory aliases, not junctions. Check
        # their chain and compare metadata without opening any account values.
        for directory in (candidate, *candidate.parents, candidate / "Default"):
            value = directory.lstat()
            if not stat.S_ISDIR(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
                return physical
        if not candidate.samefile(physical):
            return physical
        for relative in (Path("Local State"), Path("Default/Preferences")):
            short_marker, long_marker = candidate / relative, physical / relative
            for marker in (short_marker, long_marker):
                value = marker.lstat()
                if (not stat.S_ISREG(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400
                        or value.st_nlink != 1):
                    return physical
            if not short_marker.samefile(long_marker):
                return physical
        # resolve() would turn this verified short alias back into the long path.
        return candidate
    except (OSError, ValueError, RuntimeError, AttributeError, TypeError):
        return physical


def resolve_external_profile(profile: Path) -> Path:
    """Select a verified MSIX file alias for external Chrome, keeping logical ownership."""
    profile = Path(profile)
    try:
        logical = profile.resolve()
    except (OSError, ValueError, RuntimeError):
        logical = profile.absolute()

    def regular(path: Path) -> bool:
        value = path.lstat()
        return (stat.S_ISREG(value.st_mode) and not getattr(value, "st_file_attributes", 0) & 0x400
                and value.st_nlink == 1)

    def directory(path: Path) -> bool:
        value = path.lstat()
        return stat.S_ISDIR(value.st_mode) and not getattr(value, "st_file_attributes", 0) & 0x400

    try:
        if path_override("DATA") is not None:
            return logical
        local = os.environ.get("LOCALAPPDATA")
        if not local or not Path(local).is_absolute():
            return logical
        local_root = Path(local).absolute()
        owner = local_root / APP_ID
        default = owner / "chrome-caption" / "dedicated-profile"
        if profile.absolute() != default or data_root().absolute() != owner:
            return logical
        state = profile / "Local State"
        preferences = profile / "Default" / "Preferences"
        if not regular(state) or not regular(preferences):
            return logical
        physical_state = state.resolve(strict=True)
        physical_preferences = preferences.resolve(strict=True)
        physical = physical_state.parent
        if physical == logical:
            return logical
        relative = physical.relative_to(local_root / "Packages")
        expected_tail = ("LocalCache", "Local", APP_ID, "chrome-caption", "dedicated-profile")
        if (len(relative.parts) != 6 or tuple(part.casefold() for part in relative.parts[1:])
                != tuple(part.casefold() for part in expected_tail)
                or physical_state.name != "Local State"
                or physical_preferences != physical / "Default" / "Preferences"):
            return logical
        # A filesystem junction must not masquerade as Windows file virtualization.
        directories = [owner, profile.parent, profile, preferences.parent, local_root / "Packages"]
        candidate = local_root / "Packages"
        for part in relative.parts:
            candidate = candidate / part
            directories.append(candidate)
        directories.append(physical_preferences.parent)
        if not all(directory(path) for path in directories):
            return logical
        if not regular(physical_state) or not regular(physical_preferences):
            return logical
        if not state.samefile(physical_state) or not preferences.samefile(physical_preferences):
            return logical
        # Recheck link flags after identity comparison; no marker contents or cookies are read.
        if not all(regular(path) for path in (state, preferences, physical_state, physical_preferences)):
            return logical
        return _existing_short_profile(physical)
    except (OSError, ValueError, RuntimeError):
        return logical


def _rebase_caption_resource_paths(profile: Path, captions: dict) -> int:
    """A migrated profile must not keep depending on the retired profile's SODA files."""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return 0
    legacy = (Path(local) / "LocalJaZhSubtitles" / "chrome-caption" / "dedicated-profile").resolve()
    destination = profile.resolve()
    changed = 0
    for key in ("soda_binary_path", "soda_en_us_config_path", "soda_ja_jp_config_path", "soda_zh_cn_config_path"):
        raw = captions.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            relative = Path(raw).resolve().relative_to(legacy)
            expected_folder = "SODA" if key == "soda_binary_path" else "SODALanguagePacks"
            if not relative.parts or relative.parts[0] != expected_folder:
                continue
            replacement = (destination / relative).resolve()
            if not replacement.is_relative_to(destination):
                continue
            available = (replacement.is_file() and replacement.stat().st_size > 0
                         if key == "soda_binary_path" else
                         replacement.is_dir() and any(item.is_file() for item in replacement.iterdir()))
            if available and str(replacement) != raw:
                captions[key] = str(replacement)
                changed += 1
        except (OSError, ValueError):
            continue
    return changed


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
    repaired = _rebase_caption_resource_paths(profile, packs)
    if repaired:
        logging.info("CHROME_CAPTION resource_paths_rebased count=%s", repaired)
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
    danmaku_transport_recovery_requested = Signal()

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
        self._unknown_submissions = {}
        self._transport_recovery_notified = False
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
                from .process_utils import external_chrome_environment
                options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                           "stderr": subprocess.DEVNULL, "shell": False,
                           "env": external_chrome_environment()}
                args = [str(chrome), "--user-data-dir=" + str(resolve_external_profile(self.profile)),
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
                restored = False
                if self._visibility:
                    was_hidden = getattr(self._visibility, "hidden", False) is True
                    self._visibility.close()
                    restored = was_hidden and getattr(self._visibility, "last_status", None) == "restored"
                    self._visibility = None
                status = "visible" if restored else "unhidden"
                if self._visibility_notified != status:
                    # Cancelling a hide request does not create a bubble. Only a
                    # verified restoration of an existing snapshot means visible.
                    self._emit("caption_visible" if restored else "caption_unhidden")
                    self._visibility_notified = status
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
            url = self.url
            self._room_target()
            # Chrome's close button closes its caption model, independently of
            # our opacity setting. Reopen the same owned profile to rebuild that
            # model and re-enable its saved caption preferences while stopped.
            self.set_caption_hidden(False)
            self._ready = False
            pid = self.open_sync(url)
            self._emit("caption_recovering")
            return pid

    def _submission_transport_settled(self, record):
        if record["settled"]:
            return True
        process = record["process"]
        if process is not None and process.poll() is not None:
            record["settled"], record["process"] = True, None
            return True
        return False

    def _submission_guard_blocks(self, room, text):
        key = (room, text)
        if key in self._unknown_submissions:
            return True
        return any(not record["retired"] or not self._submission_transport_settled(record)
                   for record in self._unknown_submissions.values())

    def _refresh_submission_guard(self):
        self._uncertain = bool(self._unknown_submissions)
        latest = next(reversed(self._unknown_submissions), None)
        self._unknown_send = ((latest[0], latest[1], self._unknown_submissions[latest]["before"])
                              if latest is not None else None)

    def retire_danmaku_submission(self, room, text):
        """Keep the exact unknown payload fenced after its durable FIFO handoff."""
        recover = False
        with self._operation:
            record = self._unknown_submissions.get((room, text))
            if record is None:
                return False
            record["retired"] = True
            if (not self._submission_transport_settled(record) and not self._closed
                    and self.is_browser_ready and not self._transport_recovery_notified):
                self._transport_recovery_notified = True
                recover = True
        if recover:
            # The controller rebuilds its reader around the new owned PID too.
            self.danmaku_transport_recovery_requested.emit()
        return True

    def restart_danmaku_transport(self):
        """Only rebuild an already opened owned browser with an unsettled POST."""
        with self._operation:
            if (self._closed or not self.is_browser_ready or not self.url
                    or not any(record["retired"] and not self._submission_transport_settled(record)
                               for record in self._unknown_submissions.values())):
                return 0
            url = self.url
            self._room_target()
            self._ready = False
            pid = self.open_sync(url)
            self._transport_recovery_notified = False
            self._emit("danmaku_transport_recovered")
            return pid

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
        if operation == "send":
            with self._operation:
                if ((room, text) not in self._unknown_submissions
                        and any(record["retired"] and not self._submission_transport_settled(record)
                                for record in self._unknown_submissions.values())):
                    return False  # Keep the following FIFO row while recovery is pending.
        self._requests.add(request_id)
        self._executor.submit(self._send_command, command, self._generation)
        return True

    def _send_command(self, command, generation):
        operation = command["op"].removeprefix("danmaku_")
        started = False
        submission = None
        try:
            with self._operation:
                if (not self.is_browser_ready or generation != self._generation
                        or operation == "send" and self._submission_guard_blocks(command["room"], command["text"])):
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
                    self._unknown_submissions[(command["room"], command["text"])] = {
                        "before": before, "process": self.process, "settled": False, "retired": False}
                result = self._page.call("Runtime.evaluate", {"expression": script, "awaitPromise": True,
                    "returnByValue": True, "userGesture": True, "timeout": 18000}, timeout=20)
                if started:
                    # An awaited JS result, including an AbortController error,
                    # is terminal. A CDP/native timeout itself is not terminal.
                    self._unknown_submissions[(command["room"], command["text"])]["settled"] = True
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
                if result.get("outcome") != "result_unknown":
                    self._unknown_submissions.pop((command["room"], command["text"]), None)
            if operation == "check":
                record = self._unknown_submissions.get((command["room"], command["text"]))
                before = record["before"] if record is not None else None
                if (record is not None and type(before) is int
                        and result.get("present") is True and type(result.get("occurrence_count")) is int
                        and result["occurrence_count"] > before):
                    self._unknown_submissions.pop((command["room"], command["text"]), None)
            self._refresh_submission_guard()
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
