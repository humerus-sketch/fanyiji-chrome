"""Portable Windows application; model weights are downloaded separately."""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

root = Path(SPECPATH)
native_packages = ("sherpa_onnx",)
binaries = []
datas = []
for package in native_packages:
    binaries += collect_dynamic_libs(package)
    datas += collect_data_files(package, include_py_files=False)

a = Analysis(
    [str(root / "run.py")],
    pathex=[str(root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=["numpy", "sherpa_onnx", "sherpa_onnx.lib._sherpa_onnx"],
    excludes=["torch", "transformers", "huggingface_hub", "pytest", "tkinter", "matplotlib",
              "webview", "clr", "clr_loader", "ctranslate2", "sentencepiece", "pyaudiowpatch", "soxr",
              "fanyiji.pipeline", "fanyiji.asr_engine", "fanyiji.engines", "fanyiji.gguf_engine",
              "fanyiji.browser_helper", "fanyiji.live_source_service", "fanyiji.specialized_model"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="翻译姬·Chrome字幕版",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False,
    version=str(root / "version_info.txt"),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="翻译姬·Chrome字幕版")
