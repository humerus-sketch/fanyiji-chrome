"""Native caption StaticText compatibility; no pythonnet or browser access."""
import json
from types import SimpleNamespace

import pytest

from fanyiji.chrome_caption_worker import AttachedCaptionReader, NativeUiaAdapter, NativeWindow


class InvalidOperationException(Exception):
    pass


class ElementNotAvailableException(Exception):
    pass


class UnauthorizedAccessException(Exception):
    pass


class COMException(Exception):
    pass


class CurrentMetadata:
    def __init__(self, kind, name="", class_name="", *, name_error=None):
        self.ControlType, self.ClassName = kind, class_name
        self.HasKeyboardFocus = self.IsOffscreen = False
        self._name, self.name_error, self.name_reads = name, name_error, 0

    @property
    def Name(self):
        self.name_reads += 1
        if self.name_error is not None:
            raise self.name_error
        return self._name


class Node:
    def __init__(self, kind, name="", *, class_name="", children=(), name_error=None, pattern_error=None):
        self.Current = CurrentMetadata(kind, name, class_name, name_error=name_error)
        self.children = list(children)
        self.pattern_error = pattern_error or InvalidOperationException("TextPattern not supported")
        self.pattern_calls = 0

    def GetCurrentPattern(self, _pattern):
        self.pattern_calls += 1
        raise self.pattern_error


class MetadataWalker:
    def __init__(self, root, *, forbidden_children=()):
        self.siblings = {}
        self.forbidden = {id(node) for node in forbidden_children}

        def scan(parent):
            for index, child in enumerate(parent.children):
                self.siblings[id(child)] = parent.children[index + 1] if index + 1 < len(parent.children) else None
                scan(child)

        scan(root)

    def GetFirstChild(self, parent):
        if id(parent) in self.forbidden:
            raise AssertionError("Excluded subtree must not be traversed")
        return parent.children[0] if parent.children else None

    def GetNextSibling(self, node):
        return self.siblings[id(node)]


def native_adapter(root, *, forbidden_children=()):
    # Bypass native construction, process queries, .NET references and HWND lookup.
    adapter = NativeUiaAdapter.__new__(NativeUiaAdapter)
    adapter.ControlType = SimpleNamespace(Document="document", Text="text", Edit="edit", Button="button",
        Pane="pane", Window="window", Hyperlink="link", ComboBox="combo", CheckBox="check",
        Menu="menu", MenuItem="menu-item", List="list", ListItem="list-item", Tab="tab", TabItem="tab-item",
        Image="image", ToolBar="toolbar")
    adapter.TextPattern = SimpleNamespace(Pattern="text-pattern", IsReadOnlyAttribute="readonly")
    adapter.Walker = MetadataWalker(root, forbidden_children=forbidden_children)
    adapter._element = lambda _handle: root
    return adapter


def caption_document(*lines, **options):
    return Node("document", class_name="CaptionBubbleLabel", children=lines, **options)


def attached_reader(adapter):
    windows = [NativeWindow(100, 42, "Live room - Google Chrome", "Chrome_WidgetWin_1"),
               NativeWindow(101, 42, "实时字幕", "Chrome_WidgetWin_1")]
    source = SimpleNamespace(windows=lambda: windows, process_verified=lambda pid: pid == 42,
        browser_source=lambda _handle: ("live.bilibili.com/1698225", None, False),
        caption_text=adapter.caption_text)
    return AttachedCaptionReader(source, "https://live.bilibili.com/1698225")


def test_native_static_caption_without_textpattern_still_reads_visible_lines():
    first, second = Node("text", "第一行中文字幕"), Node("text", "第二行中文字幕")
    document = caption_document(first, second)
    adapter = native_adapter(Node("pane", class_name="RootView", children=[document]))
    assert adapter.caption_text(101) == "第一行中文字幕第二行中文字幕"
    assert document.pattern_calls == first.pattern_calls == second.pattern_calls == 0


def test_static_reading_does_not_even_require_adapter_textpattern_member():
    document = caption_document(Node("text", "只使用原生静态文本"))
    adapter = native_adapter(Node("pane", children=[document]))
    del adapter.TextPattern
    assert adapter.caption_text(101) == "只使用原生静态文本"
    assert document.pattern_calls == 0


def test_complete_native_document_name_takes_priority_over_visual_line_wrapping():
    text = "今天先调整一下游戏音量 然后继续下一关 这是一句完整的中文字幕"
    document = caption_document(Node("text", name_error=AssertionError("visual line must not be read")), name=text)
    adapter = native_adapter(Node("pane", children=[document]), forbidden_children=[document])
    assert adapter.caption_text(101) == text
    assert document.pattern_calls == 0
    assert document.children[0].Current.name_reads == 0


@pytest.mark.parametrize("empty_name", ["", None])
def test_empty_document_name_uses_visual_substrings_without_inventing_line_breaks(empty_name):
    document = caption_document(Node("text", "今天先调"), Node("text", "整游戏音量 "),
                                Node("text", "然后继续下一关"), name=empty_name)
    adapter = native_adapter(Node("pane", children=[document]))
    assert adapter.caption_text(101) == "今天先调整游戏音量 然后继续下一关"


@pytest.mark.parametrize("class_name", ["", "UnrelatedDocument", "WebView"])
def test_unrecognized_document_class_never_uses_name_or_children_as_caption(class_name):
    document = Node("document", class_name=class_name, name_error=AssertionError("unrelated document"),
                    children=[Node("text", name_error=AssertionError("unrelated document text"))])
    adapter = native_adapter(Node("pane", children=[document]), forbidden_children=[document])
    assert adapter.caption_text(101) is None
    assert document.Current.name_reads == document.children[0].Current.name_reads == 0


@pytest.mark.parametrize("unused_pattern_error", [
    InvalidOperationException("not supported"), ElementNotAvailableException("not available"),
    UnauthorizedAccessException("denied"), COMException("interop error"), RuntimeError("other error"),
])
def test_unused_pattern_provider_is_never_called_or_used_to_decide_caption_validity(unused_pattern_error):
    # No fallback catch is needed: TextPattern must be entirely absent from this path.
    document = caption_document(Node("text", "当前可读字幕"), pattern_error=unused_pattern_error)
    adapter = native_adapter(Node("pane", children=[document]))
    assert adapter.caption_text(101) == "当前可读字幕"
    assert document.pattern_calls == 0


def test_window_buttons_language_selector_and_plain_labels_never_become_caption_lines():
    caption = Node("text", "真正的中文字幕")
    document = caption_document(caption)
    controls = [Node("button", "关闭字幕", children=[Node("text", name_error=AssertionError("button text"))]),
                Node("combo", "选择语言", children=[Node("text", name_error=AssertionError("language value"))]),
                Node("text", name_error=AssertionError("outside document label"))]
    adapter = native_adapter(Node("pane", children=[*controls, document]), forbidden_children=controls[:2])
    assert adapter.caption_text(101) == "真正的中文字幕"
    assert all(control.Current.name_reads == 0 for control in controls)
    assert all(child.Current.name_reads == 0 for control in controls for child in control.children)


@pytest.mark.parametrize("kind", ["button", "link", "edit", "combo", "check", "menu", "menu-item"])
def test_nested_interactive_control_text_is_not_read_as_caption(kind):
    ignored = Node(kind, "native control", children=[Node("text", name_error=AssertionError("control value"))])
    document = caption_document(Node("text", "只取字幕正文"), ignored)
    adapter = native_adapter(Node("pane", children=[document]), forbidden_children=[ignored])
    assert adapter.caption_text(101) == "只取字幕正文"
    assert ignored.Current.name_reads == ignored.children[0].Current.name_reads == 0


@pytest.mark.parametrize("class_name", ["WebView", "Chrome_RenderWidgetHostHWND", "RenderWidgetHostViewAura"])
def test_web_document_host_is_never_walked_even_inside_a_chrome_caption_titled_window(class_name):
    page_document = Node("document", children=[Node("text", name_error=AssertionError("private webpage text"))])
    page_host = Node("pane", class_name=class_name, children=[page_document])
    caption = caption_document(Node("text", "来自原生字幕文档"))
    adapter = native_adapter(Node("pane", children=[page_host, caption]), forbidden_children=[page_host])
    assert adapter.caption_text(101) == "来自原生字幕文档"
    assert page_document.children[0].Current.name_reads == 0


def test_only_web_document_is_not_a_native_caption_document():
    page_host = Node("pane", class_name="WebView", children=[Node("document", children=[
        Node("text", name_error=AssertionError("private webpage text"))])])
    adapter = native_adapter(Node("pane", children=[page_host]), forbidden_children=[page_host])
    assert adapter.caption_text(101) is None


def test_no_document_never_uses_window_label_or_button_as_caption_fallback():
    plain = Node("text", name_error=AssertionError("window label"))
    button = Node("button", children=[Node("text", name_error=AssertionError("button label"))])
    adapter = native_adapter(Node("pane", children=[plain, button]), forbidden_children=[button])
    assert adapter.caption_text(101) is None
    assert plain.Current.name_reads == button.children[0].Current.name_reads == 0


def test_ambiguous_native_documents_are_rejected_before_reading_either_text():
    first = caption_document(Node("text", name_error=AssertionError("ambiguous first document")))
    second = caption_document(Node("text", name_error=AssertionError("ambiguous second document")))
    adapter = native_adapter(Node("pane", children=[first, second]), forbidden_children=[first, second])
    assert adapter.caption_text(101) is None


@pytest.mark.parametrize("error,expected_type", [
    (InvalidOperationException("private field"), "unsupported_pattern"),
    (ElementNotAvailableException("private field"), "element_unavailable"),
    (UnauthorizedAccessException("private field"), "permission_denied"),
    (COMException("private field"), "interop_error"),
    (AttributeError("private field"), "attribute_error"),
    (RuntimeError("private field"), "runtime_error"),
])
def test_real_static_text_read_errors_remain_observable_and_never_successfully_fallback(error, expected_type):
    document = caption_document(Node("text", name_error=error))
    adapter = native_adapter(Node("pane", children=[document]))
    reader = attached_reader(adapter)
    assert reader.read() == ("uia_unavailable", None)
    assert reader.last_diagnostic["stage"] == "caption_read"
    assert reader.last_diagnostic["error_type"] == expected_type
    assert "private" not in json.dumps(reader.last_diagnostic)
    assert document.pattern_calls == 0


@pytest.mark.parametrize("error,expected_type", [
    (ElementNotAvailableException("private document field"), "element_unavailable"),
    (UnauthorizedAccessException("private document field"), "permission_denied"),
    (COMException("private document field"), "interop_error"),
    (RuntimeError("private document field"), "runtime_error"),
])
def test_document_name_read_errors_do_not_fallback_to_possibly_stale_visual_text(error, expected_type):
    document = caption_document(Node("text", name_error=AssertionError("fallback must not hide metadata failure")),
                                name_error=error)
    adapter = native_adapter(Node("pane", children=[document]), forbidden_children=[document])
    reader = attached_reader(adapter)
    assert reader.read() == ("uia_unavailable", None)
    assert reader.last_diagnostic["stage"] == "caption_read"
    assert reader.last_diagnostic["error_type"] == expected_type
    assert "private" not in json.dumps(reader.last_diagnostic)
    assert document.children[0].Current.name_reads == 0
