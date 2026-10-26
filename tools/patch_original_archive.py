from __future__ import annotations

import argparse
import marshal
import shutil
import struct
import sys
import tempfile
import zlib
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader
from PyInstaller.archive.writers import ZlibArchiveWriter
from PyInstaller.loader.pyimod01_archive import PYZ_ITEM_NSPKG, PYZ_ITEM_PKG


COOKIE_MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"
COOKIE_FORMAT = "!8sIIII64s"
TOC_FORMAT = "!IIIIBc"
COOKIE_LENGTH = struct.calcsize(COOKIE_FORMAT)
TOC_LENGTH = struct.calcsize(TOC_FORMAT)

CHAT_MODULES = (
    "chat_pet",
    "chat_pet.api",
    "chat_pet.commands",
    "chat_pet.config",
    "chat_pet.chat_style",
    "chat_pet.database",
    "chat_pet.image_ocr",
    "chat_pet.integration",
    "chat_pet.memory",
    "chat_pet.paths",
    "chat_pet.persona",
    "chat_pet.qq_bridge",
    "chat_pet.qt_ui",
    "chat_pet.security",
    "chat_pet.service",
    "chat_pet.stickers",
)
SQLITE_MODULES = (
    "sqlite3",
    "sqlite3.dbapi2",
    "sqlite3.dump",
)


def module_source(project_root: Path, module: str) -> Path:
    relative = Path(*module.split("."))
    package_init = project_root / relative / "__init__.py"
    if package_init.exists():
        return package_init
    return project_root / (str(relative) + ".py")


def build_pyz(source_exe: Path, project_root: Path, target: Path) -> None:
    archive = CArchiveReader(str(source_exe))
    pyz = archive.open_embedded_archive("PYZ.pyz")
    code_dict: dict[str, object] = {}
    entries: list[tuple[str, str | None, str]] = []

    for name, (typecode, _offset, _length) in pyz.toc.items():
        if typecode == PYZ_ITEM_NSPKG:
            code_dict.pop(name, None)
            entries.append((name, None, "PYMODULE"))
            continue
        module_path = Path(*name.split("."))
        source_name = (
            str(module_path / "__init__.py")
            if typecode == PYZ_ITEM_PKG
            else str(module_path) + ".py"
        )
        code_dict[name] = pyz.extract(name)
        entries.append((name, source_name, "PYMODULE"))

    for module in CHAT_MODULES:
        path = module_source(project_root, module)
        if not path.exists():
            raise FileNotFoundError(f"Missing chat module: {path}")
        source = path.read_text(encoding="utf-8")
        code_dict[module] = compile(source, str(path), "exec", optimize=0)
        entries = [entry for entry in entries if entry[0] != module]
        entries.append((module, str(path), "PYMODULE"))

    sqlite_root = Path(sys.base_prefix) / "Lib"
    for module in SQLITE_MODULES:
        relative = Path(*module.split("."))
        package_init = sqlite_root / relative / "__init__.py"
        path = package_init if package_init.exists() else sqlite_root / (str(relative) + ".py")
        if not path.exists():
            raise FileNotFoundError(f"Python SQLite module is missing: {path}")
        source = path.read_text(encoding="utf-8")
        code_dict[module] = compile(source, str(path), "exec", optimize=0)
        entries = [entry for entry in entries if entry[0] != module]
        entries.append((module, str(path), "PYMODULE"))

    original_main = code_dict.get("_dafeiyu_original_main")
    if original_main is None:
        original_main = marshal.loads(archive.extract("桌宠"))
    code_dict["_dafeiyu_original_main"] = original_main
    entries.append(("_dafeiyu_original_main", "桌宠.py", "PYMODULE"))
    ZlibArchiveWriter(str(target), entries, code_dict=code_dict)


def _write_blob(
    handle,
    blob: bytes,
    name: str,
    typecode: str,
    *,
    compress: bool,
) -> tuple[int, int, int, int, str, str]:
    offset = handle.tell()
    uncompressed_length = len(blob)
    if compress:
        blob = zlib.compress(blob, 9)
    handle.write(blob)
    return (
        offset,
        len(blob),
        uncompressed_length,
        int(compress),
        typecode,
        name,
    )


def _serialize_toc(toc: list[tuple[int, int, int, int, str, str]]) -> bytes:
    serialized = []
    for offset, compressed_length, data_length, compressed, typecode, name in toc:
        encoded = name.encode("utf-8")
        name_length = len(encoded) + 1
        entry_length = TOC_LENGTH + name_length
        if entry_length % 16:
            name_length += 16 - (entry_length % 16)
        serialized.append(
            struct.pack(
                TOC_FORMAT + f"{name_length}s",
                TOC_LENGTH + name_length,
                offset,
                compressed_length,
                data_length,
                compressed,
                typecode.encode("ascii"),
                encoded,
            )
        )
    return b"".join(serialized)


def build_carchive(
    source_exe: Path,
    new_pyz: Path,
    entry_source: Path,
    target_archive: Path,
) -> None:
    archive = CArchiveReader(str(source_exe))
    entry_code = compile(
        entry_source.read_text(encoding="utf-8"),
        "桌宠.py",
        "exec",
        optimize=0,
    )
    entry_blob = marshal.dumps(entry_code)
    toc: list[tuple[int, int, int, int, str, str]] = []

    with target_archive.open("wb") as handle:
        for option in archive.options:
            toc.append(_write_blob(handle, b"", option, "o", compress=False))

        for name in archive.toc:
            entry = archive.toc[name]
            typecode = entry[4]
            if name == "桌宠":
                toc.append(
                    _write_blob(handle, entry_blob, name, typecode, compress=True)
                )
                continue
            if name == "PYZ.pyz":
                blob = new_pyz.read_bytes()
                compress = False
            else:
                blob = archive.extract(name)
                compress = bool(entry[3])
            toc.append(_write_blob(handle, blob, name, typecode, compress=compress))

        toc_offset = handle.tell()
        toc_data = _serialize_toc(toc)
        handle.write(toc_data)
        archive_length = toc_offset + len(toc_data) + COOKIE_LENGTH
        handle.write(
            struct.pack(
                COOKIE_FORMAT,
                COOKIE_MAGIC,
                archive_length,
                toc_offset,
                len(toc_data),
                sys.version_info[0] * 100 + sys.version_info[1],
                b"python312.dll",
            )
        )


def patch_executable(
    source_exe: Path,
    output_exe: Path,
    project_root: Path,
    entry_source: Path,
) -> None:
    reader = CArchiveReader(str(source_exe))
    with tempfile.TemporaryDirectory(prefix="dafeiyu_patch_") as folder:
        temp = Path(folder)
        new_pyz = temp / "PYZ.pyz"
        new_archive = temp / "PKG-00.pkg"
        build_pyz(source_exe, project_root, new_pyz)
        build_carchive(source_exe, new_pyz, entry_source, new_archive)
        output_exe.parent.mkdir(parents=True, exist_ok=True)
        with source_exe.open("rb") as source, output_exe.open("wb") as target:
            target.write(source.read(reader._start_offset))
            with new_archive.open("rb") as archive:
                shutil.copyfileobj(archive, target)

    verify = CArchiveReader(str(output_exe))
    patched_pyz = verify.open_embedded_archive("PYZ.pyz")
    for module in (*CHAT_MODULES, *SQLITE_MODULES):
        if module not in patched_pyz.toc:
            raise RuntimeError(f"Patched PYZ is missing {module}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inject AI chat into the original PyInstaller app.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--entry", type=Path, required=True)
    args = parser.parse_args()
    patch_executable(
        args.source.resolve(),
        args.output.resolve(),
        args.project_root.resolve(),
        args.entry.resolve(),
    )
    print(args.output.resolve())


if __name__ == "__main__":
    main()
