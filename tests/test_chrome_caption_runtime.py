from pathlib import Path

import pytest

from fanyiji.chrome_caption_runtime import (matches_installed_chrome, observed_source_matches,
                                           validate_source_url)


@pytest.mark.parametrize("url", [None, "", "http://live.bilibili.com/1698225", "file:///C:/private",
                               "https://user:secret@live.bilibili.com/1698225",
                               "https://live.bilibili.com:444/1698225", "https://live.bilibili.com/\n1698225",
                               "https://live.bilibili.com/1698225 space", "https://live.bilibili.com\\private",
                               "https://live.bilibili.com:" + "9" * 10, "https://x/" + "x" * 2050],
                         ids=["none", "empty", "http", "file", "credentials", "port", "control",
                              "space", "backslash", "invalid-port", "oversized"])
def test_invalid_sources_have_fixed_errors(url):
    with pytest.raises(ValueError) as error:
        validate_source_url(url)
    assert str(error.value) == "invalid_source"


def test_valid_source_remains_explicit_canonical_room():
    url = "https://live.bilibili.com/1698225"
    assert validate_source_url(url) == url


@pytest.mark.parametrize("value", ["https://example.com/1698225", "https://www.bilibili.com/1698225",
                                 "https://live.bilibili.com/1234567890123", "https://live.bilibili.com/0",
                                 "https://live.bilibili.com/video/1698225"])
def test_only_explicit_bilibili_live_room_is_allowed(value):
    with pytest.raises(ValueError):
        validate_source_url(value)


def test_normalized_native_address_is_bound_to_exact_room():
    expected = "https://live.bilibili.com/1698225"
    assert observed_source_matches("live.bilibili.com/1698225/", expected)
    assert observed_source_matches(expected + "?tracking=discarded#fragment", expected)
    assert validate_source_url(expected + "?tracking=discarded") == expected
    assert not observed_source_matches("live.bilibili.com/21399689", expected)
    assert not observed_source_matches("http://live.bilibili.com/1698225", expected)
    assert not observed_source_matches("evil.test/1698225", expected)


def test_settings_operation_is_not_allowed_as_caption_source():
    with pytest.raises(ValueError):
        validate_source_url("chrome://settings/accessibility")


def test_attach_verifies_official_executable_path_not_merely_filename(tmp_path):
    official, other = tmp_path / "official" / "chrome.exe", tmp_path / "other" / "chrome.exe"
    official.parent.mkdir()
    other.parent.mkdir()
    official.write_bytes(b"synthetic-official")
    other.write_bytes(b"synthetic-other")
    assert matches_installed_chrome(official, official)
    assert not matches_installed_chrome(other, official)
    assert not matches_installed_chrome(tmp_path / "missing" / "chrome.exe", official)
    assert not matches_installed_chrome(None, official)


