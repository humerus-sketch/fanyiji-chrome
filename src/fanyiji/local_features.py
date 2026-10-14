"""Public feature availability and compatibility with earlier local installers."""
from __future__ import annotations

import hashlib
import json
import os
import platform

from .paths import data_root


def _host_key() -> str:
    return hashlib.sha256(platform.node().casefold().encode("utf-8")).hexdigest()


def danmaku_available() -> bool:
    """The public edition includes sending; users still enable it explicitly."""
    return True


def enable_local_danmaku() -> None:
    """Retain the legacy installer API; the stored grant is no longer required."""
    path = data_root() / "local-features.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"schema_version": 1, "host": _host_key(), "danmaku": True}
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
