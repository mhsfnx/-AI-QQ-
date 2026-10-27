# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


root = Path(SPECPATH).resolve()
legacy_internal = root / "packaging" / "runtime"

datas = [
    (str(legacy_internal / "assets"), "assets"),
    (str(legacy_internal / "sprites"), "sprites"),
]
binaries = []
hiddenimports = []

for package_dir in ("PySide6", "shiboken6"):
    source_dir = legacy_internal / package_dir
    for source in source_dir.iterdir():
        if source.is_file():
            binaries.append((str(source), package_dir))

for runtime_dll in ("icuuc.dll", "icuuc_78.dll", "icudt78.dll"):
    binaries.append((str(root / "packaging" / runtime_dll), "."))

for package in ("requests", "psutil", "websocket", "winsdk", "pynvml"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

hiddenimports += collect_submodules("winsdk")
hiddenimports += [
    "PySide6.QtMultimedia",
    "PySide6.QtNetwork",
    "PySide6.QtSvg",
    "array",
    "asyncio",
    "audioop",
    "contextlib",
    "ctypes",
    "datetime",
    "hashlib",
    "json",
    "math",
    "os",
    "random",
    "re",
    "string",
    "subprocess",
    "sys",
    "threading",
    "time",
    "wave",
    "winreg",
    "winsound",
]

a = Analysis(
    [str(root / "tools" / "dafeiyu_clean_entry.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="大肥鱼桌宠",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(root / "packaging" / "app.ico")],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="大肥鱼桌宠",
)
