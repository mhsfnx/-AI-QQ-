from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from chat_pet.config import AppConfig, ConfigStore
from chat_pet.database import Database
from chat_pet.napcat_bridge import NapCatMonitorService, NapCatRuntime
from chat_pet.qq_bridge import QQSettings, QQSettingsStore


class _FakeOneBotClient:
    url = "ws://127.0.0.1:3001"
    token = ""

    def __init__(self, events: list[dict] | None = None):
        self.connected = True
        self.events = list(events or [])
        self.actions: list[tuple[str, dict]] = []

    def start(self) -> None:
        self.connected = True

    def stop(self) -> None:
        self.connected = False

    def poll_events(self, _limit: int = 100) -> list[dict]:
        events = list(self.events)
        self.events.clear()
        return events

    def send_action(self, action: str, params: dict | None = None):
        params = params or {}
        self.actions.append((action, params))
        if action == "get_group_list":
            return [{"group_id": 456, "group_name": "测试群"}]
        if action == "get_friend_list":
            return [{"user_id": 123, "nickname": "好友甲"}]
        if action in {"get_group_msg_history", "get_friend_msg_history"}:
            return {
                "messages": [
                    {
                        "self_id": 999,
                        "time": int(time.time()),
                        "sender": {"nickname": "好友甲", "user_id": 123},
                        "message": [
                            {"type": "text", "data": {"text": "周末去看展吗"}}
                        ],
                    }
                ]
            }
        return {"message_id": 1000 + len(self.actions)}


def _private_event(text: str = "你好") -> dict:
    return {
        "post_type": "message",
        "message_type": "private",
        "self_id": 999,
        "user_id": 123,
        "message_id": 10,
        "time": int(time.time()),
        "sender": {"nickname": "好友甲", "user_id": 123},
        "message": [{"type": "text", "data": {"text": text}}],
    }


def _group_event(text: str = "你好", *, mention: bool = True) -> dict:
    message = [
        {"type": "at", "data": {"qq": "999", "name": "大肥鱼"}}
    ] if mention else []
    message.append({"type": "text", "data": {"text": text}})
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": 999,
        "group_id": 456,
        "user_id": 321,
        "message_id": 11,
        "time": int(time.time()),
        "sender": {"card": "阿蓝", "nickname": "blue", "user_id": 321},
        "message": message,
    }


class NapCatMonitorTests(unittest.TestCase):
    def _service(
        self,
        folder: str,
        client: _FakeOneBotClient,
        *,
        auto_send: bool = False,
    ) -> NapCatMonitorService:
        root = Path(folder)
        settings_store = QQSettingsStore(root / "qq.json")
        settings_store.save(
            QQSettings(
                enabled=True,
                auto_send=auto_send,
                dry_run=not auto_send,
            )
        )
        chat_store = ConfigStore(root / "settings.json")
        chat_store.save(AppConfig(api_key="test-key"))
        return NapCatMonitorService(
            Database(root / "memory.sqlite3"),
            settings_store,
            chat_store,
            client=client,
            runtime=NapCatRuntime(root / "napcat"),
        )

    def test_private_message_creates_labelled_multi_message_draft(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": ""},
        ), patch(
            "chat_pet.napcat_bridge.request_chat",
            return_value="鱼鱼看到了，这个消息有点可疑。",
        ) as request:
            client = _FakeOneBotClient([_private_event()])
            service = self._service(folder, client)
            result = service.scan_once()
            self.assertEqual(result.stored_messages, 1)
            self.assertEqual(result.draft_texts, ["鱼鱼看到了", "这个消息有点可疑。"])
            messages = service.database.recent_qq_messages(
                service.database.ensure_qq_conversation(
                    "napcat:private:123",
                    "好友甲",
                    False,
                ).id
            )
            self.assertEqual(
                [(item.sender, item.content) for item in messages],
                [("好友甲", "你好")],
            )
            prompt = request.call_args.args[0][1]["content"]
            self.assertIn("新消息：好友甲: 你好", prompt)
            self.assertIn("昵称只用于区分", prompt)

    def test_group_mention_uses_group_title_and_stores_sender(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": ""},
        ), patch(
            "chat_pet.napcat_bridge.request_chat",
            return_value="收到。",
        ):
            client = _FakeOneBotClient([_group_event("在吗")])
            service = self._service(folder, client)
            result = service.scan_once()
            self.assertEqual(result.chat_title, "测试群")
            self.assertEqual(result.drafts_created, 1)
            profile = service.database.recent_qq_user_profiles()[0]
            self.assertEqual(profile.display_name, "阿蓝")
            self.assertEqual(profile.mention_count, 1)

    def test_group_without_mention_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            client = _FakeOneBotClient([_group_event("普通聊天", mention=False)])
            service = self._service(folder, client)
            result = service.scan_once()
            self.assertEqual(result.drafts_created, 0)
            self.assertFalse(service.database.recent_qq_drafts())

    def test_auto_send_uses_onebot_messages_not_windows_ui(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": ""},
        ), patch(
            "chat_pet.napcat_bridge.request_chat",
            return_value="第一句，第二句",
        ):
            client = _FakeOneBotClient([_private_event()])
            service = self._service(folder, client, auto_send=True)
            result = service.scan_once()
            self.assertEqual(result.sent_count, 2)
            sends = [
                params
                for action, params in client.actions
                if action == "send_private_msg"
            ]
            self.assertEqual(len(sends), 2)
            self.assertEqual(
                [item["message"][0]["data"]["text"] for item in sends],
                ["第一句", "第二句"],
            )

    def test_random_friend_event_uses_onebot_history(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": ""},
        ), patch(
            "chat_pet.napcat_bridge.request_chat",
            return_value="展览时间定了吗？",
        ):
            client = _FakeOneBotClient()
            service = self._service(folder, client)
            settings = service.settings_store.load()
            settings.random_events_enabled = True
            settings.auto_send = False
            service.settings_store.save(settings)
            result = service.run_random_event(event="friend_greeting")
            self.assertEqual(result.status, "draft")
            self.assertIn(
                "get_friend_msg_history",
                [action for action, _params in client.actions],
            )
            self.assertTrue(service.database.recent_qq_initiatives())


class NapCatRuntimeTests(unittest.TestCase):
    def test_onebot_config_is_written_with_local_websocket_settings(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config_dir = root / "runtime" / "napcat" / "config"
            config_dir.mkdir(parents=True)
            (root / "onebot11.template.json").write_text(
                json.dumps(
                    {
                        "network": {
                            "httpServers": [],
                            "httpSseServers": [],
                            "httpClients": [],
                            "websocketServers": [{}],
                            "websocketClients": [],
                            "plugins": [],
                        },
                        "enableLocalFile2Url": False,
                        "parseMultMsg": False,
                    }
                ),
                encoding="utf-8",
            )
            runtime = NapCatRuntime(root)
            runtime.configure_onebot(
                QQSettings(
                    napcat_url="ws://127.0.0.1:3010",
                    napcat_token="local-token",
                )
            )
            config = json.loads(
                (config_dir / "onebot11.json").read_text(encoding="utf-8")
            )
            server = config["network"]["websocketServers"][0]
            self.assertEqual(server["port"], 3010)
            self.assertEqual(server["token"], "local-token")
            self.assertTrue(config["enableLocalFile2Url"])

    def test_start_uses_official_admin_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch(
            "chat_pet.napcat_bridge.subprocess.Popen"
        ) as popen:
            root = Path(folder)
            launcher_dir = root / "runtime" / "napcat"
            launcher_dir.mkdir(parents=True)
            for name in (
                "launcher-win10.bat",
                "NapCatWinBootMain.exe",
                "NapCatWinBootHook.dll",
            ):
                (launcher_dir / name).touch()
            popen.return_value.poll.return_value = None
            runtime = NapCatRuntime(root)
            with patch.object(runtime, "ensure_runtime", return_value=runtime.runtime), patch.object(
                runtime,
                "configure_onebot",
            ), patch.object(
                runtime,
                "find_qq",
                return_value=Path(r"C:\QQ\QQ.exe"),
            ):
                message = runtime.start(QQSettings())
            command = popen.call_args.args[0]
            self.assertEqual(command[0], "cmd.exe")
            self.assertEqual(command[1], "/c")
            self.assertTrue(command[2].endswith("launcher-win10.bat"))
            self.assertIn("UAC", message)

    def test_standalone_qq_starts_without_admin_or_system_qq(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch(
            "chat_pet.napcat_bridge.subprocess.Popen"
        ) as popen:
            root = Path(folder)
            standalone = (
                root
                / "standalone"
                / "NapCat.52230.Shell"
            )
            config_dir = (
                standalone
                / "versions"
                / "9.9.33-52230"
                / "resources"
                / "app"
                / "napcat"
                / "config"
            )
            config_dir.mkdir(parents=True)
            (standalone / "QQ.exe").touch()
            (standalone / "napcat.bat").touch()
            (standalone / "NapCatWinBootMain.exe").touch()
            (root / "onebot11.template.json").write_text(
                json.dumps(
                    {
                        "network": {
                            "httpServers": [],
                            "httpSseServers": [],
                            "httpClients": [],
                            "websocketServers": [{}],
                            "websocketClients": [],
                            "plugins": [],
                        },
                        "enableLocalFile2Url": False,
                        "parseMultMsg": False,
                    }
                ),
                encoding="utf-8",
            )
            (config_dir / "onebot11_123.json").write_text(
                json.dumps(
                    {
                        "network": {
                            "websocketServers": [{}],
                        }
                    }
                ),
                encoding="utf-8",
            )
            popen.return_value.poll.return_value = None
            runtime = NapCatRuntime(root)
            self.assertTrue(runtime.is_standalone)
            message = runtime.start(
                QQSettings(
                    napcat_url="ws://127.0.0.1:3011",
                    napcat_token="standalone-token",
                )
            )
            command = popen.call_args.args[0]
            self.assertEqual(command[0], "cmd.exe")
            self.assertTrue(command[2].endswith("napcat.bat"))
            self.assertEqual(popen.call_args.kwargs["cwd"], standalone)
            self.assertIn("独立 QQ 9.9.33", message)
            saved = json.loads(
                (config_dir / "onebot11_123.json").read_text(
                    encoding="utf-8"
                )
            )
            server = saved["network"]["websocketServers"][0]
            self.assertEqual(server["port"], 3011)
            self.assertEqual(server["token"], "standalone-token")


if __name__ == "__main__":
    unittest.main()
