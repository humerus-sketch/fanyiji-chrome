"""Read captions from existing Chrome; only the helper belongs to this service."""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from .chrome_caption_runtime import find_chrome, source_room, validate_source_url


_STATUS_CODES = frozenset({"starting", "waiting_browser", "manual_caption_settings", "waiting_caption", "caption_found", "caption_found_unverified", "source_ambiguous", "source_unverified", "chrome_missing", "browser_missing",
                           "uia_unavailable", "unsupported_platform", "reader_failed", "reader_recovering", "browser_closed", "stopped"})


class ChromeCaptionError(RuntimeError):
    def __init__(self, code="reader_failed"):
        self.code = code if isinstance(code, str) and code in _STATUS_CODES else "reader_failed"
        super().__init__("Chrome 字幕读取暂不可用，请检查现有 Chrome 和实时字幕设置。")


class ChromeCaptionService:
    MAX_LINE_CHARACTERS = 65536
    READER_TIMEOUT_SECONDS = 15.0
    STARTUP_TIMEOUT_SECONDS = 30.0
    MAX_RECOVERY_ATTEMPTS = 3
    RECOVERY_RESET_SECONDS = 60.0

    def __init__(self, callback=None, *, profile: Path | None = None, process_factory=None, chrome_finder=None,
                 browser_closer=None, clock=time.monotonic, auto_restart=True):
        self.callback = callback or (lambda _event: None)
        # Retain constructor compatibility without creating, copying or using a profile.
        self.profile = Path(profile) if profile is not None else None
        self._factory = process_factory or subprocess.Popen
        self._protect = process_factory is None
        self._finder = chrome_finder or find_chrome
        self._lock = threading.RLock()
        self._operation = threading.RLock()
        self._browser = self._worker = None
        self._browser_job = self._worker_job = None
        self._generation = self._session = 0
        self._closed = False
        self._active = False
        self._source_url = ""
        self._clock, self._auto_restart = clock, auto_restart
        self._watchdog_stop = None
        self._worker_args = None
        self._last_event_at = self._last_snapshot_at = self._last_update_at = None
        self._worker_started_at = self._healthy_since = None
        self._last_caption_identity = self._last_caption_text = None
        self._stream_epoch = self._wire_epoch = None
        self._revision_offset = self._last_revision = 0
        self._recoveries = 0
        self._retry_at = None
        self._recovery_disabled = False
        self._reader_state = "stopped"
        self._connected = False
        self._reader_serial = 0

    @property
    def reader_generation(self):
        with self._lock:
            return self._reader_serial

    @property
    def health_snapshot(self):
        """Reader freshness is separate from caption freshness; neither proves ASR health."""
        with self._lock:
            now = self._clock()
            age = lambda at: None if at is None else round(max(0.0, now - at), 3)
            worker = self._worker
            try:
                alive = bool(worker is not None and worker.poll() is None)
            except Exception:
                alive = False
            return {"reader_alive": alive, "heartbeat_age_seconds": age(self._last_event_at),
                    "snapshot_age_seconds": age(self._last_snapshot_at),
                    "caption_update_age_seconds": age(self._last_update_at),
                    "recovery_attempts": self._recoveries, "reader_recovery_state": self._reader_state,
                    "caption_state": "disconnected" if not self._connected else
                        "waiting" if self._last_update_at is None else
                        "unchanged" if now - self._last_update_at >= 2.0 else "updated"}

    def _emit_health(self, generation):
        self._emit({"kind": "health", **self.health_snapshot}, generation)

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def _emit(self, event: dict, generation: int, *, worker=None):
        with self._lock:
            if (self._closed or not self._active or generation != self._generation
                    or worker is not None and self._worker is not worker):
                return
            value = {**event, "session": self._session, "generation": generation,
                     "reader_generation": self._reader_serial}
        # Receiver also fences queued delivery via the readonly generation property.
        try:
            self.callback(value)
        except Exception:
            pass

    def _spawn(self, argv, *, pipe=False):
        options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE if pipe else subprocess.DEVNULL,
                   "stderr": subprocess.DEVNULL, "shell": False}
        if pipe:
            options.update({"text": True, "encoding": "utf-8", "errors": "strict", "bufsize": 1})
        environment = os.environ.copy()
        if pipe:
            environment["PYTHONUTF8"] = "1"
            environment["PYTHONIOENCODING"] = "utf-8"
        if pipe and not getattr(sys, "frozen", False):
            source = str(Path(__file__).resolve().parents[1])
            environment["PYTHONPATH"] = source + os.pathsep + environment.get("PYTHONPATH", "")
        options["env"] = environment
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        job = process = None
        try:
            if self._protect and os.name == "nt":
                from .process_utils import _WindowsJob, _external_dll_search
                job = _WindowsJob()
                with _external_dll_search():
                    process = self._factory(argv, **options)
                job.assign(process)
            else:
                process = self._factory(argv, **options)
            return process, job
        except Exception:
            self._dispose(process, job)
            raise ChromeCaptionError() from None

    def start(self, url: str = "", *, session: int = 0, owned_browser_pid: int = 0):
        url = validate_source_url(url) if url != "" else ""
        if type(session) is not int or session < 0:
            raise ValueError("invalid_session")
        if type(owned_browser_pid) is not int or owned_browser_pid < 0:
            raise ValueError("invalid_browser_pid")
        with self._operation:
            self.stop()
            with self._lock:
                if self._closed:
                    raise ChromeCaptionError()
                generation = self._generation
                self._session = session
                self._active = True
                self._source_url = url
                self._last_event_at = self._last_snapshot_at = self._last_update_at = None
                self._last_caption_identity = self._last_caption_text = None
                self._stream_epoch = self._wire_epoch = None
                self._revision_offset = self._last_revision = 0
                self._recoveries, self._retry_at = 0, None
                self._recovery_disabled, self._connected = False, False
                self._reader_state = "waiting"
            if not url:
                self._emit({"kind": "status", "code": "waiting_browser"}, generation)
                return
            chrome = self._finder()
            if chrome is None:
                self._emit({"kind": "status", "code": "chrome_missing"}, generation)
                raise ChromeCaptionError("chrome_missing")
            self._emit({"kind": "status", "code": "starting"}, generation)
            with self._lock:
                if self._closed or not self._active or generation != self._generation:
                    return
                try:
                    prefix = [sys.executable, "--chrome-caption-worker"] if getattr(sys, "frozen", False) else [
                        sys.executable, "-m", "fanyiji.chrome_caption_worker"]
                    args = prefix + ["--attach-existing", "--generation", str(generation),
                                     "--session", str(session), "--source-url", url]
                    if owned_browser_pid:
                        args += ["--owned-browser-pid", str(owned_browser_pid)]
                    self._worker_args = args
                    self._start_worker(generation)
                    if self._auto_restart:
                        self._watchdog_stop = threading.Event()
                        threading.Thread(target=self._watchdog, args=(self._watchdog_stop, generation),
                                         name="chrome-caption-watchdog", daemon=True).start()
                except Exception:
                    self.stop()
                    raise ChromeCaptionError() from None

    def _start_worker(self, generation):
        self._worker, self._worker_job = self._spawn(self._worker_args, pipe=True)
        self._reader_serial += 1
        self._worker_started_at = self._clock()
        self._last_event_at = self._healthy_since = None
        self._wire_epoch = None
        self._revision_offset = self._last_revision
        self._retry_at = None
        self._reader_state = "waiting"
        threading.Thread(target=self._read, args=(self._worker, generation),
                         name="chrome-caption-pipe", daemon=True).start()

    def _watchdog(self, stop_event, generation):
        while not stop_event.wait(1.0):
            self.watchdog_tick(generation)

    def _schedule_recovery(self, generation):
        with self._lock:
            if (self._closed or not self._active or generation != self._generation
                    or self._recovery_disabled or self._retry_at is not None):
                return
            exhausted = not self._auto_restart or self._recoveries >= self.MAX_RECOVERY_ATTEMPTS
            if exhausted:
                self._recovery_disabled = True
                self._reader_state = "exhausted"
            else:
                self._retry_at = self._clock() + min(4.0, 2.0 ** self._recoveries)
                self._reader_state = "backoff"
        self._emit({"kind": "status", "code": "reader_failed" if exhausted else "reader_recovering"}, generation)
        if self._auto_restart:
            self._emit_health(generation)

    def watchdog_tick(self, generation=None):
        """Retire only an unresponsive helper. Silence never restarts Chrome or this bridge session."""
        generation = self.generation if generation is None else generation
        with self._operation:
            with self._lock:
                if (self._closed or not self._active or generation != self._generation
                        or self._recovery_disabled or not self._worker_args):
                    return
                now, worker = self._clock(), self._worker
                expired = False
                if worker is not None:
                    try:
                        expired = worker.poll() is not None
                    except Exception:
                        expired = True
                    last = self._last_event_at if self._last_event_at is not None else self._worker_started_at
                    timeout = self.READER_TIMEOUT_SECONDS if self._last_event_at is not None else self.STARTUP_TIMEOUT_SECONDS
                    expired = expired or last is not None and now - last >= timeout
                if expired:
                    self._worker, job = None, self._worker_job
                    self._worker_job = None
                else:
                    job = None
            if expired:
                self._dispose(worker, job)
                self._schedule_recovery(generation)
            with self._lock:
                due = (not self._closed and self._active and generation == self._generation
                       and not self._recovery_disabled and self._worker is None
                       and self._retry_at is not None and self._clock() >= self._retry_at)
                if due:
                    self._recoveries += 1
                    self._retry_at = None
                    try:
                        self._start_worker(generation)
                    except Exception:
                        failed = True
                    else:
                        failed = False
                else:
                    failed = False
            if failed:
                self._schedule_recovery(generation)
            if expired or due:
                self._emit_health(generation)

    def _received(self, worker, generation):
        with self._lock:
            if self._closed or not self._active or generation != self._generation or self._worker is not worker:
                return False
            now = self._clock()
            self._last_event_at = now
            if self._healthy_since is None:
                self._healthy_since = now
            if now - self._healthy_since >= self.RECOVERY_RESET_SECONDS:
                self._recoveries = 0
            self._reader_state = "healthy"
            return True

    def _snapshot_continuity(self, value, *, worker=None, generation=None):
        with self._lock:
            if worker is not None and (self._worker is not worker or generation != self._generation
                                       or self._closed or not self._active):
                return None
            wire_epoch = value["source_epoch"]
            if (self._wire_epoch is not None and wire_epoch < self._wire_epoch
                    or self._revision_offset + value["revision"] < self._last_revision):
                return None
            identity = (value["window_id"], value["connected"])
            if self._stream_epoch is None:
                self._stream_epoch = wire_epoch
            elif self._wire_epoch is None:
                if self._last_caption_identity != identity:
                    self._stream_epoch += 1
            elif wire_epoch != self._wire_epoch:
                self._stream_epoch += max(1, wire_epoch - self._wire_epoch)
            self._wire_epoch = wire_epoch
            self._last_snapshot_at = self._clock()
            if value["connected"] and (self._last_caption_text != value["text"] or self._last_caption_identity != identity):
                self._last_update_at = self._last_snapshot_at
            self._last_caption_identity = identity
            self._last_caption_text = value["text"]
            self._connected = value["connected"]
            self._last_revision = self._revision_offset + value["revision"]
            return {**value, "source_epoch": self._stream_epoch, "revision": self._last_revision}

    def open_browser(self, url: str, *, session: int | None = None):
        """Compatibility alias: connect only; never launch Chrome."""
        url = validate_source_url(url)
        with self._lock:
            current_session = self._session if session is None else session
        self.start(url, session=current_session)

    def open_caption_settings(self, *, session: int | None = None):
        """Legacy entry point emits instructions, never starts Chrome."""
        if self._closed:
            raise ChromeCaptionError()
        self._emit({"kind": "status", "code": "manual_caption_settings"}, self.generation)

    def open_room_tab(self, url: str) -> None:
        """Compatibility entry point connects only; navigation stays with the user."""
        self.open_browser(url)

    def _read(self, worker, generation):
        terminal_status = False
        try:
            while True:
                line = worker.stdout.readline(self.MAX_LINE_CHARACTERS + 1)
                if not line:
                    break
                if len(line) > self.MAX_LINE_CHARACTERS or not line.endswith("\n"):
                    break
                value = json.loads(line)
                if not isinstance(value, dict):
                    break
                if (type(value.get("generation")) is not int or type(value.get("session")) is not int
                        or value["generation"] != generation or value["session"] != self._session):
                    continue
                with self._lock:
                    if self._worker is not worker or self._generation != generation:
                        break
                if value.get("kind") == "diagnostic":
                    from .chrome_caption_worker import safe_diagnostic
                    diagnostic = safe_diagnostic(value)
                    if diagnostic is not None:
                        logging.warning("CHROME_CAPTION reader_diagnostic %s", json.dumps(diagnostic, ensure_ascii=True))
                    continue
                if (value.get("kind") == "status" and isinstance(value.get("code"), str)
                        and value["code"] in _STATUS_CODES):
                    if not self._received(worker, generation):
                        break
                    self._emit({"kind": "status", "code": value["code"]}, generation, worker=worker)
                    terminal_status = value["code"] in ("browser_closed", "unsupported_platform")
                    if terminal_status:
                        with self._lock:
                            if self._worker is worker and generation == self._generation:
                                self._recovery_disabled = True
                elif (value.get("kind") == "heartbeat"
                      and type(value.get("observed_at")) in (int, float)
                      and math.isfinite(value["observed_at"]) and value["observed_at"] >= 0
                      and type(value.get("connected")) is bool
                      and type(value.get("revision")) is int and value["revision"] >= 0
                      and type(value.get("source_epoch")) is int and value["source_epoch"] >= 0
                      and isinstance(value.get("status"), str) and value["status"] in _STATUS_CODES
                      and all(value.get(field) is None or type(value.get(field)) in (int, float)
                              and math.isfinite(value[field]) and value[field] >= 0
                              for field in ("snapshot_age_seconds", "caption_update_age_seconds"))):
                    if not self._received(worker, generation):
                        break
                    self._emit_health(generation)
                elif (value.get("kind") == "snapshot" and type(value.get("revision")) is int
                      and value["revision"] >= 0 and isinstance(value.get("text"), str)
                      and len(value["text"]) <= 4000 and isinstance(value.get("window_id"), str)
                      and value["window_id"].isascii() and value["window_id"].isdigit()
                      and 0 < len(value["window_id"]) <= 20 and type(value.get("source_epoch")) is int
                      and value["source_epoch"] >= 0 and value.get("bridge_generation") == generation
                      and type(value.get("bridge_generation")) is int
                      and type(value.get("observed_at")) in (int, float)
                      and math.isfinite(value["observed_at"]) and value["observed_at"] >= 0
                      and type(value.get("connected")) is bool and isinstance(value.get("room"), str)
                      and value["room"] == "" and self._source_url
                      and value.get("requested_room") == source_room(self._source_url)
                      and value.get("source_verified") is False
                      and value.get("source_lang") == "unknown" and value.get("target_lang") == "zh"):
                    fields = ("revision", "text", "window_id", "source_epoch", "observed_at",
                              "bridge_generation", "room", "requested_room", "source_verified", "connected", "source_lang", "target_lang")
                    if not self._received(worker, generation):
                        break
                    snapshot = self._snapshot_continuity({field: value[field] for field in fields},
                                                         worker=worker, generation=generation)
                    if snapshot is None:
                        break
                    self._emit({"kind": "snapshot", **snapshot,
                                "is_final": False, "is_official_final": False, "allow_push": False}, generation, worker=worker)
        except (OSError, ValueError, UnicodeError):
            pass
        finally:
            with self._operation:
                with self._lock:
                    if generation == self._generation and self._worker is worker:
                        self._worker, retired = None, self._worker_job
                        self._worker_job = None
                        current = True
                        if terminal_status:
                            self._reader_state, self._connected = "stopped", False
                    else:
                        retired = None
                        worker = None
                        current = False
                self._dispose(worker, retired)
            if current and not terminal_status:
                self._schedule_recovery(generation)

    @staticmethod
    def _dispose(process, job):
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)
            except Exception:
                pass
        if job is not None:
            try:
                job.close()
            except OSError:
                pass
        if process is not None and getattr(process, "stdout", None) is not None:
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass

    def stop(self):
        # Chrome is user-owned. Pause/reconnect/exit only disposes our helper.
        with self._operation:
            with self._lock:
                self._generation += 1
                self._active = False
                worker, self._worker = self._worker, None
                worker_job, self._worker_job = self._worker_job, None
                watcher, self._watchdog_stop = self._watchdog_stop, None
                self._worker_args = None
                self._retry_at = None
                self._reader_state = "stopped"
                self._connected = False
            if watcher is not None:
                watcher.set()
            self._dispose(worker, worker_job)

    def close(self):
        with self._lock:
            self._closed = True
        self.stop()
