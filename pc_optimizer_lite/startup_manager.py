"""Enumerate and reversibly disable Windows startup programs.

Covers HKCU/HKLM ``...\\CurrentVersion\\Run`` registry entries and shortcuts
in the per-user and common Startup folders. Disabling never deletes
anything: registry values are moved into a local JSON backup before being
removed from Run, and Startup-folder shortcuts are moved into a backup
folder. Re-enabling reverses exactly that.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .config import get_app_data_dir

LOGGER = logging.getLogger(__name__)

_HKCU = 0x80000001
_HKLM = 0x80000002
_REGISTRY_RUN_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_BACKUP_FILENAME = "disabled_startup.json"
_DISABLED_SHORTCUTS_DIRNAME = "DisabledStartupShortcuts"


class StartupSource(Enum):
    HKCU_RUN = "hkcu_run"
    HKLM_RUN = "hklm_run"
    STARTUP_FOLDER_USER = "startup_folder_user"
    STARTUP_FOLDER_COMMON = "startup_folder_common"


_SOURCE_LABELS = {
    StartupSource.HKCU_RUN: "Реестр (текущий пользователь)",
    StartupSource.HKLM_RUN: "Реестр (все пользователи)",
    StartupSource.STARTUP_FOLDER_USER: "Папка автозагрузки (пользователь)",
    StartupSource.STARTUP_FOLDER_COMMON: "Папка автозагрузки (все пользователи)",
}
_ADMIN_REQUIRED_SOURCES = (StartupSource.HKLM_RUN, StartupSource.STARTUP_FOLDER_COMMON)


@dataclass(frozen=True, slots=True)
class StartupEntry:
    """One startup program, enabled (live) or disabled (from backup)."""

    id: str
    name: str
    command: str
    source: StartupSource
    enabled: bool
    requires_admin: bool = False

    @property
    def source_label(self) -> str:
        return _SOURCE_LABELS.get(self.source, self.source.value)


class WindowsStartupAdapter:
    """Thin, mockable wrapper around winreg/filesystem calls."""

    @property
    def available(self) -> bool:
        return os.name == "nt"

    def is_admin(self) -> bool:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:
            return False

    def user_startup_folder(self) -> Path:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"

    def common_startup_folder(self) -> Path:
        base = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
        return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"

    def read_run_values(self, hive: int) -> dict[str, str]:
        try:
            import winreg

            values: dict[str, str] = {}
            with winreg.OpenKey(_resolve_hive(hive), _REGISTRY_RUN_PATH) as key:
                index = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    values[name] = str(value)
                    index += 1
            return values
        except FileNotFoundError:
            return {}
        except OSError as exc:
            LOGGER.debug("Cannot read Run key for hive %s: %s", hive, exc)
            return {}

    def write_run_value(self, hive: int, name: str, value: str) -> bool:
        try:
            import winreg

            with winreg.CreateKeyEx(_resolve_hive(hive), _REGISTRY_RUN_PATH, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
            return True
        except OSError as exc:
            LOGGER.warning("Cannot write Run value %s: %s", name, exc)
            return False

    def delete_run_value(self, hive: int, name: str) -> bool:
        try:
            import winreg

            with winreg.OpenKey(_resolve_hive(hive), _REGISTRY_RUN_PATH, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            LOGGER.warning("Cannot delete Run value %s: %s", name, exc)
            return False

    def list_shortcuts(self, folder: Path) -> list[Path]:
        try:
            return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in (".lnk", ".url"))
        except OSError:
            return []

    def move_file(self, source: Path, destination: Path) -> bool:
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                source.replace(destination)
            except OSError:
                shutil.move(str(source), str(destination))
            return True
        except OSError as exc:
            LOGGER.warning("Cannot move startup shortcut %s -> %s: %s", source, destination, exc)
            return False


def _resolve_hive(hive_id: int) -> Any:
    import winreg

    if hive_id == _HKCU:
        return winreg.HKEY_CURRENT_USER
    if hive_id == _HKLM:
        return winreg.HKEY_LOCAL_MACHINE
    raise ValueError(f"Unsupported registry hive: {hive_id}")


class StartupManager:
    """Lists Windows startup entries and reversibly disables/enables them."""

    def __init__(self, adapter: object | None = None, backup_path: Path | None = None) -> None:
        self._adapter = adapter or WindowsStartupAdapter()
        self._backup_path = backup_path or (get_app_data_dir() / _BACKUP_FILENAME)
        self._disabled_shortcuts_dir = self._backup_path.parent / _DISABLED_SHORTCUTS_DIRNAME

    @property
    def available(self) -> bool:
        return bool(getattr(self._adapter, "available", False))

    def is_admin(self) -> bool:
        return bool(self._adapter.is_admin())

    def list_entries(self, *, exclude_commands: set[str] | None = None) -> list[StartupEntry]:
        """Return live (enabled) and backed-up (disabled) startup entries, sorted by name."""

        if not self.available:
            return []
        exclude = {command.strip().lower() for command in (exclude_commands or set())}
        backup = self._load_backup()

        live: list[StartupEntry] = []
        live_ids: set[str] = set()

        for hive, source, requires_admin in (
            (_HKCU, StartupSource.HKCU_RUN, False),
            (_HKLM, StartupSource.HKLM_RUN, True),
        ):
            for name, command in self._adapter.read_run_values(hive).items():
                if command.strip().lower() in exclude:
                    continue
                entry_id = f"{source.value}:{name}"
                live_ids.add(entry_id)
                live.append(StartupEntry(entry_id, name, command, source, True, requires_admin))

        for folder_getter, source, requires_admin in (
            (self._adapter.user_startup_folder, StartupSource.STARTUP_FOLDER_USER, False),
            (self._adapter.common_startup_folder, StartupSource.STARTUP_FOLDER_COMMON, True),
        ):
            for path in self._adapter.list_shortcuts(folder_getter()):
                entry_id = f"{source.value}:{path.name}"
                live_ids.add(entry_id)
                live.append(StartupEntry(entry_id, path.stem, str(path), source, True, requires_admin))

        disabled: list[StartupEntry] = []
        for entry_id, data in backup.items():
            if entry_id in live_ids:
                continue
            try:
                source = StartupSource(data["source"])
            except (KeyError, ValueError):
                continue
            command = str(data.get("command", ""))
            if command.strip().lower() in exclude:
                continue
            disabled.append(
                StartupEntry(
                    entry_id,
                    str(data.get("name", "")),
                    command,
                    source,
                    False,
                    source in _ADMIN_REQUIRED_SOURCES,
                )
            )

        entries = live + disabled
        entries.sort(key=lambda entry: entry.name.lower())
        return entries

    def disable(self, entry: StartupEntry) -> tuple[bool, str]:
        """Move a live entry out of Run/Startup-folder into the local backup."""

        if not self.available:
            return False, "Доступно только на Windows."
        if entry.requires_admin and not self._adapter.is_admin():
            return False, "Требуются права администратора."

        if entry.source in (StartupSource.HKCU_RUN, StartupSource.HKLM_RUN):
            hive = _HKCU if entry.source == StartupSource.HKCU_RUN else _HKLM
            if not self._adapter.delete_run_value(hive, entry.name):
                return False, "Не удалось изменить реестр автозагрузки."
            self._save_backup_entry(entry.id, entry.name, entry.command, entry.source)
            return True, "Отключено."

        source_path = Path(entry.command)
        destination = self._disabled_shortcuts_dir / entry.source.value / source_path.name
        if not self._adapter.move_file(source_path, destination):
            return False, "Не удалось переместить ярлык автозагрузки."
        self._save_backup_entry(entry.id, entry.name, str(destination), entry.source)
        return True, "Отключено."

    def enable(self, entry: StartupEntry) -> tuple[bool, str]:
        """Restore a previously disabled entry back to Run/Startup-folder."""

        if not self.available:
            return False, "Доступно только на Windows."
        if entry.requires_admin and not self._adapter.is_admin():
            return False, "Требуются права администратора."

        if entry.source in (StartupSource.HKCU_RUN, StartupSource.HKLM_RUN):
            hive = _HKCU if entry.source == StartupSource.HKCU_RUN else _HKLM
            if not self._adapter.write_run_value(hive, entry.name, entry.command):
                return False, "Не удалось изменить реестр автозагрузки."
            self._remove_backup_entry(entry.id)
            return True, "Включено."

        source_path = Path(entry.command)
        destination_folder = (
            self._adapter.user_startup_folder()
            if entry.source == StartupSource.STARTUP_FOLDER_USER
            else self._adapter.common_startup_folder()
        )
        destination = destination_folder / source_path.name
        if not self._adapter.move_file(source_path, destination):
            return False, "Не удалось вернуть ярлык автозагрузки."
        self._remove_backup_entry(entry.id)
        return True, "Включено."

    def _load_backup(self) -> dict[str, dict[str, str]]:
        try:
            return json.loads(self._backup_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_backup_entry(self, entry_id: str, name: str, command: str, source: StartupSource) -> None:
        backup = self._load_backup()
        backup[entry_id] = {"name": name, "command": command, "source": source.value}
        self._write_backup(backup)

    def _remove_backup_entry(self, entry_id: str) -> None:
        backup = self._load_backup()
        if entry_id in backup:
            del backup[entry_id]
            self._write_backup(backup)

    def _write_backup(self, backup: dict[str, dict[str, str]]) -> None:
        try:
            self._backup_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._backup_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(backup, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(self._backup_path)
        except OSError as exc:
            LOGGER.warning("Cannot persist startup backup: %s", exc)
