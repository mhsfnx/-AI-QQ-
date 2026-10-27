from __future__ import annotations

import argparse
import os
from pathlib import Path

import pefile


SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"


def find_dll(name: str, search: list[Path]) -> Path | None:
    lowered = name.lower()
    for folder in search:
        candidate = folder / name
        if candidate.is_file():
            return candidate
    candidate = SYSTEM32 / name
    if candidate.is_file():
        return candidate
    return None


def check_dll(path: Path, search: list[Path], seen: set[Path]) -> list[str]:
    path = path.resolve()
    if path in seen:
        return []
    seen.add(path)
    pe = pefile.PE(str(path), fast_load=False)
    exports: set[str] = set()
    if hasattr(pe, "DIRECTORY_ENTRY_EXPORT"):
        for symbol in pe.DIRECTORY_ENTRY_EXPORT.symbols:
            if symbol.name:
                exports.add(symbol.name.decode("ascii", errors="replace"))

    missing = []
    for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
        dependency = entry.dll.decode("ascii", errors="replace")
        if dependency.lower().startswith(("api-ms-", "ext-ms-")):
            continue
        dependency_path = find_dll(dependency, search)
        if dependency_path is None:
            missing.append(f"{path.name}: missing DLL {dependency}")
            continue
        dependency_pe = pefile.PE(str(dependency_path), fast_load=False)
        dependency_exports = set()
        if hasattr(dependency_pe, "DIRECTORY_ENTRY_EXPORT"):
            for symbol in dependency_pe.DIRECTORY_ENTRY_EXPORT.symbols:
                if symbol.name:
                    dependency_exports.add(
                        symbol.name.decode("ascii", errors="replace")
                    )
        for imported in entry.imports:
            if imported.name:
                name = imported.name.decode("ascii", errors="replace")
                if name not in dependency_exports:
                    missing.append(
                        f"{path.name}: {dependency} is missing export {name}"
                    )
        missing.extend(
            check_dll(
                dependency_path,
                [dependency_path.parent, *search],
                seen,
            )
        )
    return missing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", type=Path)
    parser.add_argument("search", nargs="*", type=Path)
    args = parser.parse_args()
    search = [path.resolve() for path in args.search]
    problems = check_dll(args.target, search, set())
    if problems:
        print("\n".join(dict.fromkeys(problems)))
    else:
        print("No missing named imports were found.")


if __name__ == "__main__":
    main()
