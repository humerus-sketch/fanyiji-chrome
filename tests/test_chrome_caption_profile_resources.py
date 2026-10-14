import json

from fanyiji.chrome_browser_service import seed_caption_preferences


def test_migrated_caption_resources_use_the_new_owned_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    legacy = tmp_path / "LocalJaZhSubtitles/chrome-caption/dedicated-profile"
    profile = tmp_path / "FanyiJiChrome/chrome-caption/dedicated-profile"
    profile.mkdir(parents=True)
    paths = {
        "soda_binary_path": "SODA/1/SODAFiles/SODA.dll",
        "soda_en_us_config_path": "SODALanguagePacks/en-US/1/SODAModels",
        "soda_ja_jp_config_path": "SODALanguagePacks/ja-JP/1/SODAModels",
        "soda_zh_cn_config_path": "SODALanguagePacks/cmn-Hans-CN/1/SODAModels",
    }
    for key, relative in paths.items():
        target = profile / relative
        if key != "soda_binary_path":
            target = target / "model.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"synthetic resource")
    state = {"accessibility": {"captions": {key: str(legacy / relative) for key, relative in paths.items()}},
             "unrelated_preferences": {"fixture": "preserve"}}
    state_path = profile / "Local State"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    account = profile / "Default" / "fixture-account-data"
    account.parent.mkdir(parents=True)
    account.write_bytes(b"synthetic login state, do not change")
    seed_caption_preferences(profile)
    updated = json.loads(state_path.read_text(encoding="utf-8"))
    assert all(updated["accessibility"]["captions"][key] == str((profile / relative).resolve())
               for key, relative in paths.items())
    assert updated["unrelated_preferences"] == state["unrelated_preferences"]
    assert account.read_bytes() == b"synthetic login state, do not change"
    first = state_path.read_bytes()
    seed_caption_preferences(profile)
    assert state_path.read_bytes() == first


def test_missing_new_resource_keeps_old_reference_and_foreign_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    legacy = tmp_path / "LocalJaZhSubtitles/chrome-caption/dedicated-profile"
    profile = tmp_path / "new-profile"
    profile.mkdir()
    old_path = legacy / "SODA/1/SODAFiles/SODA.dll"
    state = {"accessibility": {"captions": {"soda_binary_path": str(old_path),
             "unrelated_library_path": str(old_path), "soda_ja_jp_config_path": ["invalid"]}}}
    (profile / "Local State").write_text(json.dumps(state), encoding="utf-8")
    seed_caption_preferences(profile)
    updated = json.loads((profile / "Local State").read_text(encoding="utf-8"))["accessibility"]["captions"]
    assert updated["soda_binary_path"] == str(old_path)
    assert updated["unrelated_library_path"] == str(old_path)
    assert updated["soda_ja_jp_config_path"] == ["invalid"]


def test_resource_path_outside_legacy_profile_is_not_rewritten(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    profile = tmp_path / "new-profile"
    profile.mkdir()
    foreign = tmp_path / "other-browser/SODA.dll"
    state = {"accessibility": {"captions": {"soda_binary_path": str(foreign)}}}
    (profile / "Local State").write_text(json.dumps(state), encoding="utf-8")
    seed_caption_preferences(profile)
    updated = json.loads((profile / "Local State").read_text(encoding="utf-8"))
    assert updated["accessibility"]["captions"]["soda_binary_path"] == str(foreign)
