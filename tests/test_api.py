from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from chat_pet.api import ApiError, request_chat
from chat_pet.config import AppConfig, normalize_endpoint


class _Handler(BaseHTTPRequestHandler):
    last_body: dict | None = None

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        type(self).last_body = json.loads(self.rfile.read(length).decode("utf-8"))
        if self.headers.get("Authorization") != "Bearer test-key":
            self.send_error(401)
            return
        body = json.dumps(
            {"choices": [{"message": {"content": "测试回复"}}]},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_endpoint_normalization(self) -> None:
        self.assertEqual(
            normalize_endpoint("https://api.deepseek.com/"),
            "https://api.deepseek.com/chat/completions",
        )
        self.assertEqual(
            normalize_endpoint("http://localhost/v1/chat/completions"),
            "http://localhost/v1/chat/completions",
        )

    def test_request_chat_uses_openai_compatible_payload(self) -> None:
        port = self.server.server_address[1]
        config = AppConfig(
            api_url=f"http://127.0.0.1:{port}/v1",
            model="deepseek-chat",
            api_key="test-key",
            request_timeout=5,
            max_output_tokens=123,
        )
        answer = request_chat(
            [{"role": "user", "content": "你好"}],
            config,
        )
        self.assertEqual(answer, "测试回复")
        self.assertEqual(_Handler.last_body["model"], "deepseek-chat")
        self.assertEqual(_Handler.last_body["max_tokens"], 123)

    def test_missing_key_is_a_readable_error(self) -> None:
        with self.assertRaisesRegex(ApiError, "尚未配置 API 密钥"):
            request_chat([], AppConfig(api_key=""))


if __name__ == "__main__":
    unittest.main()
