from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import AppConfig
from .persona import APP_VERSION


class ApiError(RuntimeError):
    pass


def _response_text(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ApiError("API 返回格式不符合 OpenAI Chat Completions 规范。") from exc
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        pieces = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                pieces.append(item["text"])
        return "".join(pieces).strip()
    raise ApiError("API 没有返回可显示的文本。")


def request_chat(
    messages: list[dict[str, str]],
    config: AppConfig,
    *,
    timeout: int | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> str:
    if not config.api_key.strip():
        raise ApiError("尚未配置 API 密钥，请双击桌宠打开聊天后进入“设置”。")
    endpoint = config.endpoint
    if not endpoint.lower().startswith(("http://", "https://")):
        raise ApiError("API 地址无效，应以 http:// 或 https:// 开头。")

    body = {
        "model": config.model.strip() or "deepseek-chat",
        "messages": messages,
        "stream": False,
        "temperature": config.temperature if temperature is None else temperature,
        "max_tokens": config.max_output_tokens if max_tokens is None else max_tokens,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {config.api_key.strip()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"DaFeiYu-DesktopPet/{APP_VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=max(10, int(timeout or config.request_timeout)),
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            error = payload.get("error", payload)
            if isinstance(error, dict):
                detail = str(error.get("message", ""))
            elif error:
                detail = str(error)
        except Exception:
            pass
        detail = detail.strip()[:300]
        suffix = f"：{detail}" if detail else ""
        raise ApiError(f"API 请求失败（HTTP {exc.code}）{suffix}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise ApiError(f"无法连接 API：{reason}") from exc
    except TimeoutError as exc:
        raise ApiError("API 请求超时，请检查网络或稍后重试。") from exc
    except OSError as exc:
        raise ApiError(f"网络请求失败：{exc}") from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApiError("API 返回的不是有效 JSON。") from exc
    if not isinstance(payload, dict):
        raise ApiError("API 返回格式无效。")
    text = _response_text(payload)
    if not text:
        raise ApiError("API 返回了空回复。")
    return text
