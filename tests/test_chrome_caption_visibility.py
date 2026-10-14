from dataclasses import replace
from types import SimpleNamespace

import pytest

from fanyiji.chrome_caption_visibility import (
    CaptionVisibilityController, CaptionVisibilityError, CaptionWindowIdentity,
    LayeredAttributes, LWA_ALPHA, LWA_COLORKEY, NativeCaptionVisibilityAdapter, WS_EX_LAYERED,
)
from fanyiji.chrome_caption_worker import NativeWindow


class FakeVisibility:
    """A synthetic desktop only; no ctypes DLL, pythonnet or actual HWND is used."""
    def __init__(self, *, style=0x80, attributes=None):
        self.pid, self.creation, self.serial, self.document_serial = 42, 12345, 1, 1
        self.owned, self.marked, self.closed = True, None, False
        self.style, self.attributes = style, attributes
        self.layering_allowed = True
        self.items = [NativeWindow(101, self.pid, "实时字幕", "Chrome_WidgetWin_1"),
                      NativeWindow(701, 999, "实时字幕", "Chrome_WidgetWin_1")]
        self.operations = []
        self.fail_alpha = self.fail_style = 0
        self.before_write = None

    def owned_browser_verified(self, pid):
        return self.owned and pid == self.pid

    def windows(self):
        return list(self.items)

    def window_identity(self, handle, pid):
        if not self.owned_browser_verified(pid) or not any(row.handle == handle and row.process_id == pid for row in self.items):
            raise CaptionVisibilityError("identity_changed")
        return CaptionWindowIdentity(handle, pid, self.creation, (handle, self.serial), (self.document_serial,))

    def matches(self, identity, *, marked=False):
        try:
            return identity == self.window_identity(identity.handle, identity.process_id) and (not marked or self.marked == identity)
        except CaptionVisibilityError:
            return False

    def mark(self, identity):
        assert self.matches(identity)
        self.marked = identity
        self.operations.append(("mark", identity.handle))

    def same_window_lifetime(self, identity):
        if not self.owned_browser_verified(identity.process_id):
            raise CaptionVisibilityError("ownership_required")
        try:
            current = self.window_identity(identity.handle, identity.process_id)
        except CaptionVisibilityError:
            return False
        return (self.marked == identity and current.process_creation == identity.process_creation
                and current.root_runtime_id == identity.root_runtime_id)

    def remove_marker(self, identity, *, restoring=False):
        assert self.same_window_lifetime(identity) if restoring else self.matches(identity, marked=True)
        self.marked = None
        self.operations.append(("unmark", identity.handle))

    def get_style(self, identity, *, restoring=False):
        assert self.same_window_lifetime(identity) if restoring else self.matches(identity)
        return self.style

    def get_layered_attributes(self, identity, *, restoring=False):
        assert self.same_window_lifetime(identity) if restoring else self.matches(identity)
        return self.attributes

    def can_add_layered_style(self, _identity):
        return self.layering_allowed

    def _write_guard(self, identity, *, restoring=False):
        if self.before_write:
            action, self.before_write = self.before_write, None
            action()
        if not (self.same_window_lifetime(identity) if restoring else self.matches(identity, marked=True)):
            raise CaptionVisibilityError("identity_changed")

    def set_style(self, identity, value, *, restoring=False):
        self._write_guard(identity, restoring=restoring)
        if self.fail_style:
            self.fail_style -= 1
            raise OSError("private diagnostic must not escape")
        self.style = value
        if not value & WS_EX_LAYERED:
            self.attributes = None
        self.operations.append(("style", identity.handle, value))

    def set_layered_attributes(self, identity, value, *, restoring=False):
        self._write_guard(identity, restoring=restoring)
        if self.fail_alpha:
            self.fail_alpha -= 1
            raise OSError("private diagnostic must not escape")
        self.attributes = value
        self.operations.append(("alpha", identity.handle, value))

    def close(self):
        self.operations.append(("close",))
        self.closed = True


def test_unlayered_bubble_becomes_transparent_without_visibility_or_browser_changes():
    adapter = FakeVisibility(style=0x80)
    controller = CaptionVisibilityController(adapter, 42)
    assert controller.hide() and controller.hidden and controller.last_status == "hidden"
    assert adapter.style == 0x80 | WS_EX_LAYERED
    assert adapter.attributes == LayeredAttributes(0, 0, LWA_ALPHA)
    assert controller.restore() and not controller.hidden
    assert adapter.style == 0x80 and adapter.attributes is None
    assert [row[0] for row in adapter.operations] == ["mark", "style", "alpha", "style", "unmark"]
    assert all(len(row) == 1 or row[1] == 101 for row in adapter.operations)


@pytest.mark.parametrize("alpha,flags", [(66, LWA_ALPHA), (137, LWA_ALPHA | LWA_COLORKEY), (93, LWA_COLORKEY), (0, LWA_ALPHA)])
def test_original_layered_alpha_color_key_and_flags_restored_exactly(alpha, flags):
    original = LayeredAttributes(0x102030, alpha, flags)
    adapter = FakeVisibility(style=0x80 | WS_EX_LAYERED, attributes=original)
    controller = CaptionVisibilityController(adapter, 42)
    assert controller.hide()
    assert adapter.attributes == LayeredAttributes(original.color_key, 0, flags | LWA_ALPHA)
    assert controller.restore()
    assert adapter.attributes == original and adapter.style == 0x80 | WS_EX_LAYERED
    assert not any(row[0] == "style" for row in adapter.operations)


@pytest.mark.parametrize("attributes", [None, LayeredAttributes(0, 255, 0), LayeredAttributes(0, 999, LWA_ALPHA),
                                       LayeredAttributes(True, 255, LWA_ALPHA), LayeredAttributes(0, 255, 8)])
def test_unrecoverable_layered_state_is_rejected_before_any_window_write(attributes):
    adapter = FakeVisibility(style=WS_EX_LAYERED, attributes=attributes)
    with pytest.raises(CaptionVisibilityError, match="unsupported_layered_state"):
        CaptionVisibilityController(adapter, 42).hide()
    assert adapter.operations == []


def test_readonly_personal_chrome_adapter_or_unowned_pid_never_mutates():
    readonly_adapter = SimpleNamespace(windows=lambda: pytest.fail("must not enumerate before ownership"))
    with pytest.raises(CaptionVisibilityError, match="ownership_required"):
        CaptionVisibilityController(readonly_adapter, 42).hide()
    adapter = FakeVisibility()
    adapter.owned = False
    with pytest.raises(CaptionVisibilityError, match="ownership_required"):
        CaptionVisibilityController(adapter, 42).hide()
    assert adapter.operations == []


@pytest.mark.parametrize("pid", [0, -1, True, None, "42"])
def test_browser_pid_must_be_explicit_positive_integer(pid):
    with pytest.raises(CaptionVisibilityError, match="ownership_required"):
        CaptionVisibilityController(FakeVisibility(), pid)


def test_no_caption_and_multiple_owned_captions_do_not_guess():
    adapter = FakeVisibility()
    adapter.items = [adapter.items[1]]
    controller = CaptionVisibilityController(adapter, 42)
    assert controller.hide() is False and controller.last_status == "waiting_caption"
    adapter.items.extend([NativeWindow(101, 42, "实时字幕", "Chrome_WidgetWin_1"),
                         NativeWindow(102, 42, "Live Caption", "Chrome_WidgetWin_1")])
    with pytest.raises(CaptionVisibilityError, match="caption_ambiguous"):
        controller.hide()
    assert adapter.operations == []


@pytest.mark.parametrize("name,window_class", [("My browser", "Chrome_WidgetWin_1"),
                                             ("实时字幕", "OtherAppWindow")])
def test_browser_or_another_app_window_never_becomes_caption(name, window_class):
    adapter = FakeVisibility()
    adapter.items[0] = replace(adapter.items[0], name=name, class_name=window_class)
    assert CaptionVisibilityController(adapter, 42).hide() is False
    assert not adapter.operations


def test_unsupported_window_class_layering_is_rejected():
    adapter = FakeVisibility()
    adapter.layering_allowed = False
    with pytest.raises(CaptionVisibilityError, match="unsupported_layered_state"):
        CaptionVisibilityController(adapter, 42).hide()
    assert adapter.operations == []


def test_hide_and_close_are_idempotent_and_restore_precedes_adapter_close():
    adapter = FakeVisibility()
    controller = CaptionVisibilityController(adapter, 42)
    assert controller.hide()
    previous = list(adapter.operations)
    assert controller.hide() and adapter.operations == previous
    controller.close()
    assert adapter.operations[-2:] == [("unmark", 101), ("close",)]
    previous = list(adapter.operations)
    controller.close()
    assert adapter.operations == previous
    with pytest.raises(CaptionVisibilityError, match="closed"):
        controller.hide()


@pytest.mark.parametrize("change", ["same_pid_new_hwnd", "reused_hwnd", "reused_pid", "lost_marker"])
def test_identity_change_cannot_restore_attributes_to_replacement_window(change):
    adapter = FakeVisibility()
    controller = CaptionVisibilityController(adapter, 42)
    controller.hide()
    previous = list(adapter.operations)
    if change == "same_pid_new_hwnd":
        adapter.items[0] = replace(adapter.items[0], handle=102)
    elif change == "reused_hwnd":
        adapter.serial += 1
    elif change == "reused_pid":
        adapter.creation += 1
    else:
        adapter.marked = None
    assert controller.restore() is False
    assert controller.last_status == "identity_changed" and not controller.hidden
    assert adapter.operations == previous


def test_window_replaced_between_capture_and_mutation_is_not_written():
    adapter = FakeVisibility()
    adapter.before_write = lambda: setattr(adapter, "serial", 2)
    controller = CaptionVisibilityController(adapter, 42)
    with pytest.raises(CaptionVisibilityError, match="visibility_failed"):
        controller.hide()
    assert all(row[0] not in ("style", "alpha", "unmark") for row in adapter.operations)


@pytest.mark.parametrize("failure", ["fail_alpha", "fail_style"])
def test_failed_hide_rolls_back_style_and_marker_without_raw_error(failure):
    adapter = FakeVisibility()
    setattr(adapter, failure, 1)
    controller = CaptionVisibilityController(adapter, 42)
    with pytest.raises(CaptionVisibilityError) as error:
        controller.hide()
    assert str(error.value) == "visibility_failed"
    assert not controller.hidden and adapter.style == 0x80 and adapter.marked is None


def test_restore_failure_preserves_snapshot_for_retry_before_process_disposal():
    original = LayeredAttributes(0x020304, 87, LWA_ALPHA | LWA_COLORKEY)
    adapter = FakeVisibility(style=WS_EX_LAYERED, attributes=original)
    controller = CaptionVisibilityController(adapter, 42)
    controller.hide()
    adapter.fail_alpha = 1
    with pytest.raises(CaptionVisibilityError, match="restore_failed"):
        controller.close()
    assert controller.hidden and not adapter.closed and adapter.marked is not None
    controller.close()
    assert not controller.hidden and adapter.closed and adapter.attributes == original


def test_failed_hide_and_failed_rollback_preserve_original_snapshot():
    adapter = FakeVisibility()
    adapter.fail_alpha = 1
    controller = CaptionVisibilityController(adapter, 42)
    original_set_alpha = adapter.set_layered_attributes

    def fail_hide_and_restore(identity, attributes):
        adapter.fail_style = 1
        return original_set_alpha(identity, attributes)

    adapter.set_layered_attributes = fail_hide_and_restore
    with pytest.raises(CaptionVisibilityError, match="restore_failed"):
        controller.hide()
    assert controller.hidden and adapter.style & WS_EX_LAYERED
    assert controller.restore() and adapter.style == 0x80


def test_external_style_changes_are_not_overwritten_by_restore():
    adapter = FakeVisibility()
    controller = CaptionVisibilityController(adapter, 42)
    controller.hide()
    adapter.style |= 0x4000
    previous = list(adapter.operations)
    with pytest.raises(CaptionVisibilityError, match="restore_failed"):
        controller.restore()
    assert controller.hidden and adapter.operations == previous


def test_hide_does_not_claim_success_after_chrome_replaces_hidden_opacity():
    adapter = FakeVisibility()
    controller = CaptionVisibilityController(adapter, 42)
    controller.hide()
    adapter.attributes = LayeredAttributes(0, 255, LWA_ALPHA)
    previous = list(adapter.operations)
    with pytest.raises(CaptionVisibilityError, match="visibility_failed"):
        controller.hide()
    assert adapter.operations == previous
    assert controller.restore()


def test_same_native_window_can_restore_after_caption_document_is_rebuilt():
    original = LayeredAttributes(0x020304, 87, LWA_ALPHA | LWA_COLORKEY)
    adapter = FakeVisibility(style=WS_EX_LAYERED | 0x80, attributes=original)
    controller = CaptionVisibilityController(adapter, 42)
    controller.hide()
    identity = adapter.marked
    adapter.document_serial += 1
    assert adapter.matches(identity, marked=True) is False
    assert adapter.same_window_lifetime(identity) is True
    assert controller.restore() and not controller.hidden
    assert adapter.attributes == original and adapter.style == WS_EX_LAYERED | 0x80


def test_native_restore_does_not_request_uia_runtime_or_document_text():
    adapter = NativeCaptionVisibilityAdapter.__new__(NativeCaptionVisibilityAdapter)
    adapter.same_window_lifetime = lambda _identity: True
    adapter.matches = lambda *_args, **_kwargs: pytest.fail("Restoration must not depend on virtual document identity")
    writes = []
    adapter.user = SimpleNamespace(SetLayeredWindowAttributes=lambda *args: writes.append(args) or True)
    identity = CaptionWindowIdentity(101, 42, 123, (1,), (2,))
    adapter.set_layered_attributes(identity, LayeredAttributes(0, 66, LWA_ALPHA), restoring=True)
    assert writes == [(101, 0, 66, LWA_ALPHA)]


def test_transient_native_lifetime_verification_failure_preserves_recovery_snapshot():
    adapter = FakeVisibility()
    controller = CaptionVisibilityController(adapter, 42)
    controller.hide()
    original_check = adapter.same_window_lifetime
    adapter.same_window_lifetime = lambda _identity: (_ for _ in ()).throw(CaptionVisibilityError("restore_failed"))
    with pytest.raises(CaptionVisibilityError, match="restore_failed"):
        controller.restore()
    assert controller.hidden and adapter.marked is not None
    adapter.same_window_lifetime = original_check
    assert controller.restore()


def lifetime_adapter(*, pid=42, marker=1, exists=True, title="实时字幕", window_class="Chrome_WidgetWin_1"):
    adapter = NativeCaptionVisibilityAdapter.__new__(NativeCaptionVisibilityAdapter)
    adapter._creation, adapter._marker = 123, "synthetic-window-nonce"
    adapter.owned_browser_verified = lambda value: value == 42
    adapter.window_identity = lambda *_args: pytest.fail("Native lifetime restore must not read UIA")
    title_reads = []

    def get_pid(_handle, output):
        output._obj.value = pid
        return 77

    def get_title(_handle, buffer, _size):
        title_reads.append("title")
        buffer.value = title
        return len(title)

    def get_class(_handle, buffer, _size):
        buffer.value = window_class
        return len(window_class)

    adapter.user = SimpleNamespace(IsWindow=lambda _handle: exists, GetWindowThreadProcessId=get_pid,
                                   GetPropW=lambda *_args: marker, GetWindowTextW=get_title, GetClassNameW=get_class)
    return adapter, title_reads


def test_native_window_lifetime_can_be_verified_without_uia_document():
    adapter, _reads = lifetime_adapter()
    assert adapter.same_window_lifetime(CaptionWindowIdentity(101, 42, 123, (1,), (2,))) is True


@pytest.mark.parametrize("changes", [{"exists": False}, {"marker": None}, {"pid": 999}])
def test_native_destroyed_reused_or_unmarked_window_is_not_restorable(changes):
    adapter, reads = lifetime_adapter(**changes)
    assert adapter.same_window_lifetime(CaptionWindowIdentity(101, 42, 123, (1,), (2,))) is False
    assert reads == []


@pytest.mark.parametrize("changes", [{"title": ""}, {"title": "Other caption title"}, {"window_class": "OtherApp"}])
def test_existing_owned_marked_window_metadata_failure_does_not_mean_window_destroyed(changes):
    adapter, _reads = lifetime_adapter(**changes)
    with pytest.raises(CaptionVisibilityError, match="restore_failed"):
        adapter.same_window_lifetime(CaptionWindowIdentity(101, 42, 123, (1,), (2,)))


def native_document_adapter(root):
    # Only the readonly synthetic UIA path is exercised; __init__ never loads native DLLs.
    from test_chrome_caption_worker import FakeWalker
    adapter = NativeCaptionVisibilityAdapter.__new__(NativeCaptionVisibilityAdapter)
    adapter.uia = SimpleNamespace(
        _element=lambda _handle: root,
        Walker=FakeWalker(root),
        ControlType=SimpleNamespace(Pane="pane", Document="document", Text="text", Button="button", Edit="edit"),
    )
    return adapter


class IdentityElement:
    def __init__(self, kind="pane", class_name="", children=(), runtime_id=(1,)):
        self.Current = SimpleNamespace(ControlType=kind, ClassName=class_name)
        self.children, self.runtime_id = list(children), runtime_id

    def GetRuntimeId(self):
        return self.runtime_id


def test_native_caption_validation_uses_only_unique_caption_label_identity_without_text():
    label = IdentityElement("document", "CaptionBubbleLabel", runtime_id=(2,))
    root = IdentityElement(children=[IdentityElement("button"), label], runtime_id=(1, 101))
    assert native_document_adapter(root)._document_identity(101) == ((1, 101), (2,))
    assert not hasattr(label.Current, "Name")  # No subtitle content is required for hiding.


@pytest.mark.parametrize("documents", [[], [IdentityElement("document", "WebDocument")],
                                      [IdentityElement("document", "CaptionBubbleLabel"),
                                       IdentityElement("document", "CaptionBubbleLabel")]])
def test_native_missing_foreign_or_multiple_caption_documents_are_rejected(documents):
    root = IdentityElement(children=documents)
    with pytest.raises(CaptionVisibilityError, match="identity_changed"):
        native_document_adapter(root)._document_identity(101)


def test_native_does_not_traverse_webhost_or_interactive_children():
    label = IdentityElement("document", "CaptionBubbleLabel", runtime_id=(3,))
    web = IdentityElement("pane", "WebView", [IdentityElement("document", "CaptionBubbleLabel")])
    button = IdentityElement("button", children=[IdentityElement("document", "CaptionBubbleLabel")])
    root = IdentityElement(children=[web, button, label])
    adapter = native_document_adapter(root)
    first_child = adapter.uia.Walker.GetFirstChild

    def guard(element):
        assert element is not web and element is not button
        return first_child(element)

    adapter.uia.Walker.GetFirstChild = guard
    assert adapter._document_identity(101) == ((1,), (3,))


def test_native_mutation_guard_checks_marker_and_identity_before_writing():
    adapter = NativeCaptionVisibilityAdapter.__new__(NativeCaptionVisibilityAdapter)
    adapter.matches = lambda *_args, **_kwargs: False
    adapter.user = SimpleNamespace(SetLayeredWindowAttributes=lambda *_args: pytest.fail("must not write"))
    identity = CaptionWindowIdentity(101, 42, 1, (1,), (2,))
    with pytest.raises(CaptionVisibilityError, match="identity_changed"):
        adapter.set_layered_attributes(identity, LayeredAttributes(0, 0, LWA_ALPHA))


def test_job_membership_is_required_even_if_browser_process_is_running():
    adapter = NativeCaptionVisibilityAdapter.__new__(NativeCaptionVisibilityAdapter)
    adapter.pid, adapter._creation = 42, 123
    adapter.process = SimpleNamespace(_handle=999, poll=lambda: None)
    adapter.job = SimpleNamespace(handle=777)
    adapter._process_creation = lambda: 123
    adapter.kernel = SimpleNamespace(GetProcessId=lambda _handle: 42,
                                     IsProcessInJob=lambda *_args: True)  # Out parameter stays false.
    assert adapter._native_owner_valid(42) is False
