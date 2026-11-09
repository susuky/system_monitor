"""語言保存、設定失敗與命令列的實際輸出。"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sysmon.settings import SettingsError, load_language, save_language, translate_status


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="sysmon-settings-test-")
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {"XDG_CONFIG_HOME": self.directory.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.path = Path(self.directory.name) / "vital" / "config.json"

    def test_rename_preserves_old_language_until_new_settings_are_saved(self):
        legacy = self.path.parent.parent / "sysmon" / "config.json"
        legacy.parent.mkdir()
        legacy.write_text('{"language":"en"}')
        self.assertEqual(load_language(), "en")
        save_language("zh-TW")
        self.assertEqual(load_language(), "zh-TW")
        self.assertEqual(json.loads(legacy.read_text()), {"language": "en"})

    def test_save_load_and_failed_write_preserves_previous_choice(self):
        self.assertEqual(load_language(), "zh-TW")
        save_language("en")
        self.assertEqual(load_language(), "en")
        self.assertEqual(json.loads(self.path.read_text()), {"language": "en"})
        with patch("sysmon.settings.os.replace", side_effect=PermissionError("permission denied")):
            with self.assertRaises(SettingsError) as failure:
                save_language("zh-TW")
        self.assertEqual(failure.exception.kind, "write")
        self.assertEqual(failure.exception.path, self.path)
        self.assertEqual(load_language(), "en")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        save_language("zh-TW")
        self.assertEqual(load_language(), "zh-TW")

    def test_invalid_settings_and_real_path_errors(self):
        self.path.parent.mkdir()
        for content in ('{', '[]', '{"language":"fr"}', '{}'):
            self.path.write_text(content)
            with self.assertRaises(SettingsError) as failure:
                load_language()
            self.assertEqual(failure.exception.kind, "format")
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(SettingsError) as failure:
            load_language()
        self.assertEqual(failure.exception.kind, "read")
        with self.assertRaises(SettingsError) as failure:
            save_language("en")
        self.assertEqual(failure.exception.kind, "write")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        with self.assertRaises(ValueError):
            save_language("fr")

    def test_status_translation_preserves_details(self):
        message = "GPU 1 無法讀取；GPU 2 無法讀取 C 程序資料；GPU 3 無法讀取 graphics 程序資料"
        self.assertEqual(translate_status(message, "zh-TW"), message)
        self.assertEqual(
            translate_status(message, "en"),
            "Cannot read GPU 1; Cannot read GPU 2 compute processes; Cannot read GPU 3 graphics processes",
        )
        self.assertEqual(
            translate_status("無法讀取 NVIDIA GPU：Driver not loaded", "en"),
            "Cannot read NVIDIA GPU: Driver not loaded",
        )
        self.assertEqual(translate_status("無權讀取 CPU 功耗", "en"), "Permission denied reading CPU power")

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "sysmon", *args], cwd=Path(__file__).resolve().parents[1],
            text=True, capture_output=True, timeout=15,
        )

    def test_live_cli_language_persists_and_json_stays_machine_readable(self):
        english = self.run_cli("--lang", "en", "--once", "-i", "0.2")
        self.assertEqual(english.returncode, 0, english.stderr)
        self.assertIn("cores /", english.stdout)
        self.assertIn("Available", english.stdout)
        self.assertIn("Network", english.stdout)
        self.assertEqual(load_language(), "en")
        help_text = self.run_cli("--help")
        self.assertIn("interface language", help_text.stdout)
        invalid = self.run_cli("--interval", "nan")
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("finite number", invalid.stderr)
        missing_language = self.run_cli("--lang")
        self.assertEqual(missing_language.returncode, 2)
        self.assertIn("expected one argument", missing_language.stderr)
        non_tty = self.run_cli()
        self.assertEqual(non_tty.returncode, 2)
        self.assertIn("requires a terminal", non_tty.stderr)
        snapshot = self.run_cli("--json", "-i", "0.2")
        self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
        data = json.loads(snapshot.stdout)
        self.assertIn("per_core_times", data["cpu"])
        self.assertIn("power_status", data["cpu"])
        self.assertNotIn("language", data)
        chinese = self.run_cli("--lang", "zh-TW", "--once", "-i", "0.2")
        self.assertEqual(chinese.returncode, 0, chinese.stderr)
        self.assertIn("核心 /", chinese.stdout)
        self.assertIn("可用", chinese.stdout)
        self.assertEqual(load_language(), "zh-TW")
        self.path.write_text("invalid JSON")
        broken = self.run_cli("--once", "-i", "0.2")
        self.assertEqual(broken.returncode, 2)
        self.assertIn(str(self.path), broken.stderr)
        repaired = self.run_cli("--lang", "en", "--json", "-i", "0.2")
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertEqual(load_language(), "en")


if __name__ == "__main__":
    unittest.main()
