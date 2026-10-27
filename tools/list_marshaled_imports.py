from __future__ import annotations

import dis
import marshal
import types
from pathlib import Path


def walk(code: types.CodeType, names: set[str]) -> None:
    for instruction in dis.get_instructions(code):
        if instruction.opname == "IMPORT_NAME":
            names.add(str(instruction.argval))
    for value in code.co_consts:
        if isinstance(value, types.CodeType):
            walk(value, names)


names: set[str] = set()
for path in (
    Path(
        "dist/大肥鱼桌宠AI整合版/_internal/assets/"
        "_dafeiyu_original_main.marshal"
    ),
    Path(
        "dist/大肥鱼桌宠AI整合版/_internal/assets/ui_console.marshal"
    ),
):
    walk(marshal.loads(path.read_bytes()), names)

print("\n".join(sorted(names)))
