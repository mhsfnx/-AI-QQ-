from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import queue
import random
import re
import subprocess
import threading
import time
import urllib.parse
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import websocket

from .api import ApiError, request_chat
from .chat_style import split_chat_reply
from .config import ConfigStore
from .database import Database
from .paths import napcat_dir, napcat_runtime_dir
from .persona import task_system_prompt
from .qq_bridge import (
    QQInitiativeResult,
    QQScanResult,
    QQSettings,
    QQSettingsStore,
    choose_random_event,
)
from .stickers import Sticker, StickerSelector


NAPCAT_VERSION = "4.18.41"


class NapCatError(RuntimeError):
    pass


class OneBotClient:
    def __init__(
        self,
        url: str,
        token: str = "",
        *,
        on_status: Callable[[str], None] | None = None,
    ):
        self.url = url
        self.token = token
        self.on_status = on_status
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._send_lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="dafeiyu-napcat-websocket",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        self._connected.clear()
        self._thread = None

    def _run(self) -> None:
        headers = {}
        if self.token.strip():
            headers["Authorization"] = f"Bearer {self.token.strip()}"
        while not self._stop.is_set():
            self._ws = websocket.WebSocketApp(
                self.url,
                header=headers,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            try:
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                logging.info("NapCat WebSocket stopped: %s", exc)
            self._connected.clear()
            self._notify("NapCat 连接已断开")
            if self._stop.wait(3):
                break

    def _notify(self, message: str) -> None:
        if self.on_status is not None:
            try:
                self.on_status(message)
            except Exception:
                logging.exception("NapCat status callback failed")

    def _on_open(self, _ws) -> None:
        self._connected.set()
        self._notify("NapCat WebSocket 已连接")

    def _on_close(self, _ws, _code, _message) -> None:
        self._connected.clear()

    def _on_error(self, _ws, error) -> None:
        logging.info("NapCat WebSocket error: %s", error)

    def _on_message(self, _ws, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        echo = str(payload.get("echo", "") or "")
        if echo:
            pending = self._pending.get(echo)
            if pending is not None:
                pending.put(payload)
            return
        if payload.get("post_type"):
            self._events.put(payload)

    def send_action(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 15,
    ) -> Any:
        if not self.connected or self._ws is None:
            raise NapCatError("NapCat WebSocket 尚未连接。")
        echo = uuid.uuid4().hex
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        self._pending[echo] = response_queue
        payload = {
            "action": action,
            "params": params or {},
            "echo": echo,
        }
        try:
            with self._send_lock:
                self._ws.send(json.dumps(payload, ensure_ascii=False))
            response = response_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise NapCatError(f"NapCat 接口 {action} 响应超时。") from exc
        except Exception as exc:
            raise NapCatError(f"NapCat 接口 {action} 发送失败：{exc}") from exc
        finally:
            self._pending.pop(echo, None)
        retcode = response.get("retcode", 0)
        if retcode not in {0, "0"}:
            message = response.get("message") or response.get("wording") or response
            raise NapCatError(f"NapCat 接口 {action} 失败：{message}")
        return response.get("data")

    def poll_events(self, limit: int = 100) -> list[dict[str, Any]]:
        events = []
        for _index in range(max(1, limit)):
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        return events


class NapCatRuntime:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or napcat_dir())
        self.standalone = self._find_standalone()
        self.runtime = self.standalone or (self.root / "runtime")
        self.process: subprocess.Popen | None = None

    def _find_standalone(self) -> Path | None:
        base = self.root / "standalone"
        preferred = base / "NapCat.52230.Shell"
        candidates = [preferred]
        if base.is_dir():
            candidates.extend(
                path
                for path in sorted(base.glob("NapCat.*.Shell"))
                if path != preferred
            )
        for candidate in candidates:
            if (
                candidate.is_dir()
                and (candidate / "QQ.exe").is_file()
                and (candidate / "napcat.bat").is_file()
                and (candidate / "versions").is_dir()
            ):
                return candidate
        return None

    @property
    def is_standalone(self) -> bool:
        return self.standalone is not None

    @property
    def launcher(self) -> Path:
        return self.runtime / "napcat" / "NapCatWinBootMain.exe"

    def ensure_runtime(self) -> Path:
        if self.is_standalone:
            required = (
                self.runtime / "QQ.exe",
                self.runtime / "napcat.bat",
                self.runtime / "NapCatWinBootMain.exe",
                self.runtime / "versions",
            )
            if all(path.exists() for path in required):
                return self.runtime
            raise NapCatError("独立 QQ + NapCat 运行目录不完整。")
        required = (
            self.runtime / "node.exe",
            self.runtime / "index.js",
            self.runtime / "napcat" / "napcat.mjs",
        )
        if all(path.exists() for path in required):
            return self.runtime
        archive = (
            self.root
            / "vendor"
            / f"NapCat.Shell.Windows.Node-v{NAPCAT_VERSION}.zip"
        )
        if not archive.exists():
            raise NapCatError(
                f"缺少 NapCat 运行环境：{self.runtime}，"
                "且没有可解压的官方依赖包。"
            )
        self.runtime.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(self.runtime)
        return self.runtime

    def config_dirs(self) -> list[Path]:
        if self.is_standalone:
            config_dirs = sorted(
                self.runtime.glob(
                    "versions/*/resources/app/napcat/config"
                )
            )
            return [path for path in config_dirs if path.is_dir()]
        return [self.runtime / "napcat" / "config"]

    @staticmethod
    def find_qq() -> Path:
        candidates = [
            Path(r"C:\Program Files\Tencent\QQNT\QQ.exe"),
            Path(r"C:\Program Files (x86)\Tencent\QQNT\QQ.exe"),
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Programs"
            / "Tencent"
            / "QQNT"
            / "QQ.exe",
        ]
        try:
            import winreg

            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(
                        winreg.HKEY_LOCAL_MACHINE,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
                        0,
                        winreg.KEY_READ | view,
                    ) as key:
                        value, _kind = winreg.QueryValueEx(
                            key,
                            "UninstallString",
                        )
                        uninstall = str(value).strip().strip('"')
                        candidates.insert(0, Path(uninstall).parent / "QQ.exe")
                except OSError:
                    continue
        except ImportError:
            pass
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise NapCatError("没有找到本机 QQ NT 安装，请先安装并登录 QQ。")

    def configure_onebot(self, settings: QQSettings) -> None:
        parsed = urllib.parse.urlparse(settings.napcat_url)
        if parsed.hostname not in {"127.0.0.1", "localhost"}:
            return
        port = parsed.port or 3001
        config_dirs = self.config_dirs()
        if not config_dirs:
            raise NapCatError("没有找到 NapCat 配置目录。")
        template_path = self.root / "onebot11.template.json"
        template = json.loads(template_path.read_text(encoding="utf-8"))
        template["enableLocalFile2Url"] = True
        server = template["network"]["websocketServers"][0]
        server["host"] = parsed.hostname
        server["port"] = port
        server["token"] = settings.napcat_token.strip()
        targets = []
        for config_dir in config_dirs:
            config_dir.mkdir(parents=True, exist_ok=True)
            targets.append(config_dir / "onebot11.json")
            targets.extend(config_dir.glob("onebot11_*.json"))
        for target in targets:
            payload = copy.deepcopy(template)
            if target.exists() and target.name != "onebot11.json":
                try:
                    payload = json.loads(target.read_text(encoding="utf-8"))
                    payload["enableLocalFile2Url"] = True
                    servers = payload.setdefault("network", {}).setdefault(
                        "websocketServers",
                        [],
                    )
                    current = next(
                        (
                            item
                            for item in servers
                            if isinstance(item, dict)
                        ),
                        None,
                    )
                    if current is None:
                        servers.append(server)
                    else:
                        current.update(server)
                except Exception:
                    payload = template
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, target)

    def start(self, settings: QQSettings) -> str:
        self.ensure_runtime()
        self.configure_onebot(settings)
        if self.process is not None and self.process.poll() is None:
            return "NapCat 进程已经在运行。"
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            if os.name == "nt"
            else 0
        )
        if self.is_standalone:
            self.process = subprocess.Popen(
                ["cmd.exe", "/c", "napcat.bat"],
                cwd=self.runtime,
                creationflags=creationflags,
            )
            return (
                "正在启动项目内独立 QQ 9.9.33 + NapCat；"
                "稍候会自动打开 WebUI，请扫码登录独立 QQ。"
            )
        qq_path = self.find_qq()
        hook = self.runtime / "napcat" / "NapCatWinBootHook.dll"
        launcher = self.runtime / "napcat" / "launcher-win10.bat"
        if (
            not launcher.is_file()
            or not self.launcher.is_file()
            or not hook.is_file()
        ):
            raise NapCatError("NapCat 管理员启动器或注入 DLL 缺失。")
        self.process = subprocess.Popen(
            ["cmd.exe", "/c", str(launcher)],
            cwd=self.runtime / "napcat",
            creationflags=creationflags,
        )
        return (
            f"正在通过管理员启动器连接 {qq_path}；"
            "若 QQ 仍在运行请先完全退出；出现 UAC 时请允许，"
            "然后等待 WebUI 启动并完成扫码。"
        )


@dataclass(slots=True)
class NapCatMessage:
    chat_type: str
    target_id: int
    chat_title: str
    sender_id: int
    sender_name: str
    text: str
    message_id: int
    mentioned_self: bool
    is_group: bool


class NapCatMonitorService:
    def __init__(
        self,
        database: Database,
        settings_store: QQSettingsStore,
        chat_config_store: ConfigStore,
        *,
        client: OneBotClient | None = None,
        runtime: NapCatRuntime | None = None,
        sticker_selector: StickerSelector | None = None,
    ):
        self.database = database
        self.settings_store = settings_store
        self.chat_config_store = chat_config_store
        self.client = client
        self.runtime = runtime or NapCatRuntime()
        self.sticker_selector = sticker_selector
        self._seen_message_ids: set[int] = set()
        self._group_names: dict[int, str] = {}

    def connection_status(self) -> str:
        if self.client is None:
            return "NapCat 未连接"
        return "NapCat 已连接" if self.client.connected else "NapCat 连接中"

    def ensure_client(self, settings: QQSettings, *, auto_start: bool = False) -> None:
        if self.client is None or (
            self.client.url != settings.napcat_url
            or self.client.token != settings.napcat_token
        ):
            if self.client is not None:
                self.client.stop()
            self.client = OneBotClient(
                settings.napcat_url,
                settings.napcat_token,
            )
            self.client.start()
        if auto_start and settings.napcat_auto_start:
            try:
                self.runtime.start(settings)
            except Exception:
                logging.exception("Could not start NapCat runtime")

    def start_napcat(self) -> str:
        settings = self.settings_store.load()
        message = self.runtime.start(settings)
        self.ensure_client(settings)
        return message

    def stop(self) -> None:
        if self.client is not None:
            self.client.stop()

    def open_webui(self) -> str:
        return "http://127.0.0.1:6099/webui"

    def scan_once(self, *, force: bool = False) -> QQScanResult:
        del force
        settings = self.settings_store.load()
        if not settings.enabled:
            return QQScanResult("NapCat 消息助手未启用")
        self.ensure_client(settings)
        if self.client is None or not self.client.connected:
            return QQScanResult("NapCat 尚未连接；请启动 NapCat 并完成 QQ 登录")
        events = self.client.poll_events()
        results = []
        for event in events:
            if event.get("post_type") != "message":
                continue
            try:
                result = self._process_event(event, settings)
            except Exception as exc:
                logging.exception("NapCat message processing failed")
                results.append(QQScanResult(f"NapCat 消息处理失败：{exc}"))
                continue
            if result is not None:
                results.append(result)
        if not results:
            return QQScanResult("NapCat 已连接；当前没有新消息")
        chosen = next(
            (item for item in results if item.drafts_created),
            results[-1],
        )
        chosen.stored_messages = sum(item.stored_messages for item in results)
        chosen.drafts_created = sum(item.drafts_created for item in results)
        chosen.sent_count = sum(item.sent_count for item in results)
        chosen.draft_texts = [
            text for item in results for text in item.draft_texts
        ]
        chosen.draft_message_ids = [
            message_id
            for item in results
            for message_id in item.draft_message_ids
        ]
        return chosen

    @staticmethod
    def _sender_name(event: dict[str, Any]) -> str:
        sender = event.get("sender") or {}
        if not isinstance(sender, dict):
            sender = {}
        return str(
            sender.get("card")
            or sender.get("nickname")
            or event.get("user_id")
            or ""
        ).strip()

    @staticmethod
    def _message_segments(event: dict[str, Any]) -> tuple[str, bool]:
        self_id = str(event.get("self_id", ""))
        message = event.get("message")
        pieces = []
        mentioned = False
        if isinstance(message, list):
            for segment in message:
                if not isinstance(segment, dict):
                    continue
                kind = str(segment.get("type", ""))
                data = segment.get("data") or {}
                if kind == "text":
                    pieces.append(str(data.get("text", "")))
                elif kind == "image":
                    pieces.append("[图片]")
                elif kind == "face":
                    pieces.append("[表情]")
                elif kind == "at":
                    qq = str(data.get("qq", ""))
                    if qq == self_id:
                        mentioned = True
                    else:
                        pieces.append(f"@{data.get('name') or qq}")
        else:
            raw = str(message or event.get("raw_message") or "")
            mentioned = bool(
                re.search(
                    rf"\[CQ:at,qq={re.escape(self_id)}(?:,[^\]]*)?\]",
                    raw,
                )
            )
            raw = re.sub(r"\[CQ:at,qq=[^\]]+\]", "", raw)
            raw = re.sub(r"\[CQ:image,[^\]]+\]", "[图片]", raw)
            raw = re.sub(r"\[CQ:face,[^\]]+\]", "[表情]", raw)
            pieces.append(raw)
        return re.sub(r"\s+", " ", "".join(pieces)).strip(), mentioned

    def _process_event(
        self,
        event: dict[str, Any],
        settings: QQSettings,
    ) -> QQScanResult | None:
        message_type = str(event.get("message_type", ""))
        if message_type not in {"private", "group"}:
            return None
        self_id = int(event.get("self_id", 0) or 0)
        sender_id = int(event.get("user_id", 0) or 0)
        message_id = int(event.get("message_id", 0) or 0)
        if sender_id == self_id or message_id in self._seen_message_ids:
            return None
        is_group = message_type == "group"
        if is_group and not settings.scan_group:
            return None
        if not is_group and not settings.scan_private:
            return None
        target_id = int(event.get("group_id", 0) or sender_id)
        sender_name = self._sender_name(event)
        text, mentioned = self._message_segments(event)
        if is_group and not mentioned:
            mentioned = any(
                f"@{name}" in text for name in settings.own_names
            )
        if is_group and not mentioned:
            return None
        if not text:
            return None
        self._seen_message_ids.add(message_id)
        if len(self._seen_message_ids) > 5000:
            self._seen_message_ids = set(list(self._seen_message_ids)[-2500:])
        chat_title = self._chat_title(event, is_group, sender_name)
        chat_key = f"napcat:{message_type}:{target_id}"
        conversation = self.database.ensure_qq_conversation(
            chat_key,
            chat_title,
            is_group,
        )
        stored_id = self.database.add_qq_message(
            conversation.id,
            sender_name or str(sender_id),
            text,
            is_mentioned=mentioned,
            fingerprint=f"napcat:{message_id}",
        )
        if stored_id is None:
            return None
        user_key = f"{chat_key}:{sender_id}"
        self.database.upsert_qq_user_profile(
            user_key,
            sender_name or str(sender_id),
            f"qq:{chat_key}",
            is_group=is_group,
            mentioned_self=mentioned,
            message=text,
        )
        self.database.add_event_memory(
            "qq_mention" if is_group else "qq_private_message",
            (
                f"群「{chat_title}」里 {sender_name} @ 了大肥鱼"
                if is_group
                else f"好友「{chat_title}」发来了私聊"
            ),
            scope=f"qq:{chat_key}",
            related_user_key=user_key,
            details=text,
            importance=3 if is_group else 2,
        )
        chat_config = self.chat_config_store.load()
        if not chat_config.api_key:
            return QQScanResult(
                "NapCat 收到消息，但尚未配置 AI API",
                chat_title,
                is_group,
                stored_messages=1,
            )
        history = self.database.recent_qq_messages(conversation.id, limit=12)
        previous = [item for item in history if item.id != stored_id]
        history_lines = "\n".join(
            f"{item.sender}: {item.content}" for item in previous
        ) or "暂无更早消息"
        profile = self.database.get_qq_user_profile(user_key)
        prompt = (
            f"你在为桌宠“大肥鱼”回复 QQ {message_type}，当前聊天：{chat_title}。\n"
            "昵称只用于区分每条消息的发送者。不要根据、分析或评价昵称，"
            "不要从昵称推测身份和性格。\n"
            f"当前聊天对象画像：\n"
            f"{profile.prompt_text() if profile else '- 暂无'}\n"
            f"最近 QQ 上下文：\n{history_lines}\n"
            f"新消息：{sender_name}: {text}"
        )
        try:
            answer = request_chat(
                [
                    {
                        "role": "system",
                        "content": task_system_prompt(
                            "为当前 QQ 会话生成一条可直接发送的简短回复。",
                            self.database.get_pet_persona().prompt_text(),
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                chat_config,
                max_tokens=350,
                temperature=0.75,
            )
        except ApiError as exc:
            return QQScanResult(
                f"NapCat 消息已保存，AI 草稿失败：{exc}",
                chat_title,
                is_group,
                stored_messages=1,
            )
        segments = split_chat_reply(answer) or [answer]
        sticker = (
            self.sticker_selector.consider(answer)
            if self.sticker_selector is not None
            else None
        )
        draft_text = "\n".join(segments)
        if sticker is not None:
            draft_text += f"\n[表情包：{sticker.label}]"
        self.database.add_qq_draft(stored_id, draft_text)
        sent = 0
        if settings.auto_send:
            try:
                sent = self.send_text_messages(
                    is_group,
                    target_id,
                    segments,
                    sticker,
                )
                status = "sent" if sent == len(segments) else "send-failed"
            except Exception:
                logging.exception("NapCat auto-send failed")
                status = "send-failed"
            self.database.set_qq_draft_status(stored_id, status)
        return QQScanResult(
            (
                f"NapCat 已发送 {sent} 条短消息"
                if sent
                else f"NapCat 已生成 {len(segments)} 条草稿"
            ),
            chat_title,
            is_group,
            stored_messages=1,
            drafts_created=1,
            sent_count=sent,
            draft_texts=segments,
            draft_message_ids=[stored_id],
        )

    def _chat_title(
        self,
        event: dict[str, Any],
        is_group: bool,
        sender_name: str,
    ) -> str:
        if not is_group:
            return sender_name or str(event.get("user_id", "QQ 好友"))
        group_id = int(event.get("group_id", 0) or 0)
        if group_id in self._group_names:
            return self._group_names[group_id]
        if self.client is not None and self.client.connected:
            try:
                groups = self.client.send_action("get_group_list") or []
                self._group_names.update(
                    {
                        int(group["group_id"]): str(
                            group.get("group_name")
                            or f"QQ群 {group['group_id']}"
                        )
                        for group in groups
                    }
                )
            except Exception:
                logging.exception("Could not load NapCat group list")
        if group_id in self._group_names:
            return self._group_names[group_id]
        title = str(event.get("group_name") or f"QQ群 {group_id}")
        self._group_names[group_id] = title
        return title

    def send_text_messages(
        self,
        is_group: bool,
        target_id: int,
        segments: list[str],
        sticker: Sticker | None = None,
    ) -> int:
        if self.client is None or not self.client.connected:
            raise NapCatError("NapCat 未连接。")
        action = "send_group_msg" if is_group else "send_private_msg"
        target_key = "group_id" if is_group else "user_id"
        sent = 0
        for index, text in enumerate(segments):
            message: list[dict[str, Any]] = [
                {"type": "text", "data": {"text": text}}
            ]
            if sticker is not None and index == len(segments) - 1:
                message.append(
                    {
                        "type": "image",
                        "data": {"file": str(sticker.path.resolve())},
                    }
                )
            self.client.send_action(
                action,
                {target_key: target_id, "message": message},
            )
            sent += 1
        return sent

    def run_random_event(self, *, event: str | None = None) -> QQInitiativeResult:
        settings = self.settings_store.load()
        if not settings.random_events_enabled:
            return QQInitiativeResult(
                "disabled",
                "disabled",
                "随机主动事件未启用",
            )
        event_type = event or choose_random_event(settings)
        if event_type == "no_action":
            summary = "随机事件：这次什么也没做"
            self._record_random("random_noop", summary)
            return QQInitiativeResult("no_action", "no_action", summary)
        if event_type == "qzone_post":
            if not settings.random_qzone_enabled:
                return QQInitiativeResult(
                    event_type,
                    "skipped",
                    "QQ 空间草稿事件已关闭",
                )
            return self._run_qzone_draft(settings)
        if event_type not in {"group_topic", "friend_greeting"}:
            return QQInitiativeResult(
                event_type,
                "skipped",
                "未知随机事件",
            )
        self.ensure_client(settings)
        if self.client is None or not self.client.connected:
            summary = "随机事件：NapCat 尚未连接"
            self._record_random("random_skipped", summary)
            return QQInitiativeResult(event_type, "skipped", summary)
        try:
            return self._run_social_event(event_type, settings)
        except Exception as exc:
            logging.exception("NapCat random event failed")
            summary = f"随机事件失败：{exc}"
            self._record_random("random_failed", summary)
            return QQInitiativeResult(event_type, "failed", summary)

    def _run_social_event(
        self,
        event_type: str,
        settings: QQSettings,
    ) -> QQInitiativeResult:
        assert self.client is not None
        is_group = event_type == "group_topic"
        if is_group:
            groups = self.client.send_action("get_group_list") or []
            if not groups:
                raise NapCatError("没有可用群聊")
            target = random.SystemRandom().choice(groups)
            target_id = int(target["group_id"])
            title = str(target.get("group_name") or f"QQ群 {target_id}")
            history = self.client.send_action(
                "get_group_msg_history",
                {"group_id": target_id, "count": 20},
            ) or {}
        else:
            friends = self.client.send_action("get_friend_list") or []
            if not friends:
                raise NapCatError("没有可用好友")
            target = random.SystemRandom().choice(friends)
            target_id = int(target["user_id"])
            title = str(
                target.get("remark")
                or target.get("nickname")
                or target_id
            )
            history = self.client.send_action(
                "get_friend_msg_history",
                {"user_id": target_id, "count": 20},
            ) or {}
        messages = history.get("messages") or []
        lines = []
        recent_sender = ""
        recent_time = 0
        self_id = int(messages[0].get("self_id", 0) or 0) if messages else 0
        for item in messages[-12:]:
            sender = item.get("sender") or {}
            name = str(
                sender.get("card")
                or sender.get("nickname")
                or sender.get("user_id")
                or ""
            )
            text, _mention = self._message_segments(item)
            if text:
                lines.append(f"{name or '未知'}: {text}")
            try:
                timestamp = int(item.get("time", 0) or 0)
            except (TypeError, ValueError):
                timestamp = 0
            recent_time = max(recent_time, timestamp)
            if is_group and int(sender.get("user_id", 0) or 0) != self_id:
                recent_sender = name
        history_text = "\n".join(lines) or "暂无更早消息"
        recently_active = (
            not is_group
            and recent_time > 0
            and time.time() - recent_time <= 600
        )
        config = self.chat_config_store.load()
        if not config.api_key:
            raise NapCatError("尚未配置 AI API")
        if is_group:
            task = (
                f"在 QQ 群「{title}」主动发起一个自然话题。"
                "不要 @ 或点名任何成员，不评价昵称。"
            )
        elif recently_active:
            task = f"继续和好友「{title}」聊最近的话题。"
        else:
            task = f"给好友「{title}」发一句自然问候。"
        answer = request_chat(
            [
                {
                    "role": "system",
                    "content": task_system_prompt(
                        task + "只输出消息正文。",
                        self.database.get_pet_persona().prompt_text(),
                    ),
                },
                {"role": "user", "content": history_text},
            ],
            config,
            max_tokens=260,
            temperature=0.85,
        )
        segments = split_chat_reply(answer) or [answer]
        sticker = (
            self.sticker_selector.consider(answer)
            if self.sticker_selector is not None
            else None
        )
        status = "draft"
        sent = 0
        if settings.auto_send:
            sent = self.send_text_messages(
                is_group,
                target_id,
                segments,
                sticker,
            )
            status = "sent" if sent == len(segments) else "send-failed"
        content = "\n".join(segments)
        initiative_id = self.database.add_qq_initiative(
            event_type,
            content,
            target_title=title,
            member_name=recent_sender if is_group else "",
            context=history_text,
            status=status,
        )
        summary = (
            f"随机事件：在群「{title}」发起话题"
            if is_group
            else f"随机事件：向好友「{title}」发起消息"
        )
        if status != "sent":
            summary += "（仅草稿）"
        self._record_random("qq_proactive", summary)
        return QQInitiativeResult(
            event_type,
            status,
            summary,
            target_title=title,
            content=content,
            initiative_id=initiative_id,
        )

    def _run_qzone_draft(self, settings: QQSettings) -> QQInitiativeResult:
        del settings
        config = self.chat_config_store.load()
        if not config.api_key:
            summary = "QQ 空间草稿：尚未配置 AI API"
            self._record_random("random_failed", summary)
            return QQInitiativeResult("qzone_post", "failed", summary)
        answer = request_chat(
            [
                {
                    "role": "system",
                    "content": task_system_prompt(
                        "写一条 QQ 空间动态草稿，只输出正文。",
                        self.database.get_pet_persona().prompt_text(),
                    ),
                },
                {"role": "user", "content": "写一条简短、自然的生活动态。"},
            ],
            config,
            max_tokens=220,
            temperature=0.85,
        )
        initiative_id = self.database.add_qq_initiative(
            "qzone_post",
            answer,
            status="draft-only",
        )
        summary = "随机事件：生成了 QQ 空间草稿（暂不自动发布）"
        self._record_random("qq_space_draft", summary)
        return QQInitiativeResult(
            "qzone_post",
            "draft-only",
            summary,
            content=answer,
            initiative_id=initiative_id,
        )

    def _record_random(self, kind: str, summary: str) -> None:
        conversation_id = self.database.latest_conversation(create=True)
        if conversation_id is not None:
            self.database.add_interaction(conversation_id, kind, summary)


__all__ = [
    "NAPCAT_VERSION",
    "NapCatError",
    "NapCatMessage",
    "NapCatMonitorService",
    "NapCatRuntime",
    "OneBotClient",
]
