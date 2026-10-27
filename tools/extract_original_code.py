from __future__ import annotations

import argparse
import marshal
import struct
import types
import zipfile
import zlib
from pathlib import Path

COOKIE_MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"
COOKIE_FORMAT = "!8sIIII64s"
TOC_FORMAT = "!IIIIBc"
TOC_LENGTH = struct.calcsize(TOC_FORMAT)
PYZ_MAGIC = b"PYZ\0"


def extract_archive_modules(
    package: Path,
    output: Path,
) -> dict[str, types.CodeType]:
    """Extract the app's original entry code from a patched release archive."""
    with zipfile.ZipFile(package) as archive:
        member = "大肥鱼桌宠AI整合版/大肥鱼桌宠.exe"
        payload = archive.read(member)

    cookie_start = payload.rfind(COOKIE_MAGIC)
    if cookie_start < 0:
        raise ValueError("PyInstaller cookie was not found.")
    _magic, archive_length, toc_offset, toc_length, _pyvers, _pylib = (
        struct.unpack(
            COOKIE_FORMAT,
            payload[cookie_start : cookie_start + struct.calcsize(COOKIE_FORMAT)],
        )
    )
    archive_start = cookie_start + struct.calcsize(COOKIE_FORMAT) - archive_length
    toc_data = payload[
        archive_start + toc_offset : archive_start + toc_offset + toc_length
    ]
    entries: dict[str, tuple[int, int, int, int, str]] = {}
    cursor = 0
    while cursor < len(toc_data):
        entry_length, offset, length, raw_length, compressed, typecode = struct.unpack(
            TOC_FORMAT,
            toc_data[cursor : cursor + TOC_LENGTH],
        )
        cursor += TOC_LENGTH
        name_length = entry_length - TOC_LENGTH
        name = (
            toc_data[cursor : cursor + name_length]
            .rstrip(b"\0")
            .decode("utf-8")
        )
        cursor += name_length
        entries[name] = (
            offset,
            length,
            raw_length,
            compressed,
            typecode.decode("ascii"),
        )

    pyz_entry = entries["PYZ.pyz"]
    pyz_start = archive_start + pyz_entry[0]
    if payload[pyz_start : pyz_start + len(PYZ_MAGIC)] != PYZ_MAGIC:
        raise ValueError("Embedded PYZ archive was not found.")
    pyz_toc_offset = struct.unpack(
        "!i",
        payload[pyz_start + len(PYZ_MAGIC) + 4 : pyz_start + len(PYZ_MAGIC) + 8],
    )[0]
    pyz_toc = dict(marshal.loads(payload[pyz_start + pyz_toc_offset :]))
    modules = {}
    for name in ("_dafeiyu_original_main", "ui_console"):
        entry = pyz_toc.get(name)
        if entry is None:
            raise ValueError(f"Required runtime module not found: {name}")
        _typecode, offset, length = entry
        blob = payload[pyz_start + offset : pyz_start + offset + length]
        code = marshal.loads(zlib.decompress(blob))
        if not isinstance(code, types.CodeType):
            raise TypeError(f"Extracted {name} is not a Python code object.")
        modules[name] = code

    output.parent.mkdir(parents=True, exist_ok=True)
    for name, code in modules.items():
        output.with_name(f"{name}.marshal").write_bytes(marshal.dumps(code))
    return modules


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    modules = extract_archive_modules(args.package.resolve(), args.output.resolve())
    for name, code in modules.items():
        path = args.output.with_name(f"{name}.marshal")
        print(f"Extracted {name}: {code.co_filename} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
