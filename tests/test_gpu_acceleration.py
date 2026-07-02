"""Tests for read-only hardware-acceleration detection."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pc_optimizer_lite.gpu_acceleration import AccelerationChecker


class AccelerationCheckerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.local = Path(tempfile.mkdtemp())
        self.roaming = Path(tempfile.mkdtemp())

    def _checker(self) -> AccelerationChecker:
        return AccelerationChecker(local_app_data=self.local, roaming_app_data=self.roaming)

    def _chrome_prefs_path(self) -> Path:
        path = self.local / "Google" / "Chrome" / "User Data" / "Default" / "Preferences"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _discord_settings_path(self) -> Path:
        path = self.roaming / "discord" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    # -- missing apps: silently skipped ------------------------------------

    def test_no_apps_installed_returns_empty_list(self) -> None:
        statuses = self._checker().check_all()
        self.assertEqual(statuses, [])

    # -- Chromium (Chrome/Edge/Brave share the same code path) -------------

    def test_chrome_hardware_acceleration_enabled(self) -> None:
        prefs = self._chrome_prefs_path()
        prefs.write_text(json.dumps({"hardware_acceleration_mode": {"enabled": True}}), encoding="utf-8")

        statuses = self._checker().check_all()

        chrome = next(s for s in statuses if s.app_name == "Google Chrome")
        self.assertTrue(chrome.detected)
        self.assertTrue(chrome.hardware_accel)
        self.assertIsNone(chrome.hint)
        self.assertEqual(chrome.config_path, str(prefs))

    def test_chrome_hardware_acceleration_disabled(self) -> None:
        prefs = self._chrome_prefs_path()
        prefs.write_text(json.dumps({"hardware_acceleration_mode": {"enabled": False}}), encoding="utf-8")

        statuses = self._checker().check_all()

        chrome = next(s for s in statuses if s.app_name == "Google Chrome")
        self.assertTrue(chrome.detected)
        self.assertFalse(chrome.hardware_accel)
        self.assertIsNotNone(chrome.hint)

    def test_chrome_missing_key_is_unknown_not_disabled(self) -> None:
        # Chrome defaults hardware acceleration to on and often never writes
        # this key at all -- treating "missing" as "off" would be a false
        # alarm on most untouched installs.
        prefs = self._chrome_prefs_path()
        prefs.write_text(json.dumps({"some_other_key": True}), encoding="utf-8")

        statuses = self._checker().check_all()

        chrome = next(s for s in statuses if s.app_name == "Google Chrome")
        self.assertTrue(chrome.detected)
        self.assertIsNone(chrome.hardware_accel)
        self.assertIsNotNone(chrome.hint)

    def test_chrome_corrupt_json_reports_unknown_without_raising(self) -> None:
        prefs = self._chrome_prefs_path()
        prefs.write_text("{not valid json", encoding="utf-8")

        statuses = self._checker().check_all()

        chrome = next(s for s in statuses if s.app_name == "Google Chrome")
        self.assertTrue(chrome.detected)
        self.assertIsNone(chrome.hardware_accel)
        self.assertIsNotNone(chrome.hint)

    def test_chrome_not_installed_is_omitted_from_results(self) -> None:
        statuses = self._checker().check_all()
        self.assertFalse(any(s.app_name == "Google Chrome" for s in statuses))

    # -- Discord --------------------------------------------------------

    def test_discord_hardware_acceleration_disabled(self) -> None:
        settings = self._discord_settings_path()
        settings.write_text(json.dumps({"hardwareAcceleration": False}), encoding="utf-8")

        statuses = self._checker().check_all()

        discord = next(s for s in statuses if s.app_name == "Discord")
        self.assertTrue(discord.detected)
        self.assertFalse(discord.hardware_accel)
        self.assertIsNotNone(discord.hint)

    def test_discord_missing_key_defaults_to_enabled(self) -> None:
        settings = self._discord_settings_path()
        settings.write_text(json.dumps({}), encoding="utf-8")

        statuses = self._checker().check_all()

        discord = next(s for s in statuses if s.app_name == "Discord")
        self.assertTrue(discord.detected)
        self.assertTrue(discord.hardware_accel)
        self.assertIsNone(discord.hint)

    def test_discord_corrupt_json_reports_unknown_without_raising(self) -> None:
        settings = self._discord_settings_path()
        settings.write_text("not json at all", encoding="utf-8")

        statuses = self._checker().check_all()

        discord = next(s for s in statuses if s.app_name == "Discord")
        self.assertTrue(discord.detected)
        self.assertIsNone(discord.hardware_accel)

    # -- Telegram (always "unknown" -- binary config) -----------------------

    def test_telegram_detected_but_status_unknown(self) -> None:
        (self.roaming / "Telegram Desktop").mkdir(parents=True, exist_ok=True)

        statuses = self._checker().check_all()

        telegram = next(s for s in statuses if s.app_name == "Telegram Desktop")
        self.assertTrue(telegram.detected)
        self.assertIsNone(telegram.hardware_accel)
        self.assertIsNotNone(telegram.hint)

    def test_telegram_not_installed_is_omitted(self) -> None:
        statuses = self._checker().check_all()
        self.assertFalse(any(s.app_name == "Telegram Desktop" for s in statuses))


if __name__ == "__main__":
    unittest.main()
