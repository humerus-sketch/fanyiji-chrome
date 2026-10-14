"""MSIX file alias selection uses metadata only and never launches real Chrome."""
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from fanyiji import chrome_browser_service as module
from test_chrome_browser_service import harness, open_browser

_short_profile = module._existing_short_profile


@pytest.fixture
def alias(tmp_path, monkeypatch):
    local = tmp_path / "Local"
    profile = local / "FanyiJiChrome/chrome-caption/dedicated-profile"
    physical = local / "Packages/Fixture.Package_family/LocalCache/Local/FanyiJiChrome/chrome-caption/dedicated-profile"
    for root in (profile, physical):
        (root / "Default").mkdir(parents=True)
        (root / "Local State").write_bytes(b"synthetic marker, not parsed")
        (root / "Default/Preferences").write_bytes(b"synthetic marker, not parsed")
    mappings = {profile / "Local State": physical / "Local State",
                profile / "Default/Preferences": physical / "Default/Preferences"}
    original_resolve, original_samefile = Path.resolve, Path.samefile
    def resolve(path, *args, **kwargs):
        return mappings[path] if path in mappings else original_resolve(path, *args, **kwargs)
    def samefile(path, other):
        if path in mappings:
            return mappings[path] == Path(other)
        return original_samefile(path, other)
    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "samefile", samefile)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(module, "data_root", lambda: local / "FanyiJiChrome")
    monkeypatch.setattr(module, "path_override", lambda _name: None)
    monkeypatch.setattr(module, "_existing_short_profile", lambda physical: physical)
    return SimpleNamespace(local=local, profile=profile, physical=physical, mappings=mappings)


def test_verified_package_alias_is_selected_without_reading_marker_contents(alias, monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: pytest.fail("must not read profile values"))
    monkeypatch.setattr(Path, "read_bytes", lambda *_args, **_kwargs: pytest.fail("must not read account files"))
    assert module.resolve_external_profile(alias.profile) == alias.physical


@pytest.mark.parametrize("setting", ["FANYIJI_CHROME_DATA", "FANYIJI_DATA", "LOCAL_SUBTITLES_DATA"])
def test_explicit_data_override_keeps_logical_profile(alias, monkeypatch, setting):
    from fanyiji.paths import path_override
    monkeypatch.setattr(module, "path_override", path_override)
    monkeypatch.setenv(setting, str(alias.local / "explicit-user-data"))
    assert module.resolve_external_profile(alias.profile) == alias.profile


def test_nondefault_explicit_profile_is_not_retargeted(alias):
    assert module.resolve_external_profile(alias.physical) == alias.physical


def test_normal_installation_keeps_original_resolved_profile(alias):
    alias.mappings.clear()
    assert module.resolve_external_profile(alias.profile) == alias.profile


@pytest.mark.parametrize("outside", [
    "other-profile", "Packages/Family/LocalCache/Local/OtherApplication/chrome-caption/dedicated-profile",
    "Packages/Family/LocalCache/Local/FanyiJiChrome/chrome-caption/dedicated-profile-extra",
    "Packages/Family/LocalCache/Local/FanyiJiChrome/other/dedicated-profile",
    "Packages/Family/LocalCache/Roaming/FanyiJiChrome/chrome-caption/dedicated-profile",
    "Packages/extra/Family/LocalCache/Local/FanyiJiChrome/chrome-caption/dedicated-profile",
])
def test_unrelated_resolved_parent_never_becomes_browser_profile(alias, outside):
    alias.mappings[alias.profile / "Local State"] = alias.local / outside / "Local State"
    assert module.resolve_external_profile(alias.profile) == alias.profile


def test_mixed_marker_targets_are_not_accepted(alias):
    alias.mappings[alias.profile / "Default/Preferences"] = alias.physical.parent / "other/Default/Preferences"
    assert module.resolve_external_profile(alias.profile) == alias.profile


@pytest.mark.parametrize("failure", ["state", "preferences"])
def test_both_logical_and_physical_markers_must_be_same_files(alias, monkeypatch, failure):
    previous = Path.samefile
    failed = alias.profile / ("Local State" if failure == "state" else "Default/Preferences")
    monkeypatch.setattr(Path, "samefile", lambda path, other: False if path == failed else previous(path, other))
    assert module.resolve_external_profile(alias.profile) == alias.profile


@pytest.mark.parametrize("target,kind", [
    (target, kind)
    for target in ("logical_state", "logical_preferences", "physical_state", "physical_preferences", "profile_directory", "package_directory")
    for kind in ("reparse", "symlink", "hardlink")
    if kind != "hardlink" or not target.endswith("directory")
])
def test_suspicious_marker_or_parent_is_not_accepted(alias, monkeypatch, target, kind):
    targets = {"logical_state": alias.profile / "Local State", "logical_preferences": alias.profile / "Default/Preferences",
               "physical_state": alias.physical / "Local State", "physical_preferences": alias.physical / "Default/Preferences",
               "profile_directory": alias.profile, "package_directory": alias.local / "Packages/Fixture.Package_family"}
    blocked = targets[target]
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path != blocked:
            return value
        return SimpleNamespace(st_mode=stat.S_IFLNK if kind == "symlink" else value.st_mode,
                               st_file_attributes=0x400 if kind == "reparse" else 0,
                               st_nlink=2 if kind == "hardlink" else 1)
    monkeypatch.setattr(Path, "lstat", lstat)
    assert module.resolve_external_profile(alias.profile) == alias.profile


@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError, RuntimeError])
def test_alias_resolution_or_metadata_failure_falls_back(alias, monkeypatch, failure):
    previous = Path.resolve
    def resolve(path, *args, **kwargs):
        if path == alias.profile / "Local State":
            raise failure("synthetic path failure")
        return previous(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    assert module.resolve_external_profile(alias.profile) == alias.profile


def test_browser_launch_uses_selected_path_but_keeps_logical_lock_and_preferences(harness, monkeypatch):
    physical = harness.profile.parent / "physical-profile"
    calls = []
    def choose(profile):
        calls.append(profile)
        return physical
    monkeypatch.setattr(module, "resolve_external_profile", choose)
    open_browser(harness)
    assert calls == [harness.profile]
    assert "--user-data-dir=" + str(physical) in harness.launched[0][0]
    assert harness.service.profile == harness.profile
    assert (harness.profile / "fanyiji-owner.lock").exists()
    assert (harness.profile / "Default/Preferences").exists()


@pytest.fixture
def short_alias(alias, monkeypatch):
    """Model an existing directory alias without creating real short names or links."""
    short = alias.local / "SHORT~1"
    previous_lstat, previous_samefile = Path.lstat, Path.samefile

    def physical_path(path):
        try:
            relative = Path(path).relative_to(short)
        except ValueError:
            return Path(path)
        return alias.physical / relative

    monkeypatch.setattr(Path, "lstat", lambda path, *args, **kwargs:
                        previous_lstat(physical_path(path), *args, **kwargs))
    monkeypatch.setattr(Path, "samefile", lambda path, other:
                        previous_samefile(physical_path(path), physical_path(other)))

    class GetShortPath:
        output = str(short)
        query_result = write_result = None
        query_error = write_error = None

        def __init__(self):
            self.calls = []

        def __call__(self, path, buffer, size):
            self.calls.append((path, buffer is None, size))
            if buffer is None:
                if self.query_error:
                    raise self.query_error("synthetic query failure")
                return self.query_result if self.query_result is not None else len(self.output) + 1
            if self.write_error:
                raise self.write_error("synthetic write failure")
            buffer.value = self.output
            return self.write_result if self.write_result is not None else len(self.output)

    api = GetShortPath()
    monkeypatch.setattr(module.ctypes, "WinDLL", lambda *_args, **_kwargs:
                        SimpleNamespace(GetShortPathNameW=api))
    return SimpleNamespace(alias=alias, short=short, api=api)


def test_existing_short_alias_retains_short_spelling_and_never_reads_values(short_alias, monkeypatch):
    previous = Path.resolve
    def resolve(path, *args, **kwargs):
        if path == short_alias.short:
            pytest.fail("resolving would expand the short alias")
        return previous(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: pytest.fail("must not read profile values"))
    monkeypatch.setattr(Path, "read_bytes", lambda *_args, **_kwargs: pytest.fail("must not read account files"))
    assert _short_profile(short_alias.alias.physical) == short_alias.short
    assert len(short_alias.api.calls) == 2
    assert all(call[0] == str(short_alias.alias.physical) for call in short_alias.api.calls)


def test_only_verified_msix_alias_uses_existing_short_path(short_alias, monkeypatch):
    calls = []
    def choose(physical):
        calls.append(physical)
        return _short_profile(physical)
    monkeypatch.setattr(module, "_existing_short_profile", choose)
    assert module.resolve_external_profile(short_alias.alias.profile) == short_alias.short
    assert calls == [short_alias.alias.physical]


@pytest.mark.parametrize("branch", ["ordinary", "explicit_profile", "data_override", "unrelated_alias"])
def test_fallback_profile_branches_never_request_short_names(alias, monkeypatch, branch):
    monkeypatch.setattr(module, "_existing_short_profile", lambda _physical:
                        pytest.fail("only verified MSIX virtualization can request an alias"))
    profile, expected = alias.profile, alias.profile
    if branch == "ordinary":
        alias.mappings.clear()
    elif branch == "explicit_profile":
        profile = expected = alias.physical
    elif branch == "data_override":
        monkeypatch.setattr(module, "path_override", lambda _name: "explicit-data")
    else:
        alias.mappings[alias.profile / "Local State"] = alias.local / "outside/Local State"
    assert module.resolve_external_profile(profile) == expected


@pytest.mark.parametrize("size", [0, 32769, -1])
def test_short_name_size_failure_keeps_verified_physical_profile(short_alias, size):
    short_alias.api.query_result = size
    assert _short_profile(short_alias.alias.physical) == short_alias.alias.physical
    assert len(short_alias.api.calls) == 1


@pytest.mark.parametrize("failure", [OSError, ValueError, RuntimeError, AttributeError])
@pytest.mark.parametrize("stage", ["query", "write", "library"])
def test_short_api_errors_keep_verified_physical_profile(short_alias, monkeypatch, failure, stage):
    if stage == "library":
        def load(*_args, **_kwargs):
            raise failure("synthetic library failure")
        monkeypatch.setattr(module.ctypes, "WinDLL", load)
    else:
        setattr(short_alias.api, stage + "_error", failure)
    assert _short_profile(short_alias.alias.physical) == short_alias.alias.physical


@pytest.mark.parametrize("result", ["zero", "capacity", "larger_than_capacity", "truncated"])
def test_short_buffer_failure_or_truncation_keeps_physical_profile(short_alias, result):
    length = len(short_alias.api.output)
    short_alias.api.write_result = {"zero": 0, "capacity": length + 1,
                                   "larger_than_capacity": length + 2, "truncated": length - 1}[result]
    assert _short_profile(short_alias.alias.physical) == short_alias.alias.physical


@pytest.mark.parametrize("result", ["unchanged", "longer", "relative"])
def test_short_api_must_return_a_shorter_absolute_path(short_alias, result):
    short_alias.api.output = {"unchanged": str(short_alias.alias.physical),
                             "longer": str(short_alias.alias.physical / "longer"),
                             "relative": "SHORT~1"}[result]
    assert _short_profile(short_alias.alias.physical) == short_alias.alias.physical


@pytest.mark.parametrize("target,kind", [
    (target, kind)
    for target in ("short_directory", "short_parent", "short_default", "short_state", "short_preferences",
                   "physical_state", "physical_preferences")
    for kind in ("reparse", "symlink", "hardlink")
    if kind != "hardlink" or target.endswith(("state", "preferences"))
])
def test_short_alias_rejects_suspicious_directory_or_marker(short_alias, monkeypatch, target, kind):
    short, physical = short_alias.short, short_alias.alias.physical
    blocked = {"short_directory": short, "short_parent": short.parent, "short_default": short / "Default",
               "short_state": short / "Local State", "short_preferences": short / "Default/Preferences",
               "physical_state": physical / "Local State", "physical_preferences": physical / "Default/Preferences"}[target]
    previous = Path.lstat
    def lstat(path, *args, **kwargs):
        value = previous(path, *args, **kwargs)
        if path != blocked:
            return value
        return SimpleNamespace(st_mode=stat.S_IFLNK if kind == "symlink" else value.st_mode,
                               st_file_attributes=0x400 if kind == "reparse" else 0,
                               st_nlink=2 if kind == "hardlink" else 1)
    monkeypatch.setattr(Path, "lstat", lstat)
    assert _short_profile(physical) == physical


@pytest.mark.parametrize("target", ["directory", "state", "preferences"])
def test_short_alias_identity_must_match_directory_and_both_markers(short_alias, monkeypatch, target):
    blocked = short_alias.short
    if target != "directory":
        blocked /= "Local State" if target == "state" else "Default/Preferences"
    previous = Path.samefile
    monkeypatch.setattr(Path, "samefile", lambda path, other: False if path == blocked else previous(path, other))
    assert _short_profile(short_alias.alias.physical) == short_alias.alias.physical


@pytest.mark.parametrize("operation", ["metadata", "identity"])
def test_short_alias_metadata_failure_keeps_physical_profile(short_alias, monkeypatch, operation):
    def fail(*_args, **_kwargs):
        raise OSError("synthetic metadata failure")
    monkeypatch.setattr(Path, "lstat" if operation == "metadata" else "samefile", fail)
    assert _short_profile(short_alias.alias.physical) == short_alias.alias.physical
