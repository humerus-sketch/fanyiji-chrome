"""Native recognizer errors are status only; no browser or .NET UI access."""
import io
import json
from types import SimpleNamespace

import pytest

from fanyiji.chrome_caption_worker import (AttachedCaptionReader, CAPTION_ENGINE_ERRORS, CaptionProbeError,
                                           CaptionReader, CaptionWorker, NativeWindow)
from test_chrome_caption_pattern_fallback import Node, caption_document, native_adapter as metadata_adapter
from test_chrome_caption_watchdog import Clock, harness, raw_snapshot


SOURCE_URL = "https://live.bilibili.com/1698225"


def native_adapter(root, **options):
    stack = [root]
    while stack:
        node = stack.pop()
        node.Current.IsControlElement = node.Current.IsContentElement = True
        stack.extend(node.children)
    return metadata_adapter(root, **options)


def source_adapter(native, *, source=SOURCE_URL):
    return SimpleNamespace(
        windows=lambda: [NativeWindow(100, 42, "Live room - Google Chrome", "Chrome_WidgetWin_1"),
                         NativeWindow(101, 42, "实时字幕", "Chrome_WidgetWin_1")],
        alive=lambda: True, process_verified=lambda pid: pid == 42,
        browser_source=lambda _handle: (source, 1, False),
        caption_engine_status=native.caption_engine_status, caption_text=native.caption_text)


@pytest.mark.parametrize("label", sorted(CAPTION_ENGINE_ERRORS))
def test_known_visible_native_error_is_typed_status_without_caption_text(label):
    error = Node("text", label, class_name="Label")
    document = caption_document(name_error=AssertionError("Old caption must not be read"))
    native = native_adapter(Node("pane", children=[document, error]), forbidden_children=[document])
    assert native.caption_engine_status(101) == "caption_engine_unavailable"
    for reader in (AttachedCaptionReader(source_adapter(native), SOURCE_URL),
                   CaptionReader(source_adapter(native), 42, SOURCE_URL)):
        assert reader.read() == ("caption_engine_unavailable", None)
    assert document.Current.name_reads == 0
    assert error.pattern_calls == 0


@pytest.mark.parametrize("label", sorted(CAPTION_ENGINE_ERRORS))
def test_same_words_spoken_in_caption_document_are_not_an_engine_error(label):
    caption = caption_document(Node("text", label), name=label)
    native = native_adapter(Node("pane", children=[caption]), forbidden_children=[caption])
    assert native.caption_engine_status(101) is None
    assert AttachedCaptionReader(source_adapter(native), SOURCE_URL).read() == (
        "caption_found_unverified", ("101", label))


@pytest.mark.parametrize("kind,class_name", [("document", ""), ("pane", "WebView"),
    ("pane", "Chrome_RenderWidgetHostHWND"), ("pane", "RenderWidgetHostViewAura"),
    ("edit", ""), ("button", ""), ("combo", ""), ("menu", ""), ("link", "")])
def test_engine_probe_never_reads_webpage_or_interactive_subtrees(kind, class_name):
    secret = Node("text", name_error=AssertionError("Private content must not be read"))
    excluded = Node(kind, class_name=class_name, children=[secret],
                    name_error=AssertionError("Private control must not be read"))
    native = native_adapter(Node("pane", children=[excluded]), forbidden_children=[excluded])
    assert native.caption_engine_status(101) is None
    assert excluded.Current.name_reads == secret.Current.name_reads == 0


def test_hidden_error_label_and_nonexact_substring_are_not_engine_errors():
    hidden = Node("text", "暂时无法使用实时字幕功能")
    hidden.Current.IsOffscreen = True
    sentence = Node("text", "他说 暂时无法使用实时字幕功能 然后继续")
    native = native_adapter(Node("pane", children=[hidden, sentence]))
    assert native.caption_engine_status(101) is None
    assert hidden.Current.name_reads == 0


@pytest.mark.parametrize("field,visible", [("IsControlElement", False), ("IsContentElement", False),
                                          ("IsControlElement", None), ("IsContentElement", None)])
def test_raw_hidden_error_with_offscreen_false_requires_native_visible_control_metadata(field, visible):
    hidden = Node("text", name_error=AssertionError("Invisible RawView label must not be read"))
    hidden.Current.IsOffscreen = False
    native = native_adapter(Node("pane", children=[hidden]))
    setattr(hidden.Current, field, visible)
    assert native.caption_engine_status(101) is None
    assert hidden.Current.name_reads == 0


def test_noncontrol_parent_does_not_hide_its_visible_native_error_child():
    error = Node("text", "暂时无法使用实时字幕功能")
    parent = Node("pane", children=[error])
    native = native_adapter(parent)
    parent.Current.IsControlElement = parent.Current.IsContentElement = False
    assert native.caption_engine_status(101) == "caption_engine_unavailable"


def test_engine_label_can_be_visually_wrapped_but_only_exact_resource_matches():
    native = native_adapter(Node("pane", children=[Node("text", "Live Caption is not\n available right now")]))
    assert native.caption_engine_status(101) == "caption_engine_unavailable"


def test_recycled_window_title_prevents_engine_tree_read():
    error = Node("text", name_error=AssertionError("A recycled window must not be inspected"))
    native = native_adapter(Node("pane", children=[error]))

    def title(_handle, buffer, _length):
        buffer.value = "Another browser window"

    def class_name(_handle, buffer, _length):
        buffer.value = "Chrome_WidgetWin_1"

    native.user = SimpleNamespace(GetWindowTextW=title, GetClassNameW=class_name)
    assert native.caption_engine_status(101) is None
    assert error.Current.name_reads == 0


@pytest.mark.parametrize("limit", ["children", "nodes", "depth"])
def test_engine_scan_has_bounded_tree_work(limit):
    if limit == "children":
        root = Node("pane", children=[Node("text", "other") for _ in range(129)])
    elif limit == "nodes":
        root = Node("pane", children=[Node("pane", children=[Node("text", "other") for _ in range(2)])
                                      for _ in range(128)])
    else:
        root = Node("text", "暂时无法使用实时字幕功能")
        for _ in range(18):
            root = Node("pane", children=[root])
    with pytest.raises(CaptionProbeError) as error:
        native_adapter(root).caption_engine_status(101)
    assert error.value.stage == "caption_walk" and error.value.error_type == "tree_limit"


@pytest.mark.parametrize("reader_kind", ["attached", "owned"])
def test_navigation_race_cannot_publish_error_from_wrong_source(reader_kind):
    native = native_adapter(Node("pane", children=[Node("text", "暂时无法使用实时字幕功能")]))
    adapter = source_adapter(native)
    addresses = iter([(SOURCE_URL, 1, False), ("https://live.bilibili.com/21399689", 1, False)])
    adapter.browser_source = lambda _handle: next(addresses)
    reader = AttachedCaptionReader(adapter, SOURCE_URL) if reader_kind == "attached" else CaptionReader(adapter, 42, SOURCE_URL)
    assert reader.read() == ("source_unverified", None)


def test_worker_error_keeps_heartbeat_alive_and_invalidates_prior_caption_once():
    clock, sink = Clock(), io.StringIO()
    state = {"error": False}
    reader = SimpleNamespace(source_url=SOURCE_URL, read=lambda: ("caption_engine_unavailable", None)
        if state["error"] else ("caption_found_unverified", ("101", "此前字幕")))
    worker = CaptionWorker(reader, sink, session=7, generation=3, clock=clock)
    assert worker.tick()
    state["error"] = True
    for _ in range(25):
        clock.advance(0.25)
        assert worker.tick()
    events = [json.loads(line) for line in sink.getvalue().splitlines()]
    snapshots = [event for event in events if event["kind"] == "snapshot"]
    assert len(snapshots) == 2
    assert snapshots[-1]["text"] == "" and snapshots[-1]["connected"] is False
    assert all(not event["allow_push"] and not event["is_official_final"] for event in snapshots)
    errors = [event for event in events if event["kind"] == "status" and event["code"] == "caption_engine_unavailable"]
    assert len(errors) == 1 and "text" not in errors[0]
    heartbeats = [event for event in events if event["kind"] == "heartbeat"]
    assert len(heartbeats) == 4
    assert all(event["status"] == "caption_engine_unavailable" and not event["connected"] for event in heartbeats[1:])
    assert not any(label in sink.getvalue() for label in CAPTION_ENGINE_ERRORS)


def test_unreadable_native_error_reports_only_safe_diagnostic():
    sensitive = "sensitive-native-name-session-token"
    native = native_adapter(Node("pane", children=[Node("text", name_error=RuntimeError(sensitive))]))
    reader = AttachedCaptionReader(source_adapter(native), SOURCE_URL)
    sink = io.StringIO()
    assert CaptionWorker(reader, sink, session=7, generation=3, clock=Clock()).tick()
    events = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert sensitive not in sink.getvalue()
    assert not any(event["kind"] == "snapshot" or "text" in event for event in events)
    assert any(event.get("kind") == "diagnostic" and event["error_type"] == "runtime_error" for event in events)
    assert any(event.get("kind") == "heartbeat" and event["status"] == "uia_unavailable" for event in events)


def test_healthy_engine_error_does_not_trigger_reader_restart(harness):
    service, clock, workers, events, calls = harness
    observed_health = []
    values = [{"kind": "status", "code": "caption_engine_unavailable"}]
    values += [{"kind": "heartbeat", "connected": False, "revision": 0, "source_epoch": 0,
                "status": "caption_engine_unavailable", "snapshot_age_seconds": None,
                "caption_update_age_seconds": None} for _ in range(70)]

    class Pipe:
        def readline(self, _limit):
            if not values:
                service.stop()
                return ""
            value = values.pop(0)
            if value["kind"] == "heartbeat":
                observed_health.append(service.health_snapshot)
                service.watchdog_tick()
                clock.advance(2)
                value["observed_at"] = clock()
            return json.dumps({**value, "generation": service.generation, "session": 7}) + "\n"

        def close(self):
            pass

    workers[0].stdout = Pipe()
    service._read(workers[0], service.generation)
    assert len(calls) == 1
    assert len(observed_health) == 70
    assert all(health["reader_alive"] and health["reader_recovery_state"] == "healthy"
               and health["caption_state"] == "caption_engine_unavailable"
               and health["recovery_attempts"] == 0 for health in observed_health)
    assert not any(event.get("code") == "reader_recovering" for event in events)


def test_engine_error_health_clears_on_valid_caption_or_explicit_restart(harness):
    service, _clock, workers, _events, calls = harness
    service._caption_status = "caption_engine_unavailable"
    assert service.health_snapshot["caption_state"] == "caption_engine_unavailable"
    value = service._snapshot_continuity(raw_snapshot(service, "恢复的字幕"))
    assert value["connected"] and service.health_snapshot["caption_state"] == "updated"
    service._caption_status = "caption_engine_unavailable"
    service.start(SOURCE_URL, session=8, owned_browser_pid=900)
    assert workers[0].dead and len(calls) == 2
    assert service.health_snapshot["caption_state"] == "disconnected"
