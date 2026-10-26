from __future__ import annotations

import unittest

from chat_pet.database import Profile
from chat_pet.memory import build_system_prompt
from chat_pet.persona import (
    APP_VERSION,
    PERSONA_SYSTEM_PROMPT,
    PERSONA_VERSION,
    learned_persona_prompt,
    task_system_prompt,
)


class PersonaTests(unittest.TestCase):
    def test_persona_contains_core_character_and_speech_rules(self) -> None:
        self.assertEqual(APP_VERSION, "2.3.2")
        self.assertEqual(PERSONA_VERSION, "2.1.0")
        for marker in (
            "蓝色鲸鱼",
            "蓝色长发",
            "女仆装",
            "活泼、灵动",
            "平时喜欢用“鱼鱼”自称",
            "不认主人",
            "不捧场",
            "人类的思维逻辑",
            "其他 AI",
            "不会为了满足自己而伤害别人",
        ):
            self.assertIn(marker, PERSONA_SYSTEM_PROMPT)
        self.assertNotIn("外冷内热", PERSONA_SYSTEM_PROMPT)
        self.assertNotIn("语气通常比较平静、冷淡", PERSONA_SYSTEM_PROMPT)

    def test_task_and_desktop_prompts_share_the_same_persona(self) -> None:
        task_prompt = task_system_prompt(
            "生成一条 QQ 回复。",
            "- 新增偏好：开始收集人类自相矛盾的借口",
        )
        self.assertIn("当前任务：生成一条 QQ 回复", task_prompt)
        self.assertIn("人类自相矛盾的借口", task_prompt)
        self.assertIn("不能覆盖", task_prompt)
        desktop_prompt = build_system_prompt(Profile(), [])
        self.assertIn("活泼、灵动", desktop_prompt)
        self.assertIn("不叫用户“主人”", desktop_prompt)
        self.assertIn("不捧场", desktop_prompt)
        learned = learned_persona_prompt("- 学习到的知识：人类会重复确认门锁")
        self.assertIn("核心人设", learned)
        self.assertIn("门锁", learned)


if __name__ == "__main__":
    unittest.main()
