from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import re
import random
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .api import ApiError, request_chat
from .chat_style import split_chat_reply
from .config import ConfigStore
from .database import Database
from .paths import data_dir
from .persona import task_system_prompt
from .security import CredentialError, protect_secret, unprotect_secret


class QQBridgeError(RuntimeError):
    pass


@dataclass(slots=True)
class QQSettings:
    enabled: bool = False
    poll_seconds: int = 4
    scan_private: bool = True
    scan_group: bool = True
    restore_window: bool = True
    own_names: list[str] = field(default_factory=list)
    dry_run: bool = True
    auto_send: bool = False
    random_events_enabled: bool = False
    random_event_interval_minutes: int = 30
    random_event_trigger_percent: int = 100
    random_no_action_percent: int = 55
    random_group_percent: int = 30
    random_friend_percent: int = 10
    random_qzone_percent: int = 5
    random_group_enabled: bool = True
    random_friend_enabled: bool = True
    random_qzone_enabled: bool = True
    napcat_url: str = "ws://127.0.0.1:3001"
    napcat_token: str = ""
    napcat_auto_start: bool = False


@dataclass(slots=True)
class QQSessionPreview:
    title: str
    signature: str
    preview: str


@dataclass(slots=True)
class QQMessageLine:
    sender: str
    content: str


@dataclass(slots=True)
class QQSnapshot:
    chat_title: str
    is_group: bool
    text_items: list[str]
    session_items: dict[str, QQSessionPreview]
    last_message_incoming: bool | None = None
    recent_members: list[str] = field(default_factory=list)
    message_items: list[QQMessageLine] = field(default_factory=list)

    @property
    def chat_key(self) -> str:
        return f"{'group' if self.is_group else 'private'}:{self.chat_title}"


@dataclass(slots=True)
class QQScanResult:
    status: str
    chat_title: str = ""
    is_group: bool = False
    changed_sessions: list[str] = field(default_factory=list)
    stored_messages: int = 0
    drafts_created: int = 0
    sent_count: int = 0
    draft_texts: list[str] = field(default_factory=list)
    draft_message_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class QQInitiativeResult:
    event_type: str
    status: str
    summary: str
    target_title: str = ""
    member_name: str = ""
    content: str = ""
    initiative_id: int | None = None


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(number, maximum))


def choose_random_event(
    settings: QQSettings,
    rng: random.Random | None = None,
) -> str:
    if not settings.random_events_enabled:
        return "disabled"
    source = rng or random.SystemRandom()
    trigger_percent = max(0, min(int(settings.random_event_trigger_percent), 100))
    if source.uniform(0.0, 100.0) >= trigger_percent:
        return "no_action"

    choices = [("no_action", max(0, int(settings.random_no_action_percent)))]
    if settings.random_group_enabled:
        choices.append(("group_topic", max(0, int(settings.random_group_percent))))
    if settings.random_friend_enabled:
        choices.append(("friend_greeting", max(0, int(settings.random_friend_percent))))
    if settings.random_qzone_enabled:
        choices.append(("qzone_post", max(0, int(settings.random_qzone_percent))))

    total = sum(weight for _name, weight in choices)
    if total <= 0:
        return "no_action"
    pick = source.uniform(0.0, float(total))
    cumulative = 0.0
    for name, weight in choices:
        cumulative += weight
        if pick < cumulative:
            return name
    return "no_action"


class QQSettingsStore:
    def __init__(self, path: Path | None = None):
        self.path = path or (data_dir() / "qq_bridge.json")
        self._runtime_auto_send = False

    def load(self) -> QQSettings:
        if not self.path.exists():
            return QQSettings()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return QQSettings()
        if not isinstance(payload, dict):
            return QQSettings()
        names = payload.get("own_names", [])
        if isinstance(names, str):
            names = re.split(r"[\n,，]+", names)
        if not isinstance(names, list):
            names = []
        try:
            poll_seconds = int(payload.get("poll_seconds", 4))
        except (TypeError, ValueError):
            poll_seconds = 4
        napcat_token = ""
        protected = payload.get("napcat_token_protected")
        if isinstance(protected, str) and protected:
            try:
                napcat_token = unprotect_secret(protected)
            except CredentialError:
                napcat_token = ""
        return QQSettings(
            enabled=bool(payload.get("enabled", False)),
            poll_seconds=max(2, min(poll_seconds, 60)),
            scan_private=bool(payload.get("scan_private", True)),
            scan_group=bool(payload.get("scan_group", True)),
            restore_window=bool(payload.get("restore_window", True)),
            own_names=[
                str(name).strip()
                for name in names
                if str(name).strip()
            ][:10],
            dry_run=not self._runtime_auto_send,
            auto_send=self._runtime_auto_send,
            random_events_enabled=bool(payload.get("random_events_enabled", False)),
            random_event_interval_minutes=_bounded_int(
                payload.get("random_event_interval_minutes", 30),
                30,
                1,
                1440,
            ),
            random_event_trigger_percent=_bounded_int(
                payload.get("random_event_trigger_percent", 100),
                100,
                0,
                100,
            ),
            random_no_action_percent=_bounded_int(
                payload.get("random_no_action_percent", 55),
                55,
                0,
                100,
            ),
            random_group_percent=_bounded_int(
                payload.get("random_group_percent", 30),
                30,
                0,
                100,
            ),
            random_friend_percent=_bounded_int(
                payload.get("random_friend_percent", 10),
                10,
                0,
                100,
            ),
            random_qzone_percent=_bounded_int(
                payload.get("random_qzone_percent", 5),
                5,
                0,
                100,
            ),
            random_group_enabled=bool(payload.get("random_group_enabled", True)),
            random_friend_enabled=bool(payload.get("random_friend_enabled", True)),
            random_qzone_enabled=bool(payload.get("random_qzone_enabled", True)),
            napcat_url=str(
                payload.get("napcat_url", "ws://127.0.0.1:3001")
            ).strip()
            or "ws://127.0.0.1:3001",
            napcat_token=napcat_token,
            napcat_auto_start=bool(payload.get("napcat_auto_start", False)),
        )

    def save(self, settings: QQSettings) -> None:
        self._runtime_auto_send = bool(settings.auto_send)
        settings.dry_run = not settings.auto_send
        payload = asdict(settings)
        # Auto-send is deliberately session-only. It is never inherited after
        # restarting the desktop pet.
        payload["auto_send"] = False
        payload["dry_run"] = True
        payload.pop("napcat_token", None)
        if settings.napcat_token.strip():
            payload["napcat_token_protected"] = protect_secret(
                settings.napcat_token.strip()
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


def _children(control) -> list[Any]:  # type: ignore[no-untyped-def]
    try:
        return list(control.GetChildren())
    except Exception:
        return []


def _find_named(control, name: str, depth: int = 14):  # type: ignore[no-untyped-def]
    if depth < 0:
        return None
    for child in _children(control):
        try:
            if child.Name == name:
                return child
            found = _find_named(child, name, depth - 1)
            if found is not None:
                return found
        except Exception:
            continue
    return None


def _find_type(control, type_name: str, depth: int = 16):  # type: ignore[no-untyped-def]
    if depth < 0:
        return None
    for child in _children(control):
        try:
            if child.ControlTypeName == type_name:
                return child
            found = _find_type(child, type_name, depth - 1)
            if found is not None:
                return found
        except Exception:
            continue
    return None


def _collect_texts(
    control,
    *,
    max_depth: int = 16,
    max_nodes: int = 2200,
) -> list[str]:  # type: ignore[no-untyped-def]
    output: list[str] = []
    visited = 0

    def walk(node, depth: int) -> None:  # type: ignore[no-untyped-def]
        nonlocal visited
        if depth > max_depth or visited >= max_nodes:
            return
        for child in _children(node):
            visited += 1
            if visited >= max_nodes:
                return
            try:
                if (
                    child.ControlTypeName
                    in {"TextControl", "CustomControl", "HyperlinkControl"}
                    and child.Name
                ):
                    value = str(child.Name).strip()
                    if value and (not output or output[-1] != value):
                        output.append(value)
            except Exception:
                pass
            walk(child, depth + 1)

    walk(control, 0)
    return output


def _collect_text_nodes(
    control,
    *,
    max_depth: int = 16,
    max_nodes: int = 2200,
) -> list[tuple[str, int, int]]:  # type: ignore[no-untyped-def]
    output: list[tuple[str, int, int]] = []
    visited = 0

    def walk(node, depth: int) -> None:  # type: ignore[no-untyped-def]
        nonlocal visited
        if depth > max_depth or visited >= max_nodes:
            return
        for child in _children(node):
            visited += 1
            if visited >= max_nodes:
                return
            try:
                if (
                    child.ControlTypeName
                    in {"TextControl", "CustomControl", "HyperlinkControl"}
                    and child.Name
                ):
                    value = str(child.Name).strip()
                    rect = child.BoundingRectangle
                    left = int(rect.left)
                    right = int(rect.right)
                    if value and right > left:
                        output.append((value, left, right))
            except Exception:
                pass
            walk(child, depth + 1)

    walk(control, 0)
    return output


def _parse_message_lines(
    raw_nodes: list[tuple[str, int, int]],
    *,
    chat_title: str,
    is_group: bool,
    member_names: list[str],
    list_left: int,
    list_right: int,
) -> tuple[list[QQMessageLine], tuple[int, int] | None]:
    lines: list[QQMessageLine] = []
    last_bounds: tuple[int, int] | None = None
    previous_visual: tuple[str, int, int] | None = None
    current_group_sender = ""
    ignored = {
        chat_title,
        "发送",
        "群聊成员",
        "聊天记录",
        "撤回了一条消息",
        "对方撤回了一条消息",
        "你撤回了一条消息",
    }
    for value, left, right in raw_nodes:
        if re.fullmatch(r"\d{1,2}:\d{2}", value):
            continue
        if re.fullmatch(r"\d{1,3}", value):
            continue
        if re.fullmatch(r"LV\d+", value, re.IGNORECASE):
            continue
        if value in ignored:
            continue
        visual = (value, left, right)
        if visual == previous_visual:
            continue
        previous_visual = visual
        if is_group and value in member_names:
            current_group_sender = value
            continue
        incoming_by_side: bool | None = None
        if list_right > list_left:
            left_distance = abs(left - list_left)
            right_distance = abs(list_right - right)
            if left_distance + 20 < right_distance:
                incoming_by_side = True
            elif right_distance + 20 < left_distance:
                incoming_by_side = False
        if is_group:
            sender = (
                "我"
                if incoming_by_side is False
                else current_group_sender or "群成员"
            )
        else:
            sender = "我" if incoming_by_side is False else chat_title
        lines.append(QQMessageLine(sender=sender, content=value))
        last_bounds = (left, right)
    return lines, last_bounds


class QQUiAutomation:
    def __init__(self) -> None:
        self._auto = None

    def _load(self):
        if self._auto is not None:
            return self._auto
        try:
            import uiautomation as auto
        except ImportError as exc:
            raise QQBridgeError("QQ 只读桥接组件未加载。") from exc
        auto.SetGlobalSearchTimeout(1)
        self._auto = auto
        return auto

    def _qq_pane(self):  # type: ignore[no-untyped-def]
        auto = self._load()
        root = auto.GetRootControl()
        candidates = []
        for child in _children(root):
            try:
                if child.Name == "QQ" and child.ClassName == "Chrome_WidgetWin_1":
                    candidates.append(child)
            except Exception:
                continue
        if not candidates:
            raise QQBridgeError("没有找到正在运行的 QQ 主窗口。")
        for candidate in candidates:
            if _find_named(candidate, "Rich Text Editor", depth=12) is not None:
                return candidate

        def area(control) -> int:  # type: ignore[no-untyped-def]
            try:
                rect = control.BoundingRectangle
                return max(0, int(rect.right - rect.left)) * max(
                    0,
                    int(rect.bottom - rect.top),
                )
            except Exception:
                return 0

        return max(candidates, key=area)

    def _active_document(self, pane):  # type: ignore[no-untyped-def]
        document = _find_type(pane, "DocumentControl")
        if document is not None:
            return document
        # Polling must not steal foreground focus from the user's current app.
        # The QQ window only gets activated after a new session is detected.
        time.sleep(0.1)
        document = _find_type(pane, "DocumentControl")
        if document is None:
            raise QQBridgeError(
                "QQ 的内部消息树不可用；请手动恢复 QQ 主窗口，避免最小化。"
            )
        return document

    @staticmethod
    def _restore_window() -> bool:
        try:
            user32 = ctypes.windll.user32
            user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
            user32.FindWindowW.restype = wintypes.HWND
            user32.IsIconic.argtypes = [wintypes.HWND]
            user32.IsIconic.restype = wintypes.BOOL
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            handle = user32.FindWindowW(None, "QQ")
            if not handle:
                return False
            if user32.IsIconic(handle):
                user32.ShowWindow(handle, 9)  # SW_RESTORE
            user32.SetForegroundWindow(handle)
            return True
        except Exception:
            return False

    def _snapshot_from_document(
        self,
        document,
        settings: QQSettings,
    ) -> QQSnapshot:  # type: ignore[no-untyped-def]
        editor_container = _find_named(document, "Rich Text Editor")
        editor = _find_type(editor_container, "EditControl", depth=3) if editor_container else None
        if editor is None:
            raise QQBridgeError("没有打开任何 QQ 会话。")
        chat_title = str(editor.Name or "").strip()
        if not chat_title:
            raise QQBridgeError("无法识别当前 QQ 会话名称。")

        message_list = _find_named(document, "消息列表")
        if message_list is None:
            raise QQBridgeError("无法读取当前 QQ 会话的消息列表。")
        group_members = _find_named(document, "成员列表")
        is_group = group_members is not None

        raw_nodes = _collect_text_nodes(message_list)
        member_names: list[str] = []
        recent_members: list[str] = []
        if group_members is not None:
            ignored_member_labels = {
                "群聊成员",
                "成员列表",
                "群主",
                "管理员",
                "在线",
                "离线",
                "查看更多",
                "展开",
                "收起",
            }
            for value in _collect_texts(
                group_members,
                max_depth=14,
                max_nodes=900,
            ):
                clean = value.strip()
                if (
                    not clean
                    or clean in ignored_member_labels
                    or len(clean) > 40
                    or re.fullmatch(r"[\d+\s:：/]+", clean)
                ):
                    continue
                if clean not in member_names:
                    member_names.append(clean)

            for value, _left, _right in raw_nodes:
                clean = value.strip()
                if clean in member_names and clean not in recent_members:
                    recent_members.append(clean)
            if not recent_members:
                recent_members = member_names[:20]
            recent_members = recent_members[-20:]

        try:
            list_rect = message_list.BoundingRectangle
            list_left = int(list_rect.left)
            list_right = int(list_rect.right)
        except Exception:
            list_left = 0
            list_right = 0
        message_items, last_text_bounds = _parse_message_lines(
            raw_nodes,
            chat_title=chat_title,
            is_group=is_group,
            member_names=member_names,
            list_left=list_left,
            list_right=list_right,
        )
        filtered = [line.content for line in message_items]

        last_message_incoming: bool | None = None
        if last_text_bounds is not None:
            try:
                list_rect = message_list.BoundingRectangle
                left_distance = abs(last_text_bounds[0] - int(list_rect.left))
                right_distance = abs(int(list_rect.right) - last_text_bounds[1])
                if left_distance + 20 < right_distance:
                    last_message_incoming = True
                elif right_distance + 20 < left_distance:
                    last_message_incoming = False
            except Exception:
                pass

        session_items: dict[str, QQSessionPreview] = {}
        session_list = _find_named(document, "会话列表")
        if session_list is not None:
            for row in _children(session_list):
                texts = _collect_texts(row, max_depth=10, max_nodes=220)
                if len(texts) < 2:
                    continue
                title = texts[0].strip()
                if not title or title == chat_title and len(texts) == 1:
                    continue
                source = "\x00".join(texts)
                signature = hashlib.sha256(source.encode("utf-8")).hexdigest()
                session_items[title] = QQSessionPreview(
                    title=title,
                    signature=signature,
                    preview=" ".join(texts[2:])[:300],
                )

        snapshot = QQSnapshot(
            chat_title=chat_title,
            is_group=is_group,
            text_items=filtered[-80:],
            session_items=session_items,
            last_message_incoming=last_message_incoming,
            recent_members=recent_members,
            message_items=message_items[-80:],
        )
        debug_path = os.environ.get("DAFEIYU_QQ_DEBUG_FILE")
        if debug_path:
            try:
                Path(debug_path).write_text(
                    json.dumps(asdict(snapshot), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                logging.exception("Could not write QQ UI debug snapshot")
        return snapshot

    def capture(self, settings: QQSettings) -> QQSnapshot:
        pane = self._qq_pane()
        document = self._active_document(pane)
        return self._snapshot_from_document(document, settings)

    def capture_many(
        self,
        settings: QQSettings,
        known_signatures: dict[str, str],
    ) -> list[QQSnapshot]:
        pane = self._qq_pane()
        document = self._active_document(pane)

        current = self._snapshot_from_document(document, settings)
        if not known_signatures:
            return [current]
        changed_titles = [
            title
            for title, item in current.session_items.items()
            if title != current.chat_title
            and known_signatures.get(title) != item.signature
        ]
        if not changed_titles:
            return [current]

        rows = self._session_rows(document)
        snapshots = [current]
        for title in changed_titles[:1]:
            row = rows.get(title)
            if row is None:
                continue
            try:
                switched_document = self._activate_session_row(pane, row, title)
                if switched_document is None:
                    continue
                switched = self._snapshot_from_document(switched_document, settings)
                if switched.chat_title == title:
                    snapshots.append(switched)
            except Exception:
                logging.exception("Could not read changed QQ chat %r", title)

        return snapshots

    def choose_random_conversation(
        self,
        settings: QQSettings,
        *,
        want_group: bool,
    ) -> QQSnapshot | None:
        pane = self._qq_pane()
        document = self._active_document(pane)
        current = self._snapshot_from_document(document, settings)
        if current.is_group == want_group:
            return current

        rows = self._session_rows(document)
        titles = list(current.session_items)
        random.SystemRandom().shuffle(titles)
        attempted = 0
        for title in titles:
            if attempted >= 16:
                break
            row = rows.get(title)
            if row is None:
                continue
            attempted += 1
            try:
                switched_document = self._activate_session_row(pane, row, title)
                if switched_document is None:
                    continue
                candidate = self._snapshot_from_document(switched_document, settings)
            except Exception:
                logging.exception("Could not inspect random QQ session %r", title)
                continue
            if candidate.is_group == want_group:
                return candidate
        return None

    def _session_rows(self, document) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        session_list = _find_named(document, "会话列表")
        rows: dict[str, Any] = {}
        if session_list is None:
            return rows
        for row in _children(session_list):
            texts = _collect_texts(row, max_depth=10, max_nodes=220)
            if len(texts) >= 2 and texts[0].strip():
                rows[texts[0].strip()] = row
        return rows

    def _activate_session_row(
        self,
        pane,
        row,
        title: str,
    ):  # type: ignore[no-untyped-def]
        targets = [row]
        title_control = _find_named(row, title, depth=8)
        if title_control is not None and title_control is not row:
            targets.append(title_control)
        for target in targets:
            try:
                target.Click()
                time.sleep(0.4)
                document = self._active_document(pane)
                editor_container = _find_named(document, "Rich Text Editor")
                editor = (
                    _find_type(editor_container, "EditControl", depth=3)
                    if editor_container is not None
                    else None
                )
                if editor is not None and str(editor.Name or "").strip() == title:
                    return document
            except Exception:
                continue
        return None

    def send_reply(self, chat_title: str, text: str) -> bool:
        if not text.strip():
            return False
        pane = self._qq_pane()
        document = self._active_document(pane)
        editor_container = _find_named(document, "Rich Text Editor")
        editor = (
            _find_type(editor_container, "EditControl", depth=3)
            if editor_container is not None
            else None
        )
        if editor is None:
            raise QQBridgeError("发送前无法找到 QQ 输入框。")
        original_title = str(editor.Name or "").strip()
        if original_title != chat_title:
            rows = self._session_rows(document)
            row = rows.get(chat_title)
            if row is None:
                raise QQBridgeError(f"发送前找不到 QQ 会话：{chat_title}")
            switched_document = self._activate_session_row(pane, row, chat_title)
            if switched_document is None:
                raise QQBridgeError(f"无法切换到 QQ 会话：{chat_title}")
            document = switched_document
            editor_container = _find_named(document, "Rich Text Editor")
            editor = (
                _find_type(editor_container, "EditControl", depth=3)
                if editor_container is not None
                else None
            )
        if editor is None or str(editor.Name or "").strip() != chat_title:
            raise QQBridgeError("发送目标与当前 QQ 会话不一致，已取消发送。")
        try:
            unsent = str(editor.GetValuePattern().Value or "").strip()
        except Exception:
            unsent = ""
        if unsent:
            raise QQBridgeError("QQ 输入框里有未发送内容，已保留原文并取消自动发送。")
        if not self._focus_qq_editor(editor):
            raise QQBridgeError("无法确认 QQ 已获得焦点，已取消自动发送。")
        try:
            escaped = text.strip().replace("{", "{{}").replace("}", "{}}")
            self._load().SendKeys(escaped)
        except Exception as exc:
            raise QQBridgeError("无法输入 QQ 消息，已取消发送。") from exc
        time.sleep(0.45)
        send_button = _find_named(document, "发送")
        if send_button is None:
            self._clear_editor(editor)
            raise QQBridgeError("发送前无法找到 QQ 发送按钮。")
        try:
            if not send_button.IsEnabled:
                self._clear_editor(editor)
                raise QQBridgeError("QQ 输入框没有进入可发送状态。")
            send_button.Click()
            time.sleep(0.6)
            remaining = ""
            try:
                remaining = str(editor.GetValuePattern().Value or "").strip()
            except Exception:
                pass
            if remaining:
                self._clear_editor(editor)
                return False
        except QQBridgeError:
            raise
        except Exception as exc:
            self._clear_editor(editor)
            raise QQBridgeError(f"QQ 发送失败：{exc}") from exc
        return True

    @staticmethod
    def _focus_qq_editor(editor) -> bool:  # type: ignore[no-untyped-def]
        target_pid = int(getattr(editor, "ProcessId", 0) or 0)
        if not target_pid:
            return False
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        for focus in (
            lambda: editor.SetFocus(),
            lambda: editor.Click(),
        ):
            try:
                focus()
                time.sleep(0.2)
                foreground = user32.GetForegroundWindow()
                foreground_pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(
                    foreground,
                    ctypes.byref(foreground_pid),
                )
                if int(foreground_pid.value) == target_pid:
                    return True
            except Exception:
                continue
        return False

    @classmethod
    def _clear_editor(cls, editor) -> None:  # type: ignore[no-untyped-def]
        try:
            if not cls._focus_qq_editor(editor):
                return
            uiautomation = __import__("uiautomation")
            uiautomation.SendKeys("{Ctrl}a{Delete}")
        except Exception:
            pass


def _signature(texts: list[str]) -> str:
    return hashlib.sha256("\x00".join(texts).encode("utf-8")).hexdigest()


def _new_tail(previous: list[str], current: list[str]) -> list[str]:
    if not previous:
        return []
    if current[: len(previous)] == previous:
        return current[len(previous) :]
    return current[-8:]


def _new_message_tail(
    previous: list[QQMessageLine],
    current: list[QQMessageLine],
) -> list[QQMessageLine]:
    if not previous:
        return current[-8:]
    if current[: len(previous)] == previous:
        return current[len(previous) :]
    return current[-8:]


def _message_lines_from_snapshot(snapshot: QQSnapshot) -> list[QQMessageLine]:
    if snapshot.message_items:
        return list(snapshot.message_items)
    fallback_sender = "群成员" if snapshot.is_group else snapshot.chat_title
    return [
        QQMessageLine(sender=fallback_sender, content=item)
        for item in snapshot.text_items
        if (
            item.strip()
            and item != snapshot.chat_title
            and not re.fullmatch(r"\d{1,2}:\d{2}", item)
            and not re.fullmatch(r"\d{1,3}", item)
            and not re.fullmatch(r"LV\d+", item, re.IGNORECASE)
            and item
            not in {
                "发送",
                "群聊成员",
                "聊天记录",
                "撤回了一条消息",
                "对方撤回了一条消息",
                "你撤回了一条消息",
            }
        )
    ]


def _format_message_lines(lines: list[QQMessageLine]) -> str:
    return "\n".join(
        f"{line.sender or '未知'}: {line.content}"
        for line in lines
        if line.content.strip()
    )


def _message_signature(lines: list[QQMessageLine]) -> str:
    return _signature(
        [f"{line.sender}\x1f{line.content}" for line in lines]
    )


def _guess_mention_sender(
    items: list[str],
    recent_members: list[str],
    own_names: list[str],
) -> str:
    if not recent_members:
        return ""
    mentions = [f"@{name}" for name in own_names]
    for index in range(len(items) - 1, -1, -1):
        value = items[index]
        if not any(marker in value for marker in mentions):
            continue
        for member in reversed(recent_members):
            if member in value:
                return member
        for previous in reversed(items[:index]):
            if previous in recent_members:
                return previous
    return recent_members[-1]


class QQMonitorService:
    def __init__(
        self,
        database: Database,
        settings_store: QQSettingsStore,
        chat_config_store: ConfigStore,
        automation: QQUiAutomation | None = None,
    ):
        self.database = database
        self.settings_store = settings_store
        self.chat_config_store = chat_config_store
        self.automation = automation or QQUiAutomation()
        self._last_texts: dict[str, list[str]] = {}
        self._last_lines: dict[str, list[QQMessageLine]] = {}
        self._last_message_signatures: dict[str, str] = {}
        self._last_previews: dict[str, str] = {}
        self._session_signatures: dict[str, str] = {}
        self._last_send_at: dict[str, float] = {}

    def run_random_event(
        self,
        *,
        event: str | None = None,
        rng: random.Random | None = None,
    ) -> QQInitiativeResult:
        settings = self.settings_store.load()
        if not settings.random_events_enabled:
            return QQInitiativeResult(
                "disabled",
                "disabled",
                "随机主动事件未启用",
            )
        event_type = event or choose_random_event(settings, rng)
        if event_type == "no_action":
            summary = "随机事件：这次什么也没做"
            self._record_random_interaction("random_noop", summary)
            return QQInitiativeResult("no_action", "no_action", summary)

        if event_type == "qzone_post":
            if not settings.random_qzone_enabled:
                summary = "随机事件：QQ 空间草稿开关已关闭，已跳过"
                self._record_random_interaction("random_skipped", summary)
                return QQInitiativeResult("qzone_post", "skipped", summary)
            return self._run_qzone_draft(settings)
        if event_type not in {"group_topic", "friend_greeting"}:
            summary = f"随机事件：未知事件 {event_type}，已跳过"
            self._record_random_interaction("random_skipped", summary)
            return QQInitiativeResult(event_type, "skipped", summary)

        is_group = event_type == "group_topic"
        enabled = (
            settings.random_group_enabled
            if is_group
            else settings.random_friend_enabled
        )
        if not enabled:
            summary = "随机事件：对应事件开关已关闭，已跳过"
            self._record_random_interaction("random_skipped", summary)
            return QQInitiativeResult(event_type, "skipped", summary)
        if not settings.enabled:
            summary = "随机事件：QQ 消息助手未启用，无法读取目标会话"
            self._record_random_interaction("random_skipped", summary)
            return QQInitiativeResult(event_type, "skipped", summary)

        chooser = getattr(self.automation, "choose_random_conversation", None)
        if callable(chooser):
            snapshot = chooser(settings, want_group=is_group)
        else:
            captured = self.automation.capture(settings)
            snapshot = captured if captured.is_group == is_group else None
        if snapshot is None:
            label = "群聊" if is_group else "好友私聊"
            summary = f"随机事件：没有找到可用的{label}"
            self._record_random_interaction("random_no_target", summary)
            return QQInitiativeResult(event_type, "no_target", summary)

        config = self.chat_config_store.load()
        if not config.api_key:
            summary = "随机事件：尚未配置 AI API，无法生成内容"
            self._record_random_interaction("random_failed", summary)
            return QQInitiativeResult(
                event_type,
                "failed",
                summary,
                target_title=snapshot.chat_title,
            )

        conversation = self.database.ensure_qq_conversation(
            snapshot.chat_key,
            snapshot.chat_title,
            snapshot.is_group,
        )
        history = self.database.recent_qq_messages(conversation.id, limit=12)
        history_text = "\n".join(
            f"{item.sender}: {item.content}" for item in history
        ) or "暂无更早记录"
        latest_at = self.database.latest_qq_message_at(conversation.id)
        recently_active = bool(
            latest_at is not None
            and (datetime.now(timezone.utc) - latest_at.astimezone(timezone.utc)).total_seconds()
            <= 600
        )

        member_name = ""
        if is_group:
            member_name = (
                snapshot.recent_members[-1]
                if snapshot.recent_members
                else ""
            )
            if not member_name:
                summary = (
                    f"随机事件：群「{snapshot.chat_title}」未识别到最近发言成员"
                )
                self._record_random_interaction("random_no_target", summary)
                return QQInitiativeResult(
                    event_type,
                    "no_target",
                    summary,
                    target_title=snapshot.chat_title,
                )

        recent_events = self.database.recent_event_memories(
            scope=f"qq:{snapshot.chat_key}",
            limit=8,
        )
        event_context = "\n".join(
            f"- [{event.occurred_at[:16]} #{event.id}] {event.summary}"
            for event in recent_events
            if not event.related_user_key
        ) or "- 暂无最近事件"
        try:
            content = self._generate_random_content(
                config,
                event_type,
                snapshot,
                history_text,
                recently_active,
                event_context,
            )
        except ApiError as exc:
            summary = f"随机事件草稿生成失败：{exc}"
            self._record_random_interaction("random_failed", summary)
            return QQInitiativeResult(
                event_type,
                "failed",
                summary,
                target_title=snapshot.chat_title,
                member_name=member_name,
            )

        initiative_id = self.database.add_qq_initiative(
            event_type,
            content,
            target_title=snapshot.chat_title,
            member_name=member_name,
            context=history_text,
        )
        status = "draft"
        sent = False
        if settings.auto_send:
            send_reply = getattr(self.automation, "send_reply", None)
            if callable(send_reply):
                try:
                    sent = bool(send_reply(snapshot.chat_title, content))
                except QQBridgeError as exc:
                    logging.warning("Random QQ send blocked: %s", exc)
                    sent = False
                status = "sent" if sent else "send-failed"
                self.database.set_qq_initiative_status(initiative_id, status)

        if is_group:
            target = f"群「{snapshot.chat_title}」向「{member_name}」发起话题"
        elif recently_active:
            target = f"继续和「{snapshot.chat_title}」聊最近的话题"
        else:
            target = f"向好友「{snapshot.chat_title}」问候"
        suffix = "" if sent else "（仅草稿）"
        summary = f"随机事件：{target}{suffix}"
        self._record_random_interaction(
            "qq_proactive",
            summary,
            scope=f"qq:{snapshot.chat_key}",
            related_user_key="" if is_group else snapshot.chat_key,
            details=content,
            importance=3,
        )
        return QQInitiativeResult(
            event_type,
            status,
            summary,
            target_title=snapshot.chat_title,
            member_name=member_name,
            content=content,
            initiative_id=initiative_id,
        )

    def _run_qzone_draft(self, _settings: QQSettings) -> QQInitiativeResult:
        config = self.chat_config_store.load()
        if not config.api_key:
            summary = "随机事件：尚未配置 AI API，无法生成 QQ 空间草稿"
            self._record_random_interaction("random_failed", summary)
            return QQInitiativeResult("qzone_post", "failed", summary)
        try:
            content = self._generate_random_content(
                config,
                "qzone_post",
                None,
                "",
                False,
                "",
            )
        except ApiError as exc:
            summary = f"QQ 空间草稿生成失败：{exc}"
            self._record_random_interaction("random_failed", summary)
            return QQInitiativeResult("qzone_post", "failed", summary)
        initiative_id = self.database.add_qq_initiative(
            "qzone_post",
            content,
            status="draft-only",
        )
        summary = "随机事件：生成了 QQ 空间草稿（暂不自动发布）"
        self._record_random_interaction(
            "qq_space_draft",
            summary,
            scope="qq_space",
            details=content,
            importance=3,
        )
        return QQInitiativeResult(
            "qzone_post",
            "draft-only",
            summary,
            content=content,
            initiative_id=initiative_id,
        )

    def _generate_random_content(
        self,
        config,
        event_type: str,
        snapshot: QQSnapshot | None,
        history_text: str,
        recently_active: bool,
        event_context: str,
    ) -> str:  # type: ignore[no-untyped-def]
        profile = self.database.get_profile()
        profile_text = profile.prompt_text()
        learned_persona = self.database.get_pet_persona().prompt_text()
        if event_type == "group_topic":
            assert snapshot is not None
            prompt = (
                f"你现在以桌宠“大肥鱼”的口吻，在 QQ 群「{snapshot.chat_title}」"
                "自然发起一个新话题。\n"
                f"自动整理的用户信息：\n{profile_text}\n"
                f"最近群聊：\n{history_text}\n"
                f"最近事件（仅在相关时参考）：\n{event_context}\n"
                "只输出一条可直接发送的群消息，1-2 句；"
                "不要基于任何群成员的昵称或头像进行评论，不要 @ 或点名群成员；"
                "轻松自然，不刷屏，不提及 AI、自动化、草稿或提示词。"
            )
        elif event_type == "friend_greeting":
            assert snapshot is not None
            mode = (
                "最近十分钟内刚聊过，延续最近话题，不要重新打招呼"
                if recently_active
                else "最近十分钟没有聊天记录，写一句自然、不尴尬的问候"
            )
            prompt = (
                f"你现在以桌宠“大肥鱼”的口吻给 QQ 好友"
                f"「{snapshot.chat_title}」发一条私聊。{mode}。\n"
                f"自动整理的用户信息：\n{profile_text}\n"
                f"最近私聊：\n{history_text}\n"
                f"最近事件（仅在相关时参考）：\n{event_context}\n"
                "只输出消息正文，1-2 句，不编造共同经历，不提及 AI、自动化或提示词。"
            )
        else:
            prompt = (
                "以桌宠“大肥鱼”的口吻写一条 QQ 空间动态草稿。"
                "1-3 句，真诚、轻巧、有生活感，不暴露用户隐私，"
                "不提及 AI、自动化或草稿，不添加话题标签。\n"
                f"自动整理的用户信息：\n{profile_text}"
            )
        generated = request_chat(
            [
                {
                    "role": "system",
                    "content": task_system_prompt(
                        "生成一条自然的 QQ 社交消息草稿。"
                        "只输出最终可发送的文字，不解释过程。",
                        learned_persona,
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            config,
            max_tokens=260,
            temperature=0.85,
        )
        clean = generated.strip().strip("“”\"'")
        clean = re.sub(r"^(?:草稿|消息正文|QQ空间动态)[:：]\s*", "", clean)
        if event_type == "group_topic":
            clean = re.sub(r"[@＠][^\s，,。！？!?：:；;]+", "", clean)
            if snapshot is not None:
                for name in sorted(
                    snapshot.recent_members,
                    key=len,
                    reverse=True,
                ):
                    clean = clean.replace(name, "")
            clean = re.sub(r"[ \t]{2,}", " ", clean).strip()
        if not clean:
            raise ApiError("AI 返回了空白草稿。")
        return clean[:1200]

    def _record_random_interaction(
        self,
        kind: str,
        summary: str,
        *,
        scope: str = "",
        related_user_key: str = "",
        details: str = "",
        importance: int = 2,
    ) -> None:
        conversation_id = self.database.latest_conversation(create=True)
        if conversation_id is not None:
            self.database.add_interaction(conversation_id, kind, summary)
        if scope:
            self.database.add_event_memory(
                kind,
                summary,
                scope=scope,
                related_user_key=related_user_key,
                details=details,
                importance=importance,
            )

    def scan_once(self, *, force: bool = False) -> QQScanResult:
        settings = self.settings_store.load()
        if not settings.enabled:
            return QQScanResult("QQ 只读监听未启用")
        known_signatures = dict(self._session_signatures)
        capture_many = getattr(self.automation, "capture_many", None)
        if callable(capture_many):
            snapshots = capture_many(settings, known_signatures)
        else:
            snapshots = [self.automation.capture(settings)]
        if not snapshots:
            return QQScanResult("没有读取到 QQ 会话")

        snapshot = snapshots[0]
        changed_sessions = [
            title
            for title, item in snapshot.session_items.items()
            if self._session_signatures.get(title) != item.signature
            and title != snapshot.chat_title
        ]
        self._session_signatures = {
            title: item.signature
            for title, item in snapshot.session_items.items()
        }

        results = []
        for index, changed_snapshot in enumerate(snapshots):
            force_this = (force and index == 0) or (
                changed_snapshot.chat_title in changed_sessions
            )
            results.append(
                self._process_snapshot(
                    changed_snapshot,
                    settings,
                    changed_sessions,
                    force=force_this,
                )
            )
        stored = sum(item.stored_messages for item in results)
        drafts = sum(item.drafts_created for item in results)
        chosen = next(
            (item for item in results if item.drafts_created),
            next((item for item in results if item.stored_messages), results[0]),
        )
        chosen.stored_messages = stored
        chosen.drafts_created = drafts
        chosen.draft_texts = [
            text for item in results for text in item.draft_texts
        ]
        chosen.draft_message_ids = [
            message_id
            for item in results
            for message_id in item.draft_message_ids
        ]
        if len(snapshots) > 1:
            chosen.status = (
                f"已扫描 {len(snapshots)} 个有新动态的会话；"
                f"保存消息 {stored} 条，生成草稿 {drafts} 条（未发送）"
            )

        if settings.auto_send:
            sent_total = 0
            sent_all = False
            send_reply = getattr(self.automation, "send_reply", None)
            now = time.monotonic()
            send_index = len(snapshots) - 1 if len(snapshots) > 1 else 0
            snapshot = snapshots[send_index]
            result = results[send_index]
            if result.draft_texts:
                if snapshot.last_message_incoming is not True:
                    for message_id in result.draft_message_ids:
                        self.database.set_qq_draft_status(
                            message_id,
                            "blocked-not-incoming",
                        )
                else:
                    last_sent = self._last_send_at.get(snapshot.chat_key, 0.0)
                    if now - last_sent < 15:
                        for message_id in result.draft_message_ids:
                            self.database.set_qq_draft_status(
                                message_id,
                                "blocked-cooldown",
                            )
                    elif callable(send_reply):
                        send_failed = False
                        for text in result.draft_texts:
                            try:
                                accepted = send_reply(
                                    snapshot.chat_title,
                                    text,
                                )
                            except QQBridgeError as exc:
                                logging.warning("QQ auto-send blocked: %s", exc)
                                send_failed = True
                                break
                            if not accepted:
                                send_failed = True
                                break
                            sent_total += 1
                        all_sent = (
                            not send_failed
                            and sent_total == len(result.draft_texts)
                        )
                        sent_all = all_sent
                        if all_sent:
                            for message_id in result.draft_message_ids:
                                self.database.set_qq_draft_status(
                                    message_id,
                                    "sent",
                                )
                            self._last_send_at[snapshot.chat_key] = now
                            action_conversation = self.database.latest_conversation(
                                create=True
                            )
                            if action_conversation is not None:
                                self.database.add_interaction(
                                    action_conversation,
                                    (
                                        "qq_reply_batch"
                                        if sent_total > 1
                                        else "qq_reply"
                                    ),
                                    (
                                        f"回复了「{snapshot.chat_title}」"
                                        f"（{sent_total} 条消息）"
                                        if sent_total > 1
                                        else f"回复了「{snapshot.chat_title}」"
                                    ),
                                )
                        else:
                            for message_id in result.draft_message_ids:
                                self.database.set_qq_draft_status(
                                    message_id,
                                    "send-failed",
                                )
            chosen.sent_count = sent_total
            if sent_total:
                chosen.status = (
                    f"已自动发送 {sent_total} 条短消息"
                    if sent_all
                    else f"已发送 {sent_total} 条，后续发送失败"
                )
        return chosen

    def _process_snapshot(
        self,
        snapshot: QQSnapshot,
        settings: QQSettings,
        changed_sessions: list[str],
        *,
        force: bool = False,
    ) -> QQScanResult:

        previous = self._last_texts.get(snapshot.chat_key)
        self._last_texts[snapshot.chat_key] = list(snapshot.text_items)
        current_lines = _message_lines_from_snapshot(snapshot)
        previous_lines = self._last_lines.get(snapshot.chat_key)
        current_message_signature = _message_signature(current_lines)
        previous_message_signature = self._last_message_signatures.get(
            snapshot.chat_key
        )
        self._last_lines[snapshot.chat_key] = list(current_lines)
        self._last_message_signatures[snapshot.chat_key] = (
            current_message_signature
        )
        current_preview = (
            snapshot.session_items[snapshot.chat_title].preview
            if snapshot.chat_title in snapshot.session_items
            else ""
        )
        previous_preview = self._last_previews.get(snapshot.chat_key)
        self._last_previews[snapshot.chat_key] = current_preview
        if previous is None:
            conversation = self.database.ensure_qq_conversation(
                snapshot.chat_key,
                snapshot.chat_title,
                snapshot.is_group,
            )
            self.database.set_qq_signature(
                conversation.id,
                current_message_signature,
            )
            if not force:
                return QQScanResult(
                    f"已连接 QQ；读取到 {len(snapshot.text_items)} 个文本片段，"
                    "当前会话已建立基线",
                    snapshot.chat_title,
                    snapshot.is_group,
                    changed_sessions,
                )
            previous = []

        preview_changed = bool(
            current_preview
            and previous_preview is not None
            and current_preview != previous_preview
        )
        if (
            current_message_signature == previous_message_signature
            and not force
            and not preview_changed
        ):
            logging.info(
                "QQ scan unchanged chat=%r group=%s",
                snapshot.chat_title,
                snapshot.is_group,
            )
            return QQScanResult(
                f"正在监听；已读取 {len(snapshot.text_items)} 个文本片段，"
                "当前会话暂无新消息",
                snapshot.chat_title,
                snapshot.is_group,
                changed_sessions,
            )
        if snapshot.is_group and not settings.scan_group:
            return QQScanResult(
                "群聊监听已关闭",
                snapshot.chat_title,
                True,
                changed_sessions,
            )
        if not snapshot.is_group and not settings.scan_private:
            return QQScanResult(
                "私聊监听已关闭",
                snapshot.chat_title,
                False,
                changed_sessions,
            )

        text_changed = current_message_signature != previous_message_signature
        if text_changed:
            new_messages = (
                current_lines[-8:]
                if force
                else _new_message_tail(
                    previous_lines or [],
                    current_lines,
                )
            )
            if current_preview and all(
                current_preview not in line.content for line in new_messages
            ):
                new_messages.append(
                    QQMessageLine(
                        sender=(
                            "群消息预览"
                            if snapshot.is_group
                            else snapshot.chat_title
                        ),
                        content=current_preview,
                    )
                )
        elif preview_changed:
            new_messages = [
                QQMessageLine(
                    sender=(
                        "群消息预览"
                        if snapshot.is_group
                        else snapshot.chat_title
                    ),
                    content=current_preview,
                )
            ]
        else:
            new_messages = current_lines[-8:]
            if current_preview and all(
                current_preview not in line.content for line in new_messages
            ):
                new_messages.append(
                    QQMessageLine(
                        sender=(
                            "群消息预览"
                            if snapshot.is_group
                            else snapshot.chat_title
                        ),
                        content=current_preview,
                    )
                )
        incoming = _format_message_lines(new_messages).strip()
        if not incoming:
            return QQScanResult(
                "检测到界面变化，但未提取到新消息",
                snapshot.chat_title,
                snapshot.is_group,
                changed_sessions,
            )

        own_names = settings.own_names
        mention_messages = [
            line
            for line in new_messages
            if any(f"@{name}" in line.content for name in own_names)
        ]
        mentioned = bool(mention_messages)

        conversation = self.database.ensure_qq_conversation(
            snapshot.chat_key,
            snapshot.chat_title,
            snapshot.is_group,
        )
        profile_name = ""
        if snapshot.is_group and mention_messages:
            candidate = mention_messages[-1].sender
            profile_name = (
                candidate
                if candidate not in {"", "我", "群成员", "群消息预览"}
                else (
                    snapshot.recent_members[-1]
                    if snapshot.recent_members
                    else ""
                )
            )
        elif not snapshot.is_group:
            profile_name = snapshot.chat_title
        profile_user_key = (
            f"{snapshot.chat_key}:{profile_name}"
            if snapshot.is_group and profile_name
            else snapshot.chat_key
        )
        snapshot_signature = current_message_signature
        stored_ids: list[int] = []
        mention_ids: list[int] = []
        for index, line in enumerate(new_messages):
            line_mentioned = any(
                f"@{name}" in line.content for name in own_names
            )
            fingerprint = hashlib.sha256(
                (
                    f"{snapshot.chat_key}\0{snapshot_signature}\0{index}\0"
                    f"{line.sender}\0{line.content}"
                ).encode("utf-8")
            ).hexdigest()
            line_id = self.database.add_qq_message(
                conversation.id,
                line.sender or "未知",
                line.content,
                is_mentioned=line_mentioned,
                fingerprint=fingerprint,
            )
            if line_id is None:
                continue
            stored_ids.append(line_id)
            if line_mentioned:
                mention_ids.append(line_id)
        self.database.set_qq_signature(conversation.id, current_message_signature)
        if not stored_ids:
            return QQScanResult(
                "该消息已处理，已忽略重复记录",
                snapshot.chat_title,
                snapshot.is_group,
                changed_sessions,
            )
        message_id = (mention_ids or stored_ids)[-1]

        chat_profile = None
        if profile_name:
            if snapshot.is_group and mention_messages:
                profile_content = "\n".join(
                    line.content for line in mention_messages
                )
            else:
                profile_content = "\n".join(
                    line.content
                    for line in new_messages
                    if line.sender != "我"
                )
            if profile_content:
                chat_profile = self.database.upsert_qq_user_profile(
                    profile_user_key,
                    profile_name,
                    f"qq:{snapshot.chat_key}",
                    is_group=snapshot.is_group,
                    mentioned_self=mentioned,
                    message=profile_content,
                )
            event_summary = (
                f"群「{snapshot.chat_title}」里，{profile_name} @ 了大肥鱼"
                if snapshot.is_group
                else f"好友「{snapshot.chat_title}」发来了私聊"
            )
            self.database.add_event_memory(
                "qq_mention" if snapshot.is_group else "qq_private_message",
                event_summary,
                scope=f"qq:{snapshot.chat_key}",
                related_user_key=profile_user_key,
                details=incoming,
                importance=3 if snapshot.is_group else 2,
            )

        if snapshot.is_group and not mentioned:
            return QQScanResult(
                "收到群消息，但没有检测到 @你；未生成回复",
                snapshot.chat_title,
                True,
                changed_sessions,
                stored_messages=len(stored_ids),
            )

        chat_config = self.chat_config_store.load()
        if not chat_config.api_key:
            return QQScanResult(
                "收到符合条件的消息，但尚未配置 AI API；已保存消息",
                snapshot.chat_title,
                snapshot.is_group,
                changed_sessions,
                stored_messages=len(stored_ids),
            )

        history = self.database.recent_qq_messages(conversation.id, limit=30)
        current_message_ids = set(stored_ids)
        previous_messages = [
            item
            for item in history
            if item.id not in current_message_ids
        ][-10:]
        history_lines = "\n".join(
            f"{item.sender}: {item.content}" for item in previous_messages
        ) or "暂无更早消息"
        recent_events = self.database.recent_event_memories(
            scope=f"qq:{snapshot.chat_key}",
            related_user_key=profile_user_key,
            limit=8,
        )
        event_lines = "\n".join(
            f"- [{event.occurred_at[:16]} #{event.id}] {event.summary}"
            for event in recent_events
        ) or "- 暂无最近事件"
        old_summaries = self.database.relevant_event_summaries(
            incoming,
            scope=f"qq:{snapshot.chat_key}",
            related_user_key=profile_user_key,
            limit=3,
        )
        summary_lines = "\n".join(
            f"- [{summary.period_start[:10]} 索引:"
            f"{','.join(str(value) for value in summary.event_ids)}] "
            f"{summary.summary}"
            for summary in old_summaries
        ) or "- 暂无相关旧事件摘要"
        profile_text = (
            chat_profile.prompt_text()
            if chat_profile is not None
            else "- 暂无该聊天对象的画像"
        )
        prompt = (
            f"你在为桌宠“大肥鱼”起草一条 QQ 回复，当前会话：{snapshot.chat_title}，"
            f"{'群聊' if snapshot.is_group else '私聊'}。\n"
            "只生成可直接发送的简短自然回复，不要解释过程，不要提到自动化。"
            "群聊中只回应用户被 @ 的内容。\n"
            "聊天记录前缀中的昵称只用于分辨每条消息的发送者。"
            "不要根据、分析或评价任何昵称，也不要从昵称推测身份和性格。\n"
            "最近事件和旧事件摘要只能在和当前话题、当前聊天对象都相关时参考；"
            "不相关时不要主动提起。\n"
            f"当前聊天对象画像：\n{profile_text}\n"
            f"最近发生的事件：\n{event_lines}\n"
            f"相关旧事件摘要：\n{summary_lines}\n"
            f"最近 QQ 上下文：\n{history_lines}\n新消息：\n{incoming}"
        )
        try:
            draft = request_chat(
                [
                    {
                        "role": "system",
                        "content": task_system_prompt(
                            "为当前 QQ 会话生成一条可直接发送的简短回复，"
                            "只输出回复正文。",
                            self.database.get_pet_persona().prompt_text(),
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                chat_config,
                max_tokens=350,
                temperature=0.7,
            )
            draft_messages = split_chat_reply(draft) or [draft]
            self.database.add_qq_draft(
                message_id,
                "\n".join(draft_messages),
            )
        except ApiError as exc:
            return QQScanResult(
                f"消息已保存，但 AI 草稿生成失败：{exc}",
                snapshot.chat_title,
                snapshot.is_group,
                changed_sessions,
                stored_messages=len(stored_ids),
            )
        result = QQScanResult(
            (
                f"收到符合条件的 QQ 消息；已生成 {len(draft_messages)} "
                "条短消息草稿（未发送）"
            ),
            snapshot.chat_title,
            snapshot.is_group,
            changed_sessions,
            stored_messages=len(stored_ids),
            drafts_created=1,
            draft_texts=draft_messages,
            draft_message_ids=[message_id],
        )
        logging.info(
            "QQ scan created draft chat=%r group=%s messages=%s drafts=%s",
            result.chat_title,
            result.is_group,
            result.stored_messages,
            result.drafts_created,
        )
        return result


__all__ = [
    "QQInitiativeResult",
    "QQBridgeError",
    "QQMonitorService",
    "QQScanResult",
    "QQSettings",
    "QQSettingsStore",
    "choose_random_event",
]
