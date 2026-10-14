"""Translation language validation without local recognition/model dependencies."""
from __future__ import annotations

SUPPORTED_LANGUAGES = frozenset({"zh", "ja"})


def _language(language: str) -> str:
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(f"只支持 zh 或 ja，收到：{language!r}")
    return language
