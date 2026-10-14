"""Bounded, clock-preserving preprocessing for the owned Chrome audio graph.

GTCRN uses the native online state, not independent offline utterances. This
module never removes silence or applies VAD, and it cannot identify a speaker.
The caller owns the single worker and bounded transport queues. UVR remains
available in the standard recognition pipeline: its offline minimum window is
unsuitable for this graph's 400 ms playback prebuffer.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from pathlib import Path
import threading
import time
from typing import Any, Callable

import numpy as np

from .audio_processing import DRY_RATIO, SAMPLE_RATE, WET_RATIO, ProcessingResult
from .audio_processing_models import (PROCESSING_MODES, processing_model_entry,
                                      processing_model_path, verify_processing_model)

FRAME_SAMPLES = 3200
MAX_FRAME_SAMPLES = FRAME_SAMPLES
MAX_SAFE_INTEGER = 2**53 - 1
MAX_PCM_BASE64_CHARACTERS = ((MAX_FRAME_SAMPLES * 2 + 2) // 3) * 4


def _integer(value: object, reason: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_SAFE_INTEGER:
        raise ValueError(reason)
    return value


@dataclass(frozen=True)
class ChromeAudioFrame:
    sequence: int
    start_sample: int
    samples: np.ndarray


def decode_pcm_frame(payload: object) -> ChromeAudioFrame:
    """Decode the browser's bounded mono PCM16 payload without retaining it."""
    if not isinstance(payload, dict):
        raise ValueError("invalid_pcm_frame")
    sequence = _integer(payload.get("sequence"), "invalid_audio_sequence")
    start_sample = _integer(payload.get("start_sample"), "invalid_audio_clock")
    encoded = payload.get("pcm_s16le")
    if (not isinstance(encoded, str) or not encoded
            or len(encoded) > MAX_PCM_BASE64_CHARACTERS):
        raise ValueError("invalid_pcm_payload")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid_pcm_payload") from exc
    if not raw or len(raw) % 2 or len(raw) > MAX_FRAME_SAMPLES * 2:
        raise ValueError("invalid_pcm_payload")
    if start_sample + len(raw) // 2 > MAX_SAFE_INTEGER:
        raise ValueError("invalid_audio_clock")
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / np.float32(32768)
    return ChromeAudioFrame(sequence, start_sample, samples)


def encode_pcm_samples(samples: np.ndarray) -> str:
    source = np.asarray(samples, dtype=np.float32)
    if (source.ndim != 1 or not 0 < source.size <= MAX_FRAME_SAMPLES
            or not np.isfinite(source).all()):
        raise ValueError("invalid_pcm_samples")
    # Use the inverse of the browser decoder, including the signed -32768 edge.
    pcm = np.rint(np.clip(source, -1, 32767 / 32768) * np.float32(32768)).astype("<i2")
    return base64.b64encode(pcm.tobytes()).decode("ascii")


def _create_online_backend(mode: str, model: Path, num_threads: int):
    import sherpa_onnx

    if mode != "gtcrn":
        raise ValueError("unsupported_realtime")
    config = sherpa_onnx.OnlineSpeechDenoiserConfig(
        model=sherpa_onnx.OfflineSpeechDenoiserModelConfig(
            gtcrn=sherpa_onnx.OfflineSpeechDenoiserGtcrnModelConfig(model=str(model)),
            num_threads=num_threads, debug=False, provider="cpu"))
    if not config.validate():
        raise ValueError("invalid_native_config")
    return sherpa_onnx.OnlineSpeechDenoiser(config)


class ChromeAudioProcessor:
    """Synchronous single-stream adapter; one call consumes one transport frame.

    A two-hop fixed delay absorbs GTCRN's STFT tail without time stretching.
    Both wet and dry paths share that delay, so successive frames remain aligned.
    Source changes and missing frames require ``reset`` rather than joining audio
    from different streams. There is deliberately no unbounded worker queue.
    """

    def __init__(self, models_dir: Path, mode: str = "gtcrn", num_threads: int = 1, *,
                 backend_factory: Callable = _create_online_backend,
                 verifier: Callable = verify_processing_model,
                 clock: Callable[[], float] = time.perf_counter):
        if mode not in PROCESSING_MODES:
            raise ValueError("invalid_audio_processing_mode")
        if type(num_threads) is not int or not 1 <= num_threads <= 2:
            raise ValueError("invalid_audio_processing_threads")
        self.models_dir, self.mode, self.num_threads = Path(models_dir), mode, num_threads
        self._factory, self._verifier, self._clock = backend_factory, verifier, clock
        self._lock = threading.RLock()
        self._native = None
        self._closed = False
        self._attempted = False
        self._last_identity = None
        self._state = "off" if mode == "off" else "unloaded"
        self._reason = "disabled" if mode == "off" else "not_loaded"
        self._error_type = ""
        self._startup_ms = 0.0
        self._latency_samples = 0
        self._wet = np.empty(0, np.float32)
        self._dry = np.empty(0, np.float32)
        self._stream_id: str | None = None
        self._next_sequence: int | None = None
        self._next_sample: int | None = None
        self._slow_frames = 0
        self._processed_frames = 0

    @property
    def backend_info(self) -> dict[str, Any]:
        with self._lock:
            entry = None if self.mode == "off" else processing_model_entry(self.mode)
            return {"backend": "sherpa_onnx_online", "mode": self.mode,
                    "model_id": "" if entry is None else entry.id,
                    "model_sha256": "" if entry is None else entry.files[0].sha256,
                    "state": self._state, "ready": self._state in ("ready", "off"),
                    "reason": self._reason, "error_type": self._error_type,
                    "provider": "cpu", "num_threads": self.num_threads,
                    "sample_rate": SAMPLE_RATE, "frame_samples": FRAME_SAMPLES,
                    "algorithmic_delay_samples": self._latency_samples,
                    "algorithmic_delay_ms": self._latency_samples / SAMPLE_RATE * 1000,
                    "startup_ms": self._startup_ms, "processed_frames": self._processed_frames,
                    "supports_speaker_selection": False}

    def _file_identity(self):
        try:
            stat = processing_model_path(self.models_dir, self.mode).stat()
            return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns
        except OSError:
            return None

    def _clear_buffers(self) -> None:
        self._wet = np.zeros(self._latency_samples, np.float32)
        self._dry = np.zeros(self._latency_samples, np.float32)
        self._slow_frames = 0

    def _load(self) -> None:
        if self._closed or self.mode == "off" or self._native is not None:
            return
        identity = self._file_identity()
        if self._attempted and identity == self._last_identity:
            return
        self._attempted, self._last_identity = True, identity
        started = self._clock()
        try:
            verification = self._verifier(self.models_dir, self.mode)
            if not verification.ready:
                self._state = "missing" if verification.state in ("missing", "partial") else "invalid"
                self._reason = "model_missing" if self._state == "missing" else "model_invalid"
                self._error_type = ""
                return
            if self.mode == "uvr":
                self._state, self._reason = "unsupported_realtime", "unsupported_realtime"
                return
            native = self._factory(self.mode, processing_model_path(self.models_dir, self.mode),
                                   self.num_threads)
            if native is None or type(native.sample_rate) is not int or native.sample_rate != SAMPLE_RATE:
                raise ValueError("invalid_native_sample_rate")
            shift = native.frame_shift_in_samples
            if type(shift) is not int or not 1 <= shift <= FRAME_SAMPLES // 2:
                raise ValueError("invalid_native_frame_shift")
            self._native = native
            self._latency_samples = shift * 2
            self._clear_buffers()
            self._state, self._reason, self._error_type = "ready", "ready", ""
        except Exception as exc:
            self._native = None
            self._state, self._reason, self._error_type = "error", "load_error", type(exc).__name__
        finally:
            self._startup_ms = max(0.0, (self._clock() - started) * 1000)

    def preload(self) -> dict[str, Any]:
        with self._lock:
            self._load()
            return self.backend_info

    def reset(self, stream_id: str | None = None) -> dict[str, Any]:
        if stream_id is not None and (not isinstance(stream_id, str) or not 0 < len(stream_id) <= 128):
            raise ValueError("invalid_audio_stream")
        with self._lock:
            if self._native is not None:
                try:
                    self._native.reset()
                except Exception as exc:
                    self._native = None
                    self._state, self._reason, self._error_type = "error", "reset_error", type(exc).__name__
            self._stream_id = stream_id
            self._next_sequence = self._next_sample = None
            self._processed_frames = 0
            self._clear_buffers()
            return self.backend_info

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._native = None
            self._state, self._reason, self._error_type = "closed", "closed", ""
            self._stream_id = None
            self._next_sequence = self._next_sample = None
            self._latency_samples = 0
            self._clear_buffers()

    def _result(self, source: np.ndarray, samples: np.ndarray, action: str, reason: str,
                started: float, sequence: int, start_sample: int) -> ProcessingResult:
        elapsed_ms = max(0.0, (self._clock() - started) * 1000)
        duration_ms = len(source) / SAMPLE_RATE * 1000
        delay_samples = self._latency_samples if action == "enhanced" else 0
        metadata = {"mode": self.mode, "action": action, "reason": reason,
                    "sequence": sequence, "start_sample": start_sample,
                    "input_samples": len(source), "output_samples": len(samples),
                    "sample_rate": SAMPLE_RATE, "elapsed_ms": elapsed_ms,
                    "algorithmic_delay_samples": delay_samples,
                    "algorithmic_delay_ms": delay_samples / SAMPLE_RATE * 1000,
                    "realtime_factor": elapsed_ms / duration_ms,
                    "over_budget": elapsed_ms > duration_ms,
                    "backend_info": self.backend_info, "error_type": self._error_type}
        return ProcessingResult(np.ascontiguousarray(samples, dtype=np.float32).copy(), metadata)

    def process(self, samples: np.ndarray, *, sequence: int, start_sample: int,
                stream_id: str, sample_rate: int = SAMPLE_RATE) -> ProcessingResult:
        sequence = _integer(sequence, "invalid_audio_sequence")
        start_sample = _integer(start_sample, "invalid_audio_clock")
        if not isinstance(stream_id, str) or not 0 < len(stream_id) <= 128:
            raise ValueError("invalid_audio_stream")
        if type(sample_rate) is not int or sample_rate != SAMPLE_RATE:
            raise ValueError("invalid_audio_sample_rate")
        source = np.asarray(samples, dtype=np.float32)
        if (source.ndim != 1 or not 0 < source.size <= MAX_FRAME_SAMPLES
                or not np.isfinite(source).all() or np.max(np.abs(source)) > 1):
            raise ValueError("invalid_audio_samples")
        if start_sample + len(source) > MAX_SAFE_INTEGER:
            raise ValueError("invalid_audio_clock")
        source = np.ascontiguousarray(source).copy()
        started = self._clock()
        with self._lock:
            if self._closed:
                return self._result(source, source, "bypass", "closed", started, sequence, start_sample)
            if self._stream_id is not None and stream_id != self._stream_id:
                raise ValueError("audio_stream_changed")
            if self._next_sequence is not None and sequence != self._next_sequence:
                raise ValueError("audio_sequence_discontinuity")
            if self._next_sample is not None and start_sample != self._next_sample:
                raise ValueError("audio_clock_discontinuity")
            self._stream_id = stream_id
            self._next_sequence, self._next_sample = sequence + 1, start_sample + len(source)
            if self.mode == "off":
                return self._result(source, source, "off", "disabled", started, sequence, start_sample)
            self._load()
            if self._native is None:
                return self._result(source, source, "bypass", self._reason, started, sequence, start_sample)
            try:
                output = self._native.run(source, SAMPLE_RATE)
                processed = np.asarray(output.samples, dtype=np.float32)
                if (type(output.sample_rate) is not int or output.sample_rate != SAMPLE_RATE
                        or processed.ndim != 1 or not np.isfinite(processed).all()
                        or len(processed) > len(source) + self._latency_samples):
                    raise ValueError("invalid_native_output")
                self._wet = np.concatenate((self._wet, processed))
                self._dry = np.concatenate((self._dry, source))
                if (len(self._wet) < len(source)
                        or len(self._wet) > len(source) + self._latency_samples * 2):
                    raise ValueError("invalid_native_clock")
                result = np.clip(WET_RATIO * self._wet[:len(source)]
                                 + DRY_RATIO * self._dry[:len(source)], -1, 1)
                self._wet, self._dry = self._wet[len(source):], self._dry[len(source):]
                elapsed_ms = (self._clock() - started) * 1000
                self._slow_frames = self._slow_frames + 1 if elapsed_ms > len(source) / SAMPLE_RATE * 1000 else 0
                if self._slow_frames >= 3:
                    self._native = None
                    self._state, self._reason = "too_slow", "processing_too_slow"
                    self._clear_buffers()
                    return self._result(source, source, "bypass", self._reason, started, sequence, start_sample)
                self._processed_frames += 1
                return self._result(source, result, "enhanced", "processed", started, sequence, start_sample)
            except Exception as exc:
                self._native = None
                self._state, self._reason, self._error_type = "error", "processing_error", type(exc).__name__
                self._clear_buffers()
                return self._result(source, source, "bypass", self._reason, started, sequence, start_sample)
