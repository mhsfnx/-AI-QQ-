from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from chat_pet.database import Database, Profile
from chat_pet.memory import (
    apply_profile_patch,
    build_chat_messages,
    heuristic_memories,
    is_key_message,
    parse_memory_json,
)
from chat_pet.service import prepare_user_turn


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "memory.sqlite3")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_profile_conversation_and_message_round_trip(self) -> None:
        profile = Profile(
            display_name="小蓝",
            preferences="咖啡和鲸鱼",
            communication_style="简洁",
        )
        self.database.save_profile(profile)
        self.assertEqual(self.database.get_profile().display_name, "小蓝")

        conversation = self.database.create_conversation()
        self.database.add_message(conversation, "user", "你好")
        self.database.add_message(conversation, "assistant", "你好呀")
        messages = self.database.recent_messages(conversation)
        self.assertEqual([item.content for item in messages], ["你好", "你好呀"])

    def test_message_attachment_path_round_trip(self) -> None:
        conversation = self.database.create_conversation()
        self.database.add_message(
            conversation,
            "assistant",
            "这个结论有点可疑。",
            attachment_path=r"C:\Stickers\疑惑.png",
        )
        message = self.database.recent_messages(conversation)[0]
        self.assertEqual(message.attachment_path, r"C:\Stickers\疑惑.png")

    def test_explicit_memory_is_saved_and_retrieved(self) -> None:
        conversation = self.database.create_conversation()
        prepare_user_turn(
            self.database,
            conversation,
            "请记住：我最喜欢蓝莓，最近的目标是学完 Python。",
        )
        memories = self.database.relevant_memories("我喜欢什么水果", 10)
        self.assertTrue(any("蓝莓" in item.value for item in memories))
        key_messages = self.database.key_messages()
        self.assertEqual(len(key_messages), 1)
        self.assertTrue(key_messages[0].is_key)

    def test_chat_messages_contain_profile_memory_and_recent_history(self) -> None:
        self.database.save_profile(Profile(preferred_address="阿蓝"))
        self.database.upsert_memory("preference", "饮料", "喜欢无糖乌龙茶", 4)
        conversation = self.database.create_conversation()
        self.database.add_message(conversation, "user", "今天喝什么？")
        self.database.add_interaction(conversation, "poke", "你戳了戳大肥鱼")
        profile = self.database.get_profile()
        memories = self.database.relevant_memories("喝什么", 5)
        history = self.database.recent_messages(conversation)
        interactions = self.database.recent_interactions(conversation)
        messages = build_chat_messages(profile, memories, history, interactions)
        self.assertIn("阿蓝", messages[0]["content"])
        self.assertIn("无糖乌龙茶", messages[0]["content"])
        self.assertIn("你戳了戳大肥鱼", messages[0]["content"])
        self.assertEqual(messages[-1]["content"], "今天喝什么？")

    def test_multiple_conversations_are_separate(self) -> None:
        first = self.database.create_conversation()
        second = self.database.create_conversation()
        self.database.add_message(first, "user", "第一段")
        self.database.add_message(second, "user", "第二段")
        self.assertEqual(self.database.recent_messages(first)[0].content, "第一段")
        self.assertEqual(self.database.recent_messages(second)[0].content, "第二段")

    def test_pet_interactions_are_stored_in_conversation(self) -> None:
        conversation = self.database.create_conversation()
        self.database.add_interaction(conversation, "poke", "你戳了戳大肥鱼")
        self.database.add_interaction(
            conversation,
            "drag",
            "把大肥鱼拖到了桌面上的新位置",
        )
        interactions = self.database.recent_interactions(conversation)
        self.assertEqual([item.kind for item in interactions], ["poke", "drag"])
        self.assertIn("拖到了桌面", interactions[1].summary)
        events = self.database.recent_event_memories(
            scope=f"main:{conversation}",
        )
        self.assertEqual([item.kind for item in events], ["poke", "drag"])

    def test_proactive_qq_drafts_round_trip(self) -> None:
        initiative_id = self.database.add_qq_initiative(
            "group_topic",
            "@小林 最近有什么新发现吗？",
            target_title="技术交流",
            member_name="小林",
            context="刚刚聊到 Python",
        )
        items = self.database.recent_qq_initiatives()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, initiative_id)
        self.assertEqual(items[0].event_type, "group_topic")
        self.assertEqual(items[0].member_name, "小林")
        self.assertEqual(items[0].status, "draft")
        self.database.set_qq_initiative_status(initiative_id, "sent")
        self.assertEqual(
            self.database.recent_qq_initiatives()[0].status,
            "sent",
        )

    def test_old_event_memories_are_compacted_with_source_ids(self) -> None:
        old_time = (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).isoformat(timespec="microseconds")
        key_id = self.database.add_event_memory(
            "setting_change",
            "把桌宠大小调整为 80%",
            scope="main:1",
            importance=4,
            occurred_at=old_time,
        )
        routine_id = self.database.add_event_memory(
            "poke",
            "你戳了戳大肥鱼",
            scope="main:1",
            importance=1,
            occurred_at=old_time,
        )
        self.assertEqual(
            self.database.compact_old_event_memories(older_than_days=7),
            1,
        )
        summaries = self.database.list_event_summaries()
        self.assertEqual(len(summaries), 1)
        self.assertEqual(set(summaries[0].event_ids), {key_id, routine_id})
        self.assertIn(f"#{key_id}", summaries[0].summary)
        self.assertIn("低优先级", summaries[0].summary)
        compacted = {
            event.id: event
            for event in self.database.list_event_memories()
        }
        self.assertTrue(compacted[key_id].compacted)
        self.assertTrue(compacted[routine_id].compacted)
        self.assertFalse(self.database.recent_event_memories(scope="main:1"))

    def test_qq_user_profiles_track_private_and_mentioned_users(self) -> None:
        private = self.database.upsert_qq_user_profile(
            "private:好友甲",
            "好友甲",
            "qq:private:好友甲",
            is_group=False,
            mentioned_self=False,
            message="我最近在学做甜点",
        )
        self.assertEqual(private.message_count, 1)
        self.assertIn("近期在做", private.profile_summary)

        mentioned = self.database.upsert_qq_user_profile(
            "group:群甲:小林",
            "小林",
            "qq:group:群甲",
            is_group=True,
            mentioned_self=True,
            message="@大肥鱼 周末一起看展吗？",
        )
        self.assertEqual(mentioned.mention_count, 1)
        self.assertIn("在群里主动 @ 过大肥鱼", mentioned.profile_summary)
        profiles = self.database.recent_qq_user_profiles()
        self.assertEqual(len(profiles), 2)
        self.assertTrue(profiles[0].is_group)
        self.assertIn("最近发言", mentioned.prompt_text())

    def test_old_event_summaries_stay_scoped_to_the_chatting_user(self) -> None:
        old_time = (
            datetime.now(timezone.utc) - timedelta(days=20)
        ).isoformat(timespec="microseconds")
        first_id = self.database.add_event_memory(
            "qq_mention",
            "小林聊到了周末看展",
            scope="qq:group:艺术群",
            related_user_key="group:艺术群:小林",
            importance=4,
            occurred_at=old_time,
        )
        second_id = self.database.add_event_memory(
            "qq_mention",
            "小周聊到了周末看展",
            scope="qq:group:艺术群",
            related_user_key="group:艺术群:小周",
            importance=4,
            occurred_at=old_time,
        )
        self.database.compact_old_event_memories(older_than_days=7)
        summaries = self.database.relevant_event_summaries(
            "周末看展",
            scope="qq:group:艺术群",
            related_user_key="group:艺术群:小林",
        )
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].event_ids, (first_id,))
        self.assertNotIn(second_id, summaries[0].event_ids)

    def test_pet_persona_grows_additively_and_can_be_cleared(self) -> None:
        initial = self.database.get_pet_persona()
        self.assertEqual(initial.revision, 0)
        stored = self.database.apply_pet_persona_learnings(
            [
                {
                    "category": "trait",
                    "value": "开始喜欢观察人类做选择时的犹豫",
                    "confidence": 4,
                },
                {
                    "category": "knowledge",
                    "value": "了解到人类会把惯性当成理由",
                    "confidence": 3,
                },
                {
                    "category": "trait",
                    "value": "开始喜欢观察人类做选择时的犹豫",
                    "confidence": 4,
                },
            ]
        )
        self.assertEqual(stored, 2)
        persona = self.database.get_pet_persona()
        self.assertEqual(persona.revision, 2)
        self.assertIn("人类做选择时的犹豫", persona.traits)
        self.assertIn("惯性当成理由", persona.knowledge)
        self.assertEqual(
            self.database.apply_pet_persona_learnings(
                [
                    {
                        "category": "trait",
                        "value": "开始喜欢观察人类做选择时的犹豫",
                    }
                ]
            ),
            0,
        )
        self.assertEqual(len(self.database.list_pet_persona_learnings()), 2)
        self.database.clear_pet_persona_learnings()
        self.assertEqual(self.database.get_pet_persona().revision, 0)
        self.assertFalse(self.database.list_pet_persona_learnings())


class MemoryParsingTests(unittest.TestCase):
    def test_fenced_json_is_parsed(self) -> None:
        parsed = parse_memory_json(
            '好的：\n```json\n{"profile_patch":{"goals":"学习"},"memories":[]}\n```'
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["profile_patch"]["goals"], "学习")

    def test_heuristics_collect_explicit_preferences(self) -> None:
        facts = heuristic_memories("我叫小林。我喜欢下雨天，但我不喜欢嘈杂的地方。")
        values = [item[2] for item in facts]
        self.assertIn("小林", values)
        self.assertTrue(any("下雨天" in item for item in values))
        self.assertTrue(any("嘈杂的地方" in item for item in values))

    def test_key_message_detection(self) -> None:
        self.assertTrue(is_key_message("请记住我下周三考试"))
        self.assertFalse(is_key_message("今天天气不错"))

    def test_profile_patch_keeps_manual_preferences(self) -> None:
        original = Profile(preferences="手动填写：猫")
        updated = apply_profile_patch(original, {"preferences": "模型抽取：狗"})
        self.assertIn("猫", updated.preferences)
        self.assertIn("狗", updated.preferences)


if __name__ == "__main__":
    unittest.main()
