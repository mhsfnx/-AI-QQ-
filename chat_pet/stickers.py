from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .paths import app_base_dir, stickers_dir


SUPPORTED_STICKER_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
}

EMOTION_ALIAS_GROUPS = (
    ("开心", "高兴", "愉快", "笑意", "有趣"),
    ("疑惑", "困惑", "问号", "奇怪", "不解"),
    ("无语", "离谱", "沉默", "难评", "不好说"),
    ("生气", "恼火", "不快", "不满", "哼哼"),
    ("好奇", "想知道", "有意思", "看看", "为什么"),
    ("关心", "担心", "小心", "注意", "没事吧"),
    ("得意", "当然", "不错吧", "小意思", "骄傲"),
    ("难过", "失落", "遗憾", "低落", "唉"),
    ("惊讶", "震惊", "意外", "没想到", "真的假的"),
)


@dataclass(frozen=True, slots=True)
class Sticker:
    path: Path
    label: str
    tags: tuple[str, ...]


def _normalize(text: str) -> str:
    return re.sub(r"[\s_\-—,，。.!！?？:：;；()（）\[\]【】]+", "", text).casefold()


def _tags_for_stem(stem: str) -> tuple[str, ...]:
    normalized = _normalize(stem)
    parts = [
        _normalize(part)
        for part in re.split(r"[\s_\-—,，+]+", stem)
        if _normalize(part)
    ]
    tags = set(parts)
    if normalized:
        tags.add(normalized)
    for aliases in EMOTION_ALIAS_GROUPS:
        if any(alias.casefold() in normalized for alias in aliases):
            tags.update(alias.casefold() for alias in aliases)
    return tuple(sorted(tags, key=len, reverse=True))


class StickerLibrary:
    def __init__(self, folder: Path | None = None):
        self.folder = Path(folder or stickers_dir())
        self.ensure_folder()

    def ensure_folder(self) -> Path:
        self.folder.mkdir(parents=True, exist_ok=True)
        readme = self.folder / "命名说明.txt"
        if not readme.exists():
            readme.write_text(
                "把表情图片放在这个文件夹里，文件名就是表情含义。\n"
                "支持 PNG、JPG、GIF、WEBP、BMP。\n"
                "例如：开心.png、疑惑.gif、无语_翻白眼.png。\n"
                "大肥鱼只有在回复中出现对应含义，并且冷却时间已过时才会发送。\n",
                encoding="utf-8",
            )
        return self.folder

    def stickers(self) -> list[Sticker]:
        items = []
        try:
            paths = sorted(self.folder.iterdir(), key=lambda path: path.name)
        except OSError:
            return []
        for path in paths:
            if (
                not path.is_file()
                or path.suffix.casefold() not in SUPPORTED_STICKER_SUFFIXES
            ):
                continue
            tags = _tags_for_stem(path.stem)
            if tags:
                items.append(Sticker(path=path, label=path.stem, tags=tags))
        return items

    def matches(self, reply: str) -> list[Sticker]:
        normalized_reply = _normalize(reply)
        scored: list[tuple[int, Sticker]] = []
        for sticker in self.stickers():
            score = 0
            for tag in sticker.tags:
                if not tag or tag not in normalized_reply:
                    continue
                if any(f"{negation}{tag}" in normalized_reply for negation in ("不", "没", "别")):
                    continue
                score = max(score, len(tag))
            if score:
                scored.append((score, sticker))
        scored.sort(key=lambda item: (item[0], item[1].label), reverse=True)
        return [sticker for _score, sticker in scored]

    def attachment_key(self, sticker: Sticker) -> str:
        try:
            return (
                sticker.path.resolve()
                .relative_to(app_base_dir())
                .as_posix()
            )
        except ValueError:
            return str(sticker.path)


class StickerSelector:
    def __init__(
        self,
        library: StickerLibrary,
        *,
        cooldown_seconds: int = 300,
        minimum_replies: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.library = library
        self.cooldown_seconds = max(0, int(cooldown_seconds))
        self.minimum_replies = max(1, int(minimum_replies))
        self.clock = clock
        self._last_sent_at: float | None = None
        self._last_reply_number = 0
        self._reply_number = 0
        self._last_path: Path | None = None

    def consider(self, reply: str) -> Sticker | None:
        self._reply_number += 1
        candidates = self.library.matches(reply)
        if not candidates:
            return None
        now = self.clock()
        if (
            self._last_sent_at is not None
            and now - self._last_sent_at < self.cooldown_seconds
        ):
            return None
        if (
            self._last_sent_at is not None
            and self._reply_number - self._last_reply_number
            <= self.minimum_replies
        ):
            return None
        selected = next(
            (item for item in candidates if item.path != self._last_path),
            candidates[0],
        )
        self._last_sent_at = now
        self._last_reply_number = self._reply_number
        self._last_path = selected.path
        return selected


__all__ = [
    "EMOTION_ALIAS_GROUPS",
    "SUPPORTED_STICKER_SUFFIXES",
    "Sticker",
    "StickerLibrary",
    "StickerSelector",
]
