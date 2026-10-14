import io
import json
from types import SimpleNamespace

import pytest

from fanyiji.chrome_caption_worker import (AttachedCaptionReader, CaptionProbeError, CaptionReader, CaptionWorker,
                                          NativeUiaAdapter, NativeWindow, safe_diagnostic)


class FakeNative:
    def __init__(self):
        self.is_alive = True
        self.items = [NativeWindow(100, 42, "Test live room - Google Chrome", "Chrome_WidgetWin_1"),
                      NativeWindow(101, 42, "实时字幕", "Chrome_WidgetWin_1")]
        self.url = "live.bilibili.com/1698225"
        self.tabs = 1
        self.editing = False
        self.text = "主播正在讲话"
        self.source_calls, self.text_calls = [], []

    def alive(self):
        return self.is_alive

    def windows(self):
        return self.items

    def browser_source(self, handle):
        self.source_calls.append(handle)
        return self.url, self.tabs, self.editing

    def caption_text(self, handle):
        self.text_calls.append(handle)
        return self.text


def reader(adapter=None):
    return CaptionReader(adapter or FakeNative(), 42, "https://live.bilibili.com/1698225")


def test_only_owned_caption_document_is_requested_after_exact_source_match():
    adapter = FakeNative()
    adapter.items += [NativeWindow(777, 999, "Live Caption", "Chrome_WidgetWin_1"),
                      NativeWindow(778, 999, "Personal tab", "Chrome_WidgetWin_1")]
    assert reader(adapter).read() == ("caption_found", ("101", "主播正在讲话"))
    assert adapter.source_calls == [100, 100]
    assert adapter.text_calls == [101]


@pytest.mark.parametrize("url,tabs", [("https://live.bilibili.com/21399689", 1),
                                     ("https://example.com/1698225", 1),
                                     ("live.bilibili.com/1698225", 2),
                                     ("live.bilibili.com/1698225", 0),
                                     ("live.bilibili.com/1698225", True)])
def test_source_mismatch_or_other_open_tabs_never_reads_caption_text(url, tabs):
    adapter = FakeNative()
    adapter.url, adapter.tabs = url, tabs
    assert reader(adapter).read() == ("source_unverified", None)
    assert not adapter.text_calls


def test_ambiguous_browser_or_captions_do_not_read_arbitrary_documents():
    adapter = FakeNative()
    adapter.items += [NativeWindow(103, 42, "Another tab - Chrome", "Chrome_WidgetWin_1")]
    assert reader(adapter).read() == ("source_unverified", None)
    assert not adapter.source_calls and not adapter.text_calls
    adapter.items.pop()
    adapter.items += [NativeWindow(104, 42, "Live Caption", "Chrome_WidgetWin_1")]
    assert reader(adapter).read() == ("waiting_caption", None)
    assert not adapter.text_calls


def test_no_caption_or_closed_process_never_falls_back_to_web_page_text():
    adapter = FakeNative()
    adapter.items.pop()
    assert reader(adapter).read() == ("waiting_caption", None)
    assert not adapter.text_calls
    adapter.is_alive = False
    assert reader(adapter).read() == ("browser_closed", None)


@pytest.mark.parametrize("value", [None, "x" * 4001, "protocol\x00text"], ids=["missing", "large", "control"])
def test_invalid_caption_text_does_not_become_a_snapshot(value):
    adapter = FakeNative()
    adapter.text = value
    assert reader(adapter).read() == ("waiting_caption", None)


def emitted(sink):
    return [json.loads(line) for line in sink.getvalue().splitlines()]


def test_worker_heartbeat_preserves_revision_without_claiming_official_final():
    adapter, sink = FakeNative(), io.StringIO()
    times = iter([10.0, 10.25, 10.5])
    worker = CaptionWorker(reader(adapter), sink, session=6, generation=9, clock=lambda: next(times))
    assert worker.tick() and worker.tick()
    adapter.text += " 今天天气很好"
    assert worker.tick()
    snapshots = [event for event in emitted(sink) if event["kind"] == "snapshot"]
    assert [event["revision"] for event in snapshots] == [1, 1, 2]
    assert [event["observed_at"] for event in snapshots] == [10.0, 10.25, 10.5]
    assert {event["source_epoch"] for event in snapshots} == {1}
    assert all(event["window_id"] == "101" and event["room"] == "" and event["requested_room"] == "1698225"
               and event["source_verified"] is False for event in snapshots)
    assert all(event["generation"] == event["bridge_generation"] == 9 and event["session"] == 6
               and event["is_official_final"] is False and event["allow_push"] is False for event in snapshots)
    assert all(event["source_lang"] == "unknown" and event["target_lang"] == "zh" for event in snapshots)


def test_source_switch_disconnects_and_reconnection_creates_new_baseline_identity():
    adapter, sink = FakeNative(), io.StringIO()
    worker = CaptionWorker(reader(adapter), sink, session=6, generation=9, clock=lambda: 10.0)
    worker.tick()
    adapter.url = "https://live.bilibili.com/21399689"
    worker.tick()
    worker.tick()  # Repeated unverified state cannot keep returning historical text.
    adapter.url = "live.bilibili.com/1698225"
    worker.tick()
    snapshots = [event for event in emitted(sink) if event["kind"] == "snapshot"]
    assert [event["connected"] for event in snapshots] == [True, False, True]
    assert snapshots[1]["text"] == ""
    assert snapshots[0]["source_epoch"] < snapshots[1]["source_epoch"] < snapshots[2]["source_epoch"]
    assert len(adapter.text_calls) == 2


def test_native_window_replacement_bumps_epoch():
    adapter, sink = FakeNative(), io.StringIO()
    worker = CaptionWorker(reader(adapter), sink, session=6, generation=9, clock=lambda: 10.0)
    worker.tick()
    adapter.items[1] = NativeWindow(444, 42, "实时字幕", "Chrome_WidgetWin_1")
    worker.tick()
    snapshots = [event for event in emitted(sink) if event["kind"] == "snapshot"]
    assert snapshots[1]["source_epoch"] > snapshots[0]["source_epoch"]
    assert snapshots[1]["window_id"] == "444"


def test_reader_error_is_fixed_status_without_raw_page_or_source_data():
    source_reader = reader()
    source_reader.read = lambda: (_ for _ in ()).throw(RuntimeError("private-cookie-and-URL"))
    sink = io.StringIO()
    worker = CaptionWorker(source_reader, sink, session=6, generation=9)
    assert worker.tick()
    assert [event for event in emitted(sink) if event["kind"] != "heartbeat"] == [{"kind": "diagnostic", "stage": "reader", "error_type": "runtime_error",
                             "session": 6, "generation": 9},
                            {"kind": "status", "code": "uia_unavailable", "session": 6, "generation": 9}]
    assert "private" not in sink.getvalue()


class FakeElement:
    def __init__(self, kind, *, name="", children=(), pattern=None, class_name=""):
        self.Current = SimpleNamespace(ControlType=kind, Name=name, HasKeyboardFocus=False, ClassName=class_name)
        self.children, self.pattern = list(children), pattern

    def GetCurrentPattern(self, _pattern):
        return self.pattern


class FakeWalker:
    def __init__(self, root):
        self.siblings = {}

        def scan(element):
            for index, child in enumerate(element.children):
                self.siblings[id(child)] = element.children[index + 1] if index + 1 < len(element.children) else None
                scan(child)
        scan(root)

    def GetFirstChild(self, element):
        return element.children[0] if element.children else None

    def GetNextSibling(self, element):
        return self.siblings[id(element)]


def synthetic_uia(root):
    # __init__ is deliberately bypassed: no pythonnet, HWND or native UIA is loaded in these tests.
    adapter = NativeUiaAdapter.__new__(NativeUiaAdapter)
    adapter.ControlType = SimpleNamespace(Document="document", Text="text", Edit="edit", TabItem="tab-item",
                                         Tab="tab-strip", Button="button", Image="image", Pane="pane")
    adapter.TextPattern = SimpleNamespace(Pattern="text-pattern", IsReadOnlyAttribute="readonly")
    adapter.ValuePattern = SimpleNamespace(Pattern="value-pattern")
    adapter.Walker = FakeWalker(root)
    adapter._element = lambda _handle: root
    return adapter


def test_synthetic_readonly_caption_document_reads_static_lines_and_excludes_buttons():
    text_pattern = SimpleNamespace(DocumentRange=SimpleNamespace(GetAttributeValue=lambda _attribute: True))
    document = FakeElement("document", children=[FakeElement("text", name="第一行字幕"),
                                                 FakeElement("text", name="第二行字幕")], pattern=text_pattern,
                           class_name="CaptionBubbleLabel")
    root = FakeElement("pane", children=[FakeElement("button", name="关闭实时字幕"), document])
    assert synthetic_uia(root).caption_text(101) == "第一行字幕第二行字幕"
    text_pattern.DocumentRange.GetAttributeValue = lambda _attribute: False
    assert synthetic_uia(root).caption_text(101) == "第一行字幕第二行字幕"


def test_caption_bubble_document_name_is_primary_and_does_not_read_visual_fold_lines():
    document = FakeElement("document", name="这是完整字幕 不把视觉折行当断句", class_name="CaptionBubbleLabel",
                           children=[FakeElement("text", name="incomplete visual fold")])
    root = FakeElement("pane", children=[FakeElement("text", name="日语 中文语言标签"), document])
    adapter = synthetic_uia(root)
    first_child = adapter.Walker.GetFirstChild

    def forbidden(element):
        if element is document:
            raise AssertionError("Full Document.Name must not traverse visual fold children")
        return first_child(element)

    adapter.Walker.GetFirstChild = forbidden
    assert adapter.caption_text(101) == "这是完整字幕 不把视觉折行当断句"


def test_unsupported_caption_text_pattern_is_never_requested():
    document = FakeElement("document", class_name="CaptionBubbleLabel",
                           children=[FakeElement("text", name="真实字幕", class_name="AXVirtualView")])

    def unsupported(_pattern):
        raise AssertionError("A native caption reader must not require TextPattern")

    document.GetCurrentPattern = unsupported
    assert synthetic_uia(FakeElement("pane", children=[document])).caption_text(101) == "真实字幕"


def test_arbitrary_web_document_is_not_a_caption_document():
    document = FakeElement("document", name="private-page-content", class_name="HTMLDocument",
                           children=[FakeElement("text", name="private-page-content")])
    assert synthetic_uia(FakeElement("pane", children=[document])).caption_text(101) is None


def test_recycled_caption_hwnd_with_changed_native_title_is_not_read():
    document = FakeElement("document", name="should-not-be-read", class_name="CaptionBubbleLabel")
    adapter = synthetic_uia(FakeElement("pane", children=[document]))
    adapter.user = SimpleNamespace(GetWindowTextW=lambda _handle, buffer, _length: setattr(buffer, "value", "Ordinary Chrome page"),
                                   GetClassNameW=lambda _handle, buffer, _length: setattr(buffer, "value", "Chrome_WidgetWin_1"))
    assert adapter.caption_text(101) is None


def test_synthetic_address_reader_never_descends_into_web_document():
    # A web page can contain a text input with the same accessible Name; it is excluded wholesale.
    native_address = FakeElement("edit", name="地址和搜索栏", pattern=SimpleNamespace(
        Current=SimpleNamespace(Value="live.bilibili.com/1698225")))
    page_address = FakeElement("edit", name="地址和搜索栏", pattern=SimpleNamespace(
        Current=SimpleNamespace(Value="private-page-field")))
    root = FakeElement("pane", children=[FakeElement("toolbar", children=[native_address]),
                                         FakeElement("tab-item"), FakeElement("document", children=[page_address])])
    assert synthetic_uia(root).browser_source(100) == ("live.bilibili.com/1698225", None, False)


def test_focused_address_draft_is_not_treated_as_a_committed_page():
    adapter = FakeNative()
    adapter.editing = True
    assert reader(adapter).read() == ("source_unverified", None)
    assert not adapter.text_calls


@pytest.mark.parametrize("navigation", ["other-room", "address-draft", "another-tab"])
def test_source_is_rechecked_after_caption_read(navigation):
    adapter = FakeNative()

    def navigate(_handle):
        if navigation == "other-room":
            adapter.url = "live.bilibili.com/21399689"
        elif navigation == "address-draft":
            adapter.editing = True
        else:
            adapter.tabs = 2
        return "This belongs to the previous page"

    adapter.caption_text = navigate
    assert reader(adapter).read() == ("source_unverified", None)
    assert adapter.source_calls == [100, 100]


def test_unicode_snapshots_are_ascii_ndjson_independent_of_windows_console_encoding():
    sink = io.StringIO()
    worker = CaptionWorker(reader(), sink, session=6, generation=9, clock=lambda: 10.0)
    worker.tick()
    assert sink.getvalue().isascii()
    assert emitted(sink)[1]["text"] == "主播正在讲话"


def test_oversized_synthetic_accessibility_tree_fails_closed():
    root = FakeElement("pane", children=[FakeElement("text", name="not-caption") for _ in range(129)])
    with pytest.raises(RuntimeError) as error:
        synthetic_uia(root).browser_source(100)
    assert str(error.value) == "source_unverified"


def test_smoke_main_uses_only_fixture_and_never_constructs_native_reader(monkeypatch):
    from fanyiji import chrome_caption_worker
    sink = io.StringIO()
    monkeypatch.setattr(chrome_caption_worker.sys, "stdout", sink)

    def forbidden(_pid):
        raise AssertionError("A smoke test must not initialize native UIA")

    monkeypatch.setattr(chrome_caption_worker, "NativeUiaAdapter", forbidden)
    assert chrome_caption_worker.main(["--smoke-test", "--session", "7", "--generation", "12"]) == 0
    events = emitted(sink)
    assert len(events) == 3
    assert events[0]["code"] == "caption_found_unverified"
    assert events[1]["text"] == "字幕桥本地测试"
    assert events[1]["source_lang"] == "unknown"
    assert events[1]["bridge_generation"] == 12
    assert events[1]["room"] == "" and events[1]["source_verified"] is False
    assert events[2]["kind"] == "heartbeat" and events[2]["connected"] is True


@pytest.mark.parametrize("architecture", ["Framework", "Framework64"])
def test_uia_references_use_existing_system_wpf_absolute_paths(tmp_path, architecture):
    from fanyiji.chrome_caption_worker import _load_uia_references
    windows = tmp_path / "Windows"
    runtime = windows / "Microsoft.NET" / architecture / "v4.0.30319"
    (runtime / "WPF").mkdir(parents=True)
    for name in ("UIAutomationTypes.dll", "UIAutomationClient.dll"):
        (runtime / "WPF" / name).write_bytes(b"synthetic-system-assembly")
    calls = []
    _load_uia_references(SimpleNamespace(AddReference=calls.append), runtime_directory=runtime,
                         windows_directory=windows)
    assert calls == [str((runtime / "WPF" / name).resolve())
                     for name in ("UIAutomationTypes.dll", "UIAutomationClient.dll")]


def test_uia_reference_loader_rejects_external_runtime_even_when_dlls_exist(tmp_path):
    from fanyiji.chrome_caption_worker import _load_uia_references
    runtime = tmp_path / "untrusted" / "v4.0.30319"
    (runtime / "WPF").mkdir(parents=True)
    for name in ("UIAutomationTypes.dll", "UIAutomationClient.dll"):
        (runtime / "WPF" / name).write_bytes(b"untrusted-test-file")
    calls = []
    with pytest.raises(RuntimeError) as error:
        _load_uia_references(SimpleNamespace(AddReference=calls.append), runtime_directory=runtime,
                             windows_directory=tmp_path / "Windows")
    assert str(error.value) == "uia_unavailable"
    assert not calls


def test_uia_missing_assembly_does_not_load_a_partial_set_or_download(tmp_path):
    from fanyiji.chrome_caption_worker import _load_uia_references
    windows = tmp_path / "Windows"
    runtime = windows / "Microsoft.NET" / "Framework64" / "v4.0.30319"
    (runtime / "WPF").mkdir(parents=True)
    (runtime / "WPF" / "UIAutomationTypes.dll").write_bytes(b"test-file")
    calls = []
    with pytest.raises(RuntimeError):
        _load_uia_references(SimpleNamespace(AddReference=calls.append), runtime_directory=runtime,
                             windows_directory=windows)
    assert not calls


class FakeAttachedNative(FakeNative):
    def __init__(self):
        super().__init__()
        self.verified = {42, 999}
        self.other_sources = {}

    def process_verified(self, pid):
        return pid in self.verified

    def browser_source(self, handle):
        if handle == 100:
            return super().browser_source(handle)
        self.source_calls.append(handle)
        return self.other_sources.get(handle, ("example.com", 3, False))


def attached(adapter=None):
    return AttachedCaptionReader(adapter or FakeAttachedNative(), "https://live.bilibili.com/1698225")


def test_attach_multiple_tabs_reads_global_caption_without_claiming_room_ownership():
    adapter = FakeAttachedNative()
    adapter.tabs = 12
    source_reader = attached(adapter)
    assert source_reader.read() == ("caption_found_unverified", ("101", "主播正在讲话"))
    assert source_reader.source_verified is False
    assert adapter.source_calls == [100, 100]
    sink = io.StringIO()
    worker = CaptionWorker(source_reader, sink, session=6, generation=9, clock=lambda: 10.0)
    worker.tick()
    snapshot = emitted(sink)[1]
    assert snapshot["room"] == "" and snapshot["requested_room"] == "1698225"
    assert snapshot["source_verified"] is False
    assert snapshot["allow_push"] is snapshot["is_official_final"] is False


def test_attach_unrelated_browser_windows_and_other_process_caption_are_not_read():
    adapter = FakeAttachedNative()
    adapter.items += [NativeWindow(200, 999, "Other Chrome window", "Chrome_WidgetWin_1"),
                      NativeWindow(201, 999, "Live Caption", "Chrome_WidgetWin_1")]
    assert attached(adapter).read()[0] == "caption_found_unverified"
    assert adapter.text_calls == [101]


def test_attach_rejects_nonofficial_executable_before_requesting_uia_source():
    adapter = FakeAttachedNative()
    adapter.verified.clear()
    assert attached(adapter).read() == ("waiting_browser", None)
    assert not adapter.source_calls and not adapter.text_calls


@pytest.mark.parametrize("ambiguity", ["same-pid-target", "other-pid-target", "two-captions"])
def test_attach_ambiguous_target_or_same_process_caption_is_rejected(ambiguity):
    adapter = FakeAttachedNative()
    if ambiguity == "two-captions":
        adapter.items.append(NativeWindow(102, 42, "Live Caption", "Chrome_WidgetWin_1"))
    else:
        adapter.items.append(NativeWindow(200, 42 if ambiguity == "same-pid-target" else 999,
                                          "Another target window", "Chrome_WidgetWin_1"))
        adapter.other_sources[200] = ("live.bilibili.com/1698225", 4, False)
    assert attached(adapter).read() == ("source_ambiguous", None)
    assert not adapter.text_calls


def test_attach_same_process_unrelated_window_does_not_become_source_claim():
    adapter = FakeAttachedNative()
    adapter.items.append(NativeWindow(200, 42, "Other Chrome window", "Chrome_WidgetWin_1"))
    assert attached(adapter).read()[0] == "caption_found_unverified"
    assert adapter.text_calls == [101]


def test_attach_ignores_caption_in_another_official_chrome_pid():
    adapter = FakeAttachedNative()
    adapter.items[1] = NativeWindow(101, 999, "实时字幕", "Chrome_WidgetWin_1")
    assert attached(adapter).read() == ("waiting_caption", None)
    assert not adapter.text_calls


def test_attach_checks_address_draft_and_post_read_navigation():
    adapter = FakeAttachedNative()
    adapter.editing = True
    assert attached(adapter).read() == ("source_unverified", None)
    assert not adapter.text_calls
    adapter.editing = False

    def navigate(_handle):
        adapter.url = "live.bilibili.com/21399689"
        return "Global caption from an unconfirmed source"

    adapter.caption_text = navigate
    assert attached(adapter).read() == ("source_unverified", None)


def test_attach_becoming_ambiguous_disconnects_the_display_snapshot():
    adapter, sink = FakeAttachedNative(), io.StringIO()
    worker = CaptionWorker(attached(adapter), sink, session=6, generation=9, clock=lambda: 10.0)
    worker.tick()
    adapter.items.append(NativeWindow(102, 42, "Live Caption", "Chrome_WidgetWin_1"))
    worker.tick()
    events = [event for event in emitted(sink) if event["kind"] != "heartbeat"]
    assert events[2]["code"] == "source_ambiguous"
    assert events[3]["connected"] is False
    assert events[3]["text"] == "" and events[3]["room"] == ""


class FakeProcessKernel:
    def __init__(self, paths):
        self.paths, self.dead, self.closed = paths, set(), []

    def OpenProcess(self, _access, _inherit, pid):
        return pid if pid in self.paths else 0

    def QueryFullProcessImageNameW(self, handle, _flags, buffer, _size):
        buffer.value = str(self.paths[handle])
        return True

    def WaitForSingleObject(self, handle, _wait):
        return 0 if handle in self.dead else 258

    def CloseHandle(self, handle):
        self.closed.append(handle)


def test_native_attach_checks_executable_and_retains_only_verified_process_handles(tmp_path):
    official, fake = tmp_path / "official" / "chrome.exe", tmp_path / "other" / "chrome.exe"
    official.parent.mkdir()
    fake.parent.mkdir()
    official.write_bytes(b"test")
    fake.write_bytes(b"test")
    adapter = NativeUiaAdapter.__new__(NativeUiaAdapter)
    adapter.pid, adapter.chrome_path, adapter._processes = 0, official, {}
    adapter.kernel = FakeProcessKernel({42: official, 999: fake})
    assert adapter.process_verified(42)
    assert not adapter.process_verified(999)
    assert adapter._processes == {42: 42}
    assert adapter.kernel.closed == [999]
    adapter.kernel.dead.add(42)
    assert not adapter.process_verified(42)
    assert adapter._processes == {}
    assert adapter.kernel.closed == [999, 42]


def test_native_attach_close_releases_read_handles_without_terminating_any_chrome():
    adapter = NativeUiaAdapter.__new__(NativeUiaAdapter)
    adapter.kernel = FakeProcessKernel({})
    adapter._processes = {42: 42, 99: 99}
    adapter.close()
    assert adapter.kernel.closed == [42, 99]
    assert adapter._processes == {}


@pytest.mark.parametrize("class_name", ["TabStrip", "TabStripRegionView", "Chrome_RenderWidgetHostHWND", "WebView"])
def test_address_walk_never_enumerates_tabstrip_or_webhost_subtrees(class_name):
    address = FakeElement("edit", name="地址和搜索栏", pattern=SimpleNamespace(
        Current=SimpleNamespace(Value="live.bilibili.com/1698225")))
    ignored = FakeElement("pane", class_name=class_name,
                          children=[FakeElement("text", name="private-page-or-tab-label") for _ in range(400)])
    root = FakeElement("pane", children=[ignored, FakeElement("toolbar", children=[address])])
    adapter = synthetic_uia(root)
    first_child = adapter.Walker.GetFirstChild

    def forbidden_subtree(element):
        if element is ignored:
            raise AssertionError("Skipped native subtree must never be enumerated")
        return first_child(element)

    adapter.Walker.GetFirstChild = forbidden_subtree
    assert adapter.browser_source(100) == ("live.bilibili.com/1698225", None, False)


def test_attached_reader_accepts_unknown_tab_count_without_inventing_single_tab():
    adapter = FakeAttachedNative()
    adapter.tabs = None
    assert attached(adapter).read()[0] == "caption_found_unverified"


def test_existing_chrome_without_the_target_current_page_is_source_unverified():
    adapter = FakeAttachedNative()
    adapter.url = "live.bilibili.com/21399689"
    assert attached(adapter).read() == ("source_unverified", None)
    assert not adapter.text_calls
    adapter.items = []
    assert attached(adapter).read() == ("waiting_browser", None)


def test_address_error_is_observable_and_blocks_unproven_target_uniqueness():
    class InvalidOperationException(Exception):
        pass

    adapter = FakeAttachedNative()
    adapter.items.append(NativeWindow(200, 999, "Other Chrome window", "Chrome_WidgetWin_1"))
    original = adapter.browser_source

    def unreadable(handle):
        if handle == 200:
            raise InvalidOperationException("private URL and page content")
        return original(handle)

    adapter.browser_source = unreadable
    source_reader = attached(adapter)
    assert source_reader.read() == ("source_unverified", None)
    assert source_reader.last_diagnostic == {"stage": "address_scan", "error_type": "unsupported_pattern",
                                           "chrome_window_count": 3, "target_count": 1, "address_failures": 1}
    assert not adapter.text_calls
    assert "private" not in json.dumps(source_reader.last_diagnostic)


def test_tree_limit_diagnostic_is_bounded_and_never_exposes_native_text_or_arbitrary_class():
    root = FakeElement("pane", class_name="private-class",
                       children=[FakeElement("pane") for _ in range(129)])
    adapter = synthetic_uia(root)
    with pytest.raises(CaptionProbeError) as error:
        adapter.browser_source(100)
    assert error.value.error_type == "tree_limit"
    assert error.value.metadata["class_kind"] == "other"
    assert "private" not in json.dumps(error.value.metadata)


def test_diagnostic_schema_rejects_dynamic_strings_and_private_fields():
    assert safe_diagnostic({"stage": [], "error_type": "none"}) is None
    assert safe_diagnostic({"stage": "reader", "error_type": {}}) is None
    assert safe_diagnostic({"stage": "reader", "error_type": "private-exception"}) is None
    assert safe_diagnostic({"stage": "reader", "error_type": "tree_limit", "class_kind": "private-class",
                            "control_kind": "Button", "node_count": 257, "depth": 8,
                            "Name": "private", "url": "private", "text": "private"}) == {
        "stage": "reader", "error_type": "tree_limit", "control_kind": "Button", "node_count": 257, "depth": 8}
