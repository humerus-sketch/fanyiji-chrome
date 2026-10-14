"""Owned Chrome audio lifecycle uses bounded same-page runtime, with no real browser."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fanyiji.chrome_cdp import ChromeControlError
from test_chrome_browser_service import URL, harness, open_browser


class FakeAudioRuntime:
    def __init__(self, trace, **options):
        self.trace, self.options = trace, options
        self.started = False
        self.stopped = False
        self.ended = False

    @property
    def is_running(self):
        return self.started and not self.stopped and not self.ended

    def start(self):
        self.started = True
        self.trace.append(("audio.start", self.options["generation"]))

    def stop(self):
        self.stopped = True
        self.trace.append(("audio.stop", self.options["generation"]))

    def emit_status(self, state="processing"):
        self.options["on_status"]({"mode": "gtcrn", "state": state,
            "reason": "processed_audio_experimental", "processed_frames": 4,
            "processing_mean_ms": 3.0, "processing_max_ms": 4.0,
            "processed_only_verified": False})


@pytest.fixture
def audio_browser(harness):
    runtimes = []
    def factory(**options):
        runtime = FakeAudioRuntime(harness.trace, **options)
        runtimes.append(runtime)
        return runtime
    with patch("fanyiji.chrome_audio_runtime.ChromeAudioRuntime", side_effect=factory) as constructor:
        yield SimpleNamespace(h=harness, service=harness.service, runtimes=runtimes, constructor=constructor)


def reload_calls(value):
    return [item for item in value.service._page.calls if item[0] == "Page.reload"]


def test_off_default_does_not_construct_runtime_or_reload_page(audio_browser):
    value = audio_browser
    open_browser(value.h)
    value.service.configure_audio_processing("off", reload=True)
    assert value.service.audio_processing_mode == "off"
    assert value.service.audio_status == {"mode": "off", "state": "off", "reason": "disabled"}
    assert value.service._audio_runtime is None
    value.constructor.assert_not_called()
    assert reload_calls(value) == []


def test_configure_without_owned_browser_only_remembers_choice(audio_browser):
    value = audio_browser
    value.service.configure_audio_processing("gtcrn", reload=True)
    assert value.service.audio_processing_mode == "gtcrn"
    assert value.h.launched == []
    value.constructor.assert_not_called()
    open_browser(value.h)
    assert len(value.runtimes) == 1 and value.runtimes[0].started
    assert value.runtimes[0].options["generation"] == value.service.generation
    assert value.runtimes[0].options["url"] == URL


def test_only_changed_mode_reloads_same_page_and_rebuilds_runtime(audio_browser):
    value = audio_browser
    page = open_browser(value.h)
    generation = value.service.generation
    value.service.configure_audio_processing("gtcrn", reload=True)
    first = value.runtimes[-1]
    assert len(reload_calls(value)) == 1
    assert value.service._page is page and len(value.h.launched) == 1
    assert value.service.generation == generation + 1
    value.service.configure_audio_processing("gtcrn", reload=True)
    assert len(value.runtimes) == len(reload_calls(value)) == 1
    assert not first.stopped
    value.service.configure_audio_processing("off", reload=True)
    assert first.stopped and value.service._audio_runtime is None
    assert len(reload_calls(value)) == 2 and value.service.generation == generation + 2
    assert value.service._page is page and len(value.h.launched) == 1
    assert [event["kind"] for event in value.h.events].count("audio_source_changed") == 2


def test_same_model_explicit_retry_replaces_finished_worker_without_page_reload(audio_browser):
    value = audio_browser
    value.service.configure_audio_processing("gtcrn")
    page = open_browser(value.h)
    first = value.runtimes[-1]
    generation = value.service.generation
    value.service.configure_audio_processing("gtcrn", reload=True)
    assert value.runtimes == [first] and first.is_running
    first.ended = True
    value.service.configure_audio_processing("gtcrn", reload=False)
    second = value.runtimes[-1]
    assert first.stopped and second is not first and second.is_running
    assert value.service._page is page and reload_calls(value) == []
    assert value.service.generation == generation and len(value.h.launched) == 1
    events = len(value.h.events)
    first.emit_status()
    assert len(value.h.events) == events
    second.emit_status()
    assert len(value.h.events) == events + 1


def test_recover_stops_before_reload_and_old_generation_callbacks_are_ignored(audio_browser):
    value = audio_browser
    value.service.configure_audio_processing("gtcrn")
    open_browser(value.h)
    first = value.runtimes[-1]
    value.service.recover_caption()
    second = value.runtimes[-1]
    assert first is not second and first.stopped and second.started
    assert second.options["generation"] > first.options["generation"]
    trace = value.h.trace
    stop_index = trace.index(("audio.stop", first.options["generation"]))
    reload_index = trace.index(("page.call", "Page.reload"))
    assert stop_index < reload_index
    events = len(value.h.events)
    first.emit_status()
    assert len(value.h.events) == events
    second.emit_status()
    assert len(value.h.events) == events + 1
    assert value.h.events[-1]["browser_generation"] == value.service.generation


def test_stop_invalidates_same_generation_runtime_callback(audio_browser):
    value = audio_browser
    value.service.configure_audio_processing("gtcrn")
    open_browser(value.h)
    runtime = value.runtimes[-1]
    generation = value.service.generation
    value.service.configure_audio_processing("off", reload=False)
    assert runtime.stopped and value.service.generation == generation
    events = len(value.h.events)
    runtime.emit_status()
    assert len(value.h.events) == events
    assert value.service.audio_status == {"mode": "off", "state": "off", "reason": "disabled"}


def test_browser_close_stops_runtime_before_transport_and_rejects_late_status(audio_browser):
    value = audio_browser
    value.service.configure_audio_processing("gtcrn")
    open_browser(value.h)
    runtime = value.runtimes[-1]
    value.service.stop_browser()
    assert runtime.stopped and value.service._audio_runtime is None
    stop_index = value.h.trace.index(("audio.stop", runtime.options["generation"]))
    assert stop_index < value.h.trace.index(("page.close",))
    events = len(value.h.events)
    runtime.emit_status()
    assert len(value.h.events) == events
    assert runtime.options["source_guard"]() is False


@pytest.mark.parametrize("change", ["second_page", "wrong_room", "generation", "dead_process"])
def test_runtime_source_guard_rejects_unowned_or_changed_media(audio_browser, change):
    value = audio_browser
    value.service.configure_audio_processing("gtcrn")
    open_browser(value.h)
    runtime = value.runtimes[-1]
    assert runtime.options["source_guard"]() is True
    if change == "second_page":
        value.h.state.targets.append({"type": "page", "url": "https://example.com"})
        value.h.state.now += .21
    elif change == "wrong_room":
        value.h.state.targets[0]["url"] = "https://live.bilibili.com/21399689"
        value.h.state.now += .21
    elif change == "generation":
        value.service._generation += 1
    else:
        value.service.process.returncode = 0
    assert runtime.options["source_guard"]() is False


def test_changed_wrong_room_cannot_reload_or_create_audio_runtime(audio_browser):
    value = audio_browser
    open_browser(value.h)
    value.h.state.targets[0]["url"] = "https://live.bilibili.com/21399689"
    with pytest.raises(ChromeControlError, match="wrong_room"):
        value.service.configure_audio_processing("gtcrn", reload=True)
    assert value.runtimes == [] and reload_calls(value) == []


@pytest.mark.parametrize("mode", ["uvr", "raw", None, [], {}])
def test_unsupported_modes_do_not_launch_reload_or_create_runtime(audio_browser, mode):
    value = audio_browser
    with pytest.raises(ValueError, match="invalid_chrome_audio_processing"):
        value.service.configure_audio_processing(mode, reload=True)
    assert value.service.audio_processing_mode == "off"
    assert value.h.launched == []
    value.constructor.assert_not_called()
