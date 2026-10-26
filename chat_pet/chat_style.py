from __future__ import annotations

import re


SHORT_REPLY_LIMIT = 100


def split_chat_reply(
    text: str,
    *,
    max_chars: int = SHORT_REPLY_LIMIT,
) -> list[str]:
    clean = text.strip()
    if not clean:
        return []
    if len(clean) > max_chars:
        return [clean]
    pieces = [
        piece.strip()
        for piece in re.split(r"(?:\r?\n)+|[，,]+", clean)
        if piece.strip()
    ]
    if len(pieces) <= 1:
        return [clean]
    return pieces


__all__ = ["SHORT_REPLY_LIMIT", "split_chat_reply"]
