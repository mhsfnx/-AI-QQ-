from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from chat_pet.config import AppConfig
from chat_pet.database import Database
from chat_pet.service import (
    extract_and_store_memories,
    prepare_user_turn,
    request_assistant_turn,
    store_assistant_turn,
)


class _ChatHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).requests.append(payload)
        body = json.dumps(
            {"choices": [{"message": {"content": "记得你喜欢蓝莓。"}}]},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


class ServiceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _ChatHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_full_turn_persists_history_and_sends_related_memory(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "memory.sqlite3")
            conversation_id = database.create_conversation()
            prepare_user_turn(
                database,
                conversation_id,
                "我叫小林，请记住我喜欢蓝莓。",
            )
            database.add_interaction(
                conversation_id,
                "poke",
                "你戳了戳大肥鱼",
            )
            event_id = database.add_event_memory(
                "topic",
                "昨天和小林一起做了蓝莓甜点",
                scope=f"main:{conversation_id}",
                importance=3,
            )
            database.apply_pet_persona_learnings(
                [
                    {
                        "category": "trait",
                        "value": "开始好奇人类为什么害怕沉默",
                        "confidence": 4,
                    }
                ]
            )
            config = AppConfig(
                api_url=f"http://127.0.0.1:{self.server.server_address[1]}/v1",
                api_key="test-key",
                request_timeout=5,
            )
            answer = request_assistant_turn(database, conversation_id, config)
            store_assistant_turn(database, conversation_id, answer)

            messages = database.recent_messages(conversation_id)
            self.assertEqual([item.role for item in messages], ["user", "assistant"])
            sent = _ChatHandler.requests[-1]["messages"]
            self.assertIn("蓝莓", sent[0]["content"])
            self.assertIn("你戳了戳大肥鱼", sent[0]["content"])
            self.assertIn("最近发生的事情", sent[0]["content"])
            self.assertIn(f"#{event_id}", sent[0]["content"])
            self.assertIn("蓝莓甜点", sent[0]["content"])
            self.assertIn("人类为什么害怕沉默", sent[0]["content"])
            self.assertEqual(sent[-1]["role"], "user")

    def test_memory_extraction_can_add_growing_persona(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "memory.sqlite3")
            conversation_id = database.create_conversation()
            prepare_user_turn(
                database,
                conversation_id,
                "你最近好像越来越喜欢研究人类为什么会犹豫。",
            )
            payload = {
                "profile_patch": {},
                "memories": [],
                "persona_learnings": [
                    {
                        "category": "preference",
                        "value": "开始喜欢研究人类做决定时的犹豫",
                        "confidence": 4,
                    },
                    {
                        "category": "knowledge",
                        "value": "了解到沉默有时是在组织语言",
                        "confidence": 3,
                    },
                ],
            }
            config = AppConfig(api_key="test-key")
            with patch(
                "chat_pet.service.request_chat",
                return_value=json.dumps(payload, ensure_ascii=False),
            ):
                stored = extract_and_store_memories(database, config)
            self.assertEqual(stored, 2)
            persona = database.get_pet_persona()
            self.assertIn("人类做决定时的犹豫", persona.preferences)
            self.assertIn("沉默有时是在组织语言", persona.knowledge)


if __name__ == "__main__":
    unittest.main()
