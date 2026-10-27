from __future__ import annotations

import html
import logging
import os
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .commands import execute_chat_command
from .chat_style import split_chat_reply
from .config import AppConfig
from .database import Interaction
from .image_ocr import extract_image_text
from .napcat_bridge import NapCatError, NapCatMonitorService, NapCatRuntime
from .paths import resolve_app_path, resource_dir
from .persona import APP_VERSION, PERSONA_VERSION
from .qq_bridge import (
    QQInitiativeResult,
    QQScanResult,
    QQSettings,
    QQSettingsStore,
)
from .service import (
    extract_and_store_memories,
    prepare_user_turn,
    request_assistant_turn,
    should_extract_memory,
    store_assistant_turn,
)
from .stickers import Sticker, StickerLibrary, StickerSelector


NAVY = "#27395f"
BLUE = "#577fbd"
PAPER = "#f8faff"
INK = "#17243d"
MUTED = "#71809b"


def _stylesheet() -> str:
    return f"""
    QDialog, QWidget#chatBody {{ background: {PAPER}; color: {INK}; }}
    QWidget#chatHeader {{ background: {NAVY}; }}
    QLabel#title {{ color: white; font-size: 20px; font-weight: 700; }}
    QLabel#status {{ color: #c8d7f2; font-size: 11px; }}
    QLabel#muted {{ color: {MUTED}; }}
    QPushButton {{
        background: #eef3fb; color: {INK}; border: 1px solid #cdd8ea;
        border-radius: 6px; padding: 6px 11px;
    }}
    QPushButton:hover {{ background: #e1ebfa; }}
    QPushButton#primary {{
        background: {BLUE}; color: white; border: none; font-weight: 600;
        padding: 8px 18px;
    }}
    QPushButton#primary:hover {{ background: #486ba4; }}
    QPushButton#headerButton {{
        background: #344a75; color: white; border: none; padding: 5px 9px;
    }}
    QPushButton#headerButton:hover {{ background: #476392; }}
    QTextBrowser, QPlainTextEdit, QLineEdit, QTableWidget {{
        background: white; border: 1px solid #ccd8e9; border-radius: 6px;
        selection-background-color: #b8cdf2;
    }}
    QPlainTextEdit, QLineEdit {{ padding: 6px; }}
    QTabWidget::pane {{ border: 1px solid #d3ddeb; background: white; }}
    QHeaderView::section {{ background: #edf2fa; padding: 6px; border: none; }}
    """


class _TaskSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, function: Callable[[], object]):
        super().__init__()
        self.function = function
        self.signals = _TaskSignals()

    def run(self) -> None:
        try:
            self.signals.succeeded.emit(self.function())
        except Exception as exc:
            logging.exception("Integrated chat background task failed")
            self.signals.failed.emit(str(exc))


class IntegratedChatController(QObject):
    def __init__(self, pet) -> None:  # type: ignore[no-untyped-def]
        super().__init__(pet)
        self.pet = pet
        self.database = pet._dafeiyu_chat_database
        self.config_store = pet._dafeiyu_config_store
        self.sticker_library = StickerLibrary()
        self.sticker_selector = StickerSelector(self.sticker_library)
        self.chat_dialog: ChatDialog | None = None
        self.qq_dialog: QQBridgeDialog | None = None
        self.qq_settings_store = QQSettingsStore()
        self.napcat_runtime = NapCatRuntime()
        self.qq_monitor = NapCatMonitorService(
            self.database,
            self.qq_settings_store,
            self.config_store,
            runtime=self.napcat_runtime,
            sticker_selector=self.sticker_selector,
        )
        self.qq_timer = QTimer(self)
        self.qq_timer.timeout.connect(self._qq_tick)
        self.random_event_timer = QTimer(self)
        self.random_event_timer.timeout.connect(self._random_event_tick)
        self.qq_busy = False
        self.random_event_busy = False
        self.qq_last_result: QQScanResult | None = None
        self.random_last_result: QQInitiativeResult | None = None
        self.thread_pool = QThreadPool.globalInstance()
        QTimer.singleShot(2200, self.apply_qq_settings)
        self.memory_compaction_timer = QTimer(self)
        self.memory_compaction_timer.timeout.connect(self.maintain_event_memories)
        self.memory_compaction_timer.start(6 * 60 * 60 * 1000)
        QTimer.singleShot(4500, self.maintain_event_memories)

    def open_chat(self) -> None:
        if self.chat_dialog is None:
            self.chat_dialog = ChatDialog(self)
        self.chat_dialog.show()
        self.chat_dialog.raise_()
        self.chat_dialog.activateWindow()
        self.record_interaction("chat_open", "你打开了 AI 聊天")
        capture_path = os.environ.get("DAFEIYU_QA_CAPTURE")
        if capture_path:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(
                1800,
                lambda: self.chat_dialog.grab().save(capture_path),
            )
        qa_message = os.environ.get("DAFEIYU_QA_MESSAGE")
        if qa_message:
            from PySide6.QtCore import QTimer

            def submit() -> None:
                self.chat_dialog.input.setText(qa_message)
                self.chat_dialog.send()

            QTimer.singleShot(500, submit)

    def record_interaction(self, kind: str, summary: str) -> None:
        conversation_id = (
            self.chat_dialog.conversation_id
            if self.chat_dialog is not None
            else self.database.latest_conversation()
        )
        if conversation_id is None:
            return
        try:
            self.database.add_interaction(conversation_id, kind, summary)
        except Exception:
            logging.exception("Could not save pet interaction")
            return
        if self.chat_dialog is not None:
            self.chat_dialog.reload_interactions()

    def maintain_event_memories(self) -> None:
        try:
            created = self.database.compact_old_event_memories(
                older_than_days=7,
            )
            if created:
                logging.info("Compacted %s old event-memory summaries", created)
        except Exception:
            logging.exception("Could not compact old event memories")

    def open_settings(self, parent: QWidget | None = None) -> None:
        SettingsDialog(self, parent or self.chat_dialog).exec()

    def open_memory(self, parent: QWidget | None = None) -> None:
        MemoryDialog(self, parent or self.chat_dialog).exec()

    def open_qq(self, parent: QWidget | None = None) -> None:
        if self.qq_dialog is None:
            self.qq_dialog = QQBridgeDialog(self, parent or self.chat_dialog)
        self.qq_dialog.show()
        self.qq_dialog.raise_()
        self.qq_dialog.activateWindow()

    def apply_qq_settings(self, settings: QQSettings | None = None) -> None:
        settings = settings or self.qq_settings_store.load()
        if settings.enabled:
            self.qq_monitor.ensure_client(settings, auto_start=True)
            self.qq_timer.start(1000)
            QTimer.singleShot(250, self._qq_tick)
        else:
            self.qq_timer.stop()
            self.qq_monitor.stop()
        if settings.random_events_enabled:
            interval_ms = max(
                1,
                int(settings.random_event_interval_minutes) * 60 * 1000,
            )
            self.random_event_timer.start(interval_ms)
        else:
            self.random_event_timer.stop()
        if self.qq_dialog is not None:
            self.qq_dialog.refresh_settings()

    def scan_qq_now(self, *, force: bool = False) -> None:
        if self.qq_busy:
            return
        settings = self.qq_settings_store.load()
        if not settings.enabled:
            self._qq_failed("请先在 QQ 消息助手设置中启用监听。")
            return
        self.qq_busy = True

        def work() -> QQScanResult:
            try:
                return self.qq_monitor.scan_once(force=force)
            except Exception as exc:
                logging.exception("NapCat scan failed")
                raise NapCatError(str(exc)) from exc

        self.start_task(work, self._qq_succeeded, self._qq_failed)

    def _qq_tick(self) -> None:
        if self.qq_timer.isActive():
            self.scan_qq_now()

    def _qq_succeeded(self, result: object) -> None:
        self.qq_busy = False
        if not isinstance(result, QQScanResult):
            return
        self.qq_last_result = result
        if self.qq_dialog is not None:
            self.qq_dialog.set_result(result)
        if self.chat_dialog is not None:
            self.chat_dialog.reload_interactions()
        if result.sent_count:
            self.pet.say(f"NapCat 已发送 {result.sent_count} 条 QQ 短消息。")
        elif result.drafts_created:
            self.pet.say("QQ 收到一条需要回复的消息，我已经写好草稿，没有发送。")

    def _qq_failed(self, message: str) -> None:
        self.qq_busy = False
        logging.info("QQ bridge unavailable: %s", message)
        if self.qq_dialog is not None:
            self.qq_dialog.set_error(message)

    def start_napcat(self) -> str:
        message = self.qq_monitor.start_napcat()
        self.qq_monitor.ensure_client(self.qq_settings_store.load())
        return message

    def napcat_webui_url(self) -> str:
        return self.qq_monitor.open_webui()

    def _random_event_tick(self) -> None:
        if self.random_event_busy or self.qq_busy:
            return
        self.run_random_event_now()

    def run_random_event_now(self) -> None:
        if self.random_event_busy or self.qq_busy:
            return
        settings = self.qq_settings_store.load()
        if not settings.random_events_enabled:
            return
        self.random_event_busy = True

        def work() -> QQInitiativeResult:
            try:
                return self.qq_monitor.run_random_event()
            except Exception as exc:
                logging.exception("Random NapCat event failed")
                raise NapCatError(str(exc)) from exc

        self.start_task(
            work,
            self._random_event_succeeded,
            self._random_event_failed,
        )

    def _random_event_succeeded(self, result: object) -> None:
        self.random_event_busy = False
        if not isinstance(result, QQInitiativeResult):
            return
        self.random_last_result = result
        if self.qq_dialog is not None:
            self.qq_dialog.set_initiative_result(result)
        if self.chat_dialog is not None:
            self.chat_dialog.reload_interactions()
        if result.status in {"draft", "draft-only", "sent"}:
            try:
                self.pet.say(result.summary)
            except Exception:
                logging.exception("Could not show random event hint")

    def _random_event_failed(self, message: str) -> None:
        self.random_event_busy = False
        logging.info("Random QQ event unavailable: %s", message)
        if self.qq_dialog is not None:
            self.qq_dialog.set_random_error(message)

    def start_task(
        self,
        function: Callable[[], object],
        on_success: Callable[[object], None],
        on_error: Callable[[str], None],
    ) -> None:
        task = _Task(function)
        task.signals.succeeded.connect(on_success)
        task.signals.failed.connect(on_error)
        # QThreadPool owns only the runnable; keep its signal object alive.
        if not hasattr(self, "_tasks"):
            self._tasks: set[_Task] = set()
        self._tasks.add(task)

        def cleanup(_value: object = None) -> None:
            self._tasks.discard(task)

        task.signals.succeeded.connect(cleanup)
        task.signals.failed.connect(cleanup)
        self.thread_pool.start(task)


class ChatDialog(QDialog):
    def __init__(self, controller: IntegratedChatController):
        super().__init__()
        self.controller = controller
        self.database = controller.database
        self.config_store = controller.config_store
        self.pending = False
        self.memory_busy = False
        self.image_busy = False
        self.conversation_id = self.database.latest_conversation() or self.database.create_conversation()
        self.messages = self.database.recent_messages(self.conversation_id, limit=60)
        self.interactions = self.database.recent_interactions(
            self.conversation_id,
            limit=40,
        )

        self.setWindowTitle("大肥鱼 · AI 聊天")
        self.setMinimumSize(500, 600)
        self.resize(575, 735)
        self.setStyleSheet(_stylesheet())
        icon_path = resource_dir() / "sprites" / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self._build()
        self._render()
        self.refresh_status()
        self.input.setFocus()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget()
        header.setObjectName("chatHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 12, 12, 12)
        header_layout.setSpacing(10)
        title = QLabel("大肥鱼")
        title.setObjectName("title")
        self.status = QLabel()
        self.status.setObjectName("status")
        header_layout.addWidget(title)
        header_layout.addWidget(self.status)
        header_layout.addStretch(1)
        for text, callback in (
            ("新对话", self.new_chat),
            ("QQ", lambda: self.controller.open_qq(self)),
            ("记忆", lambda: self.controller.open_memory(self)),
            ("设置", lambda: self.controller.open_settings(self)),
        ):
            button = QPushButton(text)
            button.setObjectName("headerButton")
            button.clicked.connect(callback)
            header_layout.addWidget(button)
        outer.addWidget(header)

        body = QWidget()
        body.setObjectName("chatBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(16, 12, 16, 12)
        body_layout.setSpacing(9)
        self.transcript = QTextBrowser()
        self.transcript.setOpenExternalLinks(True)
        self.transcript.setStyleSheet(
            "QTextBrowser { border: none; background: #ffffff; border-radius: 10px; }"
        )
        body_layout.addWidget(self.transcript, 1)
        self.input = QLineEdit()
        self.input.setPlaceholderText("和大肥鱼说点什么，或直接告诉我如何调整……")
        self.input.setMaxLength(12000)
        self.input.setClearButtonEnabled(True)
        self.input.setMinimumHeight(38)
        self.input.setAttribute(Qt.WA_InputMethodEnabled, True)
        self.input.returnPressed.connect(self.send)
        body_layout.addWidget(self.input)
        footer = QHBoxLayout()
        tip = QLabel("聊天与记忆保存在本机 SQLite")
        tip.setObjectName("muted")
        footer.addWidget(tip)
        footer.addStretch(1)
        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self.send)
        self.image_button = QPushButton("图片")
        self.image_button.setToolTip("选择一张图片，用 Windows 本地 OCR 提取文字")
        self.image_button.clicked.connect(self.choose_image)
        self.sticker_button = QPushButton("表情")
        self.sticker_button.setToolTip(
            f"打开表情文件夹：{self.controller.sticker_library.folder}"
        )
        self.sticker_button.clicked.connect(self.open_sticker_folder)
        footer.addWidget(self.image_button)
        footer.addWidget(self.sticker_button)
        footer.addWidget(self.send_button)
        body_layout.addLayout(footer)
        outer.addWidget(body, 1)

    def _render(self) -> None:
        parts = [
            "<style>"
            "body{font-family:'Microsoft YaHei UI';background:#fff;color:#17243d;}"
            ".meta{color:#71809b;font-size:11px;margin-top:12px;}"
            ".user{background:#e7efff;padding:11px;margin:6px 0 16px 40px;}"
            ".pet{background:#f4f7fc;padding:11px;margin:6px 40px 16px 0;}"
            ".event{text-align:center;color:#6c7c96;background:#edf2fa;"
            "padding:6px 10px;margin:8px 26px;font-size:11px;}"
            "</style>"
        ]
        if not self.messages and not self.interactions:
            parts.append(
                "<div class='meta'>大肥鱼</div>"
                "<div class='pet'>我在这儿。今天想聊点什么？你也可以直接告诉我喜欢什么、"
                "最近在忙什么，我会记在本机。</div>"
            )
        entries = [
            (item.created_at, 0, item)
            for item in self.messages
        ] + [
            (item.created_at, 1, item)
            for item in self.interactions
        ]
        display_entries: list[tuple[int, object, int]] = []
        event_bucket: list[Interaction] = []

        def flush_events() -> None:
            if not event_bucket:
                return
            grouped: dict[tuple[str, str], list[object]] = {}
            for event in event_bucket:
                key = (event.kind, event.summary)
                if key not in grouped:
                    grouped[key] = [event, 1]
                else:
                    grouped[key][1] = int(grouped[key][1]) + 1
            for event, count in grouped.values():
                display_entries.append((1, event, int(count)))
            event_bucket.clear()

        for _created, entry_type, item in sorted(
            entries,
            key=lambda row: (row[0], row[1]),
        ):
            if entry_type == 1:
                event_bucket.append(item)  # type: ignore[arg-type]
            else:
                flush_events()
                display_entries.append((0, item, 1))
        flush_events()

        for entry_type, item, repeat_count in display_entries:
            if entry_type == 1:
                event = item  # type: ignore[assignment]
                text = html.escape(event.summary)
                if event.kind == "qq_reply":
                    text += f"（{repeat_count} 条消息）"
                elif repeat_count > 1:
                    text += f" ×{repeat_count}"
                parts.append(f"<div class='event'>{text}</div>")
                continue
            message = item  # type: ignore[assignment]
            text = html.escape(message.content).replace("\n", "<br>")
            label = "你" if message.role == "user" else "大肥鱼"
            css = "user" if message.role == "user" else "pet"
            attachment = ""
            if message.attachment_path:
                path = resolve_app_path(message.attachment_path)
                if path.is_file():
                    uri = html.escape(path.resolve().as_uri(), quote=True)
                    attachment = (
                        f"<br><img src='{uri}' width='180'>"
                    )
            parts.append(
                f"<div class='meta'>{label}</div>"
                f"<div class='{css}'>{text}{attachment}</div>"
            )
        self.transcript.setHtml("".join(parts))
        scrollbar = self.transcript.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def refresh_status(self) -> None:
        config = self.config_store.load()
        state = "已配置 API" if config.api_key else "未配置 API"
        self.status.setText(
            f"Agent {PERSONA_VERSION} · v{APP_VERSION} · {state}"
        )
        self.status.setToolTip(f"模型：{config.model}")

    def new_chat(self) -> None:
        if self.pending or self.image_busy:
            QMessageBox.information(self, "正在回复", "等这一句回复完成后再开新对话。")
            return
        self.conversation_id = self.database.create_conversation()
        self.messages = []
        self.interactions = []
        self._render()
        self.input.setFocus()

    def open_sticker_folder(self) -> None:
        folder = self.controller.sticker_library.ensure_folder()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def choose_image(self) -> None:
        if self.pending or self.image_busy:
            return
        config = self.config_store.load()
        if not config.api_key:
            QMessageBox.warning(
                self,
                "先配置 API",
                "图片文字识别后仍需 DeepSeek 文本接口来理解内容。",
            )
            self.controller.open_settings(self)
            return
        filename, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "选择包含文字的图片",
            "",
            "图片 (*.png *.jpg *.jpeg *.bmp *.webp)",
        )
        if not filename:
            return
        path = Path(filename)
        self.image_busy = True
        self.image_button.setEnabled(False)
        self.image_button.setText("识图中…")
        self.status.setText("本地 OCR 正在识别图片文字…")

        self.controller.start_task(
            lambda: extract_image_text(path),
            lambda text: self._image_ready(path, text),
            self._image_failed,
        )

    def _image_ready(self, path: Path, text: object) -> None:
        self.image_busy = False
        self.image_button.setEnabled(True)
        self.image_button.setText("图片")
        self.refresh_status()
        ocr_text = str(text or "").strip()
        if not ocr_text:
            QMessageBox.information(self, "没有识别到文字", "图片中没有可读文字。")
            return
        config = self.config_store.load()
        if not config.api_key:
            self.controller.open_settings(self)
            return
        message = (
            f"我发送了一张图片「{path.name}」。"
            "以下是通过 Windows 本地 OCR 从图片中提取出的文字：\n"
            f"{ocr_text}"
        )
        try:
            prepare_user_turn(
                self.database,
                self.conversation_id,
                message,
                attachment_path=str(path),
            )
        except Exception as exc:
            QMessageBox.critical(self, "保存图片失败", str(exc))
            return
        self.messages = self.database.recent_messages(
            self.conversation_id,
            limit=60,
        )
        self._render()
        self._begin_request(config)

    def _image_failed(self, message: str) -> None:
        self.image_busy = False
        self.image_button.setEnabled(True)
        self.image_button.setText("图片")
        self.refresh_status()
        QMessageBox.warning(self, "图片识别失败", message)

    def send(self) -> None:
        if self.pending or self.image_busy:
            return
        text = self.input.text().strip()
        if not text:
            return
        if len(text) > 12000:
            QMessageBox.warning(self, "消息太长", "单条消息请控制在 12000 字以内。")
            return
        command = execute_chat_command(text, self.controller)
        if command.handled:
            try:
                prepare_user_turn(self.database, self.conversation_id, text)
                store_assistant_turn(
                    self.database,
                    self.conversation_id,
                    command.message,
                )
                self.controller.record_interaction(
                    command.event_kind,
                    command.event_summary or command.message,
                )
            except Exception as exc:
                QMessageBox.critical(self, "保存失败", str(exc))
                return
            self.input.clear()
            self.messages = self.database.recent_messages(
                self.conversation_id,
                limit=60,
            )
            self.interactions = self.database.recent_interactions(
                self.conversation_id,
                limit=40,
            )
            self._render()
            self.controller.pet.say(command.message)
            self.refresh_status()
            return
        config = self.config_store.load()
        if not config.api_key:
            QMessageBox.warning(
                self,
                "先配置 API",
                "请先打开“设置”，填写 DeepSeek API 地址、模型和密钥。",
            )
            self.controller.open_settings(self)
            return
        try:
            prepare_user_turn(self.database, self.conversation_id, text)
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self.input.clear()
        self.messages = self.database.recent_messages(self.conversation_id, limit=60)
        self._render()
        self._begin_request(config)

    def _begin_request(self, config: AppConfig) -> None:
        self.pending = True
        self.send_button.setEnabled(False)
        self.image_button.setEnabled(False)
        self.send_button.setText("思考中…")
        self.status.setText(f"{config.model} · 正在思考…")
        conversation_id = self.conversation_id

        def work() -> tuple[int, str, Sticker | None, list[str]]:
            answer = request_assistant_turn(self.database, conversation_id, config)
            segments = split_chat_reply(answer) or [answer]
            sticker = self.controller.sticker_selector.consider(answer)
            sticker_path = ""
            if sticker is not None:
                sticker_path = (
                    self.controller.sticker_library.attachment_key(sticker)
                )
            for index, segment in enumerate(segments):
                store_assistant_turn(
                    self.database,
                    conversation_id,
                    segment,
                    attachment_path=(
                        sticker_path if index == len(segments) - 1 else ""
                    ),
                )
            return conversation_id, answer, sticker, segments

        self.controller.start_task(work, self._reply_done, self._request_failed)

    def _reply_done(self, result: object) -> None:
        conversation_id, answer, sticker, segments = result  # type: ignore[misc]
        self.pending = False
        self.send_button.setEnabled(True)
        self.image_button.setEnabled(True)
        self.send_button.setText("发送")
        if sticker is not None:
            self.controller.record_interaction(
                "sticker_sent",
                f"发送了表情包「{sticker.label}」",
            )
        if conversation_id == self.conversation_id:
            self.messages = self.database.recent_messages(self.conversation_id, limit=60)
            self.interactions = self.database.recent_interactions(
                self.conversation_id,
                limit=40,
            )
            self._render()
        self.refresh_status()
        try:
            spoken = " ".join(segments)
            self.controller.pet.say(
                spoken[:140] + ("…" if len(spoken) > 140 else "")
            )
        except Exception:
            pass
        config = self.config_store.load()
        if should_extract_memory(self.database, config):
            self._start_memory_extraction(config)

    def _request_failed(self, message: str) -> None:
        self.pending = False
        self.send_button.setEnabled(True)
        self.image_button.setEnabled(True)
        self.send_button.setText("发送")
        self.refresh_status()
        QMessageBox.warning(self, "聊天请求失败", message)

    def _start_memory_extraction(self, config: AppConfig) -> None:
        if self.memory_busy:
            return
        self.memory_busy = True

        def done(_count: object = None) -> None:
            self.memory_busy = False

        def failed(message: str) -> None:
            self.memory_busy = False
            logging.info("Memory extraction skipped: %s", message)

        self.controller.start_task(
            lambda: extract_and_store_memories(self.database, config),
            done,
            failed,
        )

    def reload_interactions(self) -> None:
        self.interactions = self.database.recent_interactions(
            self.conversation_id,
            limit=40,
        )
        self._render()


class QQBridgeDialog(QDialog):
    def __init__(
        self,
        controller: IntegratedChatController,
        parent: QWidget | None = None,
    ):
        super().__init__(None)
        self.controller = controller
        self.settings = controller.qq_settings_store.load()
        self.setWindowTitle("NapCat QQ 消息助手")
        self.setWindowFlag(Qt.Window, True)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, False)
        self.setWindowModality(Qt.NonModal)
        self.setAttribute(Qt.WA_ShowWithoutActivating, False)
        self.resize(860, 700)
        self.setStyleSheet(_stylesheet())
        self._build()
        self._load_settings()
        self.refresh_drafts()
        self.refresh_initiatives()
        self.refresh_profiles()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        title = QLabel("NapCat QQ 消息助手")
        title.setStyleSheet("font-size:17px;font-weight:700;color:#27395f;")
        layout.addWidget(title)
        warning = QLabel(
            "通过本地 NapCat OneBot WebSocket 接收私聊和群聊 @ 消息并直接调用"
            "发送接口，不再操作 QQ 窗口、鼠标或键盘。项目内包含独立的"
            " QQ 9.9.33 与 NapCat 运行目录，不再修改系统中已安装的 QQ。"
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(
            "background:#fff6df;color:#725322;border:1px solid #ead49c;"
            "padding:9px;border-radius:6px;"
        )
        layout.addWidget(warning)
        self.api_status = QLabel()
        self.api_status.setWordWrap(True)
        layout.addWidget(self.api_status)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        reply_tab = QWidget()
        reply_layout = QVBoxLayout(reply_tab)
        reply_layout.setContentsMargins(10, 10, 10, 10)
        form = QFormLayout()
        self.enabled = QCheckBox("启用 NapCat 消息助手")
        self.napcat_auto_start = QCheckBox("桌宠启动时自动启动 NapCat")
        self.napcat_url = QLineEdit()
        self.napcat_url.setPlaceholderText("ws://127.0.0.1:3001")
        self.napcat_token = QLineEdit()
        self.napcat_token.setEchoMode(QLineEdit.Password)
        self.napcat_token.setPlaceholderText("本地 WebSocket Token，可留空")
        self.private_enabled = QCheckBox("监听私聊")
        self.group_enabled = QCheckBox("监听群聊中被 @ 的消息")
        self.auto_send = QCheckBox("自动发送 AI 回复（仅建议测试账号）")
        self.interval = QSpinBox()
        self.interval.setRange(2, 60)
        self.own_names = QPlainTextEdit()
        self.own_names.setMaximumHeight(65)
        self.own_names.setPlaceholderText(
            "填写你的 QQ 昵称或群昵称，每行一个；用于识别群聊中的 @你"
        )
        form.addRow("状态", self.enabled)
        form.addRow("NapCat WebSocket", self.napcat_url)
        form.addRow("访问 Token", self.napcat_token)
        form.addRow("", self.napcat_auto_start)
        form.addRow("消息范围", self.private_enabled)
        form.addRow("", self.group_enabled)
        form.addRow("", self.auto_send)
        form.addRow("你的 @ 名称", self.own_names)
        reply_layout.addLayout(form)

        self.status = QLabel("NapCat 尚未连接")
        self.status.setWordWrap(True)
        self.status.setObjectName("muted")
        reply_layout.addWidget(self.status)

        drafts_label = QLabel("最近 QQ 回复")
        drafts_label.setStyleSheet("font-weight:600;color:#27395f;")
        reply_layout.addWidget(drafts_label)
        self.drafts = QTableWidget(0, 4)
        self.drafts.setHorizontalHeaderLabels(["会话", "收到的消息", "AI 草稿", "状态"])
        self.drafts.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.drafts.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.drafts.verticalHeader().setVisible(False)
        self.drafts.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.drafts.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        reply_layout.addWidget(self.drafts, 1)
        self.tabs.addTab(reply_tab, "消息回复")

        random_tab = QWidget()
        random_layout = QVBoxLayout(random_tab)
        random_layout.setContentsMargins(10, 10, 10, 10)
        random_note = QLabel(
            "每隔指定时间抽取一次。总体触发概率为 100% 时，按下方权重直接抽取；"
            "降低总体概率会减少所有主动行为。发送权限跟随“消息回复”页的自动发送开关，"
            "关闭时群聊和好友事件只生成草稿；QQ 空间始终只生成草稿。"
        )
        random_note.setWordWrap(True)
        random_note.setStyleSheet(
            "background:#eef4ff;color:#344d75;border:1px solid #c8d7ee;"
            "padding:9px;border-radius:6px;"
        )
        random_layout.addWidget(random_note)

        random_form = QFormLayout()
        self.random_enabled = QCheckBox("启用定时随机主动事件")
        self.random_interval = QSpinBox()
        self.random_interval.setRange(1, 1440)
        self.random_interval.setSuffix(" 分钟")
        self.random_trigger = QSpinBox()
        self.random_trigger.setRange(0, 100)
        self.random_trigger.setSuffix("%")
        self.random_no_action = QSpinBox()
        self.random_no_action.setRange(0, 100)
        self.random_no_action.setSuffix("%")
        self.random_group = QSpinBox()
        self.random_group.setRange(0, 100)
        self.random_group.setSuffix("%")
        self.random_friend = QSpinBox()
        self.random_friend.setRange(0, 100)
        self.random_friend.setSuffix("%")
        self.random_qzone = QSpinBox()
        self.random_qzone.setRange(0, 100)
        self.random_qzone.setSuffix("%")
        self.random_group_enabled = QCheckBox("启用群聊话题事件")
        self.random_friend_enabled = QCheckBox("启用好友问候/续聊事件")
        self.random_qzone_enabled = QCheckBox("启用 QQ 空间草稿事件")
        random_form.addRow("总开关", self.random_enabled)
        random_form.addRow("触发间隔", self.random_interval)
        random_form.addRow("总体触发概率", self.random_trigger)
        random_form.addRow("空动作权重", self.random_no_action)
        random_form.addRow("群聊话题权重", self.random_group)
        random_form.addRow("好友问候权重", self.random_friend)
        random_form.addRow("QQ 空间权重", self.random_qzone)
        random_form.addRow("事件开关", self.random_group_enabled)
        random_form.addRow("", self.random_friend_enabled)
        random_form.addRow("", self.random_qzone_enabled)
        random_layout.addLayout(random_form)

        self.random_status = QLabel("随机主动事件尚未运行")
        self.random_status.setWordWrap(True)
        self.random_status.setObjectName("muted")
        random_layout.addWidget(self.random_status)

        initiatives_label = QLabel("最近随机行为与草稿")
        initiatives_label.setStyleSheet("font-weight:600;color:#27395f;")
        random_layout.addWidget(initiatives_label)
        self.initiatives = QTableWidget(0, 5)
        self.initiatives.setHorizontalHeaderLabels(
            ["类型", "目标", "成员/依据", "内容", "状态"]
        )
        self.initiatives.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.initiatives.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.initiatives.verticalHeader().setVisible(False)
        self.initiatives.horizontalHeader().setSectionResizeMode(
            3,
            QHeaderView.Stretch,
        )
        random_layout.addWidget(self.initiatives, 1)
        self.tabs.addTab(random_tab, "随机主动事件")

        profiles_tab = QWidget()
        profiles_layout = QVBoxLayout(profiles_tab)
        profiles_layout.setContentsMargins(10, 10, 10, 10)
        profiles_note = QLabel(
            "只为私聊联系人和在群聊中实际 @ 过大肥鱼、且能识别出发言者的成员建档。"
            "画像只记录可见聊天内容和简单互动次数，不做敏感属性推测。"
        )
        profiles_note.setWordWrap(True)
        profiles_note.setObjectName("muted")
        profiles_layout.addWidget(profiles_note)
        self.profiles = QTableWidget(0, 6)
        self.profiles.setHorizontalHeaderLabels(
            ["对象", "场景", "互动次数", "@ 次数", "简单画像", "最近出现"]
        )
        self.profiles.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.profiles.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.profiles.verticalHeader().setVisible(False)
        self.profiles.horizontalHeader().setSectionResizeMode(
            4,
            QHeaderView.Stretch,
        )
        profiles_layout.addWidget(self.profiles, 1)
        self.tabs.addTab(profiles_tab, "聊天对象画像")

        buttons = QHBoxLayout()
        save = QPushButton("保存设置")
        save.clicked.connect(self.save)
        scan = QPushButton("检查新消息")
        scan.setObjectName("primary")
        scan.clicked.connect(self.scan_now)
        start_napcat = QPushButton("启动 NapCat")
        start_napcat.clicked.connect(self.start_napcat)
        webui = QPushButton("打开 WebUI")
        webui.clicked.connect(self.open_webui)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        api_button = QPushButton("设置 AI API")
        api_button.clicked.connect(self._open_api_settings)
        buttons.addWidget(save)
        buttons.addWidget(scan)
        buttons.addWidget(start_napcat)
        buttons.addWidget(webui)
        buttons.addWidget(api_button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def _load_settings(self) -> None:
        self.enabled.setChecked(self.settings.enabled)
        self.napcat_url.setText(self.settings.napcat_url)
        self.napcat_token.setText(self.settings.napcat_token)
        self.napcat_auto_start.setChecked(self.settings.napcat_auto_start)
        self.private_enabled.setChecked(self.settings.scan_private)
        self.group_enabled.setChecked(self.settings.scan_group)
        self.auto_send.setChecked(self.settings.auto_send)
        self.interval.setValue(self.settings.poll_seconds)
        self.random_enabled.setChecked(self.settings.random_events_enabled)
        self.random_interval.setValue(
            self.settings.random_event_interval_minutes
        )
        self.random_trigger.setValue(
            self.settings.random_event_trigger_percent
        )
        self.random_no_action.setValue(
            self.settings.random_no_action_percent
        )
        self.random_group.setValue(self.settings.random_group_percent)
        self.random_friend.setValue(self.settings.random_friend_percent)
        self.random_qzone.setValue(self.settings.random_qzone_percent)
        self.random_group_enabled.setChecked(
            self.settings.random_group_enabled
        )
        self.random_friend_enabled.setChecked(
            self.settings.random_friend_enabled
        )
        self.random_qzone_enabled.setChecked(
            self.settings.random_qzone_enabled
        )
        self.own_names.setPlainText("\n".join(self.settings.own_names))
        self._refresh_api_state()
        if self.controller.qq_last_result is not None:
            self.set_result(self.controller.qq_last_result)
        if self.controller.random_last_result is not None:
            self.set_initiative_result(self.controller.random_last_result)
        if self.controller.qq_last_result is None:
            self.status.setText(self.controller.qq_monitor.connection_status())

    def _refresh_api_state(self) -> None:
        config = self.controller.config_store.load()
        if config.api_key:
            self.api_status.setText(f"AI API：已配置 · {config.model}")
            self.api_status.setStyleSheet("color:#37624a;")
        else:
            self.api_status.setText(
                "AI API：未配置。QQ 消息可以读取和记录，但无法生成 AI 草稿。"
            )
            self.api_status.setStyleSheet("color:#a33b3b;font-weight:600;")

    def _open_api_settings(self) -> None:
        self.controller.open_settings(self)
        self._refresh_api_state()

    def refresh_settings(self) -> None:
        self.settings = self.controller.qq_settings_store.load()
        self._load_settings()

    def _collect_settings(self) -> QQSettings:
        names = [
            line.strip()
            for line in self.own_names.toPlainText().splitlines()
            if line.strip()
        ]
        return QQSettings(
            enabled=self.enabled.isChecked(),
            poll_seconds=self.interval.value(),
            scan_private=self.private_enabled.isChecked(),
            scan_group=self.group_enabled.isChecked(),
            restore_window=False,
            own_names=names[:10],
            dry_run=not self.auto_send.isChecked(),
            auto_send=self.auto_send.isChecked(),
            random_events_enabled=self.random_enabled.isChecked(),
            random_event_interval_minutes=self.random_interval.value(),
            random_event_trigger_percent=self.random_trigger.value(),
            random_no_action_percent=self.random_no_action.value(),
            random_group_percent=self.random_group.value(),
            random_friend_percent=self.random_friend.value(),
            random_qzone_percent=self.random_qzone.value(),
            random_group_enabled=self.random_group_enabled.isChecked(),
            random_friend_enabled=self.random_friend_enabled.isChecked(),
            random_qzone_enabled=self.random_qzone_enabled.isChecked(),
            napcat_url=self.napcat_url.text().strip()
            or "ws://127.0.0.1:3001",
            napcat_token=self.napcat_token.text().strip(),
            napcat_auto_start=self.napcat_auto_start.isChecked(),
        )

    def save(self) -> None:
        if self.auto_send.isChecked():
            answer = QMessageBox.warning(
                self,
                "启用自动发送",
                "启用后，符合条件的私聊和群聊 @ 消息会由 QQ 账号直接回复联系人。"
                "仅建议在测试账号上使用。确定启用吗？",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                self.auto_send.setChecked(False)
                return
        try:
            settings = self._collect_settings()
            self.controller.qq_settings_store.save(settings)
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self.settings = settings
        self.controller.apply_qq_settings(settings)
        self.status.setText(
            (
                "NapCat 消息助手已启用，正在连接 "
                f"{settings.napcat_url}"
            )
            if settings.enabled
            else "NapCat 消息助手已停止"
        )
        self.random_status.setText(
            (
                f"随机主动事件已启用，每 {settings.random_event_interval_minutes} "
                "分钟抽取一次"
            )
            if settings.random_events_enabled
            else "随机主动事件已关闭"
        )

    def scan_now(self) -> None:
        self.save()
        if self.settings.enabled:
            if not self.controller.config_store.load().api_key:
                QMessageBox.warning(
                    self,
                    "AI API 未配置",
                    "请先点击“设置 AI API”填写密钥；当前只能读取 QQ，无法生成草稿。",
                )
            self.status.setText("正在检查 NapCat 事件…")
            self.controller.scan_qq_now(force=True)

    def start_napcat(self) -> None:
        self.save()
        try:
            message = self.controller.start_napcat()
        except Exception as exc:
            QMessageBox.critical(self, "NapCat 启动失败", str(exc))
            return
        self.status.setText(message)
        QTimer.singleShot(8000, self.open_webui)

    def open_webui(self) -> None:
        QDesktopServices.openUrl(QUrl(self.controller.napcat_webui_url()))

    def set_result(self, result: QQScanResult) -> None:
        extra = (
            f"；其他会话有 {len(result.changed_sessions)} 处新动态"
            if result.changed_sessions
            else ""
        )
        self.status.setText(result.status + extra)
        self.refresh_drafts()
        self._refresh_api_state()
        self.refresh_profiles()

    def set_initiative_result(self, result: QQInitiativeResult) -> None:
        self.random_status.setText(result.summary)
        self.refresh_initiatives()
        self.refresh_profiles()

    def set_random_error(self, message: str) -> None:
        self.random_status.setText(f"随机主动事件失败：{message}")

    def set_error(self, message: str) -> None:
        self.status.setText(f"监听失败：{message}")

    def refresh_drafts(self) -> None:
        drafts = self.controller.database.recent_qq_drafts(limit=30)
        self.drafts.setRowCount(len(drafts))
        for row, draft in enumerate(drafts):
            status_labels = {
                "draft": "仅草稿",
                "sent": "已发送",
                "send-failed": "发送失败",
                "blocked-cooldown": "冷却中",
                "blocked-not-incoming": "非对方消息，未发送",
            }
            values = (
                draft.chat_title,
                draft.incoming,
                draft.content.replace("\n", " ｜ "),
                status_labels.get(draft.status, draft.status),
            )
            for column, value in enumerate(values):
                self.drafts.setItem(row, column, QTableWidgetItem(value))
            self.drafts.setRowHeight(row, 66)

    def refresh_initiatives(self) -> None:
        items = self.controller.database.recent_qq_initiatives(limit=30)
        self.initiatives.setRowCount(len(items))
        type_labels = {
            "group_topic": "群聊话题",
            "friend_greeting": "好友私聊",
            "qzone_post": "QQ 空间",
        }
        status_labels = {
            "draft": "仅草稿",
            "draft-only": "仅草稿",
            "sent": "已发送",
            "send-failed": "发送失败",
        }
        for row, item in enumerate(items):
            target = item.target_title or (
                "QQ 空间" if item.event_type == "qzone_post" else ""
            )
            values = (
                type_labels.get(item.event_type, item.event_type),
                target,
                item.member_name,
                item.content,
                status_labels.get(item.status, item.status),
            )
            for column, value in enumerate(values):
                self.initiatives.setItem(
                    row,
                    column,
                    QTableWidgetItem(value),
                )
            self.initiatives.setRowHeight(row, 58)

    def refresh_profiles(self) -> None:
        profiles = self.controller.database.recent_qq_user_profiles(limit=200)
        self.profiles.setRowCount(len(profiles))
        for row, profile in enumerate(profiles):
            relation = (
                "群聊" if profile.is_group else "私聊"
            )
            values = (
                profile.display_name,
                relation,
                str(profile.message_count),
                str(profile.mention_count),
                profile.profile_summary,
                profile.last_seen[:16],
            )
            for column, value in enumerate(values):
                self.profiles.setItem(
                    row,
                    column,
                    QTableWidgetItem(value),
                )
            self.profiles.setRowHeight(row, 44)


class SettingsDialog(QDialog):
    def __init__(self, controller: IntegratedChatController, parent: QWidget | None = None):
        super().__init__(parent)
        self.controller = controller
        self.config = controller.config_store.load()
        self.setWindowTitle("AI 聊天设置")
        self.setMinimumWidth(570)
        self.setStyleSheet(_stylesheet())
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        heading = QLabel("DeepSeek / OpenAI 兼容接口")
        heading.setStyleSheet("font-size:17px;font-weight:700;color:#27395f;")
        layout.addWidget(heading)
        note = QLabel(
            "API 密钥使用 Windows DPAPI 加密。聊天时会把自动整理的画像、"
            "相关长期记忆、最近事件和最近对话发送给所配置的 API。"
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)

        form = QFormLayout()
        self.api_url = QLineEdit(self.config.api_url)
        self.model = QLineEdit(self.config.model)
        self.api_key = QLineEdit(self.config.api_key)
        self.api_key.setEchoMode(QLineEdit.Password)
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0, 2)
        self.temperature.setSingleStep(0.1)
        self.temperature.setValue(self.config.temperature)
        self.max_tokens = QSpinBox()
        self.max_tokens.setRange(64, 8192)
        self.max_tokens.setValue(self.config.max_output_tokens)
        self.timeout = QSpinBox()
        self.timeout.setRange(10, 300)
        self.timeout.setValue(self.config.request_timeout)
        self.recent_messages = QSpinBox()
        self.recent_messages.setRange(2, 60)
        self.recent_messages.setValue(self.config.recent_messages)
        self.prompt_memories = QSpinBox()
        self.prompt_memories.setRange(0, 50)
        self.prompt_memories.setValue(self.config.memories_in_prompt)
        self.memory_interval = QSpinBox()
        self.memory_interval.setRange(2, 100)
        self.memory_interval.setValue(self.config.memory_interval)
        self.auto_memory = QCheckBox("定期让模型整理稳定画像和长期记忆（会额外调用 API）")
        self.auto_memory.setChecked(self.config.auto_memory)

        form.addRow("API 地址", self.api_url)
        form.addRow("模型", self.model)
        form.addRow("API 密钥", self.api_key)
        form.addRow("温度", self.temperature)
        form.addRow("最大回复 Token", self.max_tokens)
        form.addRow("请求超时（秒）", self.timeout)
        form.addRow("最近上下文消息数", self.recent_messages)
        form.addRow("每轮携带记忆数", self.prompt_memories)
        form.addRow("整理记忆间隔", self.memory_interval)
        form.addRow("", self.auto_memory)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        clear = QPushButton("清空长期记忆")
        clear.clicked.connect(self._clear_memories)
        buttons.addWidget(clear)
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def save(self) -> None:
        url = self.api_url.text().strip()
        if not url.lower().startswith(("http://", "https://")):
            QMessageBox.warning(self, "地址无效", "API 地址应以 http:// 或 https:// 开头。")
            return
        updated = AppConfig(
            api_url=url,
            model=self.model.text().strip() or "deepseek-chat",
            api_key=self.api_key.text().strip(),
            temperature=self.temperature.value(),
            max_output_tokens=self.max_tokens.value(),
            request_timeout=self.timeout.value(),
            recent_messages=self.recent_messages.value(),
            memories_in_prompt=self.prompt_memories.value(),
            memory_interval=self.memory_interval.value(),
            auto_memory=self.auto_memory.isChecked(),
        )
        try:
            self.controller.config_store.save(updated)
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        if self.controller.chat_dialog is not None:
            self.controller.chat_dialog.refresh_status()
        self.accept()

    def _clear_memories(self) -> None:
        if QMessageBox.question(
            self,
            "清空长期记忆",
            "确定删除全部长期记忆吗？原始聊天记录和自动整理的数据不会被删除。",
        ) == QMessageBox.Yes:
            self.controller.database.clear_memories()
            QMessageBox.information(self, "已清空", "长期记忆已删除。")


class MemoryDialog(QDialog):
    def __init__(self, controller: IntegratedChatController, parent: QWidget | None = None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("AI 记忆与关键对话")
        self.resize(850, 600)
        self.setStyleSheet(_stylesheet())
        self._build()
        self.refresh()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        title = QLabel("本地记忆")
        title.setStyleSheet("font-size:17px;font-weight:700;color:#27395f;")
        layout.addWidget(title)
        note = QLabel("数据仅保存在本机 SQLite。删除长期记忆不会删除原始聊天记录。")
        note.setObjectName("muted")
        layout.addWidget(note)
        self.tabs = QTabWidget()
        self.memory_table = QTableWidget(0, 4)
        self.memory_table.setHorizontalHeaderLabels(["类型", "内容", "重要度", "更新时间"])
        self.memory_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.memory_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.memory_table.verticalHeader().setVisible(False)
        self.memory_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tabs.addTab(self.memory_table, "长期记忆")
        self.key_table = QTableWidget(0, 3)
        self.key_table.setHorizontalHeaderLabels(["角色", "对话", "时间"])
        self.key_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.key_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.key_table.verticalHeader().setVisible(False)
        self.key_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tabs.addTab(self.key_table, "关键对话")
        self.event_table = QTableWidget(0, 7)
        self.event_table.setHorizontalHeaderLabels(
            ["ID", "时间", "范围", "事件", "详情", "重要度", "压缩"]
        )
        self.event_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.event_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.event_table.verticalHeader().setVisible(False)
        self.event_table.horizontalHeader().setSectionResizeMode(
            4,
            QHeaderView.Stretch,
        )
        self.tabs.addTab(self.event_table, "事件记忆")
        self.summary_table = QTableWidget(0, 5)
        self.summary_table.setHorizontalHeaderLabels(
            ["日期", "范围", "聊天对象", "精简摘要", "原事件索引"]
        )
        self.summary_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.summary_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.summary_table.verticalHeader().setVisible(False)
        self.summary_table.horizontalHeader().setSectionResizeMode(
            3,
            QHeaderView.Stretch,
        )
        self.tabs.addTab(self.summary_table, "旧事件摘要")
        persona_tab = QWidget()
        persona_layout = QVBoxLayout(persona_tab)
        self.persona_note = QLabel("大肥鱼成长档案尚未建立")
        self.persona_note.setWordWrap(True)
        self.persona_note.setObjectName("muted")
        persona_layout.addWidget(self.persona_note)
        self.persona_table = QTableWidget(0, 4)
        self.persona_table.setHorizontalHeaderLabels(
            ["类别", "新增内容", "置信度", "学习时间"]
        )
        self.persona_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.persona_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.persona_table.verticalHeader().setVisible(False)
        self.persona_table.horizontalHeader().setSectionResizeMode(
            1,
            QHeaderView.Stretch,
        )
        persona_layout.addWidget(self.persona_table, 1)
        self.tabs.addTab(persona_tab, "大肥鱼成长")
        layout.addWidget(self.tabs, 1)
        buttons = QHBoxLayout()
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        remove = QPushButton("删除所选记忆")
        remove.clicked.connect(self._delete)
        clear = QPushButton("清空长期记忆")
        clear.clicked.connect(self._clear)
        clear_persona = QPushButton("清空成长人设")
        clear_persona.clicked.connect(self._clear_persona)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        buttons.addWidget(refresh)
        buttons.addWidget(remove)
        buttons.addWidget(clear)
        buttons.addWidget(clear_persona)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def refresh(self) -> None:
        memories = self.controller.database.list_memories()
        self.memory_table.setRowCount(len(memories))
        for row, memory in enumerate(memories):
            values = (
                memory.kind,
                memory.value,
                str(memory.importance),
                memory.updated_at[:16],
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, memory.id)
                self.memory_table.setItem(row, column, item)
            self.memory_table.setRowHeight(row, 30)
        key_messages = self.controller.database.key_messages(limit=100)
        self.key_table.setRowCount(len(key_messages))
        for row, message in enumerate(key_messages):
            values = (
                "你" if message.role == "user" else "大肥鱼",
                message.content.replace("\n", " "),
                message.created_at[:16],
            )
            for column, value in enumerate(values):
                self.key_table.setItem(row, column, QTableWidgetItem(value))
            self.key_table.setRowHeight(row, 30)
        events = self.controller.database.list_event_memories(limit=300)
        self.event_table.setRowCount(len(events))
        for row, event in enumerate(events):
            values = (
                f"#{event.id}",
                event.occurred_at[:16],
                event.scope,
                event.summary,
                event.details.replace("\n", " "),
                str(event.importance),
                "是" if event.compacted else "否",
            )
            for column, value in enumerate(values):
                self.event_table.setItem(row, column, QTableWidgetItem(value))
            self.event_table.setRowHeight(row, 34)
        summaries = self.controller.database.list_event_summaries(limit=200)
        self.summary_table.setRowCount(len(summaries))
        for row, summary in enumerate(summaries):
            values = (
                summary.period_start[:10],
                summary.scope,
                summary.related_user_key,
                summary.summary.replace("\n", " ｜ "),
                ",".join(f"#{value}" for value in summary.event_ids),
            )
            for column, value in enumerate(values):
                self.summary_table.setItem(
                    row,
                    column,
                    QTableWidgetItem(value),
                )
            self.summary_table.setRowHeight(row, 52)
        pet_persona = self.controller.database.get_pet_persona()
        self.persona_note.setText(
            f"成长档案修订 {pet_persona.revision} 次 · "
            f"最后更新 {pet_persona.updated_at[:16]}\n"
            f"{pet_persona.prompt_text()}"
        )
        learnings = self.controller.database.list_pet_persona_learnings(
            limit=300
        )
        labels = {
            "traits": "性格",
            "preferences": "偏好",
            "knowledge": "知识",
            "habits": "习惯",
            "relationships": "关系",
        }
        self.persona_table.setRowCount(len(learnings))
        for row, learning in enumerate(learnings):
            values = (
                labels.get(learning.category, learning.category),
                learning.content,
                str(learning.confidence),
                learning.created_at[:16],
            )
            for column, value in enumerate(values):
                self.persona_table.setItem(
                    row,
                    column,
                    QTableWidgetItem(value),
                )
            self.persona_table.setRowHeight(row, 32)

    def _delete(self) -> None:
        row = self.memory_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "未选择", "请先选择一条长期记忆。")
            return
        item = self.memory_table.item(row, 0)
        memory_id = int(item.data(Qt.UserRole))
        if QMessageBox.question(self, "删除记忆", "确定删除所选记忆吗？") != QMessageBox.Yes:
            return
        self.controller.database.delete_memory(memory_id)
        self.refresh()

    def _clear(self) -> None:
        if QMessageBox.question(
            self,
            "清空长期记忆",
            "确定删除全部长期记忆吗？此操作不可撤销，原始聊天记录会保留。",
        ) != QMessageBox.Yes:
            return
        self.controller.database.clear_memories()
        self.refresh()

    def _clear_persona(self) -> None:
        if QMessageBox.question(
            self,
            "清空成长人设",
            "确定删除大肥鱼后天学到的全部人设、习惯和知识吗？"
            "固定的 Agent 2.0.0 核心人设不会受影响。",
        ) != QMessageBox.Yes:
            return
        self.controller.database.clear_pet_persona_learnings()
        self.refresh()


__all__ = [
    "ChatDialog",
    "IntegratedChatController",
    "MemoryDialog",
    "SettingsDialog",
]
