"""Codex rule repair must remain available without local translation engines."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from fanyiji import codex_engine
from fanyiji.language_codes import _language


@pytest.mark.parametrize("language", ["zh", "ja"])
def test_supported_translation_target_is_preserved(language):
    assert _language(language) == language
    assert codex_engine._language(language) == language


@pytest.mark.parametrize("language", ["en", "mixed", "auto", "", "ZH", None, 1])
def test_unsupported_translation_target_raises_original_error(language):
    with pytest.raises(ValueError, match="只支持 zh 或 ja"):
        _language(language)


def test_codex_and_danmaku_ai_import_without_standard_engine_or_webview():
    script = """
import importlib.abc
import json
import sys

blocked = ('fanyiji.engines', 'fanyiji.gguf_engine', 'fanyiji.pipeline',
           'fanyiji.asr_engine', 'fanyiji.browser_helper', 'ctranslate2',
           'sentencepiece', 'pyaudiowpatch', 'soxr', 'webview')

class BlockStandard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise ImportError('standard_dependency_blocked')

sys.meta_path.insert(0, BlockStandard())
from fanyiji.codex_engine import CodexTranslationEngine
from fanyiji.danmaku_ai import DanmakuAIAnalyzer, DANMAKU_AI_OUTPUT_SCHEMA
print(json.dumps({'loaded': True,
                  'schema_fields': list(DANMAKU_AI_OUTPUT_SCHEMA['properties']),
                  'standard_modules': sorted(name for name in sys.modules
                      if any(name == prefix or name.startswith(prefix + '.') for prefix in blocked))}))
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(codex_engine.__file__).resolve().parents[1])
    result = subprocess.run([sys.executable, "-c", script], env=environment,
                            capture_output=True, text=True, encoding="utf-8", timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"loaded": True, "schema_fields": ["revised_text"],
                                       "standard_modules": []}
