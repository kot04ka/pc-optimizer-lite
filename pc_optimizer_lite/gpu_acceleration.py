"""Read-only, best-effort detection of hardware acceleration in common apps.

Only reads existing config/profile files on disk -- never launches or
modifies the target application, never runs on a timer, and never touches
the config it reads. Missing apps and unreadable/corrupt config files are
both a normal "can't tell" outcome, not an error.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AccelerationStatus:
    """Best-effort hardware-acceleration status for one application."""

    app_name: str
    detected: bool
    hardware_accel: bool | None
    config_path: str | None
    hint: str | None


def _default_local_app_data() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base)


def _default_roaming_app_data() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base)


class AccelerationChecker:
    """Checks a handful of known apps' config files for hardware acceleration."""

    def __init__(self, local_app_data: Path | None = None, roaming_app_data: Path | None = None) -> None:
        self._local = local_app_data or _default_local_app_data()
        self._roaming = roaming_app_data or _default_roaming_app_data()

    def check_all(self) -> list[AccelerationStatus]:
        """Return one status per detected app; apps not found are omitted."""

        checks = (
            self._check_chromium(
                "Google Chrome",
                self._local / "Google" / "Chrome" / "User Data" / "Default" / "Preferences",
            ),
            self._check_chromium(
                "Microsoft Edge",
                self._local / "Microsoft" / "Edge" / "User Data" / "Default" / "Preferences",
            ),
            self._check_chromium(
                "Brave",
                self._local / "BraveSoftware" / "Brave-Browser" / "User Data" / "Default" / "Preferences",
            ),
            self._check_discord(self._roaming / "discord" / "settings.json"),
            self._check_telegram(self._roaming / "Telegram Desktop"),
        )
        return [status for status in checks if status.detected]

    @staticmethod
    def _check_chromium(app_name: str, preferences_path: Path) -> AccelerationStatus:
        if not preferences_path.exists():
            return AccelerationStatus(app_name, False, None, None, None)
        try:
            data = json.loads(preferences_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.debug("Cannot read %s preferences at %s", app_name, preferences_path, exc_info=True)
            return AccelerationStatus(
                app_name, True, None, str(preferences_path), "Не удалось прочитать настройки — проверьте вручную."
            )

        # Chrome/Edge/Brave default this setting to ON and often never write
        # the key at all unless the user has explicitly changed it -- so a
        # missing key means "unknown", not "off" (verified against a real
        # Chrome profile with hardware acceleration on and no such key).
        mode = data.get("hardware_acceleration_mode")
        if not isinstance(mode, dict) or "enabled" not in mode:
            hint = f"Не удалось определить — проверьте вручную в {app_name}: Настройки → Система → «Использовать аппаратное ускорение»."
            return AccelerationStatus(app_name, True, None, str(preferences_path), hint)

        enabled = bool(mode["enabled"])
        hint = (
            None
            if enabled
            else f"Включите в {app_name}: Настройки → Система → «Использовать аппаратное ускорение», затем перезапустите браузер."
        )
        return AccelerationStatus(app_name, True, enabled, str(preferences_path), hint)

    @staticmethod
    def _check_discord(settings_path: Path) -> AccelerationStatus:
        app_name = "Discord"
        if not settings_path.exists():
            return AccelerationStatus(app_name, False, None, None, None)
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.debug("Cannot read Discord settings at %s", settings_path, exc_info=True)
            return AccelerationStatus(
                app_name, True, None, str(settings_path), "Не удалось прочитать настройки — проверьте вручную."
            )

        enabled = bool(data.get("hardwareAcceleration", True))
        hint = (
            None
            if enabled
            else "Включите в Discord: Настройки пользователя → Продвинутые → «Аппаратное ускорение», затем перезапустите Discord."
        )
        return AccelerationStatus(app_name, True, enabled, str(settings_path), hint)

    @staticmethod
    def _check_telegram(base_dir: Path) -> AccelerationStatus:
        app_name = "Telegram Desktop"
        if not base_dir.exists():
            return AccelerationStatus(app_name, False, None, None, None)
        return AccelerationStatus(
            app_name,
            True,
            None,
            str(base_dir),
            "Telegram хранит этот параметр в бинарном формате — проверьте вручную: Настройки → Продвинутые → «Аппаратное ускорение».",
        )
