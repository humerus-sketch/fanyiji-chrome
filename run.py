"""Windowed entry point for the independent Chrome caption application."""
import os
import sys
import traceback
from pathlib import Path


def _startup_data_root():
    try:
        from fanyiji.paths import data_root
        return data_root()
    except Exception:
        value = os.environ.get("FANYIJI_CHROME_DATA")
        if value:
            return Path(value).resolve()
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local"))) / "FanyiJiChrome"


def launch():
    try:
        if "--chrome-caption-worker" in sys.argv:
            from fanyiji.chrome_caption_worker import main
            return main([arg for arg in sys.argv[1:] if arg != "--chrome-caption-worker"])
        from fanyiji.app import main
        return main()
    except Exception:
        message = traceback.format_exc()
        root = _startup_data_root()
        try:
            root.mkdir(parents=True, exist_ok=True)
            (root / "startup-error.log").write_text(message, encoding="utf-8")
        except OSError:
            pass
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        if "--smoke-test" not in sys.argv:
            try:
                from PySide6.QtWidgets import QApplication, QMessageBox
                from fanyiji.identity import APP_NAME
                app = QApplication.instance() or QApplication(sys.argv[:1])
                QMessageBox.critical(None, APP_NAME + " — 无法启动", "启动失败。诊断信息：\n" + str(root / "startup-error.log"))
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(launch())
