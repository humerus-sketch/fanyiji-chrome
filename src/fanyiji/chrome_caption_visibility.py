"""Reversible opacity changes for a caption bubble in this app's Chrome Job.

This never closes/hides a caption model, moves a browser, or changes a profile.
The caller must restore before disposing its Chrome process/Job handles.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
import threading
import uuid

from .chrome_caption_worker import CAPTION_TITLES, NativeUiaAdapter


WS_EX_LAYERED = 0x00080000
LWA_COLORKEY = 1
LWA_ALPHA = 2
_ERRORS = frozenset({"ownership_required", "caption_ambiguous", "identity_changed",
                     "unsupported_layered_state", "visibility_failed", "restore_failed", "closed",
                     "unsupported_platform"})


class CaptionVisibilityError(RuntimeError):
    def __init__(self, code="visibility_failed"):
        self.code = code if code in _ERRORS else "visibility_failed"
        super().__init__(self.code)


@dataclass(frozen=True)
class LayeredAttributes:
    color_key: int
    alpha: int
    flags: int

    def valid(self):
        return (type(self.color_key) is int and 0 <= self.color_key <= 0xFFFFFFFF
                and type(self.alpha) is int and 0 <= self.alpha <= 255
                and type(self.flags) is int and self.flags in (LWA_COLORKEY, LWA_ALPHA, LWA_COLORKEY | LWA_ALPHA))


@dataclass(frozen=True)
class CaptionWindowIdentity:
    handle: int
    process_id: int
    process_creation: int
    root_runtime_id: tuple
    document_runtime_id: tuple


@dataclass(frozen=True)
class _Snapshot:
    identity: CaptionWindowIdentity
    style: int
    attributes: LayeredAttributes | None
    hidden_attributes: LayeredAttributes


class CaptionVisibilityController:
    """Serialize changes and retain the original state until restoration succeeds.

An injected adapter must implement the same ownership and identity fencing as
NativeCaptionVisibilityAdapter; ordinary readonly attach adapters are rejected.
"""
    def __init__(self, adapter, browser_pid: int):
        if type(browser_pid) is not int or browser_pid <= 0:
            raise CaptionVisibilityError("ownership_required")
        self.adapter, self.browser_pid = adapter, browser_pid
        self._snapshot = None
        self._lock = threading.RLock()
        self._closed = False
        self.last_status = "restored"

    @property
    def hidden(self):
        with self._lock:
            return self._snapshot is not None

    def _owned(self):
        check = getattr(self.adapter, "owned_browser_verified", None)
        if check is None or check(self.browser_pid) is not True:
            raise CaptionVisibilityError("ownership_required")

    def hide(self) -> bool:
        with self._lock:
            if self._closed:
                raise CaptionVisibilityError("closed")
            try:
                self._owned()
                windows = [window for window in self.adapter.windows()
                           if window.process_id == self.browser_pid and window.handle > 0
                           and window.name in CAPTION_TITLES
                           and window.class_name.startswith("Chrome_WidgetWin_")]
                if len(windows) > 1:
                    raise CaptionVisibilityError("caption_ambiguous")
                if self._snapshot is not None:
                    previous = self._snapshot
                    if (self.adapter.matches(previous.identity, marked=True)
                            and len(windows) == 1 and windows[0].handle == previous.identity.handle):
                        if (self.adapter.get_style(previous.identity) != previous.style | WS_EX_LAYERED
                                or self.adapter.get_layered_attributes(previous.identity) != previous.hidden_attributes):
                            raise CaptionVisibilityError("visibility_failed")
                        self.last_status = "hidden"
                        return True
                    self.restore()
                if not windows:
                    self.last_status = "waiting_caption"
                    return False
                identity = self.adapter.window_identity(windows[0].handle, self.browser_pid)
                if not isinstance(identity, CaptionWindowIdentity) or identity.process_id != self.browser_pid:
                    raise CaptionVisibilityError("identity_changed")
                style = self.adapter.get_style(identity)
                if type(style) is not int or not 0 <= style <= 0xFFFFFFFF:
                    raise CaptionVisibilityError("unsupported_layered_state")
                attributes = self.adapter.get_layered_attributes(identity) if style & WS_EX_LAYERED else None
                if style & WS_EX_LAYERED and (not isinstance(attributes, LayeredAttributes) or not attributes.valid()):
                    # UpdateLayeredWindow cannot supply a recoverable original alpha via this API.
                    raise CaptionVisibilityError("unsupported_layered_state")
                if not style & WS_EX_LAYERED and not self.adapter.can_add_layered_style(identity):
                    raise CaptionVisibilityError("unsupported_layered_state")
                hidden_attributes = LayeredAttributes(attributes.color_key if attributes else 0, 0,
                                                     (attributes.flags if attributes else 0) | LWA_ALPHA)
                self.adapter.mark(identity)
                self._snapshot = _Snapshot(identity, style, attributes, hidden_attributes)
                try:
                    if not style & WS_EX_LAYERED:
                        self.adapter.set_style(identity, style | WS_EX_LAYERED)
                    self.adapter.set_layered_attributes(identity, hidden_attributes)
                    if (not self.adapter.matches(identity, marked=True)
                            or self.adapter.get_style(identity) != style | WS_EX_LAYERED
                            or self.adapter.get_layered_attributes(identity) != hidden_attributes):
                        raise CaptionVisibilityError("visibility_failed")
                except Exception:
                    # Keep the snapshot on rollback failure so the owner can retry restore before shutdown.
                    try:
                        self.restore()
                    except Exception:
                        raise CaptionVisibilityError("restore_failed") from None
                    raise CaptionVisibilityError("visibility_failed") from None
                self.last_status = "hidden"
                return True
            except CaptionVisibilityError as error:
                self.last_status = error.code
                raise
            except Exception:
                self.last_status = "visibility_failed"
                raise CaptionVisibilityError() from None

    def restore(self) -> bool:
        with self._lock:
            snapshot = self._snapshot
            if snapshot is None:
                self.last_status = "restored"
                return True
            try:
                self._owned()
                if not self.adapter.same_window_lifetime(snapshot.identity):
                    # A destroyed bubble/PID/HWND must never receive the old bubble's attributes.
                    self._snapshot = None
                    self.last_status = "identity_changed"
                    return False
                style = self.adapter.get_style(snapshot.identity, restoring=True)
                if style not in (snapshot.style, snapshot.style | WS_EX_LAYERED):
                    raise CaptionVisibilityError("restore_failed")
                if snapshot.attributes is not None:
                    self.adapter.set_layered_attributes(snapshot.identity, snapshot.attributes, restoring=True)
                if style != snapshot.style:
                    self.adapter.set_style(snapshot.identity, snapshot.style, restoring=True)
                if (not self.adapter.same_window_lifetime(snapshot.identity)
                        or self.adapter.get_style(snapshot.identity, restoring=True) != snapshot.style
                        or snapshot.attributes is not None
                        and self.adapter.get_layered_attributes(snapshot.identity, restoring=True) != snapshot.attributes):
                    raise CaptionVisibilityError("restore_failed")
                self.adapter.remove_marker(snapshot.identity, restoring=True)
                self._snapshot = None
                self.last_status = "restored"
                return True
            except Exception:
                self.last_status = "restore_failed"
                raise CaptionVisibilityError("restore_failed") from None

    def close(self):
        with self._lock:
            if self._closed:
                return
            self.restore()
            self.adapter.close()
            self._closed = True


class NativeCaptionVisibilityAdapter:
    """Native mutations require both an owned Chrome process and its exact Job.

Never pass an attached personal Chrome process. IsProcessInJob, retained process
creation time, native title/class, and a unique CaptionBubbleLabel are checked.
"""
    def __init__(self, owned_process, owned_job, *, uia_adapter=None, chrome_path=None):
        if os.name != "nt":
            raise CaptionVisibilityError("unsupported_platform")
        if (type(getattr(owned_process, "pid", None)) is not int or owned_process.pid <= 0
                or not getattr(owned_process, "_handle", None) or not getattr(owned_job, "handle", None)):
            raise CaptionVisibilityError("ownership_required")
        from ctypes import wintypes
        self.process, self.job, self.pid = owned_process, owned_job, owned_process.pid
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        self.kernel.IsProcessInJob.restype = wintypes.BOOL
        self.kernel.GetProcessId.argtypes = [wintypes.HANDLE]
        self.kernel.GetProcessId.restype = wintypes.DWORD
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.kernel.GetProcessTimes.restype = wintypes.BOOL
        self._marker = "Fanyiji.CaptionVisibility." + uuid.uuid4().hex
        self._creation = self._process_creation()
        self.uia = uia_adapter
        self._own_uia = uia_adapter is None
        if not self._native_owner_valid(self.pid):
            raise CaptionVisibilityError("ownership_required")
        if self.uia is None:
            self.uia = NativeUiaAdapter(self.pid, chrome_path=chrome_path)
        if getattr(self.uia, "pid", None) != self.pid or not self.uia.process_verified(self.pid):
            if self._own_uia:
                self.uia.close()
            raise CaptionVisibilityError("ownership_required")
        self.user.IsWindow.argtypes = [wintypes.HWND]
        self.user.IsWindow.restype = wintypes.BOOL
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self._get_long = getattr(self.user, "GetWindowLongPtrW", self.user.GetWindowLongW)
        self._set_long = getattr(self.user, "SetWindowLongPtrW", self.user.SetWindowLongW)
        self._get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        self._get_long.restype = ctypes.c_ssize_t
        self._set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self._set_long.restype = ctypes.c_ssize_t
        self._get_class_long = getattr(self.user, "GetClassLongPtrW", self.user.GetClassLongW)
        self._get_class_long.argtypes = [wintypes.HWND, ctypes.c_int]
        self._get_class_long.restype = ctypes.c_size_t
        self.user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user.SetWindowPos.restype = wintypes.BOOL
        self.user.GetLayeredWindowAttributes.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
                                                       ctypes.POINTER(wintypes.BYTE), ctypes.POINTER(wintypes.DWORD)]
        self.user.GetLayeredWindowAttributes.restype = wintypes.BOOL
        self.user.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.BYTE, wintypes.DWORD]
        self.user.SetLayeredWindowAttributes.restype = wintypes.BOOL
        for name in ("SetPropW", "GetPropW", "RemovePropW"):
            function = getattr(self.user, name)
            function.argtypes = [wintypes.HWND, wintypes.LPCWSTR] + ([wintypes.HANDLE] if name == "SetPropW" else [])
            function.restype = wintypes.BOOL if name == "SetPropW" else wintypes.HANDLE

    def _process_creation(self):
        from ctypes import wintypes
        values = [wintypes.FILETIME() for _ in range(4)]
        if not self.kernel.GetProcessTimes(int(self.process._handle), *(ctypes.byref(value) for value in values)):
            raise CaptionVisibilityError("ownership_required")
        return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime

    def _native_owner_valid(self, pid):
        from ctypes import wintypes
        member = wintypes.BOOL()
        return (pid == self.pid and self.process.poll() is None and bool(getattr(self.job, "handle", None))
                and self.kernel.GetProcessId(int(self.process._handle)) == pid
                and self._process_creation() == self._creation
                and bool(self.kernel.IsProcessInJob(int(self.process._handle), int(self.job.handle), ctypes.byref(member)))
                and bool(member.value))

    def owned_browser_verified(self, pid):
        try:
            return bool(self._native_owner_valid(pid) and self.uia.process_verified(pid))
        except Exception:
            return False

    def windows(self):
        if not self.owned_browser_verified(self.pid):
            raise CaptionVisibilityError("ownership_required")
        return self.uia.windows()

    def _document_identity(self, handle):
        root = self.uia._element(handle)
        stack, documents, count = [(root, 0)], [], 0
        while stack:
            element, depth = stack.pop()
            count += 1
            if count > 256 or depth > 16:
                raise CaptionVisibilityError("identity_changed")
            current = element.Current
            kind, class_name = current.ControlType, str(getattr(current, "ClassName", ""))
            if (class_name in {"Chrome_RenderWidgetHostHWND", "WebView", "RenderWidgetHostViewAura"}
                    or kind in tuple(getattr(self.uia.ControlType, name, None) for name in
                                     ("Button", "Link", "Hyperlink", "Edit", "ComboBox", "Menu", "MenuItem",
                                      "CheckBox", "RadioButton", "ScrollBar"))):
                continue
            if kind == self.uia.ControlType.Document:
                if class_name == "CaptionBubbleLabel":
                    documents.append(element)
                continue  # Never inspect foreign/native webpage documents or caption text.
            if kind == self.uia.ControlType.Text:
                continue
            children = []
            child = self.uia.Walker.GetFirstChild(element)
            while child is not None:
                children.append(child)
                if len(children) > 128:
                    raise CaptionVisibilityError("identity_changed")
                child = self.uia.Walker.GetNextSibling(child)
            stack.extend((child, depth + 1) for child in reversed(children))
        if len(documents) != 1:
            raise CaptionVisibilityError("identity_changed")
        root_id, document_id = tuple(root.GetRuntimeId()), tuple(documents[0].GetRuntimeId())
        if not root_id or not document_id:
            raise CaptionVisibilityError("identity_changed")
        return root_id, document_id

    def window_identity(self, handle, pid):
        from ctypes import wintypes
        actual_pid = wintypes.DWORD()
        if not self.owned_browser_verified(pid) or not self.user.IsWindow(handle):
            raise CaptionVisibilityError("identity_changed")
        self.user.GetWindowThreadProcessId(handle, ctypes.byref(actual_pid))
        if actual_pid.value != pid:
            raise CaptionVisibilityError("identity_changed")
        title, class_name = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(128)
        self.user.GetWindowTextW(handle, title, len(title))
        self.user.GetClassNameW(handle, class_name, len(class_name))
        if title.value not in CAPTION_TITLES or not class_name.value.startswith("Chrome_WidgetWin_"):
            raise CaptionVisibilityError("identity_changed")
        root_id, document_id = self._document_identity(handle)
        return CaptionWindowIdentity(handle, pid, self._creation, root_id, document_id)

    def matches(self, identity, *, marked=False):
        try:
            return (self.window_identity(identity.handle, identity.process_id) == identity
                    and (not marked or self.user.GetPropW(identity.handle, self._marker) == 1))
        except Exception:
            return False

    def same_window_lifetime(self, identity):
        """A caption's virtual AX document can change without replacing its HWND.

The nonce property belongs to the native window lifetime. Restoration therefore
does not depend on a live UIA document or its current RuntimeId.
"""
        from ctypes import wintypes
        if not self.owned_browser_verified(identity.process_id):
            raise CaptionVisibilityError("ownership_required")
        if identity.process_creation != self._creation or not self.user.IsWindow(identity.handle):
            return False
        pid = wintypes.DWORD()
        if not self.user.GetWindowThreadProcessId(identity.handle, ctypes.byref(pid)):
            if not self.user.IsWindow(identity.handle):
                return False
            raise CaptionVisibilityError("restore_failed")
        if pid.value != identity.process_id or self.user.GetPropW(identity.handle, self._marker) != 1:
            return False
        title, class_name = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(128)
        self.user.GetWindowTextW(identity.handle, title, len(title))
        self.user.GetClassNameW(identity.handle, class_name, len(class_name))
        if title.value not in CAPTION_TITLES or not class_name.value.startswith("Chrome_WidgetWin_"):
            # The owned marked window still exists; do not lose its original attributes on a transient read failure.
            raise CaptionVisibilityError("restore_failed")
        return True

    def _require(self, identity, *, marked=True, restoring=False):
        if not (self.same_window_lifetime(identity) if restoring else self.matches(identity, marked=marked)):
            raise CaptionVisibilityError("identity_changed")

    def mark(self, identity):
        self._require(identity, marked=False)
        if self.user.GetPropW(identity.handle, self._marker) or not self.user.SetPropW(identity.handle, self._marker, 1):
            raise CaptionVisibilityError()
        self._require(identity)

    def remove_marker(self, identity, *, restoring=False):
        self._require(identity, restoring=restoring)
        if self.user.RemovePropW(identity.handle, self._marker) != 1:
            raise CaptionVisibilityError("restore_failed")

    def get_style(self, identity, *, restoring=False):
        self._require(identity, marked=False, restoring=restoring)
        ctypes.set_last_error(0)
        value = self._get_long(identity.handle, -20)
        if value == 0 and ctypes.get_last_error():
            raise CaptionVisibilityError()
        return int(value) & 0xFFFFFFFF

    def can_add_layered_style(self, identity):
        self._require(identity, marked=False)
        ctypes.set_last_error(0)
        value = self._get_class_long(identity.handle, -26)  # GCL_STYLE
        return not (value == 0 and ctypes.get_last_error()) and not (int(value) & 0xC0)  # CLASSDC/PARENTDC

    def set_style(self, identity, style, *, restoring=False):
        self._require(identity, restoring=restoring)
        ctypes.set_last_error(0)
        if self._set_long(identity.handle, -20, style) == 0 and ctypes.get_last_error():
            raise CaptionVisibilityError()
        self._require(identity, restoring=restoring)
        if not self.user.SetWindowPos(identity.handle, None, 0, 0, 0, 0, 0x0037):
            raise CaptionVisibilityError()  # FRAMECHANGED, no move/size/activate/z-order/visibility change.

    def get_layered_attributes(self, identity, *, restoring=False):
        from ctypes import wintypes
        self._require(identity, marked=False, restoring=restoring)
        color, alpha, flags = wintypes.DWORD(), wintypes.BYTE(), wintypes.DWORD()
        if not self.user.GetLayeredWindowAttributes(identity.handle, ctypes.byref(color), ctypes.byref(alpha), ctypes.byref(flags)):
            return None
        return LayeredAttributes(int(color.value), int(alpha.value), int(flags.value))

    def set_layered_attributes(self, identity, attributes, *, restoring=False):
        self._require(identity, restoring=restoring)
        if not isinstance(attributes, LayeredAttributes) or not attributes.valid():
            raise CaptionVisibilityError()
        if not self.user.SetLayeredWindowAttributes(identity.handle, attributes.color_key, attributes.alpha, attributes.flags):
            raise CaptionVisibilityError()

    def close(self):
        if self._own_uia:
            self.uia.close()  # The process and Job still belong exclusively to the service.
