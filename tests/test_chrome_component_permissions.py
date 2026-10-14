"""Only synthetic ACLs are written; never invoke a real Windows security API."""
from copy import deepcopy
import json
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from fanyiji import chrome_component_permissions as permissions, migration

NativeAclApi = permissions._WindowsAclApi


class FakeSecurityApi:
    def __init__(self, paths, *, propagate=False):
        self.acls = {path: {"other_ace": "private synthetic account marker", "protected": False,
                            "users_rx": False, "inherit_children": path.is_dir()} for path in paths}
        self.original = deepcopy(self.acls)
        self.calls = []
        self.propagate = propagate
        self.fail = self.deny = None
        self.before_first_write = None

    def has_users_rx(self, path, directory):
        self.calls.append(("inspect", path))
        return self.acls[path]["users_rx"]

    def snapshot(self, path, directory):
        self.calls.append(("snapshot", path))
        return json.dumps(self.acls[path]).encode()

    def grant_users_rx(self, path, directory):
        self.calls.append(("grant", path))
        if self.before_first_write:
            callback, self.before_first_write = self.before_first_write, None
            callback()
        # Simulate a partially applied rule before the platform call fails.
        self.acls[path]["users_rx"] = True
        if self.propagate and directory:
            for child in self.acls:
                if child.is_relative_to(path):
                    self.acls[child]["users_rx"] = True
        if path == self.fail:
            raise OSError("PRIVATE_SID_AND_ACL_DETAILS")
        if path == self.deny:
            self.acls[path]["users_rx"] = False

    def restore(self, path, directory, descriptor):
        self.calls.append(("restore", path))
        self.acls[path] = json.loads(descriptor)


@pytest.fixture
def owned(tmp_path, monkeypatch):
    owner = tmp_path / "owned-app"
    profile = owner / "chrome-caption" / "dedicated-profile"
    profile.mkdir(parents=True)
    (profile / "Default").mkdir()
    (profile / "Default" / "Cookies").write_bytes(b"fake-account-marker")
    monkeypatch.setattr(permissions, "data_root", lambda: owner)
    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must not load or write real Windows ACLs")
    monkeypatch.setattr(permissions, "_WindowsAclApi", forbidden)
    for name, relative in (("SODA", "1.0/SODAFiles/SODA.dll"),
                           ("SODALanguagePacks", "ja-JP/1.0/SODAModels/model.bin")):
        target = profile / name / relative
        target.parent.mkdir(parents=True)
        target.write_bytes(b"fake-component-bytes")
    nodes = [path for path, _value in permissions._component_nodes(profile)]
    return SimpleNamespace(owner=owner, profile=profile, nodes=nodes,
                           api=FakeSecurityApi(nodes), cookies=profile / "Default" / "Cookies")


def test_readonly_inspection_never_changes_acl_or_reads_account_data(owned):
    result = permissions.inspect_component_permissions(owned.profile, security_api=owned.api)
    assert result == {"paths_examined": len(owned.nodes), "paths_needing_repair": len(owned.nodes)}
    assert owned.api.acls == owned.api.original
    assert all(kind == "inspect" for kind, _path in owned.api.calls)
    assert owned.cookies.read_bytes() == b"fake-account-marker"


def test_repair_targets_only_component_trees_preserves_other_rules_and_is_idempotent(owned):
    report = permissions.repair_component_permissions(owned.profile, security_api=owned.api)
    assert report.writes == len(owned.nodes)
    assert report.summary()["paths_needing_repair"] == len(owned.nodes)
    assert "private" not in repr(report) and "private" not in json.dumps(report.summary())
    for path, value in owned.api.acls.items():
        assert value["users_rx"]
        assert value["other_ace"] == owned.api.original[path]["other_ace"]
        assert value["protected"] == owned.api.original[path]["protected"]
        assert path.relative_to(owned.profile).parts[0] in ("SODA", "SODALanguagePacks")
    before = deepcopy(owned.api.acls)
    owned.api.calls.clear()
    second = permissions.repair_component_permissions(owned.profile, security_api=owned.api)
    assert second.writes == second.paths_needing_repair == 0
    assert all(kind == "inspect" for kind, _path in owned.api.calls)
    assert owned.api.acls == before
    assert owned.cookies.read_bytes() == b"fake-account-marker"


def test_inherited_rx_from_component_root_does_not_add_duplicate_child_rules(owned):
    owned.api.propagate = True
    report = permissions.repair_component_permissions(owned.profile, security_api=owned.api)
    assert report.writes == 2
    assert {path.relative_to(owned.profile).as_posix() for kind, path in owned.api.calls if kind == "grant"} == {
        "SODA", "SODALanguagePacks"}
    assert permissions.inspect_component_permissions(owned.profile, security_api=owned.api)["paths_needing_repair"] == 0


@pytest.mark.parametrize("failure", ["write", "verification"])
def test_failed_repair_restores_full_component_dacls_after_partial_application(owned, failure):
    path = owned.nodes[-1]
    setattr(owned.api, "fail" if failure == "write" else "deny", path)
    with pytest.raises(permissions.ComponentPermissionError, match="component_acl_repair_failed") as error:
        permissions.repair_component_permissions(owned.profile, security_api=owned.api)
    assert "PRIVATE" not in str(error.value)
    assert owned.api.acls == owned.api.original
    restored = [path for kind, path in owned.api.calls if kind == "restore"]
    assert restored == owned.nodes
    assert owned.cookies.read_bytes() == b"fake-account-marker"


def test_private_backup_exists_before_first_write_and_can_restore_after_process_restart(owned):
    backup = owned.owner / "permission-backups" / "soda.json"
    owned.api.before_first_write = lambda: assert_backup_ready(backup)
    permissions.repair_component_permissions(owned.profile, security_api=owned.api, backup_file=backup)
    restored = permissions.load_component_permission_backup(backup, security_api=owned.api)
    permissions.rollback_component_permissions(restored)
    assert restored.rolled_back and owned.api.acls == owned.api.original
    assert owned.cookies.read_bytes() == b"fake-account-marker"


def assert_backup_ready(path):
    assert path.is_file()
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["schema_version"] == 1 and value["snapshots"]


@pytest.mark.parametrize("location", ["external", "component", "profile"])
def test_backup_cannot_publish_acl_in_component_or_arbitrary_directory(owned, location):
    backup = (owned.owner.parent / "external.json" if location == "external" else
              owned.profile / "SODA" / "backup.json" if location == "component" else
              owned.profile / "backup.json")
    with pytest.raises(permissions.ComponentPermissionError, match="component_backup_location_invalid"):
        permissions.repair_component_permissions(owned.profile, security_api=owned.api, backup_file=backup)
    assert all(kind != "grant" for kind, _path in owned.api.calls)


def test_existing_private_backup_is_preserved_before_any_grant(owned):
    backup = owned.owner / "original-backup.json"
    backup.write_bytes(b"keep-original-backup")
    with pytest.raises(FileExistsError):
        permissions.repair_component_permissions(owned.profile, security_api=owned.api, backup_file=backup)
    assert backup.read_bytes() == b"keep-original-backup"
    assert all(kind != "grant" for kind, _path in owned.api.calls)


@pytest.mark.parametrize("target", ["profile", "component", "child"])
def test_reparse_point_rejection_precedes_all_permission_api_calls(owned, monkeypatch, target):
    original = permissions._safe_stat
    blocked = (owned.profile if target == "profile" else owned.profile / "SODA"
               if target == "component" else owned.nodes[-1])
    def check(path):
        if path == blocked:
            raise permissions.ComponentPermissionError("component_path_is_link")
        return original(path)
    monkeypatch.setattr(permissions, "_safe_stat", check)
    with pytest.raises(permissions.ComponentPermissionError, match="component_path_is_link"):
        permissions.repair_component_permissions(owned.profile, security_api=owned.api)
    assert not owned.api.calls


def test_other_chrome_and_legacy_profiles_are_not_permission_targets(owned):
    for profile in (owned.owner.parent / "legacy" / "dedicated-profile", owned.profile / "Default", owned.owner):
        with pytest.raises(permissions.ComponentPermissionError, match="profile_not_owned"):
            permissions.repair_component_permissions(profile, security_api=owned.api)
    assert not owned.api.calls


def test_msix_file_virtualization_does_not_escape_the_validated_component_directory(owned, monkeypatch):
    original_resolve = Path.resolve
    def virtualized(path, *args, **kwargs):
        if path.is_file() and path.is_relative_to(owned.owner):
            return owned.owner.parent / "package-local-cache" / path.relative_to(owned.owner)
        return original_resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", virtualized)
    assert permissions.inspect_component_permissions(owned.profile, security_api=owned.api)["paths_examined"] == len(owned.nodes)
    backup = owned.owner / "private-backup.json"
    report = permissions.repair_component_permissions(owned.profile, security_api=owned.api, backup_file=backup)
    assert report.writes == len(owned.nodes)
    restored = permissions.load_component_permission_backup(backup, security_api=owned.api)
    permissions.rollback_component_permissions(restored)
    assert owned.api.acls == owned.api.original


def test_resolved_directory_escape_is_rejected_before_acl_access(owned, monkeypatch):
    original_resolve = Path.resolve
    escaped = owned.profile / "SODA" / "1.0"
    def resolve(path, *args, **kwargs):
        if path == escaped:
            return owned.owner.parent / "unowned-component"
        return original_resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(permissions.ComponentPermissionError, match="component_path_outside_root"):
        permissions.repair_component_permissions(owned.profile, security_api=owned.api)
    assert not owned.api.calls


def test_hardlinked_component_file_is_rejected_before_acl_access(owned, monkeypatch):
    original = permissions._safe_stat
    blocked = next(path for path in owned.nodes if path.is_file())
    def check(path):
        value = original(path)
        if path == blocked:
            return SimpleNamespace(st_mode=value.st_mode, st_nlink=2)
        return value
    monkeypatch.setattr(permissions, "_safe_stat", check)
    with pytest.raises(permissions.ComponentPermissionError, match="component_path_is_hardlink"):
        permissions.repair_component_permissions(owned.profile, security_api=owned.api)
    assert not owned.api.calls


def test_no_components_requires_no_native_permission_api(tmp_path, monkeypatch):
    owner = tmp_path / "owned-app"
    monkeypatch.setattr(permissions, "data_root", lambda: owner)
    monkeypatch.setattr(permissions, "_WindowsAclApi", lambda: pytest.fail("must not load Windows ACLs"))
    assert permissions.inspect_component_permissions() == {"paths_examined": 0, "paths_needing_repair": 0}
    report = permissions.repair_component_permissions()
    assert report.summary() == {"paths_examined": 0, "paths_needing_repair": 0, "writes": 0, "rolled_back": False}


@pytest.mark.parametrize("relative", ["../Default/Cookies", "Default/Cookies", "SODA/../../Default/Cookies", "C:/outside"])
def test_tampered_backup_cannot_restore_permissions_on_accounts_or_other_paths(owned, relative):
    backup = owned.owner / "rollback.json"
    permissions.repair_component_permissions(owned.profile, security_api=owned.api, backup_file=backup)
    value = json.loads(backup.read_text(encoding="utf-8"))
    value["snapshots"][0]["relative"] = relative
    backup.write_text(json.dumps(value), encoding="utf-8")
    owned.api.calls.clear()
    with pytest.raises(permissions.ComponentPermissionError, match="component_backup_invalid"):
        permissions.load_component_permission_backup(backup, security_api=owned.api)
    assert not owned.api.calls


def test_migration_repairs_copy_in_staging_before_rename_without_touching_original(owned, tmp_path, monkeypatch):
    source = tmp_path / "legacy" / "chrome-caption"
    component = source / "dedicated-profile" / "SODA" / "1.0" / "SODAFiles" / "SODA.dll"
    component.parent.mkdir(parents=True)
    component.write_bytes(b"original-component")
    account = source / "dedicated-profile" / "Default" / "Cookies"
    account.parent.mkdir()
    account.write_bytes(b"original-account")
    target = owned.owner / "fresh" / "chrome-caption"
    calls = []
    def repair(profile):
        assert profile == target.with_name("chrome-caption.migration-staging") / "dedicated-profile"
        assert not target.exists()
        assert (profile / "SODA/1.0/SODAFiles/SODA.dll").read_bytes() == b"original-component"
        calls.append(profile)
    monkeypatch.setattr(migration, "repair_component_permissions", repair)
    assert migration._copy_directory(source, target)
    assert len(calls) == 1
    assert (target / "dedicated-profile/Default/Cookies").read_bytes() == b"original-account"
    assert component.read_bytes() == b"original-component" and account.read_bytes() == b"original-account"


def test_failed_staging_permission_repair_never_publishes_partial_profile(owned, tmp_path, monkeypatch):
    source = tmp_path / "legacy" / "chrome-caption"
    (source / "dedicated-profile").mkdir(parents=True)
    target = owned.owner / "fresh" / "chrome-caption"
    def failed(profile):
        raise permissions.ComponentPermissionError("component_acl_repair_failed")
    monkeypatch.setattr(migration, "repair_component_permissions", failed)
    with pytest.raises(permissions.ComponentPermissionError):
        migration._copy_directory(source, target)
    assert not target.exists()
    assert target.with_name("chrome-caption.migration-staging").is_dir()
    assert source.is_dir()


def fake_native_api():
    class Identity:
        def __init__(self, name):
            self.name = name
        def Equals(self, other):
            return self.name == other.name
    class Acl:
        def __init__(self):
            self.other_rules = ["original-private-ace"]
            self.protected = True
            self.rules = []
            self.added = []
            self.restored = []
        def GetAccessRules(self, explicit, inherited, identity_type):
            assert explicit is inherited is True
            return self.rules
        def AddAccessRule(self, rule):
            self.added.append(rule)
        def GetSecurityDescriptorBinaryForm(self):
            return b"fake-original-access-descriptor"
        def SetSecurityDescriptorBinaryForm(self, value, sections):
            self.restored.append((bytes(value), sections))
    acl, reads, writes = Acl(), [], []
    def get(path, sections):
        reads.append((path, sections))
        return acl
    def set_acl(path, value):
        writes.append((path, value))
    api = NativeAclApi.__new__(NativeAclApi)
    api.Directory = api.File = SimpleNamespace(GetAccessControl=get, SetAccessControl=set_acl)
    api.users, api.world = Identity("users"), Identity("everyone")
    api.sections, api.identity_type = "access-only", "security-identifier-type"
    api.rights, api.inherit, api.no_inherit, api.propagate = 0x200A9, 3, 0, 0
    api.allow, api.deny = "allow", "deny"
    api.rule_type = lambda *args: args
    class ArrayFactory:
        def __getitem__(self, item):
            return lambda values: bytes(values)
    api.Array, api.Byte = ArrayFactory(), object()
    return api, acl, reads, writes, Identity


@pytest.mark.parametrize("directory,inheritance", [(True, 3), (False, 0)])
def test_native_acl_adapter_uses_read_execute_and_access_section_only(directory, inheritance):
    api, acl, reads, writes, _identity = fake_native_api()
    path = Path("synthetic-component")
    before = list(acl.other_rules), acl.protected
    snapshot = api.snapshot(path, directory)
    api.grant_users_rx(path, directory)
    assert acl.added == [(api.users, api.rights, inheritance, api.propagate, api.allow)]
    assert (acl.other_rules, acl.protected) == before
    api.restore(path, directory, snapshot)
    assert acl.restored == [(snapshot, "access-only")]
    assert all(sections == "access-only" for _path, sections in reads)
    assert len(writes) == 2 and all(value is acl for _path, value in writes)


@pytest.mark.parametrize("inherited", [False, True])
def test_native_acl_inspection_accepts_existing_rx_inheritance_without_duplicate_grant(inherited):
    api, acl, _reads, writes, _identity = fake_native_api()
    acl.rules = [SimpleNamespace(IdentityReference=api.users, AccessControlType=api.allow,
                                FileSystemRights=api.rights | 0x100000, InheritanceFlags=3,
                                IsInherited=inherited)]
    assert api.has_users_rx(Path("synthetic-component"), True)
    assert not writes and not acl.added


@pytest.mark.parametrize("reason", ["read-only", "noninheriting-dir", "inherit-only", "users-deny", "everyone-deny", "other-user"])
def test_native_acl_verification_does_not_mistake_insufficient_or_denied_rights_for_rx(reason):
    api, acl, _reads, writes, identity = fake_native_api()
    rule = SimpleNamespace(IdentityReference=api.users, AccessControlType=api.allow,
                           FileSystemRights=api.rights, InheritanceFlags=3)
    if reason == "read-only":
        rule.FileSystemRights = 0x20089
    elif reason == "noninheriting-dir":
        rule.InheritanceFlags = 0
    elif reason == "inherit-only":
        rule.PropagationFlags = 2
    elif reason == "other-user":
        rule.IdentityReference = identity("private-other-identity")
    else:
        rule.AccessControlType = api.deny
        if reason == "everyone-deny":
            rule.IdentityReference = api.world
    acl.rules = [rule]
    assert not api.has_users_rx(Path("synthetic-component"), True)
    assert not writes


def test_rollback_attempts_remaining_components_after_one_restore_failure(owned):
    report = permissions.repair_component_permissions(owned.profile, security_api=owned.api)
    original = owned.api.restore
    calls = []
    failed = owned.nodes[1]
    def restore(path, directory, descriptor):
        calls.append(path)
        if path == failed:
            raise OSError("private failure details")
        original(path, directory, descriptor)
    owned.api.restore = restore
    with pytest.raises(permissions.ComponentPermissionError, match="component_acl_rollback_failed"):
        permissions.rollback_component_permissions(report)
    assert calls == owned.nodes
    assert not report.rolled_back
    assert all(owned.api.acls[path] == owned.api.original[path] for path in owned.nodes if path != failed)
