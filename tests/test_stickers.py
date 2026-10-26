from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chat_pet.paths import app_base_dir, resolve_app_path
from chat_pet.stickers import Sticker, StickerLibrary, StickerSelector


class StickerTests(unittest.TestCase):
    def test_project_sticker_folder_uses_relative_packaged_path(self) -> None:
        library = StickerLibrary()
        self.assertEqual(library.folder.resolve(), (app_base_dir() / "stickers").resolve())
        sticker = Sticker(
            path=app_base_dir() / "stickers" / "开心.png",
            label="开心",
            tags=("开心",),
        )
        self.assertEqual(library.attachment_key(sticker), "stickers/开心.png")
        self.assertEqual(
            resolve_app_path("stickers/开心.png"),
            (app_base_dir() / "stickers" / "开心.png").resolve(),
        )

    def test_filename_meanings_match_only_relevant_replies(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "无语.png").touch()
            (root / "开心.gif").touch()
            (root / "疑惑.png").touch()
            library = StickerLibrary(root)
            self.assertEqual(
                [item.label for item in library.matches("这个结论有点离谱")],
                ["无语"],
            )
            self.assertEqual(
                [item.label for item in library.matches("我挺好奇的")],
                [],
            )
            self.assertEqual(library.matches("我才不无语"), [])
            self.assertTrue((root / "命名说明.txt").exists())

    def test_selector_respects_time_and_reply_cooldowns(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "无语_白眼.png").touch()
            (root / "无语_沉默.gif").touch()
            now = [0.0]
            selector = StickerSelector(
                StickerLibrary(root),
                cooldown_seconds=300,
                minimum_replies=3,
                clock=lambda: now[0],
            )
            first = selector.consider("这确实有点无语")
            self.assertIsNotNone(first)
            self.assertIsNone(selector.consider("还是无语"))
            now[0] = 301
            self.assertIsNone(selector.consider("依然无语"))
            self.assertIsNone(selector.consider("继续无语"))
            second = selector.consider("真的无语")
            self.assertIsNotNone(second)
            self.assertNotEqual(first.path, second.path)


if __name__ == "__main__":
    unittest.main()
