"""Public distributions need no grant and retain the earlier installer API."""
import json
import sys

import pytest

from fanyiji import local_features


@pytest.fixture
def grant_root(tmp_path, monkeypatch):
    monkeypatch.setattr(local_features, "data_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(local_features.platform, "node", lambda: "test-host")
    return tmp_path


def test_distribution_without_external_grant_is_available_without_writing_data(grant_root):
    assert local_features.danmaku_available()
    assert not list(grant_root.iterdir())


def test_legacy_local_installer_remains_atomic_and_new_host_is_available(grant_root, monkeypatch):
    local_features.enable_local_danmaku()
    assert local_features.danmaku_available()
    assert not (grant_root / "local-features.tmp").exists()
    path = grant_root / "local-features.json"
    contents = path.read_bytes()
    assert json.loads(contents)["danmaku"] is True
    monkeypatch.setattr(local_features.platform, "node", lambda: "different-host")
    assert local_features.danmaku_available()
    assert path.read_bytes() == contents


@pytest.mark.parametrize("value", [None, [], {}, {"schema_version": 99},
    {"schema_version": 1, "host": "test-host", "danmaku": True}])
def test_old_external_grant_does_not_restrict_public_access_or_get_rewritten(grant_root, value):
    path = grant_root / "local-features.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    contents = path.read_bytes()
    assert local_features.danmaku_available()
    assert path.read_bytes() == contents


def test_malformed_legacy_grant_is_preserved_and_not_required(grant_root):
    path = grant_root / "local-features.json"
    path.write_bytes(b"\xffnot-json")
    assert local_features.danmaku_available()
    assert path.read_bytes() == b"\xffnot-json"


@pytest.mark.parametrize("frozen", [False, True])
def test_availability_never_reads_host_or_userdata(monkeypatch, frozen):
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    def forbidden():
        raise AssertionError("public availability must not inspect host grants")
    monkeypatch.setattr(local_features, "data_root", forbidden)
    monkeypatch.setattr(local_features.platform, "node", forbidden)
    assert local_features.danmaku_available()
