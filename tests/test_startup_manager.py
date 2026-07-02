"""Tests for reversible Windows startup-entry management."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pc_optimizer_lite.startup_manager import (
    StartupManager,
    StartupSource,
    _HKCU,
    _HKLM,
)


class _FakeStartupAdapter:
    available = True

    def __init__(self, admin: bool = False) -> None:
        self._admin = admin
        self._run_values: dict[int, dict[str, str]] = {_HKCU: {}, _HKLM: {}}
        self._user_shortcuts: list[Path] = []
        self._common_shortcuts: list[Path] = []
        self.move_calls: list[tuple[Path, Path]] = []

    def is_admin(self) -> bool:
        return self._admin

    def user_startup_folder(self) -> Path:
        return Path("C:/fake/user/startup")

    def common_startup_folder(self) -> Path:
        return Path("C:/fake/common/startup")

    def read_run_values(self, hive: int) -> dict[str, str]:
        return dict(self._run_values.get(hive, {}))

    def write_run_value(self, hive: int, name: str, value: str) -> bool:
        self._run_values.setdefault(hive, {})[name] = value
        return True

    def delete_run_value(self, hive: int, name: str) -> bool:
        self._run_values.get(hive, {}).pop(name, None)
        return True

    def list_shortcuts(self, folder: Path) -> list[Path]:
        if folder == self.user_startup_folder():
            return list(self._user_shortcuts)
        if folder == self.common_startup_folder():
            return list(self._common_shortcuts)
        return []

    def move_file(self, source: Path, destination: Path) -> bool:
        self.move_calls.append((source, destination))
        if source in self._user_shortcuts:
            self._user_shortcuts.remove(source)
        if source in self._common_shortcuts:
            self._common_shortcuts.remove(source)
        if str(destination).startswith(str(self.user_startup_folder())):
            self._user_shortcuts.append(destination)
        elif str(destination).startswith(str(self.common_startup_folder())):
            self._common_shortcuts.append(destination)
        return True


class StartupManagerTests(unittest.TestCase):
    def _manager(self, adapter: _FakeStartupAdapter) -> StartupManager:
        tmp_dir = tempfile.mkdtemp()
        return StartupManager(adapter, backup_path=Path(tmp_dir) / "disabled_startup.json")

    def test_lists_hkcu_and_hklm_run_entries(self) -> None:
        adapter = _FakeStartupAdapter()
        adapter._run_values[_HKCU]["Skype"] = r"C:\Program Files\Skype\Skype.exe /min"
        adapter._run_values[_HKLM]["SomeDriverTray"] = r"C:\Program Files\Driver\tray.exe"
        manager = self._manager(adapter)

        entries = manager.list_entries()

        names = {entry.name for entry in entries}
        self.assertIn("Skype", names)
        self.assertIn("SomeDriverTray", names)
        skype = next(e for e in entries if e.name == "Skype")
        self.assertEqual(skype.source, StartupSource.HKCU_RUN)
        self.assertTrue(skype.enabled)
        self.assertFalse(skype.requires_admin)
        driver = next(e for e in entries if e.name == "SomeDriverTray")
        self.assertEqual(driver.source, StartupSource.HKLM_RUN)
        self.assertTrue(driver.requires_admin)

    def test_excludes_our_own_launch_command(self) -> None:
        adapter = _FakeStartupAdapter()
        adapter._run_values[_HKCU]["PC Optimizer Lite"] = r"C:\App\PCOptimizerLite.exe --tray"
        manager = self._manager(adapter)

        entries = manager.list_entries(exclude_commands={r"C:\App\PCOptimizerLite.exe --tray"})

        self.assertEqual(entries, [])

    def test_disable_and_enable_hkcu_registry_entry_round_trips(self) -> None:
        adapter = _FakeStartupAdapter()
        adapter._run_values[_HKCU]["Skype"] = "skype.exe /min"
        manager = self._manager(adapter)
        entry = manager.list_entries()[0]

        ok, _ = manager.disable(entry)
        self.assertTrue(ok)
        self.assertNotIn("Skype", adapter._run_values[_HKCU])

        entries_after_disable = manager.list_entries()
        self.assertEqual(len(entries_after_disable), 1)
        self.assertFalse(entries_after_disable[0].enabled)
        self.assertEqual(entries_after_disable[0].command, "skype.exe /min")

        ok, _ = manager.enable(entries_after_disable[0])
        self.assertTrue(ok)
        self.assertEqual(adapter._run_values[_HKCU]["Skype"], "skype.exe /min")

        entries_after_enable = manager.list_entries()
        self.assertEqual(len(entries_after_enable), 1)
        self.assertTrue(entries_after_enable[0].enabled)

    def test_disable_hklm_entry_requires_admin(self) -> None:
        adapter = _FakeStartupAdapter(admin=False)
        adapter._run_values[_HKLM]["SomeDriverTray"] = "tray.exe"
        manager = self._manager(adapter)
        entry = manager.list_entries()[0]

        ok, message = manager.disable(entry)

        self.assertFalse(ok)
        self.assertIn("администратора", message)
        self.assertIn("SomeDriverTray", adapter._run_values[_HKLM])

    def test_disable_hklm_entry_succeeds_when_admin(self) -> None:
        adapter = _FakeStartupAdapter(admin=True)
        adapter._run_values[_HKLM]["SomeDriverTray"] = "tray.exe"
        manager = self._manager(adapter)
        entry = manager.list_entries()[0]

        ok, _ = manager.disable(entry)

        self.assertTrue(ok)
        self.assertNotIn("SomeDriverTray", adapter._run_values[_HKLM])

    def test_disable_and_enable_startup_folder_shortcut_round_trips(self) -> None:
        adapter = _FakeStartupAdapter()
        shortcut = adapter.user_startup_folder() / "Discord.lnk"
        adapter._user_shortcuts.append(shortcut)
        manager = self._manager(adapter)
        entry = manager.list_entries()[0]
        self.assertEqual(entry.source, StartupSource.STARTUP_FOLDER_USER)
        self.assertTrue(entry.enabled)

        ok, _ = manager.disable(entry)
        self.assertTrue(ok)
        self.assertNotIn(shortcut, adapter._user_shortcuts)

        entries_after_disable = manager.list_entries()
        self.assertEqual(len(entries_after_disable), 1)
        self.assertFalse(entries_after_disable[0].enabled)

        ok, _ = manager.enable(entries_after_disable[0])
        self.assertTrue(ok)
        self.assertIn(shortcut, adapter._user_shortcuts)

        entries_after_enable = manager.list_entries()
        self.assertEqual(len(entries_after_enable), 1)
        self.assertTrue(entries_after_enable[0].enabled)

    def test_unavailable_on_non_windows_adapter(self) -> None:
        class _UnavailableAdapter(_FakeStartupAdapter):
            available = False

        manager = self._manager(_UnavailableAdapter())
        self.assertEqual(manager.list_entries(), [])


if __name__ == "__main__":
    unittest.main()
