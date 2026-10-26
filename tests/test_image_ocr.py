from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chat_pet.image_ocr import (
    ImageTextError,
    extract_image_text,
    validate_image_file,
)


class ImageOcrTests(unittest.TestCase):
    def test_unsupported_or_missing_image_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            text_file = Path(folder) / "note.txt"
            text_file.write_text("not an image", encoding="utf-8")
            with self.assertRaisesRegex(ImageTextError, "只支持"):
                validate_image_file(text_file)
            with self.assertRaisesRegex(ImageTextError, "不存在"):
                validate_image_file(Path(folder) / "missing.png")

    def test_ocr_text_is_returned_without_vision_model(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "screenshot.png"
            image.write_bytes(b"test image placeholder")
            with patch(
                "chat_pet.image_ocr._recognize_blocking",
                return_value="第一行\n第二行",
            ):
                text = extract_image_text(image)
            self.assertEqual(text, "第一行\n第二行")


if __name__ == "__main__":
    unittest.main()
