from __future__ import annotations

import unittest

from chat_pet.chat_style import split_chat_reply


class ChatStyleTests(unittest.TestCase):
    def test_short_reply_replaces_commas_with_multiple_messages(self) -> None:
        self.assertEqual(
            split_chat_reply("鱼鱼看到了，这个有点可疑，不过挺有意思。"),
            ["鱼鱼看到了", "这个有点可疑", "不过挺有意思。"],
        )

    def test_long_reply_is_not_split(self) -> None:
        text = "第一段，" + "很长的内容" * 20
        self.assertEqual(split_chat_reply(text), [text])

    def test_existing_line_breaks_are_respected(self) -> None:
        self.assertEqual(
            split_chat_reply("第一句\n第二句"),
            ["第一句", "第二句"],
        )


if __name__ == "__main__":
    unittest.main()
