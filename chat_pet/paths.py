from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = "大肥鱼桌宠"
CHAT_FOLDER = "chat"


def resource_dir() -> Path:
    """Return the directory that contains bundled assets."""
    override = os.environ.get("CHAT_PET_RESOURCE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "assets"
    return Path(__file__).resolve().parent.parent / "assets"


def app_base_dir() -> Path:
    """Return the source root or the directory containing the packaged EXE."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resolve_app_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (app_base_dir() / path).resolve()


def data_dir() -> Path:
    """Return a writable, per-user data directory."""
    override = os.environ.get("CHAT_PET_DATA_DIR")
    if override:
        path = Path(override).expanduser().resolve()
    else:
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        path = base / APP_NAME / CHAT_FOLDER
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    return data_dir() / "memory.sqlite3"


def config_path() -> Path:
    return data_dir() / "settings.json"


def ui_state_path() -> Path:
    return data_dir() / "ui_state.json"


def log_path() -> Path:
    return data_dir() / "chat_pet.log"


def stickers_dir() -> Path:
    override = os.environ.get("CHAT_PET_STICKERS_DIR")
    path = (
        resolve_app_path(override)
        if override
        else app_base_dir() / "stickers"
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def napcat_dir() -> Path:
    return app_base_dir() / "napcat"


def napcat_runtime_dir() -> Path:
    return napcat_dir() / "runtime"
