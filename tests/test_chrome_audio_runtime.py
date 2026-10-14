"""PCM worker tests with no browser, website, model downloads, or audio files."""
from collections import deque
import json
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from fanyiji import chrome_audio_runtime as module
from fanyiji.audio_processing import ProcessingResult
from fanyiji.chrome_audio_processing import FRAME_SAMPLES, encode_pcm_samples
from fanyiji.chrome_cdp import ChromeControlError


URL = "https://live.bilibili.com/1698225"
NONCE = "a" * 32
GENERATION = 31


class ControlledEvent:
    def __init__(self, state):
        self.state = state
        self.stopped = False
        self.on_wait = None

    def set(self):
        self.stopped = True

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.state.now += seconds
        if self.on_wait:
            self.on_wait(seconds)
        return self.stopped


def status(**extra):
    return {"state": "buffering", "reason": "prebuffer", "session_nonce": NONCE,
            "generation": GENERATION, **extra}


def frame(sequence=0, **extra):
    return {"sequence": sequence, "start_sample": sequence * FRAME_SAMPLES,
            "pcm_s16le": encode_pcm_samples(np.full(FRAME_SAMPLES, .25, np.float32)), **extra}


class Processor:
    def __init__(self, trace):
        self.trace = trace
        self.ready = True
        self.preload_reason = "ready"
        self.action = "enhanced"
        self.process_reason = "processed"
        self.process_hook = None
        self.sequences = []
        self.closed = False

    def preload(self):
        self.trace.append("preload")
        return {"ready": self.ready, "reason": self.preload_reason}

    def process(self, samples, *, sequence, start_sample, stream_id):
        self.trace.append(f"process:{sequence}")
        assert stream_id == NONCE and start_sample == sequence * FRAME_SAMPLES
        if self.sequences and sequence != self.sequences[-1] + 1:
            raise ValueError("audio_sequence_discontinuity")
        self.sequences.append(sequence)
        if self.process_hook:
            self.process_hook(sequence)
        return ProcessingResult(samples * .6,
            {"action": self.action, "reason": self.process_reason, "elapsed_ms": 11.0})

    def close(self):
        self.closed = True
        self.trace.append("processor.close")


class Connection:
    def __init__(self, state, trace):
        self.state, self.trace = state, trace
        self.install = deque([status()])
        self.pulls = deque([status(state="bypassed", reason="controller_timeout")])
        self.pushes = deque()
        self.hook = None
        self.closed = False
        self.calls = []
        self.close_error = None

    def call(self, method, params, *, timeout):
        assert method == "Runtime.evaluate" and params["returnByValue"]
        expression = params["expression"]
        if params.get("awaitPromise"):
            operation, cfg = "install", None
            reply = self.install.popleft() if len(self.install) > 1 else self.install[0]
        else:
            cfg = json.loads(expression.split("const cfg=", 1)[1].split(", manager=", 1)[0])
            if "active.pull(cfg)" in expression:
                operation = "pull"
                reply = self.pulls.popleft()
            elif "active.push(cfg)" in expression:
                operation = "push"
                reply = self.pushes.popleft() if self.pushes else status(accepted_frames=len(cfg["frames"]))
            else:
                assert "active.stop(cfg)" in expression
                operation, reply = "stop", status(state="stopped")
        self.trace.append(operation)
        self.calls.append((operation, cfg, params, timeout))
        if self.hook:
            self.hook(operation)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, dict) and ("result" in reply or "exceptionDetails" in reply):
            return reply
        return {"result": {"value": reply}}

    def close(self):
        self.trace.append("connection.close")
        self.closed = True
        if self.close_error:
            raise self.close_error


@pytest.fixture
def harness(tmp_path, monkeypatch):
    state, trace, public = SimpleNamespace(valid=True, now=100.0), [], []
    processor, connection = Processor(trace), Connection(state, trace)

    def connect():
        trace.append("connect")
        return connection

    def create(models_dir, **kwargs):
        assert models_dir == tmp_path and kwargs == {"mode": "gtcrn", "num_threads": 1}
        trace.append("processor.create")
        return processor

    monkeypatch.setattr(module.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(module.secrets, "token_hex", lambda size: NONCE)
    runtime = module.ChromeAudioRuntime(connection_factory=connect,
        source_guard=lambda: state.valid, generation=GENERATION, url=URL,
        models_dir=tmp_path, on_status=public.append, processor_factory=create)
    runtime._stop = ControlledEvent(state)
    # Legacy single-session fixtures cancel at the first backoff. Recovery
    # regressions below provide independently scripted fresh graph sessions.
    runtime._stop.on_wait = lambda seconds: runtime._stop.set() if seconds >= 1 else None
    return SimpleNamespace(runtime=runtime, processor=processor, connection=connection,
                           trace=trace, state=state, public=public)


def operations(harness, name):
    return [row for row in harness.connection.calls if row[0] == name]


def test_constructor_has_no_browser_native_audio_or_status_side_effects(harness):
    assert not harness.trace and not harness.public
    assert harness.runtime._connection is None and harness.runtime._thread is None


def test_start_is_idempotent_and_creates_one_background_worker(harness, monkeypatch):
    thread = Mock()
    factory = Mock(return_value=thread)
    monkeypatch.setattr(module.threading, "Thread", factory)
    harness.runtime.start()
    harness.runtime.start()
    factory.assert_called_once_with(target=harness.runtime._run, daemon=True, name="chrome-audio-gtcrn")
    thread.start.assert_called_once_with()


def test_missing_model_never_connects_and_close_keeps_bypass_explicit(harness):
    harness.processor.ready = False
    harness.processor.preload_reason = "model_missing"
    harness.runtime._run()
    assert "connect" not in harness.trace and not harness.connection.calls
    assert harness.processor.closed
    assert harness.public[-1]["state"] == "bypassed"
    assert harness.public[-1]["reason"] == "model_missing"


def test_source_changed_during_preload_never_connects(harness):
    original = harness.processor.preload
    def preload():
        result = original()
        harness.state.valid = False
        return result
    harness.processor.preload = preload
    harness.runtime._run()
    assert "connect" not in harness.trace and harness.processor.closed


def test_sequence_preserved_one_ordered_batch_and_no_asr_vad_or_network_sender(harness):
    harness.connection.pulls = deque([
        status(frames=[frame(0), frame(1), frame(2)], captured_frames=3),
        status(state="bypassed", reason="controller_timeout")])
    harness.runtime._run()
    assert harness.processor.sequences == [0, 1, 2]
    sent = operations(harness, "push")
    assert len(sent) == 1
    assert [item["sequence"] for item in sent[0][1]["frames"]] == [0, 1, 2]
    assert [item["start_sample"] for item in sent[0][1]["frames"]] == [0, 3200, 6400]
    assert harness.runtime._processed == 3
    assert harness.runtime._elapsed_ms == 33 and harness.runtime._max_elapsed_ms == 11
    assert all(row["processed_only_verified"] is False for row in harness.public)
    assert harness.trace[-2:] == ["connection.close", "processor.close"]
    assert len(operations(harness, "stop")) == 1
    assert all(call[2]["userGesture"] and call[3] == 1.5 for call in harness.connection.calls if call[0] != "stop")


def test_bypass_output_never_returned_as_enhanced_audio_or_pushed(harness):
    harness.connection.pulls = deque([status(frames=[frame(0)])])
    harness.processor.action, harness.processor.process_reason = "bypass", "processing_too_slow"
    harness.runtime._run()
    assert not operations(harness, "push")
    assert harness.runtime._processed == 0
    assert harness.public[-1]["state"] == "bypassed"
    assert harness.public[-1]["reason"] == "processing_too_slow"
    assert harness.connection.closed and harness.processor.closed


@pytest.mark.parametrize("frames", [None, {}, [frame(0)] * 9,
    [{"sequence": 0, "start_sample": 0, "pcm_s16le": "AA=="}],
    [{"sequence": 0, "start_sample": 0, "pcm_s16le": "AAA="}],
    [{"sequence": 0, "start_sample": 0, "pcm_s16le": "TOKEN=secret"}]])
def test_invalid_or_overflow_batch_cannot_process_or_push(harness, frames):
    harness.connection.pulls = deque([status(frames=frames)])
    harness.runtime._run()
    assert not harness.processor.sequences and not operations(harness, "push")
    assert harness.public[-1]["state"] == "bypassed"
    assert harness.public[-1]["reason"] == "processing_error"
    assert harness.connection.closed and harness.processor.closed


@pytest.mark.parametrize("change_on", ["install", "pull", "push"])
def test_guard_after_cdp_prevents_consuming_or_reporting_returned_stale_result(harness, change_on):
    harness.connection.pulls = deque([status(frames=[frame(0)])])
    harness.connection.hook = lambda operation: setattr(harness.state, "valid", False) if operation == change_on else None
    harness.runtime._run()
    if change_on != "push":
        assert not operations(harness, "push")
    if change_on != "push":
        assert not harness.processor.sequences
    assert harness.public[-1]["state"] == "bypassed"
    assert harness.public[-1]["reason"] == "source_changed"
    assert harness.connection.closed and harness.processor.closed


@pytest.mark.parametrize("stop", [False, True])
def test_change_or_stop_during_processing_never_pushes_old_audio(harness, stop):
    harness.connection.pulls = deque([status(frames=[frame(0)])])
    def change(_):
        if stop:
            harness.runtime._stop.set()
        else:
            harness.state.valid = False
    harness.processor.process_hook = change
    harness.runtime._run()
    assert not operations(harness, "push")
    assert harness.connection.closed and harness.processor.closed
    if not stop:
        assert harness.public[-1]["reason"] == "source_changed"


@pytest.mark.parametrize("response", [{"result": {"value": []}},
    {"result": {"value": None}}, {"exceptionDetails": {"private_error": "cookie=SECRET"}},
    ChromeControlError("private_code_cookie_SECRET")])
def test_malformed_cdp_and_errors_never_leak_private_payload(harness, response):
    harness.connection.install = deque([response])
    harness.runtime._run()
    assert harness.public[-1]["reason"] == "processing_error"
    assert "SECRET" not in repr(harness.public)
    assert not operations(harness, "push")
    assert harness.connection.closed and harness.processor.closed


def test_output_partial_ack_fails_open_and_never_resubmits_audio(harness):
    harness.connection.pulls = deque([status(frames=[frame(0), frame(1)])])
    harness.connection.pushes = deque([status(accepted_frames=1)])
    harness.runtime._run()
    assert len(operations(harness, "push")) == 1
    assert harness.public[-1]["reason"] == "audio_output_rejected"
    assert harness.connection.closed and harness.processor.closed


def test_graph_stopped_even_if_cleanup_script_fails(harness):
    def fail(operation):
        if operation == "stop":
            raise RuntimeError("private_cookie_SECRET")
    harness.connection.hook = fail
    harness.runtime._run()
    assert len(operations(harness, "stop")) == 1
    assert harness.connection.closed and harness.processor.closed
    assert "SECRET" not in repr(harness.public)


def test_browser_connection_failure_always_closes_processor(harness):
    def fails():
        raise RuntimeError("private_url_or_model_path_SECRET")
    harness.runtime._connect = fails
    harness.runtime._run()
    assert harness.processor.closed
    assert harness.public[-1]["reason"] == "processing_error"
    assert "SECRET" not in repr(harness.public)


def test_public_telemetry_only_numeric_counters_and_fixed_fields_no_audio_nonce_url(harness):
    private = {"frames": [frame(0)], "session_nonce": "SECRET_NONCE",
               "url": "https://private.example/COOKIE", "model_path": "C:/PRIVATE/MODEL",
               "pcm_s16le": "PRIVATE_AUDIO", "captured_frames": 8, "played_frames": True,
               "buffered_input_frames": -1, "buffered_output_frames": 2**53}
    harness.runtime._report("buffering", "processed_audio_experimental", private)
    public = harness.public[-1]
    assert public["captured_frames"] == 8
    assert "played_frames" not in public and "buffered_input_frames" not in public
    assert "buffered_output_frames" not in public
    assert not any(value in repr(public) for value in ("SECRET", "COOKIE", "PRIVATE", NONCE, URL))
    assert not any(key in public for key in ("frames", "pcm_s16le", "url", "session_nonce", "model_path"))


def test_report_rate_limited_but_state_change_immediately_visible(harness):
    harness.runtime._report("buffering", "processed_audio_experimental")
    harness.runtime._report("buffering", "processed_audio_experimental")
    assert len(harness.public) == 1
    harness.state.now += 2
    harness.runtime._report("buffering", "processed_audio_experimental")
    harness.runtime._report("bypassed", "processing_error")
    assert len(harness.public) == 3


def test_waiting_source_times_out_without_attaching_graph_or_pushing(harness):
    harness.connection.install = deque([status(state="unavailable", reason="media_not_playing")])
    harness.runtime._run()
    assert harness.state.now >= 130
    assert not operations(harness, "push") and not operations(harness, "stop")
    assert harness.public[-1]["reason"] == "audio_unavailable"
    assert harness.connection.closed and harness.processor.closed


def test_waiting_source_then_available_installs_once_and_closes(harness):
    harness.connection.install = deque([
        status(state="unavailable", reason="media_not_playing"),
        status(state="unavailable", reason="media_not_playing"), status()])
    harness.runtime._run()
    assert len(operations(harness, "install")) == 3
    assert len(operations(harness, "stop")) == 1
    assert harness.connection.closed and harness.processor.closed


def test_stop_sets_flag_before_join_and_wakes_blocked_transport(harness, monkeypatch):
    thread = Mock()
    thread.is_alive.return_value = True
    harness.runtime._thread, harness.runtime._connection = thread, harness.connection
    thread.join.side_effect = lambda **kwargs: pytest.fail("stop must precede join") if not harness.runtime._stop.is_set() else None
    monkeypatch.setattr(module.threading, "current_thread", lambda: object())
    harness.runtime.stop()
    assert harness.runtime._stop.is_set()
    thread.join.assert_called_once_with(timeout=2)
    assert harness.connection.closed


def test_stop_from_worker_does_not_join_itself(harness, monkeypatch):
    thread = Mock()
    thread.is_alive.return_value = False
    harness.runtime._thread = thread
    monkeypatch.setattr(module.threading, "current_thread", lambda: thread)
    harness.runtime.stop()
    thread.join.assert_not_called()


def test_media_not_playing_contract_waits_for_live_video_instead_of_disabling(harness):
    harness.connection.install = deque([
        status(state="unavailable", reason="media_not_playing"), status()])
    harness.runtime._run()
    assert len(operations(harness, "install")) == 2
    assert len(operations(harness, "stop")) == 1


@pytest.mark.parametrize("where", ["install", "pull"])
@pytest.mark.parametrize("identity", [{"generation": GENERATION - 1}, {"session_nonce": "b" * 32}])
def test_frontend_generation_or_session_mismatch_never_processed_or_pushed(harness, where, identity):
    if where == "install":
        harness.connection.install = deque([status(**identity)])
    else:
        harness.connection.pulls = deque([status(frames=[frame(0)], **identity)])
    harness.runtime._run()
    assert not harness.processor.sequences and not operations(harness, "push")
    assert harness.public[-1]["state"] == "bypassed"


def test_page_supplied_status_and_processor_reason_are_privacy_whitelisted(harness):
    harness.runtime._report("PRIVATE_STATE_COOKIE_SECRET", "PRIVATE_REASON_COOKIE_SECRET")
    assert "PRIVATE" not in repr(harness.public) and "SECRET" not in repr(harness.public)


def test_transport_close_exception_cannot_skip_processor_cleanup(harness):
    harness.connection.close_error = RuntimeError("closed socket SECRET")
    harness.runtime._run()
    assert harness.processor.closed


def test_maximum_eight_frame_batch_then_next_frame_maintains_sequence_across_pulls(harness):
    harness.connection.pulls = deque([
        status(frames=[frame(i) for i in range(8)]), status(frames=[frame(8)]),
        status(state="bypassed", reason="controller_timeout")])
    harness.runtime._run()
    assert harness.processor.sequences == list(range(9))
    assert [len(call[1]["frames"]) for call in operations(harness, "push")] == [8, 1]
    assert harness.runtime._processed == 9


def test_no_capture_available_polls_without_playing_fabricated_audio(harness):
    harness.connection.pulls = deque([status(frames=[]), status(frames=[]),
                                      status(state="bypassed", reason="controller_timeout")])
    harness.runtime._run()
    assert not harness.processor.sequences and not operations(harness, "push")
    assert harness.state.now == pytest.approx(101.1)


@pytest.mark.parametrize("frames", [[frame(0), frame(0)], [frame(0), frame(2)], [frame(1), frame(0)]])
def test_duplicate_or_reordered_capture_never_partially_pushes_a_batch(harness, frames):
    harness.connection.pulls = deque([status(frames=frames)])
    harness.runtime._run()
    assert not operations(harness, "push")
    assert harness.public[-1]["reason"] == "processing_error"


def test_source_guard_false_before_first_install_prevents_all_cdp_mutations(harness):
    def connection_changed():
        harness.state.valid = False
        return harness.connection
    harness.runtime._connect = connection_changed
    harness.runtime._run()
    assert not harness.connection.calls
    assert harness.connection.closed and harness.processor.closed


@pytest.mark.parametrize("reason", ["wrong_source", "ambiguous_media", "bridge_conflict",
                                   "audio_graph_unavailable", "audio_worklet_unavailable"])
def test_unrecoverable_frontend_graph_failure_does_not_retry_or_keep_audio_worker(harness, reason):
    harness.connection.install = deque([status(state="unavailable", reason=reason)])
    harness.runtime._run()
    assert len(operations(harness, "install")) == 1
    assert not operations(harness, "push") and not operations(harness, "stop")
    assert harness.public[-1]["reason"] == ("source_changed" if reason == "wrong_source" else "audio_unavailable")
    assert harness.connection.closed and harness.processor.closed


def test_stop_while_waiting_for_media_restores_without_installing_or_connecting_again(harness):
    harness.connection.install = deque([status(state="unavailable", reason="media_not_playing")])
    harness.runtime._stop.on_wait = lambda _: harness.runtime._stop.set()
    harness.runtime._run()
    assert len(operations(harness, "install")) == 1
    assert not operations(harness, "pull") and not operations(harness, "push")
    assert not operations(harness, "stop")
    assert harness.connection.closed and harness.processor.closed


def test_push_wrong_generation_never_reported_as_successful_processing(harness):
    harness.connection.pulls = deque([status(frames=[frame(0)])])
    harness.connection.pushes = deque([status(generation=GENERATION + 1, accepted_frames=1)])
    harness.runtime._run()
    assert harness.public[-1]["state"] == "bypassed"
    assert harness.public[-1]["reason"] == "source_changed"


def test_processor_report_reason_and_page_state_cannot_inject_private_strings(harness):
    harness.processor.ready = False
    harness.processor.preload_reason = "SECRET_PATH_COOKIE"
    harness.runtime._run()
    assert harness.public[-1]["reason"] == "processing_error"
    assert "SECRET" not in repr(harness.public)


def test_stop_transport_close_failure_does_not_escape_to_caller(harness, monkeypatch):
    thread = Mock()
    thread.is_alive.return_value = True
    harness.runtime._thread, harness.runtime._connection = thread, harness.connection
    harness.connection.close_error = RuntimeError("SECRET_close")
    monkeypatch.setattr(module.threading, "current_thread", lambda: object())
    harness.runtime.stop()
    assert harness.runtime._stop.is_set()


class RecoveringProcessor(Processor):
    def __init__(self, trace, *, failure=None):
        super().__init__(trace)
        self.streams = []
        self.resets = []
        self.failure = failure
        self.reset_failure = None

    def reset(self, *, stream_id):
        self.resets.append(stream_id)
        self.sequences = []
        self.trace.append("processor.reset")
        return {"ready": self.reset_failure is None, "reason": self.reset_failure or "ready"}

    def process(self, samples, *, sequence, start_sample, stream_id):
        self.streams.append(stream_id)
        assert start_sample == sequence * FRAME_SAMPLES
        if self.sequences and sequence != self.sequences[-1] + 1:
            raise ValueError("audio_sequence_discontinuity")
        self.sequences.append(sequence)
        self.trace.append(f"process:{sequence}")
        if self.failure:
            return ProcessingResult(samples, {"action": "bypass", "reason": self.failure, "elapsed_ms": 10})
        return ProcessingResult(samples * .6, {"action": "enhanced", "reason": "processed", "elapsed_ms": 10})


class RecoveringConnection:
    def __init__(self, state, trace, *, pulls=None, pushes=None, install=None):
        self.state, self.trace = state, trace
        self.pulls = deque(pulls or [])
        self.pushes = deque(pushes or [])
        self.install = install or {"state": "buffering", "reason": "prebuffer"}
        self.calls = []
        self.closed = False
        self.nonce = None
        self.hook = None

    def call(self, method, params, *, timeout):
        expression = params["expression"]
        if params.get("awaitPromise"):
            operation = "install"
            cfg = json.loads(expression.split("const cfg = ", 1)[1].split(";", 1)[0])
            self.nonce = cfg["session_nonce"]
            reply = self.install
        else:
            cfg = json.loads(expression.split("const cfg=", 1)[1].split(", manager=", 1)[0])
            if "active.pull(cfg)" in expression:
                operation = "pull"
                reply = self.pulls.popleft() if self.pulls else {"state": "buffering", "frames": []}
            elif "active.push(cfg)" in expression:
                operation = "push"
                reply = self.pushes.popleft() if self.pushes else {"state": "processing", "accepted_frames": len(cfg["frames"])}
            else:
                operation, reply = "stop", {"state": "stopped"}
        self.trace.append(operation)
        self.calls.append((operation, cfg))
        if self.hook:
            self.hook(operation)
        if isinstance(reply, Exception):
            raise reply
        return {"result": {"value": {"session_nonce": cfg["session_nonce"], "generation": cfg["generation"], **reply}}}

    def close(self):
        self.closed = True
        self.trace.append("connection.close")


@pytest.fixture
def recovery_harness(tmp_path, monkeypatch):
    state = SimpleNamespace(now=100.0, valid=True)
    trace, public, processors, connections, waits = [], [], [], [], []
    session_specs = deque()
    processor_specs = deque()
    nonces = iter(f"{index:032x}" for index in range(1, 100))
    monkeypatch.setattr(module.secrets, "token_hex", lambda size: next(nonces))

    def create(models_dir, **kwargs):
        processor = RecoveringProcessor(trace, **(processor_specs.popleft() if processor_specs else {}))
        processors.append(processor)
        return processor

    def connect():
        connection = RecoveringConnection(state, trace, **session_specs.popleft())
        connections.append(connection)
        return connection

    runtime = module.ChromeAudioRuntime(connection_factory=connect,
        source_guard=lambda: state.valid, generation=GENERATION, url=URL, models_dir=tmp_path,
        on_status=public.append, processor_factory=create, clock=lambda: state.now)
    runtime._stop = ControlledEvent(state)
    def wait(seconds):
        waits.append(seconds)
        # A successful final graph stops once all explicitly supplied frames
        # are consumed. Retry waits remain available for failure regressions.
        if seconds == .05 and not session_specs and connections and not connections[-1].pulls:
            runtime._stop.set()
    runtime._stop.on_wait = wait
    return SimpleNamespace(runtime=runtime, state=state, trace=trace, public=public,
        processors=processors, connections=connections, sessions=session_specs,
        processor_specs=processor_specs, waits=waits)


def test_first_js_push_failure_survives_cleanup_and_recovers_new_nonce(recovery_harness):
    h = recovery_harness
    h.sessions.extend([
        {"pulls": [{"state": "processing", "frames": [frame(0)], "captured_frames": 1}],
         "pushes": [{"state": "stopped", "reason": "disabled", "failure_reason": "playback_underrun",
                     "failure_state": "processing", "accepted_frames": 0, "captured_frames": 1}]},
        {"pulls": [{"state": "processing", "frames": [frame(0)], "captured_frames": 1}],
         "pushes": [{"state": "processing", "accepted_frames": 1, "captured_frames": 1, "played_frames": 1}]}])
    h.runtime._run()
    failure = next(row for row in h.public if row["reason"] == "playback_underrun")
    assert failure["last_fault_state"] == "processing"
    assert failure["retry_delay_seconds"] == 1 and failure["recovery_allowed"]
    assert failure["captured_frames"] == 1  # No fault counters counted twice.
    first, second = h.connections
    assert first.nonce != second.nonce
    assert first.calls[-1][0] == "stop" and first.calls[-1][1]["reason"] == "playback_underrun"
    assert first.calls[-1][1]["session_nonce"] == first.nonce
    assert h.processors[0].resets == [second.nonce]
    assert h.processors[0].streams == [first.nonce, second.nonce]
    final = h.public[-1]
    assert final["state"] == "processing" and final["recovery_count"] == 1
    assert final["processed_frames"] == 2 and final["captured_frames"] == 2
    assert final["session_captured_frames"] == 1
    assert not h.runtime.is_running and not h.runtime.alive
    assert all(connection.closed for connection in h.connections)
    assert h.processors[0].closed


@pytest.mark.parametrize("fault", ["playback_underrun", "capture_overflow", "controller_timeout", "startup_timeout",
                                  "invalid_output", "invalid_capture"])
def test_graph_faults_recover_but_never_replay_old_captured_frames(recovery_harness, fault):
    h = recovery_harness
    h.sessions.extend([
        {"pulls": [{"state": "bypassed", "reason": fault, "frames": [frame(0)], "dropped_frames": 1}]},
        {"pulls": [{"state": "processing", "frames": [frame(0)]}]}])
    h.runtime._run()
    assert h.processors[0].sequences == [0]
    assert sum(operation == "push" for connection in h.connections for operation, _ in connection.calls) == 1
    assert h.runtime._recovery_count == 1 and h.runtime._error_count == 1
    assert h.public[-1]["dropped_frames"] == 1


def test_backoff_caps_at_eight_seconds_without_a_busy_retry_loop(recovery_harness):
    h = recovery_harness
    h.sessions.extend([{"pulls": [{"state": "bypassed", "reason": "controller_timeout"}]} for _ in range(6)])
    def stop_after_six_delays(seconds):
        h.waits.append(seconds)
        if len(h.waits) == 6:
            h.runtime._stop.set()
    h.runtime._stop.on_wait = stop_after_six_delays
    h.runtime._run()
    assert h.waits == [1, 2, 4, 8, 8, 8]
    assert h.runtime._recovery_attempt == 5 and len(h.connections) == 6
    assert len({connection.nonce for connection in h.connections}) == 6
    assert all(connection.closed for connection in h.connections)


@pytest.mark.parametrize("change", ["nonce", "generation", "guard", "reason"])
def test_recovery_never_mutates_after_stale_session_or_wrong_source(recovery_harness, change):
    h = recovery_harness
    reply = {"state": "bypassed", "reason": "controller_timeout"}
    if change == "nonce":
        reply["session_nonce"] = "b" * 32
    elif change == "generation":
        reply["generation"] = GENERATION - 1
    elif change == "reason":
        reply["reason"] = "wrong_source"
    h.sessions.append({"pulls": [reply]})
    original = h.runtime._connect
    def connect():
        result = original()
        if change == "guard":
            result.hook = lambda operation: setattr(h.state, "valid", False) if operation == "pull" else None
        return result
    h.runtime._connect = connect
    h.runtime._run()
    assert len(h.connections) == 1 and not h.waits
    assert not any(operation in ("stop", "push") for operation, _ in h.connections[0].calls)
    assert h.public[-1]["reason"] == "source_changed"
    assert not h.public[-1]["recovery_allowed"] and h.runtime._recovery_attempt == 0


def test_source_changes_during_backoff_cancel_retry_without_opening_a_new_connection(recovery_harness):
    h = recovery_harness
    h.sessions.append({"pulls": [{"state": "bypassed", "reason": "controller_timeout"}]})
    h.runtime._stop.on_wait = lambda _: setattr(h.state, "valid", False)
    h.runtime._run()
    assert len(h.connections) == 1 and h.runtime._recovery_attempt == 0
    assert h.public[-1]["reason"] == "source_changed"


@pytest.mark.parametrize("fault", ["processing_error", "processing_too_slow"])
def test_repeated_native_failures_have_three_recovery_attempt_budget(recovery_harness, fault):
    h = recovery_harness
    h.sessions.extend([{"pulls": [{"state": "processing", "frames": [frame(0)]}]} for _ in range(4)])
    h.processor_specs.extend([{"failure": fault} for _ in range(4)])
    h.runtime._run()
    assert len(h.processors) == len(h.connections) == 4
    assert h.waits == [1, 2, 4]
    assert h.runtime._recovery_attempt == 3
    assert h.public[-1]["retry_exhausted"] and not h.public[-1]["recovery_allowed"]
    assert h.public[-1]["native_recovery_attempts"] == 3
    assert all(processor.closed for processor in h.processors)
    assert not any(operation == "push" for connection in h.connections for operation, _ in connection.calls)


def test_native_transient_recreates_model_and_recovers_without_reloading_browser(recovery_harness):
    h = recovery_harness
    h.sessions.extend([{"pulls": [{"state": "processing", "frames": [frame(0)]}]} for _ in range(2)])
    h.processor_specs.extend([{"failure": "processing_error"}, {}])
    h.runtime._run()
    assert len(h.processors) == 2 and h.processors[0].closed and h.processors[1].closed
    assert h.public[-1]["state"] == "processing" and h.public[-1]["recovery_count"] == 1
    assert all(operation in ("install", "pull", "push", "stop") for connection in h.connections for operation, _ in connection.calls)


def test_ten_seconds_stable_processing_resets_the_recovery_backoff(recovery_harness):
    h = recovery_harness
    h.sessions.extend([
        {"pulls": [{"state": "bypassed", "reason": "capture_overflow"}]},
        {"pulls": [{"state": "processing", "frames": [frame(index)]} for index in range(53)] + [
                   {"state": "bypassed", "reason": "capture_overflow"}]},
        {"pulls": [{"state": "processing", "frames": [frame(0)]}]}])
    connect = h.runtime._connect
    def advance_second_session():
        connection = connect()
        if len(h.connections) == 2:
            def hook(operation):
                if operation == "push":
                    h.state.now += .15
            connection.hook = hook
        return connection
    h.runtime._connect = advance_second_session
    h.runtime._run()
    assert [seconds for seconds in h.waits if seconds >= 1] == [1, 1]
    assert h.runtime._recovery_count == 2


def test_two_isolated_outputs_ten_seconds_apart_do_not_reset_retry_budget(recovery_harness):
    h = recovery_harness
    h.sessions.extend([
        {"pulls": [{"state": "bypassed", "reason": "capture_overflow"}]},
        {"pulls": [{"state": "processing", "frames": [frame(0)]},
                   {"state": "processing", "frames": [frame(1)]},
                   {"state": "bypassed", "reason": "capture_overflow"}]},
        {"pulls": [{"state": "processing", "frames": [frame(0)]}]}])
    connect = h.runtime._connect
    def advance_second_session():
        connection = connect()
        if len(h.connections) == 2:
            def hook(operation):
                if operation == "push" and sum(name == "push" for name, _ in connection.calls) == 2:
                    h.state.now += 10
            connection.hook = hook
        return connection
    h.runtime._connect = advance_second_session
    h.runtime._run()
    assert [seconds for seconds in h.waits if seconds >= 1] == [1, 2]


def test_successful_install_without_processed_playback_is_not_counted_as_recovery(recovery_harness):
    h = recovery_harness
    h.sessions.extend([{"pulls": [{"state": "bypassed", "reason": "startup_timeout"}]} for _ in range(2)])
    def stop_second_backoff(seconds):
        if seconds == 2:
            h.runtime._stop.set()
    h.runtime._stop.on_wait = stop_second_backoff
    h.runtime._run()
    assert h.runtime._recovery_attempt == 1 and h.runtime._recovery_count == 0


def test_recovery_telemetry_keeps_fault_and_only_safe_numeric_progress(harness):
    h = harness
    h.runtime._last_fault_reason, h.runtime._last_fault_state = "capture_overflow", "processing"
    h.runtime._last_progress = h.state.now - .3
    h.runtime._last_audio_frame_time = 1770000000000
    h.runtime._report("processing", "processed_audio_experimental", {
        "last_input_rms": .2, "last_output_rms": .1, "last_input_age_ms": 200.0,
        "last_output_age_ms": float("nan"), "first_input_wall_ms": 1770000000000,
        "first_failure_wall_ms": 2**2000, "last_playback_wall_ms": True,
        "frames": [frame(0)], "failure_reason": "PRIVATE_SECRET"})
    value = h.public[-1]
    assert value["last_fault_reason"] == "capture_overflow" and value["last_fault_state"] == "processing"
    assert value["last_input_rms"] == .2 and value["last_output_rms"] == .1
    assert value["last_progress_age_ms"] == pytest.approx(300)
    assert value["last_audio_frame_time"] == 1770000000000
    assert "last_output_age_ms" not in value and "first_failure_wall_ms" not in value
    assert "last_playback_wall_ms" not in value and "PRIVATE" not in repr(value)


def test_finite_native_elapsed_required_no_false_success(harness):
    h = harness
    h.connection.pulls = deque([status(frames=[frame(0)])])
    h.processor.process = lambda *args, **kwargs: ProcessingResult(
        np.zeros(FRAME_SAMPLES, np.float32), {"action": "enhanced", "elapsed_ms": float("nan")})
    h.runtime._run()
    assert h.runtime._processed == 0 and not operations(h, "push")
    assert h.public[-1]["reason"] == "processing_error"


@pytest.mark.parametrize("fault", ["script_timeout", "script_unavailable", "browser_unavailable"])
def test_transport_failure_recovers_only_with_the_same_still_valid_source(recovery_harness, fault):
    h = recovery_harness
    h.sessions.extend([
        {"pulls": [ChromeControlError(fault)]},
        {"pulls": [{"state": "processing", "frames": [frame(0)]}]}])
    h.runtime._run()
    assert len(h.connections) == 2 and h.runtime._recovery_count == 1
    assert h.public[-1]["last_fault_reason"] == fault
    assert h.waits == [1, .05]


def test_unknown_page_failure_reason_is_not_blindly_retried(recovery_harness):
    h = recovery_harness
    h.sessions.append({"pulls": [{"state": "bypassed", "reason": "PRIVATE_COOKIE_SECRET"}]})
    h.runtime._run()
    assert len(h.connections) == 1 and not h.waits
    assert h.public[-1]["reason"] == "audio_stream_unavailable"
    assert not h.public[-1]["recovery_allowed"] and "PRIVATE" not in repr(h.public)


def test_missing_model_on_recovery_is_fatal_and_never_opens_a_third_connection(recovery_harness):
    h = recovery_harness
    h.sessions.append({"pulls": [{"state": "processing", "frames": [frame(0)]}]})
    h.processor_specs.append({"failure": "processing_error"})
    factory = h.runtime._factory
    def factory_with_missing_second_model(*args, **kwargs):
        result = factory(*args, **kwargs)
        if len(h.processors) == 2:
            result.ready, result.preload_reason = False, "model_missing"
        return result
    h.runtime._factory = factory_with_missing_second_model
    h.runtime._run()
    assert len(h.connections) == 1 and len(h.processors) == 2
    assert h.public[-1]["reason"] == "model_missing" and not h.public[-1]["recovery_allowed"]
    assert h.waits == [1]
    assert all(processor.closed for processor in h.processors)


def test_reset_error_recreates_native_state_instead_of_reusing_broken_processor(recovery_harness):
    h = recovery_harness
    h.sessions.extend([
        {"pulls": [{"state": "bypassed", "reason": "capture_overflow"}]},
        {"pulls": [{"state": "processing", "frames": [frame(0)]}]}])
    factory = h.runtime._factory
    def factory_with_one_bad_reset(*args, **kwargs):
        result = factory(*args, **kwargs)
        if len(h.processors) == 1:
            result.reset_failure = "reset_error"
        return result
    h.runtime._factory = factory_with_one_bad_reset
    h.runtime._run()
    assert len(h.processors) == 2 and len(h.connections) == 2
    assert h.waits == [1, 2, .05]
    assert h.runtime._recovery_attempt == 2 and h.public[-1]["last_fault_reason"] == "reset_error"
    assert h.processors[0].closed


def test_source_guard_exception_is_fatal_without_exposing_its_error(harness):
    h = harness
    def fail():
        raise RuntimeError("PRIVATE_CREDENTIAL_SECRET")
    h.runtime._guard = fail
    h.runtime._run()
    assert not h.connection.calls and not h.processor.sequences
    assert h.public[-1]["reason"] == "source_changed"
    assert "SECRET" not in repr(h.public) and not h.runtime.is_running


def test_invalid_native_configuration_never_retries_load_or_connect(harness):
    h = harness
    def fail(*args, **kwargs):
        raise ValueError("SECRET_CONFIGURATION")
    h.runtime._factory = fail
    h.runtime._run()
    assert not h.connection.calls and h.public[-1]["reason"] == "model_invalid"
    assert not h.public[-1]["recovery_allowed"] and "SECRET" not in repr(h.public)
