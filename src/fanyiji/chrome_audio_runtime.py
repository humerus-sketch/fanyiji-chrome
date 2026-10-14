"""One bounded, recoverable PCM worker for the owned Chrome audio graph."""
from __future__ import annotations

import math
import secrets
import threading
import time

from .chrome_audio_processing import ChromeAudioProcessor, decode_pcm_frame, encode_pcm_samples
from .chrome_audio_script import install_script, pull_script, push_script, stop_script
from .chrome_cdp import ChromeControlError


_GRAPH_FAULTS = {"playback_underrun", "capture_overflow", "controller_timeout", "startup_timeout",
                 "invalid_output", "invalid_capture", "audio_output_rejected", "audio_stream_unavailable"}
_TRANSPORT_FAULTS = {"browser_unavailable", "script_timeout", "script_unavailable"}
_NATIVE_FAULTS = {"processing_error", "processing_too_slow", "reset_error", "load_error"}
_IDENTITY_FAULTS = {"source_changed", "wrong_source", "stale_session", "session_replaced"}
_REASONS = (_GRAPH_FAULTS | _TRANSPORT_FAULTS | _NATIVE_FAULTS | _IDENTITY_FAULTS |
            {"model_loading", "model_missing", "model_invalid", "model_unavailable", "unsupported_realtime",
             "media_not_playing", "processed_audio_experimental", "audio_unavailable", "disabled"})
_COUNTERS = ("captured_frames", "played_frames", "dropped_frames", "buffered_input_frames",
             "buffered_output_frames", "last_input_sequence", "last_output_sequence")
_TIMES = ("last_input_wall_ms", "last_output_wall_ms", "first_input_wall_ms", "first_playback_wall_ms",
          "last_playback_wall_ms",
          "first_failure_wall_ms", "audio_context_time_seconds", "last_input_age_ms", "last_output_age_ms")
_RMS = ("last_input_rms", "last_output_rms")


def _safe_numbers(value):
    """Keep only bounded diagnostic values; never retain a PCM frame or page string."""
    result = {}
    if not isinstance(value, dict):
        return result
    for key in _COUNTERS:
        number = value.get(key)
        if type(number) is int and 0 <= number <= 2**53 - 1:
            result[key] = number
    for key in _TIMES + _RMS:
        number = value.get(key)
        limit = 1 if key in _RMS else 2**53 - 1
        if type(number) in (int, float) and 0 <= number <= limit and math.isfinite(number):
            result[key] = number
    return result


class _AudioFault(Exception):
    def __init__(self, reason, *, value=None, retryable=False, native=False):
        self.reason = reason if isinstance(reason, str) and reason in _REASONS else "processing_error"
        self.value = _safe_numbers(value)
        if isinstance(value, dict):
            state = value.get("failure_state", value.get("state"))
            if state in ("buffering", "processing", "bypassed", "stopped", "unavailable"):
                self.value["failure_state"] = state
        self.retryable, self.native = retryable, native


class ChromeAudioRuntime:
    def __init__(self, *, connection_factory, source_guard, generation, url, models_dir,
                 on_status, processor_factory=ChromeAudioProcessor, clock=None, wait=None):
        self._connect, self._guard = connection_factory, source_guard
        self.generation, self.url = generation, url
        self._on_status, self._factory = on_status, processor_factory
        self._models_dir = models_dir
        self._nonce = secrets.token_hex(16)
        self._stop = threading.Event()
        self._clock = clock or (lambda: time.monotonic())
        self._wait = wait or (lambda seconds: self._stop.wait(seconds))
        self._connection = self._thread = None
        self._worker_active = False
        self._last_public = None
        self._last_report = 0.0
        self._processed = 0
        self._elapsed_ms = 0.0
        self._max_elapsed_ms = 0.0
        self._installed = False
        self._last_fault_reason = ""
        self._last_fault_state = ""
        self._error_count = self._recovery_count = self._recovery_attempt = 0
        self._backoff_step = self._native_recovery_attempts = 0
        self._retry_in_seconds = 0
        self._retry_allowed = self._retry_exhausted = False
        self._totals = {key: 0 for key in ("captured_frames", "played_frames", "dropped_frames")}
        self._session_counts = dict(self._totals)
        self._last_progress = None
        self._last_audio_frame_time = None

    @property
    def is_running(self):
        return not self._stop.is_set() and (self._worker_active or
            self._thread is not None and self._thread.is_alive())

    @property
    def alive(self):
        return self.is_running

    def _source_valid(self):
        try:
            return bool(self._guard())
        except Exception:
            return False

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="chrome-audio-gtcrn")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        # Wake a timed-out transport without holding the browser's sending lock.
        if self._thread and self._thread.is_alive() and self._connection:
            try:
                self._connection.close()
            except Exception:
                pass

    def _report(self, state, reason, value=None):
        if state not in {"loading", "waiting", "buffering", "processing", "bypassed", "recovering"}:
            state = "bypassed"
        if not isinstance(reason, str) or reason not in _REASONS:
            reason = "processing_error"
        safe = _safe_numbers(value)
        now = self._clock()
        public = (state, reason, self._recovery_attempt, self._recovery_count, self._retry_in_seconds)
        if public == self._last_public and now - self._last_report < 2:
            return
        self._last_public, self._last_report = public, now
        status = {"mode": "gtcrn", "state": state, "reason": reason,
                  "processed_frames": self._processed,
                  "processing_mean_ms": self._elapsed_ms / max(1, self._processed),
                  "processing_max_ms": self._max_elapsed_ms,
                  "frame_ms": 200, "prebuffer_ms": 400,
                  "processed_only_verified": False,
                  "recovery_count": self._recovery_count, "recovery_attempt": self._recovery_attempt,
                  "error_count": self._error_count, "retry_in_seconds": self._retry_in_seconds,
                  "retry_delay_seconds": self._retry_in_seconds,
                  "recovery_allowed": self._retry_allowed, "retry_exhausted": self._retry_exhausted,
                  "native_recovery_attempts": min(3, self._native_recovery_attempts),
                  "last_fault_reason": self._last_fault_reason, "last_fault_state": self._last_fault_state}
        status.update(safe)
        for key in self._totals:
            if key in safe:
                status[key] = (self._totals[key] + safe[key] if self._installed else
                               max(self._totals[key], safe[key]))
                status["session_" + key] = safe[key]
            elif self._totals[key]:
                status[key] = self._totals[key]
        if self._last_progress is not None:
            status["last_progress_age_ms"] = max(0.0, (now - self._last_progress) * 1000)
        if self._last_audio_frame_time is not None:
            status["last_audio_frame_time"] = self._last_audio_frame_time
        self._on_status(status)

    def _observe(self, value):
        safe = _safe_numbers(value)
        for key in self._session_counts:
            self._session_counts[key] = max(self._session_counts[key], safe.get(key, 0))
        if "last_input_wall_ms" in safe:
            self._last_audio_frame_time = safe["last_input_wall_ms"]
        return value

    def _evaluate(self, script, *, await_promise=False):
        if self._stop.is_set() or not self._source_valid():
            raise ChromeControlError("source_changed")
        result = self._connection.call("Runtime.evaluate", {"expression": script,
            "awaitPromise": await_promise, "returnByValue": True, "userGesture": True,
            "timeout": 1200}, timeout=1.5)
        if self._stop.is_set() or not self._source_valid():
            raise ChromeControlError("source_changed")
        value = result.get("result", {}).get("value")
        if "exceptionDetails" in result or not isinstance(value, dict):
            raise ChromeControlError("audio_script_unavailable")
        # Bypass/failure replies also require identity checks. An old session
        # must never restart or stop a new page graph with the same room number.
        if isinstance(value.get("reason"), str) and value.get("reason") in _IDENTITY_FAULTS:
            raise ChromeControlError("source_changed")
        if (value.get("session_nonce") != self._nonce or value.get("generation") != self.generation):
            raise ChromeControlError("source_changed")
        return self._observe(value)

    def _page_fault(self, value, default):
        reason = next((value.get(key) for key in ("failure_reason", "reason", "error")
                       if isinstance(value.get(key), str) and
                       value.get(key) in _GRAPH_FAULTS | _IDENTITY_FAULTS), None)
        return _AudioFault(reason or default, value=value,
                           retryable=reason in _GRAPH_FAULTS or reason is None and default == "audio_output_rejected")

    def _run_session(self, processor):
        deadline = self._clock() + 30
        while not self._stop.is_set():
            value = self._evaluate(install_script(self._nonce, self.generation, self.url), await_promise=True)
            state, reason = value.get("state"), value.get("reason")
            if state in ("buffering", "processing"):
                self._installed = True
                break
            self._report("waiting", reason if reason == "media_not_playing" else "audio_unavailable", value)
            if reason != "media_not_playing":
                raise self._page_fault(value, "audio_unavailable")
            if self._clock() >= deadline:
                raise _AudioFault("audio_unavailable")
            self._wait(.25)
        stable_started = None
        recovered = False
        submitted = False
        while not self._stop.is_set() and self._installed:
            value = self._evaluate(pull_script(self._nonce, self.generation, max_frames=8))
            if value.get("state") not in ("buffering", "processing"):
                raise self._page_fault(value, "audio_stream_unavailable")
            frames = value.get("frames", [])
            if not isinstance(frames, list) or len(frames) > 8:
                raise _AudioFault("processing_error")
            output = []
            for frame in frames:
                if self._stop.is_set() or not self._source_valid():
                    raise ChromeControlError("source_changed")
                decoded = decode_pcm_frame(frame)
                if len(decoded.samples) != 3200:
                    raise _AudioFault("processing_error")
                try:
                    processed = processor.process(decoded.samples, sequence=decoded.sequence,
                        start_sample=decoded.start_sample, stream_id=self._nonce)
                except Exception as exc:
                    # Identity/sequence corruption is not a native transient.
                    if isinstance(exc, ValueError):
                        raise _AudioFault("processing_error") from None
                    raise _AudioFault("processing_error", retryable=True, native=True) from None
                if processed.metadata.get("action") != "enhanced":
                    reason = processed.metadata.get("reason", "processing_error")
                    raise _AudioFault(reason, retryable=reason in _NATIVE_FAULTS, native=reason in _NATIVE_FAULTS)
                elapsed = processed.metadata.get("elapsed_ms")
                if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
                    raise _AudioFault("processing_error")
                self._processed += 1
                self._elapsed_ms += elapsed
                self._max_elapsed_ms = max(self._max_elapsed_ms, elapsed)
                output.append({"sequence": decoded.sequence, "start_sample": decoded.start_sample,
                               "pcm_s16le": encode_pcm_samples(processed.samples)})
            if output:
                value = self._evaluate(push_script(self._nonce, self.generation, output))
                if (value.get("accepted_frames") != len(output) or
                        value.get("state") not in ("buffering", "processing")):
                    raise self._page_fault(value, "audio_output_rejected")
                now = self._clock()
                if stable_started is None or self._last_progress is None or now - self._last_progress > 2:
                    stable_started = now
                self._last_progress = now
                submitted = True
                if now - stable_started >= 10:
                    self._backoff_step = self._native_recovery_attempts = 0
            if submitted and value.get("state") == "processing" and self._recovery_attempt and not recovered:
                self._recovery_count += 1
                recovered = True
            self._retry_allowed, self._retry_in_seconds = False, 0
            self._report(value.get("state", "buffering"), "processed_audio_experimental", value)
            self._wait(.05)

    @staticmethod
    def _close_processor(processor):
        if processor:
            try:
                processor.close()
            except Exception:
                pass

    def _end_session(self, fault=None):
        connection, self._connection = self._connection, None
        if connection:
            if self._installed and not (fault and fault.reason in _IDENTITY_FAULTS):
                try:
                    if self._source_valid():
                        connection.call("Runtime.evaluate", {
                            "expression": stop_script(self._nonce, self.generation,
                                reason=fault.reason if fault else "disabled"),
                            "returnByValue": True, "timeout": 500}, timeout=.75)
                        # Cleanup never adopts counters or an identity after a
                        # concurrent navigation. The JS command is nonce fenced.
                        self._source_valid()
                except Exception:
                    pass
            try:
                connection.close()
            except Exception:
                pass
        for key in self._totals:
            self._totals[key] += self._session_counts[key]
            self._session_counts[key] = 0
        self._installed = False

    def _run(self):
        processor = None
        native_fault = False
        self._worker_active = True
        try:
            while not self._stop.is_set():
                fault = None
                try:
                    if not self._source_valid():
                        raise ChromeControlError("source_changed")
                    if processor is None or native_fault:
                        self._close_processor(processor)
                        processor = None
                        self._report("loading", "model_loading")
                        try:
                            processor = self._factory(self._models_dir, mode="gtcrn", num_threads=1)
                            information = processor.preload()
                        except ValueError:
                            raise _AudioFault("model_invalid") from None
                        except Exception:
                            raise _AudioFault("load_error", retryable=True, native=True) from None
                    else:
                        reset = getattr(processor, "reset", None)
                        try:
                            information = reset(stream_id=self._nonce) if reset else processor.preload()
                        except ValueError:
                            raise _AudioFault("model_invalid") from None
                        except Exception:
                            raise _AudioFault("reset_error", retryable=True, native=True) from None
                    if not isinstance(information, dict) or not information.get("ready"):
                        reason = information.get("reason", "model_unavailable") if isinstance(information, dict) else "model_unavailable"
                        raise _AudioFault(reason, retryable=reason in _NATIVE_FAULTS, native=reason in _NATIVE_FAULTS)
                    if self._stop.is_set() or not self._source_valid():
                        raise ChromeControlError("source_changed")
                    self._connection = self._connect()
                    self._run_session(processor)
                    break
                except _AudioFault as exc:
                    fault = exc
                except Exception as exc:
                    code = getattr(exc, "code", "processing_error")
                    if code in _IDENTITY_FAULTS:
                        code = "source_changed"
                    fault = _AudioFault(code if code in _REASONS else "processing_error",
                                        retryable=code in _TRANSPORT_FAULTS)
                finally:
                    self._end_session(fault)
                if self._stop.is_set():
                    break
                self._error_count += 1
                self._last_fault_reason = fault.reason
                self._last_fault_state = fault.value.get("failure_state", "bypassed")
                if fault.native:
                    self._native_recovery_attempts += 1
                self._retry_exhausted = fault.native and self._native_recovery_attempts > 3
                self._retry_allowed = fault.retryable and not self._retry_exhausted and self._source_valid()
                self._retry_in_seconds = min(8, 2 ** min(self._backoff_step, 3)) if self._retry_allowed else 0
                self._report("recovering" if self._retry_allowed else "bypassed", fault.reason, fault.value)
                if not self._retry_allowed:
                    break
                # Raw audio stays audible while waiting. No page reload, source
                # opening, replay, VAD dropping, or old-frame retry occurs here.
                self._wait(self._retry_in_seconds)
                if self._stop.is_set() or not self._source_valid():
                    if not self._stop.is_set():
                        self._retry_allowed, self._retry_in_seconds = False, 0
                        self._report("bypassed", "source_changed")
                    break
                self._backoff_step += 1
                self._recovery_attempt += 1
                self._retry_in_seconds = 0
                self._nonce = secrets.token_hex(16)
                native_fault = fault.native
        finally:
            self._end_session()
            self._close_processor(processor)
            self._worker_active = False
            self._retry_allowed, self._retry_in_seconds = False, 0
            if self._last_public and self._last_public[0] == "recovering":
                self._report("bypassed", self._last_fault_reason)

