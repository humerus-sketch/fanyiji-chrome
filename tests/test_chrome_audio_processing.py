from types import SimpleNamespace
import base64
import threading

import numpy as np
import pytest

from fanyiji.chrome_audio_processing import (
    ChromeAudioProcessor, FRAME_SAMPLES, MAX_PCM_BASE64_CHARACTERS, MAX_SAFE_INTEGER,
    decode_pcm_frame, encode_pcm_samples)
from fanyiji.audio_processing_models import processing_model_path
from fanyiji.models_download import VerificationResult


def ready(*args):
    return VerificationResult(True, "ready", "verified")


class Native:
    sample_rate = 16000
    frame_shift_in_samples = 256

    def __init__(self):
        self.calls = []
        self.resets = 0

    def run(self, samples, rate):
        self.calls.append(samples.copy())
        return SimpleNamespace(sample_rate=rate, samples=samples * np.float32(.5))

    def reset(self):
        self.resets += 1


def create(tmp_path, native=None, **kwargs):
    backend = native or Native()
    return ChromeAudioProcessor(tmp_path, backend_factory=lambda *args: backend,
                                verifier=ready, **kwargs), backend


def process(processor, samples, seq=0, *, stream="room-a", start=None):
    return processor.process(samples, sequence=seq,
                             start_sample=seq * FRAME_SAMPLES if start is None else start,
                             stream_id=stream)


def test_pcm16_roundtrip_all_signed_values_and_endian():
    values = np.array([-32768, -12345, -1, 0, 1, 12345, 32767], dtype="<i2")
    frame = decode_pcm_frame({"sequence": 8, "start_sample": 25600,
                              "pcm_s16le": base64.b64encode(values.tobytes()).decode("ascii")})
    assert frame.sequence == 8 and frame.start_sample == 25600
    assert frame.samples.dtype == np.float32
    assert encode_pcm_samples(frame.samples) == base64.b64encode(values.tobytes()).decode("ascii")


@pytest.mark.parametrize("value", [None, [], {}, {"sequence": True, "start_sample": 0, "pcm_s16le": "AAA="},
    {"sequence": 0, "start_sample": -1, "pcm_s16le": "AAA="},
    {"sequence": 0, "start_sample": 0, "pcm_s16le": ""},
    {"sequence": 0, "start_sample": 0, "pcm_s16le": "not base64"},
    {"sequence": 0, "start_sample": 0, "pcm_s16le": "AAA=\n"},
    {"sequence": 0, "start_sample": 0, "pcm_s16le": "AA=="},
    {"sequence": 0, "start_sample": 0, "pcm_s16le": "A" * (MAX_PCM_BASE64_CHARACTERS + 4)},
    {"sequence": 0, "start_sample": MAX_SAFE_INTEGER, "pcm_s16le": "AAA="}])
def test_untrusted_pcm_payload_is_bounded_and_strict(value):
    with pytest.raises(ValueError):
        decode_pcm_frame(value)


@pytest.mark.parametrize("value", [[], [[0]], [np.nan], [np.inf], np.zeros(FRAME_SAMPLES + 1)])
def test_pcm_encoder_rejects_nonfinite_or_oversize(value):
    with pytest.raises(ValueError):
        encode_pcm_samples(value)


def test_pcm_encoder_clips_without_wrapping():
    output = decode_pcm_frame({"sequence": 0, "start_sample": 0,
                               "pcm_s16le": encode_pcm_samples(np.array([-2, 2], np.float32))})
    np.testing.assert_array_equal(output.samples, [-1, 32767 / 32768])


def test_continuous_native_history_and_both_paths_share_exact_fixed_delay(tmp_path):
    processor, native = create(tmp_path)
    inputs = np.linspace(-.5, .5, FRAME_SAMPLES * 10, dtype=np.float32)
    original = inputs.copy()
    outputs = []
    for seq in range(10):
        result = process(processor, inputs[seq * FRAME_SAMPLES:(seq + 1) * FRAME_SAMPLES], seq)
        assert result.metadata["action"] == "enhanced"
        assert result.metadata["input_samples"] == result.metadata["output_samples"] == FRAME_SAMPLES
        assert result.metadata["sequence"] == seq
        assert result.metadata["start_sample"] == seq * FRAME_SAMPLES
        assert result.metadata["backend_info"]["algorithmic_delay_ms"] == 32
        assert not result.metadata["backend_info"]["supports_speaker_selection"]
        outputs.append(result.samples)
        assert len(processor._wet) <= 1024 and len(processor._dry) == 512
    joined = np.concatenate(outputs)
    np.testing.assert_array_equal(joined[:512], np.zeros(512, np.float32))
    np.testing.assert_allclose(joined[512:], inputs[:-512] * .6, atol=1e-7)
    np.testing.assert_array_equal(original, inputs)
    assert len(native.calls) == 10 and native.resets == 0


def test_native_variable_frame_quantization_never_time_stretches_or_loses_sample(tmp_path):
    class Quantized(Native):
        def __init__(self):
            super().__init__()
            self.buffer = np.zeros(256, np.float32)
        def run(self, samples, rate):
            self.buffer = np.concatenate((self.buffer, samples))
            count = max(0, (len(self.buffer) - 512) // 256 * 256)
            result, self.buffer = self.buffer[:count], self.buffer[count:]
            return SimpleNamespace(sample_rate=rate, samples=result)
    processor, _ = create(tmp_path, Quantized())
    audio = np.zeros(FRAME_SAMPLES * 20, np.float32)
    audio[8000] = .5
    output = np.concatenate([process(processor, audio[i:i + FRAME_SAMPLES], i // FRAME_SAMPLES).samples
                             for i in range(0, len(audio), FRAME_SAMPLES)])
    # The fake native also has one-hop delay; wet and dry remain deterministic.
    assert output[8000 + 512] == pytest.approx(.1)
    assert output[8000 + 512 + 256] == pytest.approx(.4)
    assert len(output) == len(audio)


@pytest.mark.parametrize("kwargs", [{"sequence": True}, {"sequence": -1}, {"sequence": 1.5},
    {"sequence": MAX_SAFE_INTEGER + 1}, {"start_sample": -1}, {"start_sample": MAX_SAFE_INTEGER},
    {"stream_id": ""}, {"stream_id": "x" * 129}, {"sample_rate": 48000}, {"sample_rate": True}])
def test_invalid_frame_identity_never_reaches_native(tmp_path, kwargs):
    processor, native = create(tmp_path)
    params = {"sequence": 0, "start_sample": 0, "stream_id": "room-a", **kwargs}
    with pytest.raises(ValueError):
        processor.process(np.zeros(FRAME_SAMPLES, np.float32), **params)
    assert not native.calls


@pytest.mark.parametrize("source", [np.zeros(0), np.zeros((2, 100)), np.zeros(FRAME_SAMPLES + 1),
    np.array([np.nan]), np.array([np.inf]), np.array([1.01]), np.array([-1.01])])
def test_invalid_audio_never_loads_or_advances_stream(tmp_path, source):
    processor, native = create(tmp_path)
    with pytest.raises(ValueError, match="invalid_audio_samples"):
        process(processor, source)
    assert processor.backend_info["state"] == "unloaded"
    assert not native.calls
    assert process(processor, np.zeros(FRAME_SAMPLES, np.float32)).metadata["action"] == "enhanced"


def test_source_sequence_and_clock_discontinuity_require_explicit_reset(tmp_path):
    processor, native = create(tmp_path)
    process(processor, np.full(FRAME_SAMPLES, .5, np.float32))
    with pytest.raises(ValueError, match="audio_stream_changed"):
        process(processor, np.zeros(FRAME_SAMPLES, np.float32), 1, stream="room-b")
    with pytest.raises(ValueError, match="audio_sequence_discontinuity"):
        process(processor, np.zeros(FRAME_SAMPLES, np.float32), 2)
    with pytest.raises(ValueError, match="audio_clock_discontinuity"):
        process(processor, np.zeros(FRAME_SAMPLES, np.float32), 1, start=123)
    process(processor, np.zeros(FRAME_SAMPLES, np.float32), 1)
    assert native.resets == 0
    processor.reset("room-b")
    result = process(processor, np.zeros(FRAME_SAMPLES, np.float32), 5, stream="room-b")
    assert native.resets == 1 and not result.samples.any()


@pytest.mark.parametrize("mode", ["off", "uvr"])
def test_disabled_or_offline_uvr_never_start_native_and_do_not_claim_enhanced(tmp_path, mode):
    def forbidden(*args):
        pytest.fail("offline UVR cannot run on the realtime playback worker")
    processor = ChromeAudioProcessor(tmp_path, mode, verifier=ready, backend_factory=forbidden)
    info = processor.preload()
    source = np.full(FRAME_SAMPLES, .25, np.float32)
    result = process(processor, source)
    np.testing.assert_array_equal(result.samples, source)
    assert result.metadata["action"] == ("off" if mode == "off" else "bypass")
    assert result.metadata["reason"] == ("disabled" if mode == "off" else "unsupported_realtime")
    assert info["ready"] == (mode == "off")


def test_missing_invalid_model_explicit_bypass_and_download_can_recover(tmp_path):
    native = Native()
    processor = ChromeAudioProcessor(tmp_path, backend_factory=lambda *args: native)
    assert processor.preload()["state"] == "missing"
    result = process(processor, np.full(FRAME_SAMPLES, .25, np.float32))
    assert result.metadata["action"] == "bypass" and result.metadata["reason"] == "model_missing"
    target = processing_model_path(tmp_path, "gtcrn")
    target.parent.mkdir()
    target.write_bytes(b"not the pinned model")
    assert processor.preload()["state"] == "invalid"
    assert not native.calls
    processor._verifier = ready
    target.write_bytes(b"new model identity")
    assert processor.preload()["state"] == "ready"
    assert process(processor, np.zeros(FRAME_SAMPLES, np.float32), 1).metadata["action"] == "enhanced"


@pytest.mark.parametrize("native", [SimpleNamespace(sample_rate=48000, frame_shift_in_samples=256),
    SimpleNamespace(sample_rate=16000, frame_shift_in_samples=True),
    SimpleNamespace(sample_rate=16000, frame_shift_in_samples=0),
    SimpleNamespace(sample_rate=16000, frame_shift_in_samples=3200)])
def test_invalid_native_clock_is_explicit_load_error(tmp_path, native):
    processor, _ = create(tmp_path, native)
    assert processor.preload()["state"] == "error"
    assert process(processor, np.zeros(FRAME_SAMPLES, np.float32)).metadata["reason"] == "load_error"


@pytest.mark.parametrize("output", [SimpleNamespace(sample_rate=48000, samples=np.zeros(FRAME_SAMPLES)),
    SimpleNamespace(sample_rate=16000, samples=np.zeros((2, FRAME_SAMPLES))),
    SimpleNamespace(sample_rate=16000, samples=np.full(FRAME_SAMPLES, np.nan)),
    SimpleNamespace(sample_rate=16000, samples=np.zeros(FRAME_SAMPLES + 1024)),
    SimpleNamespace(sample_rate=16000, samples=np.zeros(0))])
def test_bad_native_output_fails_open_once_without_unbounded_retry(tmp_path, output):
    class Bad(Native):
        def run(self, samples, rate):
            self.calls.append(samples)
            return output
    processor, native = create(tmp_path, Bad())
    source = np.full(FRAME_SAMPLES, .25, np.float32)
    result = process(processor, source)
    assert result.metadata["action"] == "bypass" and result.metadata["reason"] == "processing_error"
    np.testing.assert_array_equal(result.samples, source)
    process(processor, source, 1)
    assert len(native.calls) == 1
    assert len(processor._wet) <= 512 and len(processor._dry) <= 512


def test_processing_failure_metadata_never_contains_exception_text_or_source_id(tmp_path):
    class Bad(Native):
        def run(self, samples, rate):
            raise RuntimeError("private/path cookie=SECRET")
    processor, _ = create(tmp_path, Bad())
    result = process(processor, np.zeros(FRAME_SAMPLES, np.float32), stream="secret-room-id")
    assert result.metadata["error_type"] == "RuntimeError"
    assert "SECRET" not in repr(result.metadata) and "private" not in repr(result.metadata)
    assert "secret-room-id" not in repr(result.metadata)


def test_three_continuously_slow_frames_stop_processing_instead_of_accumulating_latency(tmp_path):
    now = [0.0]
    class Slow(Native):
        def run(self, samples, rate):
            now[0] += .21
            return super().run(samples, rate)
    processor, native = create(tmp_path, Slow(), clock=lambda: now[0])
    processor.preload()
    source = np.full(FRAME_SAMPLES, .25, np.float32)
    for seq in range(2):
        result = process(processor, source, seq)
        assert result.metadata["action"] == "enhanced" and result.metadata["over_budget"]
    result = process(processor, source, 2)
    assert result.metadata["action"] == "bypass" and result.metadata["reason"] == "processing_too_slow"
    np.testing.assert_array_equal(result.samples, source)
    process(processor, source, 3)
    assert len(native.calls) == 3


def test_close_waits_for_inflight_processing_and_never_reopens(tmp_path):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    class Blocking(Native):
        def run(self, samples, rate):
            entered.set()
            assert release.wait(2)
            return super().run(samples, rate)
    processor, _ = create(tmp_path, Blocking())
    worker = threading.Thread(target=lambda: process(processor, np.zeros(FRAME_SAMPLES, np.float32)))
    worker.start()
    assert entered.wait(2)
    closer = threading.Thread(target=lambda: (processor.close(), closed.set()))
    closer.start()
    assert not closed.wait(.02)
    release.set()
    worker.join(2)
    closer.join(2)
    assert closed.is_set() and processor.preload()["state"] == "closed"
    processor.reset("room-b")
    assert processor.preload()["state"] == "closed"
    result = process(processor, np.zeros(FRAME_SAMPLES, np.float32), stream="room-b")
    assert result.metadata["action"] == "bypass" and result.metadata["reason"] == "closed"


@pytest.mark.parametrize("kwargs", [{"mode": "other"}, {"num_threads": True}, {"num_threads": 0},
                                  {"num_threads": 3}, {"num_threads": 1.5}])
def test_constructor_rejects_invalid_mode_and_unbounded_cpu(tmp_path, kwargs):
    with pytest.raises(ValueError):
        ChromeAudioProcessor(tmp_path, **kwargs)
