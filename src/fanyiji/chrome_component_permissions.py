"""Preserve SODA component access after copying an owned Chrome profile.

Only the SODA and SODALanguagePacks trees receive the installer-equivalent
BUILTIN Users read/execute rule. Account/profile directories are never targets.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import stat

from .paths import data_root

_COMPONENTS = ("SODA", "SODALanguagePacks")


class ComponentPermissionError(RuntimeError):
    """Fixed errors must not expose account SIDs or security descriptors."""
    def __init__(self, code="component_acl_failed"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _Snapshot:
    path: Path = field(repr=False)
    directory: bool
    identity: tuple[int, int]
    descriptor: bytes = field(repr=False)


@dataclass
class ComponentPermissionRepair:
    paths_examined: int
    paths_needing_repair: int
    writes: int = 0
    rolled_back: bool = False
    _profile: Path | None = field(default=None, repr=False)
    _snapshots: tuple[_Snapshot, ...] = field(default=(), repr=False)
    _security_api: object | None = field(default=None, repr=False)

    def summary(self) -> dict:
        return {"paths_examined": self.paths_examined, "paths_needing_repair": self.paths_needing_repair,
                "writes": self.writes, "rolled_back": self.rolled_back}


def _safe_stat(path: Path):
    value = path.lstat()
    if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
        raise ComponentPermissionError("component_path_is_link")
    if stat.S_ISREG(value.st_mode) and value.st_nlink > 1:
        raise ComponentPermissionError("component_path_is_hardlink")
    return value


def _owned_profile(profile: Path | None) -> Path:
    owner = data_root().absolute()
    requested = (Path(profile).absolute() if profile is not None else
                 owner / "chrome-caption" / "dedicated-profile")
    allowed = (owner / "chrome-caption" / "dedicated-profile",
               owner / "chrome-caption.migration-staging" / "dedicated-profile")
    if requested not in allowed:
        raise ComponentPermissionError("profile_not_owned")
    # Reject links before resolving: a junction must not redirect a permission grant.
    for path in (owner, requested.parent, requested):
        if path.exists() or path.is_symlink():
            _safe_stat(path)
    if not requested.resolve().is_relative_to(owner.resolve()):
        raise ComponentPermissionError("profile_not_owned")
    return requested


def _component_nodes(profile: Path):
    nodes = []
    for name in _COMPONENTS:
        root = profile / name
        if not root.exists() and not root.is_symlink():
            continue
        value = _safe_stat(root)
        if not stat.S_ISDIR(value.st_mode):
            raise ComponentPermissionError("component_not_directory")
        nodes.append((root, value))
        for parent, directories, files in os.walk(root, followlinks=False):
            directories.sort()
            for child in sorted(directories + files):
                path = Path(parent) / child
                value = _safe_stat(path)
                # MSIX can resolve regular files to its package LocalCache while
                # their containing directories retain the logical profile path.
                # Fence every directory and reject file links instead of treating
                # this OS file-virtualization alias as a directory traversal.
                boundary = path if stat.S_ISDIR(value.st_mode) else path.parent
                if not boundary.resolve().is_relative_to(root.resolve()):
                    raise ComponentPermissionError("component_path_outside_root")
                if not stat.S_ISDIR(value.st_mode) and not stat.S_ISREG(value.st_mode):
                    raise ComponentPermissionError("component_not_regular")
                if stat.S_ISREG(value.st_mode) and value.st_nlink > 1:
                    raise ComponentPermissionError("component_path_is_hardlink")
                nodes.append((path, value))
    # Parents are restored before children to undo any inherited ACL propagation.
    return sorted(nodes, key=lambda item: (len(item[0].parts), str(item[0]).casefold()))


class _WindowsAclApi:
    """Update only AccessControlSections.Access, retaining other ACEs and metadata."""
    def __init__(self):
        if os.name != "nt":
            raise ComponentPermissionError("unsupported_platform")
        import clr
        clr.AddReference("System")
        from System import Array, Byte
        from System.IO import Directory, File
        from System.Security.AccessControl import (AccessControlSections, AccessControlType,
            FileSystemAccessRule, FileSystemRights, InheritanceFlags, PropagationFlags)
        from System.Security.Principal import SecurityIdentifier, WellKnownSidType
        self.Array, self.Byte, self.Directory, self.File = Array, Byte, Directory, File
        self.sections = AccessControlSections.Access
        self.allow = AccessControlType.Allow
        self.deny = AccessControlType.Deny
        self.rule_type = FileSystemAccessRule
        self.rights = FileSystemRights.ReadAndExecute
        self.inherit = InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit
        self.no_inherit = getattr(InheritanceFlags, "None")
        self.propagate = getattr(PropagationFlags, "None")
        self.users = SecurityIdentifier(WellKnownSidType.BuiltinUsersSid, None)
        self.world = SecurityIdentifier(WellKnownSidType.WorldSid, None)
        self.identity_type = clr.GetClrType(SecurityIdentifier)

    def _get(self, path: Path, directory: bool):
        owner = self.Directory if directory else self.File
        return owner.GetAccessControl(str(path), self.sections)

    def snapshot(self, path: Path, directory: bool) -> bytes:
        return bytes(self._get(path, directory).GetSecurityDescriptorBinaryForm())

    def has_users_rx(self, path: Path, directory: bool) -> bool:
        allowed = 0
        required = int(self.rights)
        for rule in self._get(path, directory).GetAccessRules(True, True, self.identity_type):
            # InheritOnly applies to descendants, not to this directory itself.
            if int(getattr(rule, "PropagationFlags", 0)) & 2:
                continue
            if (rule.IdentityReference.Equals(self.users) or rule.IdentityReference.Equals(self.world)):
                if rule.AccessControlType == self.deny and int(rule.FileSystemRights) & required:
                    return False
            if (rule.IdentityReference.Equals(self.users) and rule.AccessControlType == self.allow
                    and (not directory or int(rule.InheritanceFlags) & int(self.inherit) == int(self.inherit))):
                allowed |= int(rule.FileSystemRights)
        return allowed & required == required

    def grant_users_rx(self, path: Path, directory: bool) -> None:
        acl = self._get(path, directory)
        acl.AddAccessRule(self.rule_type(self.users, self.rights,
            self.inherit if directory else self.no_inherit, self.propagate, self.allow))
        (self.Directory if directory else self.File).SetAccessControl(str(path), acl)

    def restore(self, path: Path, directory: bool, descriptor: bytes) -> None:
        acl = self._get(path, directory)
        acl.SetSecurityDescriptorBinaryForm(self.Array[self.Byte](list(descriptor)), self.sections)
        (self.Directory if directory else self.File).SetAccessControl(str(path), acl)


def _api(security_api):
    if security_api is not None:
        return security_api
    try:
        return _WindowsAclApi()
    except Exception:
        raise ComponentPermissionError("component_acl_api_unavailable") from None


def inspect_component_permissions(profile: Path | None = None, *, security_api=None) -> dict:
    """Read only; return counts without paths, SIDs or ACL contents."""
    profile = _owned_profile(profile)
    nodes = _component_nodes(profile)
    if not nodes:
        return {"paths_examined": 0, "paths_needing_repair": 0}
    api = _api(security_api)
    try:
        missing = sum(not api.has_users_rx(path, stat.S_ISDIR(value.st_mode)) for path, value in nodes)
    except Exception:
        raise ComponentPermissionError("component_acl_read_failed") from None
    return {"paths_examined": len(nodes), "paths_needing_repair": missing}


def _write_backup(report: ComponentPermissionRepair, backup_file: Path):
    # Never place account-SID-bearing rollback data in publicly readable component trees.
    owner = data_root().resolve()
    backup_file = Path(backup_file).absolute()
    if (not backup_file.parent.resolve().is_relative_to(owner) or
            backup_file.parent.resolve().is_relative_to(report._profile.resolve())):
        raise ComponentPermissionError("component_backup_location_invalid")
    for parent in (backup_file.parent, *backup_file.parent.parents):
        if parent.exists():
            _safe_stat(parent)
        if parent.resolve() == owner:
            break
    backup_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "profile": str(report._profile), "snapshots": [
        {"relative": item.path.relative_to(report._profile).as_posix(), "directory": item.directory,
         "identity": list(item.identity), "descriptor": base64.b64encode(item.descriptor).decode("ascii")}
        for item in report._snapshots]}
    with backup_file.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())


def _assert_unchanged(snapshot: _Snapshot):
    value = _safe_stat(snapshot.path)
    if ((value.st_dev, value.st_ino) != snapshot.identity or
            stat.S_ISDIR(value.st_mode) is not snapshot.directory):
        raise ComponentPermissionError("component_identity_changed")


def rollback_component_permissions(report: ComponentPermissionRepair, *, security_api=None) -> dict:
    """Restore the saved Access section only; parents before their children."""
    profile = _owned_profile(report._profile)
    current = {path for path, _value in _component_nodes(profile)}
    if not report._snapshots:
        report.rolled_back = True
        return report.summary()
    api = _api(security_api if security_api is not None else report._security_api)
    failed = False
    report.rolled_back = False
    for item in report._snapshots:
        try:
            if item.path not in current:
                raise ComponentPermissionError("component_identity_changed")
            _assert_unchanged(item)
            api.restore(item.path, item.directory, item.descriptor)
        except Exception:
            # One locked/changed component must not prevent restoring its siblings.
            failed = True
    if failed:
        raise ComponentPermissionError("component_acl_rollback_failed") from None
    report.rolled_back = True
    return report.summary()


def load_component_permission_backup(backup_file: Path, *, security_api=None) -> ComponentPermissionRepair:
    """Load an explicit private rollback file, validating every path before use."""
    owner = data_root().resolve()
    backup_file = Path(backup_file).absolute()
    if not backup_file.parent.resolve().is_relative_to(owner):
        raise ComponentPermissionError("component_backup_location_invalid")
    if _safe_stat(backup_file).st_size > 32 * 1024 * 1024:
        raise ComponentPermissionError("component_backup_invalid")
    try:
        payload = json.loads(backup_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError
        profile = _owned_profile(Path(payload["profile"]))
        if backup_file.parent.resolve().is_relative_to(profile.resolve()):
            raise ComponentPermissionError("component_backup_location_invalid")
        current = {path for path, _value in _component_nodes(profile)}
        values = payload["snapshots"]
        if not isinstance(values, list) or not values:
            raise ValueError
        snapshots = []
        for value in values:
            relative = Path(value["relative"])
            if (relative.is_absolute() or ".." in relative.parts or not relative.parts
                    or relative.parts[0] not in _COMPONENTS or ":" in str(relative)):
                raise ValueError
            path = profile / relative
            identity = value["identity"]
            descriptor = base64.b64decode(value["descriptor"], validate=True)
            if (path not in current or type(value["directory"]) is not bool or not descriptor
                    or not isinstance(identity, list) or len(identity) != 2
                    or any(type(item) is not int or item < 0 for item in identity)):
                raise ValueError
            snapshots.append(_Snapshot(path, value["directory"], tuple(identity), descriptor))
        if len({item.path for item in snapshots}) != len(snapshots):
            raise ValueError
        snapshots.sort(key=lambda item: (len(item.path.parts), str(item.path).casefold()))
        for item in snapshots:
            _assert_unchanged(item)
    except ComponentPermissionError:
        raise
    except Exception:
        raise ComponentPermissionError("component_backup_invalid") from None
    return ComponentPermissionRepair(len(snapshots), len(snapshots), _profile=profile,
                                       _snapshots=tuple(snapshots), _security_api=security_api)


def repair_component_permissions(profile: Path | None = None, *, security_api=None,
                                 backup_file: Path | None = None) -> ComponentPermissionRepair:
    """Explicit repair, or repair of a copied staging profile before publication.

Existing-profile callers can provide a private backup file before granting.
They must have authorization to change these component permissions.
"""
    profile = _owned_profile(profile)
    nodes = _component_nodes(profile)
    if not nodes:
        return ComponentPermissionRepair(0, 0, _profile=profile)
    api = _api(security_api)
    try:
        missing = [path for path, value in nodes if not api.has_users_rx(path, stat.S_ISDIR(value.st_mode))]
        if not missing:
            return ComponentPermissionRepair(len(nodes), 0, _profile=profile)
        snapshots = tuple(_Snapshot(path, stat.S_ISDIR(value.st_mode), (value.st_dev, value.st_ino),
                                   api.snapshot(path, stat.S_ISDIR(value.st_mode))) for path, value in nodes)
    except Exception:
        raise ComponentPermissionError("component_acl_read_failed") from None
    report = ComponentPermissionRepair(len(nodes), len(missing), _profile=profile,
                                       _snapshots=snapshots, _security_api=api)
    if backup_file is not None:
        _write_backup(report, backup_file)
    try:
        for item in snapshots:
            _assert_unchanged(item)
            if not api.has_users_rx(item.path, item.directory):
                api.grant_users_rx(item.path, item.directory)
                report.writes += 1
        if any(not api.has_users_rx(item.path, item.directory) for item in snapshots):
            raise ComponentPermissionError("component_acl_verification_failed")
    except Exception:
        try:
            rollback_component_permissions(report, security_api=api)
        except Exception:
            raise ComponentPermissionError("component_acl_rollback_failed") from None
        raise ComponentPermissionError("component_acl_repair_failed") from None
    return report
