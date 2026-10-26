from __future__ import annotations

import logging
import os
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication

from .config import ConfigStore, initialize_first_run_preferences
from .database import Database
from .paths import data_dir, log_path, resource_dir
from .qt_ui import IntegratedChatController


_INSTALLED = False


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            filename=log_path(),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            encoding="utf-8",
        )


def _patch_pet_window(pet_class: type) -> None:
    if getattr(pet_class, "_dafeiyu_ai_patched", False):
        return
    original_init = pet_class.__init__
    original_build_menu = pet_class._build_menu
    original_build_quick_menu = getattr(pet_class, "_build_quick_menu", None)
    original_single_click = getattr(pet_class, "_on_single_click", None)
    original_double_click = getattr(pet_class, "_on_double_click", None)
    original_mouse_release = getattr(pet_class, "mouseReleaseEvent", None)

    def integrated_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        original_config_path = data_dir().parent / "config.json"
        first_run = not original_config_path.exists()
        original_init(self, *args, **kwargs)
        if first_run:
            try:
                if initialize_first_run_preferences(
                    original_config_path,
                    getattr(self, "cfg", None),
                    first_run=first_run,
                ):
                    logging.info(
                        "Initialized defaults: Beijing weather, small pet size"
                    )
            except Exception:
                logging.exception("Could not initialize Beijing weather location")
        self._dafeiyu_config_store = ConfigStore()
        self._dafeiyu_chat_database = Database()
        chat_config = self._dafeiyu_config_store.load()
        legacy_key = str(getattr(self, "cfg", {}).get("ds_api_key", "") or "").strip()
        if not chat_config.api_key and legacy_key:
            chat_config.api_key = legacy_key
            try:
                self._dafeiyu_config_store.save(chat_config)
            except Exception:
                logging.exception("Could not migrate the original DeepSeek key")
        self._dafeiyu_chat_controller = IntegratedChatController(self)
        self._dafeiyu_chat_shortcut = QShortcut(QKeySequence("Ctrl+Alt+C"), self)
        self._dafeiyu_chat_shortcut.setContext(Qt.ApplicationShortcut)
        self._dafeiyu_chat_shortcut.activated.connect(
            self._dafeiyu_chat_controller.open_chat
        )
        if os.environ.get("DAFEIYU_OPEN_CHAT") == "1":
            QTimer.singleShot(700, self._dafeiyu_chat_controller.open_chat)
        if os.environ.get("DAFEIYU_OPEN_QQ") == "1":
            QTimer.singleShot(900, self._dafeiyu_chat_controller.open_qq)
        if os.environ.get("DAFEIYU_QA_QQ_FORCE_SCAN") == "1":
            QTimer.singleShot(
                12000,
                lambda: self._dafeiyu_chat_controller.scan_qq_now(force=True),
            )
        if os.environ.get("DAFEIYU_QA_MENU") == "1":
            QTimer.singleShot(900, lambda: _log_menu_tree(self))
        if os.environ.get("DAFEIYU_QA_QUICK_MENU") == "1":
            QTimer.singleShot(900, lambda: _log_quick_menu_tree(self))
        if os.environ.get("DAFEIYU_QA_TRIGGER_AI_MENU") == "1":
            QTimer.singleShot(1000, lambda: _trigger_ai_menu(self))
        if not self._dafeiyu_config_store.load().api_key:
            QTimer.singleShot(
                1600,
                lambda: _safe_chat_hint(self),
            )

    def add_ai_menu(self, menu):  # type: ignore[no-untyped-def]
        controller = getattr(self, "_dafeiyu_chat_controller", None)
        if controller is None:
            return menu
        ai_action = QAction("AI 聊天", menu)
        qq_action = QAction("QQ 自动回复", menu)
        icon_path = resource_dir() / "sprites" / "icon.png"
        if icon_path.exists():
            ai_action.setIcon(QIcon(str(icon_path)))
        ai_action.triggered.connect(lambda _checked=False: controller.open_chat())
        qq_action.triggered.connect(lambda _checked=False: controller.open_qq(self))

        actions = menu.actions()
        if actions:
            first = actions[0]
            menu.insertAction(first, ai_action)
            separator = menu.insertSeparator(first)
            menu.insertAction(separator, qq_action)
        else:
            menu.addAction(ai_action)
            menu.addAction(qq_action)
        _instrument_menu_actions(controller, menu)
        return menu

    def integrated_build_menu(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        menu = original_build_menu(self, *args, **kwargs)
        logging.info("AI chat submenu inserted into full menu")
        controller = getattr(self, "_dafeiyu_chat_controller", None)
        if controller is not None:
            controller.record_interaction(
                "context_menu",
                "打开了桌宠完整菜单",
            )
        return add_ai_menu(self, menu)

    def integrated_build_quick_menu(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        menu = original_build_quick_menu(self, *args, **kwargs)
        logging.info("AI chat submenu inserted into quick menu")
        controller = getattr(self, "_dafeiyu_chat_controller", None)
        if controller is not None:
            controller.record_interaction(
                "context_menu",
                "右键打开了桌宠菜单",
            )
        return add_ai_menu(self, menu)

    def integrated_single_click(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        result = original_single_click(self, *args, **kwargs)
        controller = getattr(self, "_dafeiyu_chat_controller", None)
        if controller is not None:
            controller.record_interaction("poke", "你戳了戳大肥鱼")
        return result

    def integrated_double_click(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        result = original_double_click(self, *args, **kwargs)
        controller = getattr(self, "_dafeiyu_chat_controller", None)
        if controller is not None:
            try:
                choice = self._double_click_choice()
            except Exception:
                choice = ""
            labels = {
                "balance": "双击查看了余额",
                "weather": "双击查看了天气",
                "music": "双击查看了正在播放的音乐",
            }
            controller.record_interaction(
                "double_poke",
                labels.get(choice, "双击逗了逗大肥鱼"),
            )
        return result

    def integrated_mouse_release(self, event, *args, **kwargs):  # type: ignore[no-untyped-def]
        was_dragging = bool(getattr(self, "dragging", False))
        result = original_mouse_release(self, event, *args, **kwargs)
        controller = getattr(self, "_dafeiyu_chat_controller", None)
        if controller is not None and was_dragging:
            controller.record_interaction(
                "drag",
                "把大肥鱼拖到了桌面上的新位置",
            )
        return result

    pet_class.__init__ = integrated_init
    pet_class._build_menu = integrated_build_menu
    if callable(original_build_quick_menu):
        pet_class._build_quick_menu = integrated_build_quick_menu
    if callable(original_single_click):
        pet_class._on_single_click = integrated_single_click
    if callable(original_double_click):
        pet_class._on_double_click = integrated_double_click
    if callable(original_mouse_release):
        pet_class.mouseReleaseEvent = integrated_mouse_release
    pet_class._dafeiyu_ai_patched = True


def _patch_main_window_class() -> None:
    module = sys.modules.get("_dafeiyu_original_main") or sys.modules.get("__main__")
    pet_class = getattr(module, "PetWindow", None)
    if pet_class is not None:
        _patch_pet_window(pet_class)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _configure_logging()
    logging.info("Installing integrated AI chat into original 大肥鱼桌宠")

    original_application = QApplication
    if getattr(original_application, "_dafeiyu_ai_application_patched", False):
        return

    class IntegratedApplication(original_application):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            super().__init__(*args, **kwargs)
            _patch_main_window_class()

    IntegratedApplication._dafeiyu_ai_application_patched = True
    IntegratedApplication.__name__ = "QApplication"
    IntegratedApplication.__qualname__ = "QApplication"

    # The original entry module imports QApplication only after this runtime hook.
    qt_widgets = sys.modules.get("PySide6.QtWidgets")
    if qt_widgets is not None:
        qt_widgets.QApplication = IntegratedApplication
    else:
        import PySide6.QtWidgets as qt_widgets_module

        qt_widgets_module.QApplication = IntegratedApplication


def _safe_chat_hint(self) -> None:  # type: ignore[no-untyped-def]
    try:
        self.say("AI 聊天已接入：右键菜单选“AI 聊天”，或按 Ctrl+Alt+C")
    except Exception:
        logging.exception("Could not show integrated chat hint")


def _instrument_menu_actions(controller, menu) -> None:  # type: ignore[no-untyped-def]
    for action in menu.actions():
        submenu = action.menu()
        if submenu is not None:
            _instrument_menu_actions(controller, submenu)
            continue
        label = action.text().replace("&", "").strip()
        if (
            not label
            or label in {"AI 聊天", "QQ 自动回复", "打开聊天…"}
            or action.isSeparator()
        ):
            continue

        def record(
            _checked: bool = False,
            _label: str = label,
        ) -> None:
            controller.record_interaction(
                "menu_action",
                f"从右键菜单执行了“{_label}”",
            )

        action.triggered.connect(record)


def _log_menu_tree(pet) -> None:  # type: ignore[no-untyped-def]
    try:
        menu = pet._build_menu()
        texts = []
        for action in menu.actions():
            texts.append(action.text() or "<separator>")
            submenu = action.menu()
            if submenu is not None:
                texts.extend(
                    f"{action.text()}/{child.text()}" for child in submenu.actions()
                )
        logging.info("QA original menu with AI entries: %s", texts)
    except Exception:
        logging.exception("QA menu inspection failed")


def _log_quick_menu_tree(pet) -> None:  # type: ignore[no-untyped-def]
    try:
        menu = pet._build_quick_menu()
        labels = []
        for action in menu.actions():
            labels.append(action.text() or "<separator>")
            submenu = action.menu()
            if submenu is not None:
                labels.extend(
                    f"{action.text()}/{child.text()}" for child in submenu.actions()
                )
        logging.info("QA quick menu with AI entries: %s", labels)
    except Exception:
        logging.exception("QA quick menu inspection failed")


def _trigger_ai_menu(pet) -> None:  # type: ignore[no-untyped-def]
    try:
        menu = pet._build_quick_menu()
        action = next(
            item for item in menu.actions() if item.text().replace("&", "") == "AI 聊天"
        )
        action.trigger()
        logging.info("QA triggered direct AI chat menu action")
    except Exception:
        logging.exception("QA direct AI menu trigger failed")


if __name__ == "__main__":
    install()
