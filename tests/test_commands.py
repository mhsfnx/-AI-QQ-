from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chat_pet.commands import execute_chat_command
from chat_pet.qq_bridge import QQSettings, QQSettingsStore


class _FakePet:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.cfg = {"size": 0.7}

    def set_size(self, value: float) -> None:
        self.calls.append(("size", value))

    def set_opacity(self, value: float) -> None:
        self.calls.append(("opacity", value))

    def set_volume(self, value: float) -> None:
        self.calls.append(("volume", value))

    def set_locked(self, value: bool) -> None:
        self.calls.append(("locked", value))

    def set_topmost(self, value: bool) -> None:
        self.calls.append(("topmost", value))


class _FakeController:
    def __init__(self, folder: str):
        self.pet = _FakePet()
        self.qq_settings_store = QQSettingsStore(Path(folder) / "qq.json")
        self.qq_dialog = None
        self.chat_dialog = None

    def apply_qq_settings(self, _settings: QQSettings) -> None:
        pass

    def open_qq(self, _parent: object = None) -> None:
        pass

    def open_memory(self, _parent: object = None) -> None:
        pass

    def open_settings(self, _parent: object = None) -> None:
        pass


class CommandTests(unittest.TestCase):
    def test_pet_size_and_lock_commands(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            controller = _FakeController(folder)
            result = execute_chat_command("把桌宠大小调到 60%", controller)
            self.assertTrue(result.handled)
            self.assertIn(("size", 0.6), controller.pet.calls)
            execute_chat_command("锁定位置", controller)
            self.assertIn(("locked", True), controller.pet.calls)

    def test_relative_size_commands(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            controller = _FakeController(folder)
            result = execute_chat_command("把自己调整大一号", controller)
            self.assertTrue(result.handled)
            self.assertIn(("size", 0.75), controller.pet.calls)
            execute_chat_command("你把自己调到最大", controller)
            self.assertIn(("size", 1.5), controller.pet.calls)

    def test_qq_auto_send_can_be_disabled_by_chat_command(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            controller = _FakeController(folder)
            controller.qq_settings_store.save(QQSettings(auto_send=True))
            result = execute_chat_command("关闭自动回复", controller)
            self.assertTrue(result.handled)
            self.assertFalse(controller.qq_settings_store.load().auto_send)

    def test_nickname_command_updates_qq_mention_names(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            controller = _FakeController(folder)
            result = execute_chat_command("我的 QQ 昵称叫 小鱼", controller)
            self.assertTrue(result.handled)
            self.assertIn("小鱼", controller.qq_settings_store.load().own_names)

    def test_open_qq_reply_feature_command(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            controller = _FakeController(folder)
            result = execute_chat_command("打开QQ回复功能", controller)
            self.assertTrue(result.handled)
            settings = controller.qq_settings_store.load()
            self.assertTrue(settings.enabled)
            self.assertTrue(settings.auto_send)

    def test_normal_chat_is_not_treated_as_command(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            controller = _FakeController(folder)
            result = execute_chat_command("今天心情不太好", controller)
            self.assertFalse(result.handled)

    def test_random_event_settings_can_be_changed_from_chat(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            controller = _FakeController(folder)
            execute_chat_command("打开定时随机主动事件", controller)
            execute_chat_command("随机事件每 45 分钟触发一次", controller)
            execute_chat_command("随机事件触发概率改成 80%", controller)
            execute_chat_command("群聊随机话题概率改成 20%", controller)
            execute_chat_command("关闭好友随机问候事件", controller)
            settings = controller.qq_settings_store.load()
            self.assertTrue(settings.random_events_enabled)
            self.assertEqual(settings.random_event_interval_minutes, 45)
            self.assertEqual(settings.random_event_trigger_percent, 80)
            self.assertEqual(settings.random_group_percent, 20)
            self.assertFalse(settings.random_friend_enabled)


if __name__ == "__main__":
    unittest.main()
