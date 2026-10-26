from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class CommandResult:
    handled: bool
    message: str = ""
    event_kind: str = "setting_change"
    event_summary: str = ""


HELP_TEXT = (
    "我现在能直接调整：桌宠大小、透明度、音量、锁定位置、置顶、鼠标穿透、"
    "显示/隐藏；QQ 监听开关、自动回复开关、扫描间隔、私聊/群聊范围和 @ 昵称。"
    "也能调整定时随机主动事件的总开关、间隔、触发概率、三类事件开关和权重。"
    "你也可以让我打开设置、记忆或 QQ 消息助手。"
)


def _percent(text: str, words: tuple[str, ...]) -> int | None:
    if not any(word in text for word in words):
        return None
    match = re.search(r"(\d{1,3})\s*%", text)
    if not match:
        return None
    return max(0, min(int(match.group(1)), 100))


def _call(
    method: Any,
    *args: Any,
    message: str,
    event: str,
) -> CommandResult:
    try:
        method(*args)
    except Exception as exc:
        return CommandResult(
            True,
            f"这项调整没有成功：{exc}",
            event_kind="command_failed",
            event_summary=f"尝试调整失败：{event}",
        )
    return CommandResult(True, message, event_summary=event)


def execute_chat_command(text: str, controller: Any) -> CommandResult:
    value = text.strip()
    pet = controller.pet

    if re.search(r"(?:你|桌宠).{0,6}(?:能调|可以调|会调|能改|可以改|指令|帮助)", value):
        return CommandResult(True, HELP_TEXT, event_kind="command_help")

    if re.search(r"打开.{0,8}(?:QQ.{0,4}(?:助手|监听)|QQ消息助手)", value):
        controller.open_qq(controller.chat_dialog)
        return CommandResult(True, "已经打开 QQ 消息助手。", event_summary="打开了 QQ 消息助手")
    if "打开记忆" in value or "记忆管理" in value:
        controller.open_memory(controller.chat_dialog)
        return CommandResult(True, "已经打开记忆管理。", event_summary="打开了记忆管理")
    if "打开设置" in value or "API 设置" in value:
        controller.open_settings(controller.chat_dialog)
        return CommandResult(True, "已经打开 API 设置。", event_summary="打开了 API 设置")

    qq_settings = controller.qq_settings_store.load()
    random_changed = False
    random_message = ""
    if re.search(
        r"(?:关闭|停用|关掉).{0,4}(?:定时)?随机(?:主动)?(?:事件|行为)",
        value,
    ):
        qq_settings.random_events_enabled = False
        random_changed = True
        random_message = "已关闭定时随机主动事件。"
    elif re.search(
        r"(?:打开|启用|开启).{0,4}(?:定时)?随机(?:主动)?(?:事件|行为)",
        value,
    ):
        qq_settings.random_events_enabled = True
        random_changed = True
        random_message = "已启用定时随机主动事件。"
    interval_match = re.search(
        r"随机.{0,8}每\s*(\d{1,4})\s*分钟",
        value,
    )
    if interval_match:
        qq_settings.random_event_interval_minutes = max(
            1,
            min(int(interval_match.group(1)), 1440),
        )
        random_changed = True
        random_message = (
            f"随机事件触发间隔已改为 "
            f"{qq_settings.random_event_interval_minutes} 分钟。"
        )
    trigger_match = re.search(
        r"随机.{0,10}触发概率[^\d%]{0,10}(\d{1,3})\s*%",
        value,
    )
    if trigger_match:
        qq_settings.random_event_trigger_percent = max(
            0,
            min(int(trigger_match.group(1)), 100),
        )
        random_changed = True
        random_message = (
            f"随机事件总体触发概率已改为 "
            f"{qq_settings.random_event_trigger_percent}%。"
        )
    weight_rules = (
        (
            ("空动作", "什么也不做"),
            "random_no_action_percent",
            "空动作",
        ),
        (("群聊", "群话题"), "random_group_percent", "群聊话题"),
        (("好友", "问候"), "random_friend_percent", "好友问候"),
        (("QQ空间", "空间"), "random_qzone_percent", "QQ 空间"),
    )
    for words, attribute, label in weight_rules:
        if not any(word in value for word in words):
            continue
        match = re.search(
            r"(?:概率|权重)[^\d%]{0,10}(\d{1,3})\s*%",
            value,
        )
        if match:
            setattr(
                qq_settings,
                attribute,
                max(0, min(int(match.group(1)), 100)),
            )
            random_changed = True
            random_message = (
                f"{label}权重已改为 "
                f"{getattr(qq_settings, attribute)}%。"
            )
            break
    if re.search(r"(?:关闭|停用).{0,8}群聊.{0,6}(?:随机)?(?:话题|事件)", value):
        qq_settings.random_group_enabled = False
        random_changed = True
        random_message = "已关闭群聊随机话题事件。"
    elif re.search(r"(?:打开|启用).{0,8}群聊.{0,6}(?:随机)?(?:话题|事件)", value):
        qq_settings.random_group_enabled = True
        random_changed = True
        random_message = "已启用群聊随机话题事件。"
    if re.search(r"(?:关闭|停用).{0,8}(?:好友|问候).{0,6}(?:随机)?(?:事件|问候)", value):
        qq_settings.random_friend_enabled = False
        random_changed = True
        random_message = "已关闭好友随机问候事件。"
    elif re.search(r"(?:打开|启用).{0,8}(?:好友|问候).{0,6}(?:随机)?(?:事件|问候)", value):
        qq_settings.random_friend_enabled = True
        random_changed = True
        random_message = "已启用好友随机问候事件。"
    if re.search(r"(?:关闭|停用).{0,8}QQ空间.{0,6}(?:随机)?(?:草稿|事件)", value):
        qq_settings.random_qzone_enabled = False
        random_changed = True
        random_message = "已关闭 QQ 空间随机草稿事件。"
    elif re.search(r"(?:打开|启用).{0,8}QQ空间.{0,6}(?:随机)?(?:草稿|事件)", value):
        qq_settings.random_qzone_enabled = True
        random_changed = True
        random_message = "已启用 QQ 空间随机草稿事件。"
    if random_changed:
        controller.qq_settings_store.save(qq_settings)
        controller.apply_qq_settings(qq_settings)
        if controller.qq_dialog is not None:
            controller.qq_dialog._load_settings()
        return CommandResult(
            True,
            random_message,
            event_kind="random_setting_change",
            event_summary=random_message,
        )

    qq_changed = False
    if re.search(r"(?:关闭|停用|关掉|不要).{0,8}自动回复", value):
        qq_settings.auto_send = False
        qq_changed = True
        qq_message = "已关闭自动发送，QQ 回复只会生成草稿。"
    elif re.search(r"(?:打开|启用|开启).{0,8}自动回复", value):
        qq_settings.auto_send = True
        qq_changed = True
        qq_message = "已为本次运行启用自动发送。"
    if re.search(r"(?:打开|启用|开启).{0,8}QQ.{0,6}(?:回复功能|自动回复)", value):
        qq_settings.enabled = True
        qq_settings.auto_send = True
        qq_changed = True
        qq_message = "已启用 QQ 自动回复。"
    elif re.search(r"(?:关闭|停用|关掉|不要).{0,8}QQ.{0,6}(?:回复功能|自动回复)", value):
        qq_settings.enabled = False
        qq_settings.auto_send = False
        qq_changed = True
        qq_message = "已关闭 QQ 自动回复。"
    if re.search(r"(?:关闭|停用|关掉|不要).{0,8}(?:QQ.{0,4}(?:助手|监听)|QQ消息助手)", value):
        qq_settings.enabled = False
        qq_changed = True
        qq_message = "已停止 QQ 消息助手。"
    elif re.search(r"(?:打开|启用|开启).{0,8}(?:QQ.{0,4}(?:助手|监听)|QQ消息助手)", value):
        qq_settings.enabled = True
        qq_changed = True
        qq_message = "已启用 QQ 消息助手。"
    interval_match = re.search(r"(?:QQ.{0,8})?每\s*(\d{1,2})\s*秒.{0,8}(?:扫描|检查)", value)
    if interval_match:
        qq_settings.poll_seconds = max(2, min(int(interval_match.group(1)), 60))
        qq_changed = True
        qq_message = f"QQ 扫描间隔已改为 {qq_settings.poll_seconds} 秒。"
    if re.search(r"只(?:监听|回复).{0,4}私聊", value):
        qq_settings.scan_private = True
        qq_settings.scan_group = False
        qq_changed = True
        qq_message = "已设置为只处理私聊。"
    elif re.search(r"(?:同时|也).{0,4}(?:监听|处理).{0,4}群聊", value):
        qq_settings.scan_group = True
        qq_changed = True
        qq_message = "已启用群聊 @ 监听。"
    nickname_match = re.search(
        r"(?:我的|群里的|群的)?(?:QQ\s*)?(?:@)?昵称.{0,6}"
        r"(?:是|叫|改成|改为|设置为)[：:\s]*([^，。；\n]{1,50})",
        value,
    )
    if nickname_match:
        nickname = nickname_match.group(1).strip("“”\"' ")
        if nickname:
            if nickname not in qq_settings.own_names:
                qq_settings.own_names.append(nickname)
            qq_settings.own_names = qq_settings.own_names[:10]
            qq_changed = True
            qq_message = f"已记下 QQ @ 昵称：{nickname}"
    if qq_changed:
        controller.qq_settings_store.save(qq_settings)
        controller.apply_qq_settings(qq_settings)
        if controller.qq_dialog is not None:
            controller.qq_dialog._load_settings()
        return CommandResult(True, qq_message, event_summary=qq_message)

    opacity = _percent(value, ("透明度",))
    if opacity is not None:
        return _call(
            pet.set_opacity,
            opacity / 100,
            message=f"桌宠透明度已调整为 {opacity}%。",
            event=f"调整了桌宠透明度：{opacity}%",
        )

    relative_size = bool(
        re.search(
            r"(?:桌宠|大肥鱼|自己|你).{0,10}(?:大一号|大一点|小一点|变大|变小|放大|缩小)",
            value,
        )
        or re.search(r"^(?:先)?(?:变大|变小|放大|缩小).{0,8}$", value)
        or ("变大" in value and "最大" in value)
        or ("缩小" in value and "最小" in value)
        or (
            "调到最大" in value
            and any(word in value for word in ("桌宠", "大肥鱼", "自己", "你"))
        )
    )
    if relative_size:
        current_size = float(getattr(pet, "cfg", {}).get("size", 0.7))
        if "最大" in value:
            target_size = 1.5
        elif "最小" in value:
            target_size = 0.3
        elif any(word in value for word in ("小一点", "变小", "缩小")):
            target_size = max(0.3, current_size - 0.05)
        else:
            target_size = min(1.5, current_size + 0.05)
        percent = int(round(target_size * 100))
        return _call(
            pet.set_size,
            target_size,
            message=f"桌宠大小已调整为 {percent}%。",
            event=f"调整了桌宠大小：{percent}%",
        )

    size = _percent(value, ("大小", "尺寸"))
    if size is not None:
        size = max(10, size)
        return _call(
            pet.set_size,
            size / 100,
            message=f"桌宠大小已调整为 {size}%。",
            event=f"调整了桌宠大小：{size}%",
        )

    volume = _percent(value, ("音量",))
    if volume is not None:
        return _call(
            pet.set_volume,
            volume / 100,
            message=f"桌宠音量已调整为 {volume}%。",
            event=f"调整了桌宠音量：{volume}%",
        )

    if "锁定" in value and "解锁" not in value:
        return _call(
            pet.set_locked,
            True,
            message="桌宠位置已锁定。",
            event="锁定了桌宠位置",
        )
    if "解锁" in value or "可以拖动" in value:
        return _call(
            pet.set_locked,
            False,
            message="桌宠位置已解锁。",
            event="解锁了桌宠位置",
        )
    if "取消置顶" in value or "不要置顶" in value:
        return _call(
            pet.set_topmost,
            False,
            message="已取消桌宠置顶。",
            event="取消了桌宠置顶",
        )
    if "置顶" in value:
        return _call(
            pet.set_topmost,
            True,
            message="桌宠已置顶。",
            event="把桌宠设置为置顶",
        )
    if "取消穿透" in value or "不要穿透" in value:
        return _call(
            pet.set_passthrough,
            False,
            message="已取消鼠标穿透。",
            event="取消了鼠标穿透",
        )
    if "鼠标穿透" in value or "穿透" in value:
        return _call(
            pet.set_passthrough,
            True,
            message="已开启鼠标穿透；可从托盘菜单恢复。",
            event="开启了鼠标穿透",
        )
    if re.search(r"(?:隐藏|藏起来).{0,4}(?:桌宠|大肥鱼)", value):
        pet.hide()
        return CommandResult(True, "桌宠已隐藏，可从托盘图标重新显示。", event_summary="隐藏了桌宠")
    if re.search(r"(?:显示|出来).{0,4}(?:桌宠|大肥鱼)", value):
        pet.show()
        return CommandResult(True, "桌宠已显示。", event_summary="显示了桌宠")

    return CommandResult(False)


__all__ = ["CommandResult", "execute_chat_command"]
