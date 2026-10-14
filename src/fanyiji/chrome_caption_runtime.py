"""Discover installed stable Chrome; never install or modify another profile."""
from __future__ import annotations

import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from .paths import data_root


def caption_profile() -> Path:
    return data_root() / "chrome-caption" / "profile"


def find_chrome() -> Path | None:
    if os.name != "nt":
        return None
    import winreg
    candidates = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                                    0, winreg.KEY_READ | view) as key:
                    value, _kind = winreg.QueryValueEx(key, "")
                if isinstance(value, str):
                    candidates.append(Path(os.path.expandvars(value.strip('"'))))
            except OSError:
                pass
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        if os.environ.get(variable):
            candidates.append(Path(os.environ[variable]) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for path in candidates:
        if path.is_absolute() and path.name.lower() == "chrome.exe" and path.is_file():
            return path.resolve()
    return None


def validate_source_url(url: object) -> str:
    if not isinstance(url, str) or len(url) > 2048 or any(ord(char) < 33 for char in url):
        raise ValueError("invalid_source")
    try:
        value = urlsplit(url)
        if (value.scheme != "https" or value.username is not None or value.password is not None
                or value.port not in (None, 443) or value.hostname != "live.bilibili.com" or "\\" in url
                or not re.fullmatch(r"/[1-9][0-9]{0,11}/?", value.path)):
            raise ValueError("invalid_source")
        return "https://live.bilibili.com/" + str(int(value.path.strip("/")))
    except ValueError:
        raise ValueError("invalid_source") from None


def observed_source_matches(observed: object, expected: str) -> bool:
    """Chrome may elide the scheme; discard tracking, never accept a room redirect."""
    if not isinstance(observed, str):
        return False
    observed = observed.strip()
    if not observed.startswith("https://"):
        observed = "https://" + observed
    try:
        return validate_source_url(observed) == validate_source_url(expected)
    except ValueError:
        return False


def source_room(url: str) -> str:
    return urlsplit(validate_source_url(url)).path.strip("/")


def matches_installed_chrome(executable: object, installed: Path | None = None) -> bool:
    """An executable merely named chrome.exe is insufficient for readonly attach."""
    if not isinstance(executable, (str, Path)):
        return False
    expected = find_chrome() if installed is None else Path(installed)
    if expected is None:
        return False
    try:
        actual, expected = Path(executable).resolve(), expected.resolve()
        return (actual.is_absolute() and actual.name.lower() == "chrome.exe" and expected.is_file()
                and actual.is_file() and str(actual).casefold() == str(expected).casefold())
    except (OSError, ValueError):
        return False


