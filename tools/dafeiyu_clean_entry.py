from __future__ import annotations

import marshal
import os
import sys
import traceback
import types
from datetime import datetime
from pathlib import Path

from chat_pet.integration import install


def _trace(message: str) -> None:
    try:
        log_dir = (
            Path(os.environ.get("APPDATA", Path.home()))
            / "大肥鱼桌宠"
            / "chat"
        )
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with (log_dir / "clean_entry.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {message}\n")
    except Exception:
        pass


def _load_marshaled_module(name: str) -> types.ModuleType:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        code_path = Path(sys._MEIPASS) / "assets" / f"{name}.marshal"
    else:
        code_path = (
            Path(__file__).resolve().parent.parent
            / "dist"
            / "大肥鱼桌宠AI整合版"
            / "_internal"
            / "assets"
            / f"{name}.marshal"
        )

    module = types.ModuleType(name)
    module.__file__ = str(code_path)
    module.__package__ = ""
    sys.modules[name] = module
    code = marshal.loads(code_path.read_bytes())
    exec(code, module.__dict__)
    _trace(f"Loaded marshaled module: {name}")
    return module


try:
    _trace("Clean entry started")
    install()
    _trace("Integrated chat installed")
    _load_marshaled_module("ui_console")
    original_main = _load_marshaled_module("_dafeiyu_original_main")
    _trace("Original desktop pet module loaded; entering main")
    original_main.main()
except BaseException:
    _trace("Clean entry failed:\n" + traceback.format_exc())
    raise
