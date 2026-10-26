from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .paths import config_path
from .security import CredentialError, protect_secret, unprotect_secret


@dataclass(slots=True)
class AppConfig:
    api_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    temperature: float = 0.8
    max_output_tokens: int = 700
    request_timeout: int = 75
    recent_messages: int = 16
    memories_in_prompt: int = 12
    memory_interval: int = 6
    auto_memory: bool = True
    api_key: str = ""

    @property
    def endpoint(self) -> str:
        return normalize_endpoint(self.api_url)

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("api_key", None)
        return data


def normalize_endpoint(url: str) -> str:
    value = url.strip().rstrip("/")
    if not value:
        return ""
    if value.endswith("/chat/completions"):
        return value
    return value + "/chat/completions"


def initialize_first_run_preferences(
    config_path: Path,
    config: object,
    *,
    first_run: bool,
) -> bool:
    if not first_run or not isinstance(config, dict):
        return False
    config["city"] = "北京"
    config["size"] = 0.54
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_name(config_path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, config_path)
    return True


class ConfigStore:
    def __init__(self, path: Path | None = None):
        self.path = path or config_path()

    def load(self) -> AppConfig:
        raw: dict[str, Any] = {}
        try:
            if self.path.exists():
                with self.path.open("r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    raw = loaded
        except (OSError, ValueError):
            raw = {}

        config = AppConfig(**_validated_values(raw))
        protected = raw.get("api_key_protected")
        if isinstance(protected, str) and protected:
            try:
                config.api_key = unprotect_secret(protected)
            except CredentialError:
                config.api_key = ""
        env_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if env_key:
            config.api_key = env_key
        return config

    def save(self, config: AppConfig) -> None:
        payload = config.public_dict()
        if config.api_key.strip():
            payload["api_key_protected"] = protect_secret(config.api_key.strip())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, self.path)


def _validated_values(raw: dict[str, Any]) -> dict[str, Any]:
    defaults = asdict(AppConfig())
    defaults.pop("api_key")
    values: dict[str, Any] = {}
    for key in ("api_url", "model"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            values[key] = value.strip()

    ranges = {
        "temperature": (0.0, 2.0),
        "max_output_tokens": (64, 8192),
        "request_timeout": (10, 300),
        "recent_messages": (2, 60),
        "memories_in_prompt": (0, 50),
        "memory_interval": (2, 100),
    }
    for key, (minimum, maximum) in ranges.items():
        try:
            number = float(raw.get(key, defaults[key]))
        except (TypeError, ValueError):
            continue
        if minimum <= number <= maximum:
            values[key] = number if key == "temperature" else int(number)

    auto_memory = raw.get("auto_memory")
    if isinstance(auto_memory, bool):
        values["auto_memory"] = auto_memory
    return values
