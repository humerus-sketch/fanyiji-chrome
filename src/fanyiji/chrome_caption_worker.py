"""Readonly attachment to Chrome's global Live Caption window.

Chromium's CaptionBubbleLabel is a readonly Document with StaticText children:
components/live_caption/views/caption_bubble.cc. These are revisable viewport
snapshots, not official utterance-final events. Unsupported UIA layouts fail closed.
"""
from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import time

from .chrome_caption_runtime import (find_chrome, matches_installed_chrome, observed_source_matches,
                                     source_room, validate_source_url)


# Chromium's IDS_LIVE_CAPTION_BUBBLE_TITLE / IDS_ACCNAME_LOCATION resources.
# Actual platform accessibility mappings still require validation on the user's Chrome.
CAPTION_TITLES = frozenset({"Live Caption", "实时字幕"})
ADDRESS_NAMES = frozenset({"Address and search bar", "地址和搜索栏", "地址栏和搜索栏"})
# IDS_LIVE_CAPTION_BUBBLE_ERROR, verified against Chromium 155.0.8059.40
# components/live_caption_strings.grdp and components_strings_{en-GB,zh-CN}.xtb.
CAPTION_ENGINE_ERRORS = frozenset({"Live Caption is not available right now",
                                   "Live Caption is not available at the moment",
                                   "暂时无法使用实时字幕功能"})
_PROBE_STAGES = frozenset({"adapter_init", "windows", "address_scan", "address_walk", "caption_read",
                           "caption_walk", "address_recheck", "reader"})
_ERROR_TYPES = frozenset({"none", "tree_limit", "runtime_error", "attribute_error", "type_error", "value_error",
                         "os_error", "unsupported_pattern", "element_unavailable", "permission_denied",
                         "interop_error", "assembly_missing", "dependency_missing", "other_error"})
_CLASS_KINDS = frozenset({"RootView", "BrowserView", "ToolbarView", "LocationBarView", "OmniboxViewViews",
                         "TabStrip", "TabStripRegionView", "TabStripFlatEdgeButton", "Chrome_WidgetWin_1",
                         "Chrome_RenderWidgetHostHWND", "WebView", "RenderWidgetHostViewAura", "other"})
_CONTROL_KINDS = frozenset({"Document", "Button", "Pane", "Text", "Tab", "TabItem", "Edit", "Image",
                           "ToolBar", "Window", "other"})


class CaptionProbeError(RuntimeError):
    def __init__(self, stage, error_type, **metadata):
        self.stage, self.error_type, self.metadata = stage, error_type, metadata
        super().__init__("source_unverified")


def safe_diagnostic(value):
    if (not isinstance(value, dict) or not isinstance(value.get("stage"), str)
            or not isinstance(value.get("error_type"), str) or value["stage"] not in _PROBE_STAGES
            or value["error_type"] not in _ERROR_TYPES):
        return None
    result = {"stage": value["stage"], "error_type": value["error_type"]}
    for key in ("node_count", "depth", "chrome_window_count", "target_count", "caption_count", "address_failures"):
        if type(value.get(key)) is int and 0 <= value[key] <= 65536:
            result[key] = value[key]
    for key, allowed in (("class_kind", _CLASS_KINDS), ("control_kind", _CONTROL_KINDS)):
        if isinstance(value.get(key), str) and value[key] in allowed:
            result[key] = value[key]
    return result


def exception_diagnostic(error, stage):
    # Never inspect str(error), Message, Data, Name, URL or a provider's arbitrary class name.
    names = {"ModuleNotFoundError": "dependency_missing", "ImportError": "dependency_missing",
             "RuntimeError": "runtime_error", "AttributeError": "attribute_error", "TypeError": "type_error",
             "ValueError": "value_error", "OSError": "os_error", "InvalidOperationException": "unsupported_pattern",
             "ElementNotAvailableException": "element_unavailable", "UnauthorizedAccessException": "permission_denied",
             "COMException": "interop_error", "ArgumentException": "interop_error",
             "FileNotFoundException": "assembly_missing"}
    if isinstance(error, CaptionProbeError):
        result = {"stage": error.stage, "error_type": error.error_type, **error.metadata}
    else:
        result = {"stage": stage, "error_type": names.get(type(error).__name__, "other_error")}
    return safe_diagnostic(result) or {"stage": "reader", "error_type": "other_error"}


def _windows_directory() -> Path:
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetWindowsDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel.GetWindowsDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    size = kernel.GetWindowsDirectoryW(buffer, len(buffer))
    if size == 0 or size >= len(buffer):
        raise RuntimeError("uia_unavailable")
    return Path(buffer.value)


def _load_uia_references(clr, *, runtime_directory=None, windows_directory=None) -> None:
    """pythonnet does not always resolve the WPF assemblies by their GAC names."""
    try:
        if runtime_directory is None:
            from System.Runtime.InteropServices import RuntimeEnvironment
            runtime_directory = str(RuntimeEnvironment.GetRuntimeDirectory())
        windows = Path(windows_directory) if windows_directory is not None else _windows_directory()
        runtime = Path(runtime_directory).resolve()
        roots = {(windows / "Microsoft.NET" / name).resolve() for name in ("Framework", "Framework64")}
        if (runtime.parent not in roots or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", runtime.name)
                or not runtime.is_dir()):
            raise RuntimeError("uia_unavailable")
        wpf = (runtime / "WPF").resolve()
        references = [(wpf / name).resolve() for name in ("UIAutomationTypes.dll", "UIAutomationClient.dll")]
        if (wpf.parent != runtime or any(path.parent != wpf or not path.is_file() or path.stat().st_size == 0
                                         for path in references)):
            raise RuntimeError("uia_unavailable")
        for path in references:
            clr.AddReference(str(path))
    except Exception:
        raise RuntimeError("uia_unavailable") from None


def _initialize_uia_types():
    """Load the actual CLR/UIA components without touching a browser or UI tree."""
    if os.name != "nt":
        raise RuntimeError("unsupported_platform")
    import clr
    _load_uia_references(clr)
    from System import IntPtr
    from System.Windows.Automation import AutomationElement, ControlType, TextPattern, TreeWalker, ValuePattern
    return IntPtr, AutomationElement, ControlType, TextPattern, TreeWalker, ValuePattern


def check_uia_dependencies():
    """Use the adapter's real initialization path, without enumerating windows."""
    _initialize_uia_types()
    return {"uia_dependencies": "passed", "windows_enumerated": False,
            "browser_opened": False, "profile_read": False}


@dataclass(frozen=True)
class NativeWindow:
    handle: int
    process_id: int
    name: str
    class_name: str


class CaptionReader:
    """The pure selection layer never requests text before source verification."""

    def __init__(self, adapter, browser_pid: int, source_url: str):
        self.adapter, self.browser_pid = adapter, browser_pid
        self.source_url = validate_source_url(source_url)

    def read(self):
        if not self.adapter.alive():
            return "browser_closed", None
        # Even a faulty adapter cannot make a different Chrome PID eligible.
        windows = [window for window in self.adapter.windows()
                   if window.process_id == self.browser_pid and window.handle > 0
                   and window.class_name.startswith("Chrome_WidgetWin_")]
        captions = [window for window in windows if window.name in CAPTION_TITLES]
        browsers = [window for window in windows if window.name not in CAPTION_TITLES]
        if len(browsers) != 1:
            return "source_unverified", None
        observed_url, tab_count, editing = self.adapter.browser_source(browsers[0].handle)
        if (type(tab_count) is not int or tab_count != 1 or type(editing) is not bool or editing
                or not observed_source_matches(observed_url, self.source_url)):
            return "source_unverified", None
        if len(captions) != 1:
            return "waiting_caption", None
        probe = getattr(self.adapter, "caption_engine_status", None)
        engine_status = probe(captions[0].handle) if callable(probe) else None
        text = None if engine_status == "caption_engine_unavailable" else self.adapter.caption_text(captions[0].handle)
        if engine_status != "caption_engine_unavailable" and (not isinstance(text, str) or len(text) > 4000
                or any(ord(char) < 32 and char not in "\n\r\t" for char in text)):
            return "waiting_caption", None
        # Native page navigation can race the UIA text read; an editable draft is not a URL commit.
        after_url, after_tabs, after_editing = self.adapter.browser_source(browsers[0].handle)
        if (type(after_tabs) is not int or after_tabs != 1 or type(after_editing) is not bool or after_editing
                or not observed_source_matches(after_url, self.source_url)):
            return "source_unverified", None
        if engine_status == "caption_engine_unavailable":
            return engine_status, None
        return "caption_found", (str(captions[0].handle), text)


class AttachedCaptionReader:
    """A target page selects a Chrome PID; it does not identify the media stream.

    Chrome selects which audio stream is shown in its global CaptionBubble. UIA
    exposes no per-tab session/source relation, so all output remains unverified.
    """
    source_verified = False

    def __init__(self, adapter, source_url: str):
        self.adapter, self.source_url = adapter, validate_source_url(source_url)
        self.last_diagnostic = None

    def read(self):
        try:
            windows = [window for window in self.adapter.windows()
                       if window.handle > 0 and window.class_name.startswith("Chrome_WidgetWin_")
                       and self.adapter.process_verified(window.process_id)]
        except Exception as error:
            self.last_diagnostic = exception_diagnostic(error, "windows")
            return "uia_unavailable", None
        candidates, failures = [], []
        for window in windows:
            if window.name in CAPTION_TITLES:
                continue
            try:
                address, _unknown_tabs, editing = self.adapter.browser_source(window.handle)
            except Exception as error:
                failures.append(exception_diagnostic(error, "address_scan"))
                continue
            if (type(editing) is bool and not editing
                    and observed_source_matches(address, self.source_url)):
                candidates.append(window)
        counts = {"chrome_window_count": len(windows), "target_count": len(candidates), "address_failures": len(failures)}
        if failures:
            self.last_diagnostic = {**failures[0], **counts}
            return "source_unverified", None  # Cannot prove a unique target if a window was unreadable.
        self.last_diagnostic = {"stage": "address_scan", "error_type": "none", **counts}
        if not candidates:
            return ("source_unverified" if windows else "waiting_browser"), None
        if len(candidates) != 1:
            return "source_ambiguous", None
        browser = candidates[0]
        captions = [window for window in windows
                    if window.process_id == browser.process_id and window.name in CAPTION_TITLES]
        self.last_diagnostic["caption_count"] = len(captions)
        if len(captions) > 1:
            return "source_ambiguous", None
        if not captions:
            return "waiting_caption", None
        try:
            probe = getattr(self.adapter, "caption_engine_status", None)
            engine_status = probe(captions[0].handle) if callable(probe) else None
            text = None if engine_status == "caption_engine_unavailable" else self.adapter.caption_text(captions[0].handle)
        except Exception as error:
            self.last_diagnostic = {**exception_diagnostic(error, "caption_read"), **counts, "caption_count": len(captions)}
            return "uia_unavailable", None
        if engine_status != "caption_engine_unavailable" and (not isinstance(text, str) or len(text) > 4000
                or any(ord(char) < 32 and char not in "\n\r\t" for char in text)):
            return "waiting_caption", None
        # This checks target-page continuity only; never call it caption-source proof.
        try:
            address, _unknown_tabs, editing = self.adapter.browser_source(browser.handle)
        except Exception as error:
            self.last_diagnostic = {**exception_diagnostic(error, "address_recheck"), **counts}
            return "source_unverified", None
        if (type(editing) is not bool or editing
                or not observed_source_matches(address, self.source_url)
                or not self.adapter.process_verified(browser.process_id)):
            return "source_unverified", None
        if engine_status == "caption_engine_unavailable":
            return engine_status, None
        return "caption_found_unverified", (str(captions[0].handle), text)


class NativeUiaAdapter:
    """Only installed Chrome processes are eligible for native shell/caption UIA."""

    def __init__(self, browser_pid: int = 0, *, chrome_path=None):
        if os.name != "nt":
            raise RuntimeError("unsupported_platform")
        from ctypes import wintypes
        IntPtr, AutomationElement, ControlType, TextPattern, TreeWalker, ValuePattern = _initialize_uia_types()
        self.IntPtr, self.Element, self.ControlType = IntPtr, AutomationElement, ControlType
        self.TextPattern, self.ValuePattern, self.Walker = TextPattern, ValuePattern, TreeWalker.RawViewWalker
        self.pid = browser_pid
        self.chrome_path = find_chrome() if chrome_path is None else Path(chrome_path)
        if self.chrome_path is None:
            raise RuntimeError("chrome_missing")
        self._processes = {}
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                         wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsWindowVisible.restype = wintypes.BOOL
        self.user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self._enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumWindows.argtypes = [self._enum_callback, wintypes.LPARAM]
        self.user.EnumWindows.restype = wintypes.BOOL
        if browser_pid and not self.process_verified(browser_pid):
            raise RuntimeError("browser_closed")

    def process_verified(self, pid):
        from ctypes import wintypes
        if type(pid) is not int or pid <= 0 or self.pid and pid != self.pid:
            return False
        if pid in self._processes:
            if self.kernel.WaitForSingleObject(self._processes[pid], 0) == 258:
                return True
            self.kernel.CloseHandle(self._processes.pop(pid))
            return False
        handle = self.kernel.OpenProcess(0x00101000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        path, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        if (not self.kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size))
                or not matches_installed_chrome(path.value, self.chrome_path)
                or self.kernel.WaitForSingleObject(handle, 0) != 258):
            self.kernel.CloseHandle(handle)
            return False
        # Keep the handle: a reused PID cannot silently replace the verified process.
        self._processes[pid] = handle
        return True

    def alive(self) -> bool:
        return not self.pid or self.process_verified(self.pid)

    def windows(self) -> list[NativeWindow]:
        from ctypes import wintypes
        result = []

        def visit(handle, _parameter):
            pid = wintypes.DWORD()
            self.user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            if not self.user.IsWindowVisible(handle):
                return True
            name, class_name = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(128)
            self.user.GetClassNameW(handle, class_name, len(class_name))
            if not class_name.value.startswith("Chrome_WidgetWin_") or not self.process_verified(int(pid.value)):
                return True
            # Titles/UIA are read only after Chrome class and official executable path verification.
            self.user.GetWindowTextW(handle, name, len(name))
            result.append(NativeWindow(int(handle), pid.value, name.value, class_name.value))
            return True

        self.user.EnumWindows(self._enum_callback(visit), 0)
        return result

    def _element(self, handle):
        from ctypes import wintypes
        pid = wintypes.DWORD()
        self.user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        if not self.process_verified(int(pid.value)):
            raise RuntimeError("source_unverified")
        return self.Element.FromHandle(self.IntPtr(handle))

    def _walk(self, root, *, skip_documents=False):
        stack, count = [(root, 0)], 0
        while stack:
            element, depth = stack.pop()
            count += 1
            current = element.Current
            class_name = str(getattr(current, "ClassName", ""))
            class_kind = class_name if class_name in _CLASS_KINDS else "other"
            control_kind = next((name for name in _CONTROL_KINDS
                                 if name != "other" and current.ControlType == getattr(self.ControlType, name, None)), "other")
            if count > 256 or depth > 16:
                raise CaptionProbeError("address_walk" if skip_documents else "caption_walk", "tree_limit",
                                        node_count=count, depth=depth, class_kind=class_kind, control_kind=control_kind)
            if skip_documents and (current.ControlType == self.ControlType.Document
                    or class_name in {"Chrome_RenderWidgetHostHWND", "WebView", "RenderWidgetHostViewAura"}
                    or class_name.startswith(("TabStrip", "VerticalTabStrip"))
                    or current.ControlType == getattr(self.ControlType, "Tab", None)):
                continue  # No webpage tree or complete multi-tab strip is needed to read the omnibox.
            yield element
            if skip_documents and control_kind in {"Button", "Text", "Image", "Edit", "TabItem"}:
                continue
            children = []
            child = self.Walker.GetFirstChild(element)
            while child is not None:
                children.append(child)
                if len(children) > 128:
                    raise CaptionProbeError("address_walk" if skip_documents else "caption_walk", "tree_limit",
                                            node_count=count, depth=depth, class_kind=class_kind, control_kind=control_kind)
                child = self.Walker.GetNextSibling(child)
            stack.extend((child, depth + 1) for child in reversed(children))

    def browser_source(self, handle):
        addresses, editing = [], False
        for element in self._walk(self._element(handle), skip_documents=True):
            current = element.Current
            if current.ControlType == self.ControlType.Edit and str(current.Name) in ADDRESS_NAMES:
                editing = editing or bool(current.HasKeyboardFocus)
                pattern = element.GetCurrentPattern(self.ValuePattern.Pattern)
                addresses.append(str(pattern.Current.Value))
        return (addresses[0] if len(addresses) == 1 else "", None, editing)  # Tab count is deliberately unknown.

    def caption_text(self, handle):
        root = self._element(handle)
        if hasattr(self, "user"):
            # Recheck the caption title after PID validation; a recycled HWND must not become a webpage read.
            title, window_class = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(128)
            self.user.GetWindowTextW(handle, title, len(title))
            self.user.GetClassNameW(handle, window_class, len(window_class))
            if title.value not in CAPTION_TITLES or not window_class.value.startswith("Chrome_WidgetWin_"):
                return None

        def caption_nodes(start, *, find_documents):
            stack, count = [(start, 0)], 0
            while stack:
                element, depth = stack.pop()
                count += 1
                if count > 256 or depth > 16:
                    raise CaptionProbeError("caption_walk", "tree_limit", node_count=count, depth=depth)
                current = element.Current
                kind, class_name = current.ControlType, str(getattr(current, "ClassName", ""))
                if (class_name in {"Chrome_RenderWidgetHostHWND", "WebView", "RenderWidgetHostViewAura"}
                        or kind in tuple(getattr(self.ControlType, name, None)
                                         for name in ("Button", "Link", "Hyperlink", "Edit", "ComboBox", "Menu", "MenuItem",
                                                      "CheckBox", "RadioButton", "ScrollBar"))
                        or not find_documents and element is not start and kind == self.ControlType.Document):
                    continue
                yield element
                if kind == self.ControlType.Text or find_documents and kind == self.ControlType.Document:
                    continue
                children = []
                child = self.Walker.GetFirstChild(element)
                while child is not None:
                    children.append(child)
                    if len(children) > 128:
                        raise CaptionProbeError("caption_walk", "tree_limit", node_count=count, depth=depth)
                    child = self.Walker.GetNextSibling(child)
                stack.extend((child, depth + 1) for child in reversed(children))

        documents = [element for element in caption_nodes(root, find_documents=True)
                     if element.Current.ControlType == self.ControlType.Document
                     and str(getattr(element.Current, "ClassName", "")) == "CaptionBubbleLabel"]
        if len(documents) != 1:
            return None
        document = documents[0]
        # Chromium exposes readonly Document/StaticText in AX, but Windows UIA need not implement TextPattern.
        full_text = str(document.Current.Name or "")
        if full_text:
            return full_text
        lines = [str(element.Current.Name) for element in caption_nodes(document, find_documents=False)
                 if element.Current.ControlType == self.ControlType.Text]
        if not lines:
            return None
        # StaticText nodes are visual folds of the label, not semantic sentence boundaries.
        return "".join(lines)

    def caption_engine_status(self, handle):
        """Inspect fixed native error labels; never treat spoken caption text as an error."""
        root = self._element(handle)
        if hasattr(self, "user"):
            title, window_class = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(128)
            self.user.GetWindowTextW(handle, title, len(title))
            self.user.GetClassNameW(handle, window_class, len(window_class))
            if title.value not in CAPTION_TITLES or not window_class.value.startswith("Chrome_WidgetWin_"):
                return None
        stack, count = [(root, 0)], 0
        while stack:
            element, depth = stack.pop()
            count += 1
            if count > 256 or depth > 16:
                raise CaptionProbeError("caption_walk", "tree_limit", node_count=count, depth=depth)
            current = element.Current
            kind, class_name = current.ControlType, str(getattr(current, "ClassName", ""))
            if (bool(getattr(current, "IsOffscreen", False))
                    or kind == self.ControlType.Document
                    or class_name in {"CaptionBubbleLabel", "Chrome_RenderWidgetHostHWND", "WebView", "RenderWidgetHostViewAura"}
                    or kind in tuple(getattr(self.ControlType, name, None)
                                     for name in ("Button", "Link", "Hyperlink", "Edit", "ComboBox", "Menu", "MenuItem",
                                                  "CheckBox", "RadioButton", "ScrollBar", "Image", "Tab", "TabItem"))):
                continue
            if kind == self.ControlType.Text:
                # Chromium Views' IsOffscreen is always false. RawView can still
                # expose an invisible Label; native UIA control/content flags
                # exclude that label using its invisible/ignored AX state.
                if (getattr(current, "IsControlElement", None) is not True
                        or getattr(current, "IsContentElement", None) is not True):
                    continue
                name = str(current.Name or "")
                if len(name) <= 160 and " ".join(name.split()) in CAPTION_ENGINE_ERRORS:
                    return "caption_engine_unavailable"
                continue
            children = []
            child = self.Walker.GetFirstChild(element)
            while child is not None:
                children.append(child)
                if len(children) > 128:
                    raise CaptionProbeError("caption_walk", "tree_limit", node_count=count, depth=depth)
                child = self.Walker.GetNextSibling(child)
            stack.extend((child, depth + 1) for child in reversed(children))
        return None

    def close(self):
        processes, self._processes = self._processes, {}
        for process in processes.values():
            self.kernel.CloseHandle(process)


class CaptionWorker:
    HEARTBEAT_SECONDS = 2.0

    def __init__(self, reader: CaptionReader, sink, *, session: int, generation: int, clock=time.monotonic):
        self.reader, self.sink, self.session, self.generation, self.clock = reader, sink, session, generation, clock
        self.epoch = self.revision = 0
        self._last_caption = None
        self._connected = False
        self._last_status = None
        self._last_diagnostic = None
        self._last_heartbeat_at = self._last_snapshot_at = self._last_update_at = None

    def emit(self, value):
        self.sink.write(json.dumps({**value, "session": self.session, "generation": self.generation},
                                   ensure_ascii=True) + "\n")
        self.sink.flush()

    def tick(self) -> bool:
        now = self.clock()
        try:
            status, caption = self.reader.read()
        except Exception as error:
            status, caption = "uia_unavailable", None
            diagnostic = exception_diagnostic(error, "reader")
        else:
            diagnostic = safe_diagnostic(getattr(self.reader, "last_diagnostic", None))
        if diagnostic and diagnostic["error_type"] != "none" and diagnostic != self._last_diagnostic:
            self.emit({"kind": "diagnostic", **diagnostic})
            self._last_diagnostic = diagnostic
        if status != self._last_status:
            self.emit({"kind": "status", "code": status})
            self._last_status = status
        if caption is not None:
            window_id, text = caption
            if not self._connected or self._last_caption is None or self._last_caption[0] != window_id:
                self.epoch += 1
            if caption != self._last_caption:
                self.revision += 1
                self._last_update_at = now
            self._last_caption, self._connected = caption, True
            self._snapshot(window_id, text, True, now)
        elif self._connected:
            self.epoch += 1
            self._snapshot(self._last_caption[0], "", False, now)
            self._connected = False
            self._last_caption = None
        # A quiet stream or a missing bubble is normal. A completed reader tick
        # must still be distinguishable from a blocked native UIA call.
        if self._last_heartbeat_at is None or now - self._last_heartbeat_at >= self.HEARTBEAT_SECONDS:
            self.emit({"kind": "heartbeat", "observed_at": now, "connected": self._connected,
                       "revision": self.revision, "source_epoch": self.epoch,
                       "status": status,
                       "snapshot_age_seconds": None if self._last_snapshot_at is None else max(0.0, now - self._last_snapshot_at),
                       "caption_update_age_seconds": None if self._last_update_at is None else max(0.0, now - self._last_update_at)})
            self._last_heartbeat_at = now
        return status != "browser_closed"

    def _snapshot(self, window_id, text, connected, now):
        self._last_snapshot_at = now
        self.emit({"kind": "snapshot", "text": text, "window_id": window_id, "source_epoch": self.epoch,
                   "revision": self.revision, "observed_at": now, "bridge_generation": self.generation,
                   "room": "",
                   "requested_room": source_room(self.reader.source_url), "source_verified": False,
                   "connected": connected,
                   # These describe the bridge's Chinese output policy, not Chrome's detected language.
                   "source_lang": "unknown", "target_lang": "zh",
                   "is_official_final": False, "allow_push": False})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="翻译姬 · 专用 Chrome 字幕读取")
    parser.add_argument("--browser-pid", type=int, default=0)
    parser.add_argument("--attach-existing", action="store_true")
    parser.add_argument("--generation", type=int, default=0)
    parser.add_argument("--session", type=int, default=0)
    parser.add_argument("--source-url", default="")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--dependency-check", action="store_true")
    parser.add_argument("--owned-browser-pid", type=int, default=0)
    args = parser.parse_args(argv)
    from .process_utils import _pipe
    sink = _pipe(sys.stdout, -11, "w")

    def fail(code):
        sink.write(json.dumps({"kind": "status", "code": code,
                               "session": args.session, "generation": args.generation}) + "\n")
        sink.flush()
        return 1

    if args.session < 0 or args.generation < 0:
        return fail("source_unverified")
    if args.dependency_check:
        # No native adapter/source selection: this checks the frozen CLR/DLL/FFI
        # dependencies without observing any user window, account or profile.
        try:
            result = check_uia_dependencies()
        except Exception as error:
            sink.write(json.dumps({"kind": "diagnostic", **exception_diagnostic(error, "adapter_init"),
                                   "session": args.session, "generation": args.generation}) + "\n")
            sink.flush()
            return 1
        sink.write(json.dumps({"kind": "dependency_check", **result,
                               "session": args.session, "generation": args.generation}) + "\n")
        sink.flush()
        return 0
    if args.smoke_test:
        # Pure NDJSON fixture: no Chrome, native UIA, networking, audio or profile writes.
        from types import SimpleNamespace
        fixture = SimpleNamespace(source_url="https://live.bilibili.com/1698225",
                                  source_verified=False,
                                  read=lambda: ("caption_found_unverified", ("1", "字幕桥本地测试")))
        worker = CaptionWorker(fixture, sink, session=args.session, generation=args.generation)
        worker.tick()
        return 0

    if not args.attach_existing or args.browser_pid != 0 or args.owned_browser_pid < 0 or args.session < 0 or args.generation < 0:
        return fail("source_unverified")
    try:
        url = validate_source_url(args.source_url)
    except ValueError:
        return fail("source_unverified")
    if os.name != "nt":
        return fail("unsupported_platform")
    # Third-party/.NET diagnostics must not share the structured caption pipe.
    sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    adapter = None
    try:
        adapter = NativeUiaAdapter(browser_pid=args.owned_browser_pid)
        reader = AttachedCaptionReader(adapter, url)
        worker = CaptionWorker(reader, sink, session=args.session, generation=args.generation)
        while worker.tick():
            time.sleep(0.25)
        return 0
    except Exception as error:
        diagnostic = exception_diagnostic(error, "adapter_init")
        sink.write(json.dumps({"kind": "diagnostic", **diagnostic,
                               "session": args.session, "generation": args.generation}) + "\n")
        sink.flush()
        return fail("dependency_missing" if diagnostic["error_type"] == "dependency_missing" else "uia_unavailable")
    finally:
        if adapter is not None:
            adapter.close()


if __name__ == "__main__":
    raise SystemExit(main())
