"""Reader liveness recovery cannot infer recognizer failure or replay captions."""
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fanyiji.chrome_caption_protocol import CaptionDeltaProtocol, CaptionSnapshot
from fanyiji.chrome_caption_service import ChromeCaptionService
from fanyiji.chrome_caption_worker import CaptionWorker


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Worker:
    def __init__(self, pid):
        self.pid, self.stdout, self.dead = pid, io.StringIO(), False

    def poll(self):
        return 0 if self.dead else None

    def kill(self):
        self.dead = True

    def wait(self, timeout):
        return 0


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setattr("fanyiji.chrome_caption_service.threading.Thread",
                        lambda **_kwargs: SimpleNamespace(start=lambda: None))
    clock, workers, events, calls = Clock(), [], [], []

    def factory(argv, **options):
        assert "--attach-existing" in argv
        assert "--source-url" in argv and "--owned-browser-pid" in argv
        assert not any(arg.startswith(("--user-data-dir", "--new-window")) for arg in argv)
        calls.append(argv)
        worker = Worker(1000 + len(workers))
        workers.append(worker)
        return worker

    service = ChromeCaptionService(events.append, process_factory=factory,
        chrome_finder=lambda: Path("C:/Chrome/chrome.exe"), clock=clock)
    service.start("https://live.bilibili.com/1698225", session=7, owned_browser_pid=900)
    yield service, clock, workers, events, calls
    service.close()


def raw_snapshot(service, text, *, epoch=1, window="111", connected=True, revision=1):
    return {"kind": "snapshot", "text": text, "window_id": window, "source_epoch": epoch,
            "revision": revision, "observed_at": service._clock(), "bridge_generation": service.generation,
            "room": "", "requested_room": "1698225", "source_verified": False, "connected": connected,
            "source_lang": "unknown", "target_lang": "zh", "generation": service.generation, "session": 7}


def accept_snapshot(service, text, **options):
    assert service._received(service._worker, service.generation)
    return service._snapshot_continuity(raw_snapshot(service, text, **options))


def crash_and_restart(service, clock, worker, delay):
    worker.dead = True
    generation = service.generation
    service.watchdog_tick()
    assert service._worker is None
    clock.advance(delay)
    service.watchdog_tick()
    assert service._worker is not None
    assert service.generation == generation


def test_silent_or_missing_bubble_emits_liveness_without_fabricating_captions():
    clock, sink = Clock(), io.StringIO()
    reader = SimpleNamespace(source_url="https://live.bilibili.com/1698225",
                             read=lambda: ("waiting_caption", None))
    worker = CaptionWorker(reader, sink, session=7, generation=3, clock=clock)
    for _ in range(25):
        worker.tick()
        clock.advance(0.25)
    events = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert len([event for event in events if event["kind"] == "status"]) == 1
    heartbeats = [event for event in events if event["kind"] == "heartbeat"]
    assert len(heartbeats) == 4
    assert all(not event["connected"] and event["revision"] == 0
               and event["snapshot_age_seconds"] is None and event["caption_update_age_seconds"] is None
               for event in heartbeats)
    assert all(event["kind"] != "snapshot" and "text" not in event for event in events)


def test_repeated_caption_reports_update_age_independently_of_snapshot_age():
    clock, sink = Clock(), io.StringIO()
    reader = SimpleNamespace(source_url="https://live.bilibili.com/1698225",
                             read=lambda: ("caption_found_unverified", ("111", "主播正在讲话。")))
    worker = CaptionWorker(reader, sink, session=7, generation=3, clock=clock)
    for _ in range(17):
        worker.tick()
        clock.advance(0.25)
    heartbeats = [json.loads(line) for line in sink.getvalue().splitlines()
                  if json.loads(line)["kind"] == "heartbeat"]
    assert [event["caption_update_age_seconds"] for event in heartbeats] == [0.0, 2.0, 4.0]
    assert all(event["snapshot_age_seconds"] == 0.0 and event["revision"] == 1 for event in heartbeats)


def test_no_new_words_never_causes_helper_restart(harness):
    service, clock, workers, _events, calls = harness
    for _ in range(101):
        accept_snapshot(service, "保持不变的字幕。")
        service.watchdog_tick()
        clock.advance(2.0)
    assert len(calls) == 1 and not workers[0].dead
    health = service.health_snapshot
    assert health["reader_alive"] and health["caption_update_age_seconds"] == 202.0
    assert health["snapshot_age_seconds"] == 2.0 and health["caption_state"] == "unchanged"


def test_healthy_waiting_reader_is_not_restarted_without_a_caption(harness):
    service, clock, _workers, _events, calls = harness
    for _ in range(40):
        assert service._received(service._worker, service.generation)
        service.watchdog_tick()
        clock.advance(2.0)
    assert len(calls) == 1
    assert service.health_snapshot["caption_update_age_seconds"] is None


def test_blocked_reader_gets_startup_grace_then_restarts_only_helper(harness):
    service, clock, workers, events, calls = harness
    clock.advance(29.9)
    service.watchdog_tick()
    assert len(calls) == 1
    clock.advance(0.1)
    service.watchdog_tick()
    assert workers[0].dead and service._worker is None
    assert any(event.get("code") == "reader_recovering" for event in events)
    clock.advance(1.0)
    service.watchdog_tick()
    assert len(calls) == 2 and service.reader_generation == 2
    assert service.generation == 1 and calls[0] == calls[1]


def test_active_reader_timeout_is_fifteen_seconds_not_text_age(harness):
    service, clock, workers, _events, calls = harness
    accept_snapshot(service, "没有变更的字幕。")
    clock.advance(14.9)
    service.watchdog_tick()
    assert not workers[0].dead
    clock.advance(0.1)
    service.watchdog_tick()
    assert workers[0].dead and len(calls) == 1


def test_recovery_cannot_replay_initial_sentences_or_reuse_old_helper_callbacks(harness):
    service, clock, workers, events, _calls = harness
    protocol = CaptionDeltaProtocol(service.generation, rolling_publication=True, include_initial_completed=True)
    fields = ("text", "window_id", "source_epoch", "observed_at", "bridge_generation", "room", "connected")

    def observe(value):
        return protocol.observe(CaptionSnapshot(**{field: value[field] for field in fields}, source_lang="unknown"))

    first = accept_snapshot(service, "这句话已经进入字幕。")
    observe(first)
    clock.advance(1.3)
    stable = observe(accept_snapshot(service, first["text"]))
    assert any(event["kind"] == "stable_candidate" for event in stable)
    old_worker, old_serial = workers[0], service.reader_generation
    crash_and_restart(service, clock, old_worker, 1.0)
    same = accept_snapshot(service, first["text"])
    assert same["source_epoch"] == first["source_epoch"]
    assert same["revision"] > first["revision"]
    assert observe(same) == []
    assert not service._received(old_worker, service.generation)
    before = list(events)
    service._emit({"kind": "snapshot", "text": "过期旧进程字幕"}, service.generation, worker=old_worker)
    assert events == before and service.reader_generation > old_serial


def test_actual_window_or_wire_source_change_still_creates_new_epoch(harness):
    service, clock, workers, _events, _calls = harness
    first = accept_snapshot(service, "旧字幕。", epoch=8)
    crash_and_restart(service, clock, workers[0], 1.0)
    changed = accept_snapshot(service, "另一个气泡。", window="222")
    assert changed["source_epoch"] == first["source_epoch"] + 1
    disconnected = accept_snapshot(service, "", window="222", epoch=2, connected=False)
    assert disconnected["source_epoch"] > changed["source_epoch"]


def test_old_reader_snapshot_cannot_modify_current_continuity_or_freshness(harness):
    service, clock, workers, _events, _calls = harness
    first = accept_snapshot(service, "当前字幕。", epoch=8, revision=5)
    old_worker, generation = workers[0], service.generation
    crash_and_restart(service, clock, old_worker, 1.0)
    assert service._snapshot_continuity(raw_snapshot(service, "旧进程回调。"),
        worker=old_worker, generation=generation) is None
    current = accept_snapshot(service, first["text"])
    assert current["source_epoch"] == first["source_epoch"]
    assert service._last_caption_text == first["text"]


def test_out_of_order_wire_epoch_or_revision_does_not_create_a_new_source(harness):
    service, _clock, _workers, _events, _calls = harness
    first = accept_snapshot(service, "当前字幕。", epoch=8, revision=5)
    assert service._snapshot_continuity(raw_snapshot(service, "过期字幕。", epoch=7, revision=5)) is None
    assert service._snapshot_continuity(raw_snapshot(service, "过期字幕。", epoch=8, revision=4)) is None
    assert service._stream_epoch == first["source_epoch"]
    assert service._last_caption_text == first["text"]


def test_repeated_failures_use_bounded_backoff_and_stop_after_three_attempts(harness):
    service, clock, workers, events, calls = harness
    for delay in (1.0, 2.0, 4.0):
        crash_and_restart(service, clock, workers[-1], delay)
    workers[-1].dead = True
    service.watchdog_tick()
    assert service._worker is None and service.health_snapshot["reader_recovery_state"] == "exhausted"
    assert service.health_snapshot["recovery_attempts"] == 3
    clock.advance(1000.0)
    service.watchdog_tick()
    assert len(calls) == 4
    assert [event["code"] for event in events if event["kind"] == "status"][-1] == "reader_failed"


def test_sustained_healthy_ticks_restore_recovery_budget(harness):
    service, clock, workers, _events, _calls = harness
    crash_and_restart(service, clock, workers[0], 1.0)
    assert service.health_snapshot["recovery_attempts"] == 1
    for _ in range(31):
        service._received(service._worker, service.generation)
        clock.advance(2.0)
    assert service.health_snapshot["recovery_attempts"] == 0


@pytest.mark.parametrize("method", ["stop", "close"])
def test_pause_close_or_old_watchdog_can_never_spawn_again(harness, method):
    service, clock, workers, _events, calls = harness
    old_generation = service.generation
    workers[0].dead = True
    service.watchdog_tick()
    getattr(service, method)()
    clock.advance(100.0)
    service.watchdog_tick(old_generation)
    service.watchdog_tick()
    assert len(calls) == 1


def test_terminal_browser_close_is_not_treated_as_a_reader_crash(harness):
    service, clock, workers, events, calls = harness
    packet = {"kind": "status", "code": "browser_closed", "generation": service.generation, "session": 7}
    workers[0].stdout = io.StringIO(json.dumps(packet) + "\n")
    service._read(workers[0], service.generation)
    clock.advance(1000.0)
    service.watchdog_tick()
    assert len(calls) == 1 and all(event.get("code") != "reader_recovering" for event in events)


@pytest.mark.parametrize("changes", [{"observed_at": float("nan")}, {"observed_at": True},
    {"status": {}}, {"revision": True}, {"connected": 1}, {"source_epoch": -1},
    {"snapshot_age_seconds": float("inf")}, {"caption_update_age_seconds": -1}])
def test_invalid_heartbeat_cannot_keep_reader_alive_or_leak_data(harness, changes):
    service, _clock, workers, events, _calls = harness
    packet = {"kind": "heartbeat", "observed_at": 100.0, "connected": False, "revision": 0,
        "source_epoch": 0, "status": "waiting_caption", "snapshot_age_seconds": None,
        "caption_update_age_seconds": None, "generation": service.generation, "session": 7,
        "text": "PRIVATE_CAPTION", "url": "PRIVATE_URL", **changes}
    workers[0].stdout = io.StringIO(json.dumps(packet) + "\n")
    service._read(workers[0], service.generation)
    assert service._last_event_at is None
    assert "PRIVATE_" not in json.dumps(events)


def test_valid_heartbeat_exports_only_service_freshness_and_no_text(harness):
    service, clock, workers, events, _calls = harness
    packet = {"kind": "heartbeat", "observed_at": 100.0, "connected": False, "revision": 0,
        "source_epoch": 0, "status": "waiting_caption", "snapshot_age_seconds": None,
        "caption_update_age_seconds": None, "generation": service.generation, "session": 7,
        "text": "PRIVATE_CAPTION", "cookie": "PRIVATE_COOKIE"}
    workers[0].stdout = io.StringIO(json.dumps(packet) + "\n")
    service._read(workers[0], service.generation)
    health = next(event for event in events if event["kind"] == "health")
    assert health["heartbeat_age_seconds"] == 0.0 and health["reader_alive"] is True
    assert health["caption_update_age_seconds"] is None
    assert "PRIVATE_" not in json.dumps(events)
