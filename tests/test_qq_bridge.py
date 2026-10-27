from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chat_pet.config import AppConfig, ConfigStore
from chat_pet.database import Database
from chat_pet.qq_bridge import (
    _collect_texts,
    _parse_message_lines,
    choose_random_event,
    QQMonitorService,
    QQMessageLine,
    QQSessionPreview,
    QQSettings,
    QQSettingsStore,
    QQSnapshot,
)


class _FakeAutomation:
    def __init__(self, snapshots: list[QQSnapshot]):
        self.snapshots = snapshots

    def capture(self, _settings: QQSettings) -> QQSnapshot:
        return self.snapshots.pop(0)


class _FakeMultiChatAutomation:
    def __init__(self, batches: list[list[QQSnapshot]]):
        self.batches = batches

    def capture_many(
        self,
        _settings: QQSettings,
        _known_signatures: dict[str, str],
    ) -> list[QQSnapshot]:
        return self.batches.pop(0)


class _FakeSendingMultiChatAutomation(_FakeMultiChatAutomation):
    def __init__(self, batches: list[list[QQSnapshot]]):
        super().__init__(batches)
        self.sent: list[tuple[str, str]] = []

    def send_reply(self, chat_title: str, text: str) -> bool:
        self.sent.append((chat_title, text))
        return True


class _FixedRng:
    def __init__(self, *values: float):
        self.values = list(values)

    def uniform(self, _minimum: float, _maximum: float) -> float:
        return self.values.pop(0)


class _FakeRandomAutomation:
    def __init__(self, target: QQSnapshot):
        self.target = target
        self.sent: list[tuple[str, str]] = []

    def choose_random_conversation(
        self,
        _settings: QQSettings,
        *,
        want_group: bool,
    ) -> QQSnapshot | None:
        return self.target if self.target.is_group == want_group else None

    def send_reply(self, chat_title: str, text: str) -> bool:
        self.sent.append((chat_title, text))
        return True


class _FakeControl:
    def __init__(
        self,
        name: str = "",
        type_name: str = "GroupControl",
        children: list["_FakeControl"] | None = None,
    ):
        self.Name = name
        self.ControlTypeName = type_name
        self._children = children or []

    def GetChildren(self) -> list["_FakeControl"]:
        return self._children


def snapshot(
    title: str,
    texts: list[str],
    *,
    group: bool,
    message_items: list[QQMessageLine] | None = None,
) -> QQSnapshot:
    return QQSnapshot(
        chat_title=title,
        is_group=group,
        text_items=texts,
        session_items={
            title: QQSessionPreview(
                title=title,
                signature="|".join(texts),
                preview=texts[-1] if texts else "",
            )
        },
        message_items=message_items or [],
    )


class QQMonitorTests(unittest.TestCase):
    def test_group_and_private_message_lines_keep_speakers(self) -> None:
        group_lines, _ = _parse_message_lines(
            [
                ("09:30", 20, 70),
                ("小王", 20, 80),
                ("你好", 20, 120),
                ("阿蓝", 20, 80),
                ("@大肥鱼 在吗", 20, 150),
                ("我在", 280, 350),
            ],
            chat_title="测试群",
            is_group=True,
            member_names=["小王", "阿蓝"],
            list_left=0,
            list_right=400,
        )
        self.assertEqual(
            [(line.sender, line.content) for line in group_lines],
            [("小王", "你好"), ("阿蓝", "@大肥鱼 在吗"), ("我", "我在")],
        )

        private_lines, _ = _parse_message_lines(
            [("对方消息", 20, 130), ("我的消息", 280, 390)],
            chat_title="好友甲",
            is_group=False,
            member_names=[],
            list_left=0,
            list_right=400,
        )
        self.assertEqual(
            [(line.sender, line.content) for line in private_lines],
            [("好友甲", "对方消息"), ("我", "我的消息")],
        )

    def test_deep_custom_text_nodes_are_extracted(self) -> None:
        control: _FakeControl = _FakeControl(
            type_name="CustomControl",
            name="实际聊天正文",
        )
        for _ in range(11):
            control = _FakeControl(children=[control])
        self.assertIn("实际聊天正文", _collect_texts(control))

    def _service(
        self,
        folder: str,
        automation: _FakeAutomation,
        *,
        own_names: list[str] | None = None,
        auto_send: bool = False,
    ) -> QQMonitorService:
        root = Path(folder)
        settings_store = QQSettingsStore(root / "qq.json")
        settings_store.save(
            QQSettings(
                enabled=True,
                poll_seconds=3,
                own_names=own_names or [],
                auto_send=auto_send,
                dry_run=not auto_send,
            )
        )
        chat_store = ConfigStore(root / "settings.json")
        chat_store.save(AppConfig(api_key="test-key"))
        return QQMonitorService(
            Database(root / "memory.sqlite3"),
            settings_store,
            chat_store,
            automation=automation,
        )

    def test_private_change_creates_sqlite_draft(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": ""},
        ), patch("chat_pet.qq_bridge.request_chat", return_value="晚上好呀。"):
            service = self._service(
                folder,
                _FakeAutomation(
                    [
                        snapshot("好友", ["09:00", "旧消息"], group=False),
                        snapshot("好友", ["09:01", "新消息"], group=False),
                    ]
                ),
            )
            first = service.scan_once()
            second = service.scan_once()
            self.assertEqual(first.drafts_created, 0)
            self.assertEqual(second.drafts_created, 1)
            drafts = service.database.recent_qq_drafts()
            self.assertEqual(drafts[0].chat_title, "好友")
            self.assertEqual(drafts[0].content, "晚上好呀。")
            self.assertEqual(drafts[0].status, "draft")
            profiles = service.database.recent_qq_user_profiles()
            self.assertEqual(profiles[0].display_name, "好友")
            self.assertFalse(profiles[0].is_group)

    def test_forced_scan_can_draft_current_message(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": ""},
        ), patch("chat_pet.qq_bridge.request_chat", return_value="手动扫描草稿"):
            service = self._service(
                folder,
                _FakeAutomation(
                    [
                        snapshot("好友", ["09:01", "现有消息"], group=False),
                        snapshot("好友", ["09:01", "现有消息"], group=False),
                    ]
                ),
            )
            service.scan_once()
            result = service.scan_once(force=True)
            self.assertEqual(result.drafts_created, 1)
            self.assertEqual(
                service.database.recent_qq_drafts()[0].content,
                "手动扫描草稿",
            )

    def test_session_preview_detects_when_message_tree_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": ""},
        ), patch("chat_pet.qq_bridge.request_chat", return_value="预览草稿"):
            first = snapshot("好友", ["旧正文"], group=False)
            second = snapshot("好友", ["旧正文"], group=False)
            second.session_items["好友"].preview = "新的私聊正文"
            service = self._service(
                folder,
                _FakeAutomation([first, second]),
            )
            service.scan_once()
            result = service.scan_once()
            self.assertEqual(result.drafts_created, 1)
            self.assertEqual(
                service.database.recent_qq_drafts()[0].incoming,
                "新的私聊正文",
            )

    def test_group_without_mention_never_creates_draft(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": ""},
        ):
            service = self._service(
                folder,
                _FakeAutomation(
                    [
                        snapshot("群", ["09:00", "旧消息"], group=True),
                        snapshot("群", ["09:01", "普通群消息"], group=True),
                    ]
                ),
                own_names=["大肥鱼"],
            )
            service.scan_once()
            second = service.scan_once()
            self.assertEqual(second.stored_messages, 1)
            self.assertEqual(second.drafts_created, 0)
            self.assertFalse(service.database.recent_qq_drafts())

    def test_group_mention_creates_draft(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": ""},
        ):
            first = snapshot("群", ["09:00", "旧消息"], group=True)
            second = snapshot(
                "群",
                ["09:01", "小林", "@大肥鱼 你好"],
                group=True,
            )
            second.recent_members = ["小林"]
            prompts: list[str] = []

            def fake_request(messages, _config, **_kwargs):
                prompts.append(messages[1]["content"])
                return "我在呢。"

            service = self._service(
                folder,
                _FakeAutomation([first, second]),
                own_names=["大肥鱼"],
            )
            with patch(
                "chat_pet.qq_bridge.request_chat",
                side_effect=fake_request,
            ):
                service.scan_once()
                result = service.scan_once()
            self.assertEqual(result.drafts_created, 1)
            self.assertEqual(service.database.recent_qq_drafts()[0].content, "我在呢。")
            profile = service.database.recent_qq_user_profiles()[0]
            self.assertEqual(profile.display_name, "小林")
            self.assertEqual(profile.mention_count, 1)
            self.assertIn("当前聊天对象画像", prompts[0])
            self.assertIn("小林", prompts[0])

    def test_multi_speaker_group_context_keeps_labels_and_splits_reply(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": ""},
        ):
            first = snapshot(
                "讨论群",
                ["旧消息"],
                group=True,
                message_items=[QQMessageLine("小王", "旧消息")],
            )
            second = snapshot(
                "讨论群",
                ["普通消息", "@大肥鱼 你怎么看"],
                group=True,
                message_items=[
                    QQMessageLine("小王", "普通消息"),
                    QQMessageLine("阿蓝", "@大肥鱼 你怎么看"),
                ],
            )
            prompts: list[str] = []

            def fake_request(messages, _config, **_kwargs):
                prompts.append(messages[1]["content"])
                return "鱼鱼看到了，这个有点可疑，不过挺有意思。"

            service = self._service(
                folder,
                _FakeAutomation([first, second]),
                own_names=["大肥鱼"],
            )
            with patch(
                "chat_pet.qq_bridge.request_chat",
                side_effect=fake_request,
            ):
                service.scan_once()
                result = service.scan_once()
            self.assertEqual(result.stored_messages, 2)
            self.assertEqual(
                result.draft_texts,
                ["鱼鱼看到了", "这个有点可疑", "不过挺有意思。"],
            )
            stored = service.database.recent_qq_messages(
                service.database.ensure_qq_conversation(
                    second.chat_key,
                    second.chat_title,
                    True,
                ).id,
                limit=10,
            )
            self.assertEqual(
                [(item.sender, item.content) for item in stored],
                [("小王", "普通消息"), ("阿蓝", "@大肥鱼 你怎么看")],
            )
            self.assertIn("小王: 普通消息", prompts[0])
            self.assertIn("阿蓝: @大肥鱼 你怎么看", prompts[0])
            self.assertIn("昵称只用于分辨", prompts[0])
            self.assertIn("不要根据、分析或评价任何昵称", prompts[0])
            self.assertEqual(
                service.database.recent_qq_drafts()[0].content,
                "鱼鱼看到了\n这个有点可疑\n不过挺有意思。",
            )
            profile = service.database.recent_qq_user_profiles()[0]
            self.assertEqual(profile.display_name, "阿蓝")
            self.assertNotIn("阿蓝", profile.profile_summary)

    def test_group_mention_is_detected_from_session_preview(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": ""},
        ), patch("chat_pet.qq_bridge.request_chat", return_value="收到啦。"):
            first = snapshot("群", ["旧正文"], group=True)
            second = snapshot("群", ["旧正文"], group=True)
            second.session_items["群"].preview = "@大肥鱼 在吗"
            service = self._service(
                folder,
                _FakeAutomation([first, second]),
                own_names=["大肥鱼"],
            )
            service.scan_once()
            result = service.scan_once()
            self.assertEqual(result.drafts_created, 1)

    def test_auto_send_is_not_inherited_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "qq.json"
            first_store = QQSettingsStore(path)
            first_store.save(QQSettings(auto_send=True))
            self.assertTrue(first_store.load().auto_send)
            self.assertFalse(QQSettingsStore(path).load().auto_send)

    def test_napcat_token_is_encrypted_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "qq.json"
            store = QQSettingsStore(path)
            store.save(
                QQSettings(
                    napcat_url="ws://127.0.0.1:3001",
                    napcat_token="local-secret-token",
                )
            )
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("local-secret-token", raw)
            self.assertEqual(store.load().napcat_token, "local-secret-token")

    def test_random_event_defaults_and_weight_switches(self) -> None:
        settings = QQSettings(
            enabled=True,
            random_events_enabled=True,
        )
        self.assertEqual(
            (
                settings.random_event_interval_minutes,
                settings.random_event_trigger_percent,
                settings.random_no_action_percent,
                settings.random_group_percent,
                settings.random_friend_percent,
                settings.random_qzone_percent,
            ),
            (30, 100, 55, 30, 10, 5),
        )
        self.assertEqual(
            choose_random_event(settings, _FixedRng(99, 54.9)),
            "no_action",
        )
        self.assertEqual(
            choose_random_event(settings, _FixedRng(99, 70)),
            "group_topic",
        )
        self.assertEqual(
            choose_random_event(settings, _FixedRng(99, 87)),
            "friend_greeting",
        )
        self.assertEqual(
            choose_random_event(settings, _FixedRng(99, 98)),
            "qzone_post",
        )
        settings.random_group_enabled = False
        self.assertEqual(
            choose_random_event(settings, _FixedRng(99, 60)),
            "friend_greeting",
        )

    def test_group_random_event_uses_recent_member_and_creates_draft(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": ""},
        ):
            target = snapshot(
                "摸鱼群",
                ["阿蓝", "最近新番不错", "小王", "确实"],
                group=True,
            )
            target.recent_members = ["小王", "阿蓝"]
            service = self._service(
                folder,
                _FakeRandomAutomation(target),
            )
            settings = service.settings_store.load()
            settings.random_events_enabled = True
            service.settings_store.save(settings)
            prompts: list[str] = []

            def fake_request(messages, _config, **_kwargs):
                prompts.append(messages[1]["content"])
                return "@阿蓝 刚看到你们聊到新番，最近有哪部值得追吗？"

            with patch("chat_pet.qq_bridge.request_chat", side_effect=fake_request):
                result = service.run_random_event(event="group_topic")
            self.assertEqual(result.status, "draft")
            self.assertEqual(result.member_name, "阿蓝")
            self.assertNotIn("@", result.content)
            self.assertIn("不要 @", prompts[0])
            self.assertIn("不要基于任何群成员的昵称或头像", prompts[0])
            self.assertNotIn("最近活跃成员是", prompts[0])
            initiatives = service.database.recent_qq_initiatives()
            self.assertEqual(initiatives[0].event_type, "group_topic")
            self.assertEqual(initiatives[0].target_title, "摸鱼群")
            self.assertIn(
                "发起话题",
                service.database.recent_interactions(
                    service.database.latest_conversation(),
                )[-1].summary,
            )

    def test_friend_event_continues_recent_conversation_context(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": ""},
        ):
            target = snapshot("好友甲", ["刚聊完"], group=False)
            service = self._service(
                folder,
                _FakeRandomAutomation(target),
            )
            settings = service.settings_store.load()
            settings.random_events_enabled = True
            service.settings_store.save(settings)
            conversation = service.database.ensure_qq_conversation(
                target.chat_key,
                target.chat_title,
                False,
            )
            service.database.add_qq_message(
                conversation.id,
                "好友甲",
                "刚刚聊到周末去看展",
                is_mentioned=False,
                fingerprint="recent-friend-message",
            )
            prompts: list[str] = []

            def fake_request(messages, _config, **_kwargs):
                prompts.append(messages[1]["content"])
                return "那周末那场展，你定好时间了吗？"

            with patch("chat_pet.qq_bridge.request_chat", side_effect=fake_request):
                result = service.run_random_event(event="friend_greeting")
            self.assertEqual(result.status, "draft")
            self.assertIn("延续最近话题", prompts[0])
            self.assertIn("继续和", result.summary)

    def test_qzone_event_is_always_draft_only(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": ""},
        ), patch(
            "chat_pet.qq_bridge.request_chat",
            return_value="今天也在桌面上认真摸鱼。",
        ):
            automation = _FakeRandomAutomation(
                snapshot("不需要读取", ["占位"], group=False)
            )
            service = self._service(folder, automation)
            settings = service.settings_store.load()
            settings.random_events_enabled = True
            settings.auto_send = True
            service.settings_store.save(settings)

            result = service.run_random_event(event="qzone_post")
            self.assertEqual(result.status, "draft-only")
            self.assertEqual(automation.sent, [])
            self.assertEqual(
                service.database.recent_qq_initiatives()[0].status,
                "draft-only",
            )

    def test_group_event_sends_only_when_auto_send_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": ""},
        ), patch(
            "chat_pet.qq_bridge.request_chat",
            return_value="今天群里这么热闹，在聊什么？",
        ):
            target = snapshot("测试群", ["小王", "在聊游戏"], group=True)
            target.recent_members = ["小王"]
            automation = _FakeRandomAutomation(target)
            service = self._service(folder, automation)
            settings = service.settings_store.load()
            settings.random_events_enabled = True
            settings.auto_send = True
            service.settings_store.save(settings)

            result = service.run_random_event(event="group_topic")
            self.assertEqual(result.status, "sent")
            self.assertEqual(
                automation.sent,
                [("测试群", "今天群里这么热闹，在聊什么？")],
            )

    def test_changed_inactive_chat_is_switched_and_drafted(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": ""},
        ), patch("chat_pet.qq_bridge.request_chat", return_value="后台私聊草稿"):
            current1 = snapshot("当前好友", ["旧正文"], group=False)
            current1.session_items["另一个好友"] = QQSessionPreview(
                title="另一个好友",
                signature="old",
                preview="旧后台消息",
            )
            current2 = snapshot("当前好友", ["旧正文"], group=False)
            current2.session_items["另一个好友"] = QQSessionPreview(
                title="另一个好友",
                signature="changed",
                preview="后台新消息",
            )
            inactive = snapshot("另一个好友", ["后台新消息"], group=False)
            service = self._service(
                folder,
                _FakeMultiChatAutomation(
                    [
                        [current1],
                        [current2, inactive],
                    ]
                ),
            )
            service.scan_once()
            second = service.scan_once()
            self.assertEqual(second.drafts_created, 1)
            self.assertEqual(
                service.database.recent_qq_drafts()[0].content,
                "后台私聊草稿",
            )

    def test_short_incoming_reply_is_sent_as_multiple_messages(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": ""},
        ), patch(
            "chat_pet.qq_bridge.request_chat",
            return_value="收到，马上看看。",
        ):
            current1 = snapshot("当前好友", ["旧正文"], group=False)
            current1.session_items["另一个好友"] = QQSessionPreview(
                title="另一个好友",
                signature="old",
                preview="旧消息",
            )
            current2 = snapshot("当前好友", ["旧正文"], group=False)
            current2.session_items["另一个好友"] = QQSessionPreview(
                title="另一个好友",
                signature="changed",
                preview="后台新消息",
            )
            inactive = snapshot("另一个好友", ["后台新消息"], group=False)
            inactive.last_message_incoming = True
            automation = _FakeSendingMultiChatAutomation(
                [
                    [current1],
                    [current2, inactive],
                ]
            )
            service = self._service(
                folder,
                automation,
                auto_send=True,
            )
            service.scan_once()
            second = service.scan_once()
            self.assertEqual(second.sent_count, 2)
            self.assertEqual(
                automation.sent,
                [("另一个好友", "收到"), ("另一个好友", "马上看看。")],
            )
            self.assertEqual(service.database.recent_qq_drafts()[0].status, "sent")


if __name__ == "__main__":
    unittest.main()
