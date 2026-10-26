from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chat_pet.config import (
    AppConfig,
    ConfigStore,
    initialize_first_run_preferences,
)


class ConfigTests(unittest.TestCase):
    def test_first_run_sets_beijing_and_small_size_without_later_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            config = {"city": "汕头", "size": 0.7}
            self.assertTrue(
                initialize_first_run_preferences(path, config, first_run=True)
            )
            self.assertEqual(config["city"], "北京")
            self.assertEqual(config["size"], 0.54)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["city"], "北京")
            self.assertEqual(saved["size"], 0.54)

            later = {"city": "上海", "size": 0.72}
            self.assertFalse(
                initialize_first_run_preferences(path, later, first_run=False)
            )
            self.assertEqual(later["city"], "上海")
            self.assertEqual(later["size"], 0.72)

    def test_api_key_is_encrypted_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": ""},
        ):
            store = ConfigStore(Path(folder) / "settings.json")
            store.save(
                AppConfig(
                    api_url="https://example.test/v1",
                    api_key="local-test-value",
                    model="test-model",
                )
            )
            raw = store.path.read_text(encoding="utf-8")
            self.assertNotIn("local-test-value", raw)
            loaded = store.load()
            self.assertEqual(loaded.api_key, "local-test-value")
            self.assertEqual(loaded.model, "test-model")

    def test_environment_key_overrides_saved_key(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "environment-key"},
        ):
            store = ConfigStore(Path(folder) / "settings.json")
            store.save(AppConfig(api_key="saved-key"))
            self.assertEqual(store.load().api_key, "environment-key")


if __name__ == "__main__":
    unittest.main()
