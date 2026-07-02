"""Safe Windows background-load controls with restore support."""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Any

LOGGER = logging.getLogger(__name__)

_HKCU = 0x80000001
_REG_DWORD = 4
_MISSING = object()


class BackgroundLoadSource(Enum):
    REGISTRY = "registry"
    SERVICE_PAUSE = "service_pause"


@dataclass(frozen=True, slots=True)
class BackgroundLoadControl:
    id: str
    label: str
    description: str
    source: BackgroundLoadSource
    reg_hive: int = 0
    reg_path: str = ""
    reg_name: str = ""
    reg_on: Any = None
    reg_off: Any = None
    reg_type: int = _REG_DWORD
    service_name: str = ""


BACKGROUND_LOAD_CONTROLS: tuple[BackgroundLoadControl, ...] = (
    BackgroundLoadControl(
        id="windows_widgets",
        label="Windows Widgets",
        description="Taskbar Widgets entry on Windows 11.",
        source=BackgroundLoadSource.REGISTRY,
        reg_hive=_HKCU,
        reg_path=r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
        reg_name="TaskbarDa",
        reg_on=1,
        reg_off=0,
    ),
    BackgroundLoadControl(
        id="windows_news",
        label="News and interests",
        description="Windows 10 taskbar news/interests feed.",
        source=BackgroundLoadSource.REGISTRY,
        reg_hive=_HKCU,
        reg_path=r"Software\Microsoft\Windows\CurrentVersion\Feeds",
        reg_name="ShellFeedsTaskbarViewMode",
        reg_on=0,
        reg_off=2,
    ),
    BackgroundLoadControl(
        id="xbox_game_bar",
        label="Xbox Game Bar",
        description="Game Bar shell overlay shortcut integration.",
        source=BackgroundLoadSource.REGISTRY,
        reg_hive=_HKCU,
        reg_path=r"Software\Microsoft\GameBar",
        reg_name="UseNexusForGameBarEnabled",
        reg_on=1,
        reg_off=0,
    ),
    BackgroundLoadControl(
        id="game_dvr",
        label="Game DVR",
        description="Windows game capture feature.",
        source=BackgroundLoadSource.REGISTRY,
        reg_hive=_HKCU,
        reg_path=r"System\GameConfigStore",
        reg_name="GameDVR_Enabled",
        reg_on=1,
        reg_off=0,
    ),
    BackgroundLoadControl(
        id="game_recording",
        label="Background game recording",
        description="Background recording/capture toggle for games.",
        source=BackgroundLoadSource.REGISTRY,
        reg_hive=_HKCU,
        reg_path=r"Software\Microsoft\Windows\CurrentVersion\GameDVR",
        reg_name="AppCaptureEnabled",
        reg_on=1,
        reg_off=0,
    ),
    BackgroundLoadControl(
        id="search_indexing",
        label="Windows Search indexing",
        description="Temporary Search indexer pause while the PC is under high CPU load.",
        source=BackgroundLoadSource.SERVICE_PAUSE,
        service_name="WSearch",
    ),
    BackgroundLoadControl(
        id="delivery_optimization",
        label="Delivery Optimization",
        description="Temporary Delivery Optimization pause while the PC is under high CPU load.",
        source=BackgroundLoadSource.SERVICE_PAUSE,
        service_name="DoSvc",
    ),
)

SAFE_BACKGROUND_LOAD_PRESET: tuple[str, ...] = (
    "windows_widgets",
    "windows_news",
    "xbox_game_bar",
    "game_dvr",
    "game_recording",
)
AUTO_PAUSE_BACKGROUND_LOAD_IDS: tuple[str, ...] = (
    "search_indexing",
    "delivery_optimization",
)

_CONTROLS_BY_ID = {control.id: control for control in BACKGROUND_LOAD_CONTROLS}


class WindowsBackgroundLoadAdapter:
    @property
    def available(self) -> bool:
        return os.name == "nt"

    def read_registry_value(self, control: BackgroundLoadControl) -> Any | None:
        try:
            import winreg

            with winreg.OpenKey(_resolve_hive(control.reg_hive), control.reg_path) as key:
                value, _ = winreg.QueryValueEx(key, control.reg_name)
                return value
        except FileNotFoundError:
            return None
        except OSError as exc:
            LOGGER.debug("Cannot read registry value %s: %s", control.id, exc)
            return None

    def write_registry_value(self, control: BackgroundLoadControl, value: Any) -> bool:
        try:
            import winreg

            with winreg.CreateKeyEx(_resolve_hive(control.reg_hive), control.reg_path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, control.reg_name, 0, winreg.REG_DWORD, int(value))
            return True
        except OSError as exc:
            LOGGER.warning("Cannot write registry value %s: %s", control.id, exc)
            return False

    def delete_registry_value(self, control: BackgroundLoadControl) -> bool:
        try:
            import winreg

            with winreg.OpenKey(_resolve_hive(control.reg_hive), control.reg_path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, control.reg_name)
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            LOGGER.warning("Cannot delete registry value %s: %s", control.id, exc)
            return False

    def pause_service(self, service_name: str) -> bool:
        if not self._is_admin():
            return False
        command = ["sc", "stop" if service_name == "DoSvc" else "pause", service_name]
        return _run_service_command(command)

    def resume_service(self, service_name: str) -> bool:
        if not self._is_admin():
            return False
        command = ["sc", "start" if service_name == "DoSvc" else "continue", service_name]
        return _run_service_command(command)

    def _is_admin(self) -> bool:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:
            return False


class BackgroundLoadManager:
    """Apply and restore reversible Windows background-load controls."""

    def __init__(self, adapter: object | None = None) -> None:
        self._adapter = adapter or WindowsBackgroundLoadAdapter()
        self._restore_point: dict[str, Any] = {}
        self._paused_services: set[str] = set()
        self._applied = False

    @property
    def available(self) -> bool:
        return bool(getattr(self._adapter, "available", False))

    @property
    def active(self) -> bool:
        return self._applied or bool(self._paused_services)

    def get_state(self, control: BackgroundLoadControl) -> bool | None:
        if not self.available:
            return None
        if control.source == BackgroundLoadSource.SERVICE_PAUSE:
            return control.service_name not in self._paused_services
        raw = self._read_registry(control)
        if raw is None:
            return None
        return raw == control.reg_on

    def apply_disabled_set(self, disabled_ids: set[str] | tuple[str, ...] | list[str]) -> int:
        if not self.available:
            return 0
        disabled = set(disabled_ids)
        changed = 0
        for control in BACKGROUND_LOAD_CONTROLS:
            if control.id not in disabled or control.source != BackgroundLoadSource.REGISTRY:
                continue
            if control.id not in self._restore_point:
                value = self._read_registry(control)
                self._restore_point[control.id] = _MISSING if value is None else value
            if self._write_registry(control, control.reg_off):
                changed += 1
        self._applied = bool(self._restore_point)
        if changed:
            LOGGER.info("Applied background-load controls: %d disabled", changed)
        return changed

    def update_auto_pause(
        self,
        enabled_ids: set[str] | tuple[str, ...] | list[str],
        *,
        cpu_percent: float,
        idle_seconds: float,
        threshold_percent: float,
        required_idle_seconds: float,
    ) -> list[str]:
        if not self.available:
            return []
        enabled = set(enabled_ids)
        should_pause = cpu_percent >= threshold_percent and idle_seconds >= required_idle_seconds
        actions: list[str] = []
        for control in BACKGROUND_LOAD_CONTROLS:
            if control.source != BackgroundLoadSource.SERVICE_PAUSE or control.id not in enabled:
                continue
            service_name = control.service_name
            if should_pause and service_name not in self._paused_services:
                if self._pause_service(service_name):
                    self._paused_services.add(service_name)
                    actions.append(f"paused:{service_name}")
            elif not should_pause and service_name in self._paused_services:
                if self._resume_service(service_name):
                    self._paused_services.remove(service_name)
                    actions.append(f"resumed:{service_name}")
        return actions

    def restore(self) -> bool:
        if not self.available:
            self._applied = False
            self._restore_point = {}
            self._paused_services.clear()
            return False
        changed = False
        for control_id, value in list(self._restore_point.items()):
            control = _CONTROLS_BY_ID.get(control_id)
            if control is None or control.source != BackgroundLoadSource.REGISTRY:
                continue
            if value is _MISSING:
                changed = self._delete_registry(control) or changed
            else:
                changed = self._write_registry(control, value) or changed
        for service_name in list(self._paused_services):
            if self._resume_service(service_name):
                self._paused_services.remove(service_name)
                changed = True
        self._restore_point = {}
        self._applied = False
        if changed:
            LOGGER.info("Background-load controls restored")
        return changed

    def _read_registry(self, control: BackgroundLoadControl) -> Any | None:
        fn = getattr(self._adapter, "read_registry_value", None)
        return fn(control) if callable(fn) else None

    def _write_registry(self, control: BackgroundLoadControl, value: Any) -> bool:
        fn = getattr(self._adapter, "write_registry_value", None)
        return bool(fn(control, value)) if callable(fn) else False

    def _delete_registry(self, control: BackgroundLoadControl) -> bool:
        fn = getattr(self._adapter, "delete_registry_value", None)
        return bool(fn(control)) if callable(fn) else False

    def _pause_service(self, service_name: str) -> bool:
        fn = getattr(self._adapter, "pause_service", None)
        return bool(fn(service_name)) if callable(fn) else False

    def _resume_service(self, service_name: str) -> bool:
        fn = getattr(self._adapter, "resume_service", None)
        return bool(fn(service_name)) if callable(fn) else False


def _resolve_hive(hive_id: int) -> Any:
    import winreg

    if hive_id == _HKCU:
        return winreg.HKEY_CURRENT_USER
    raise ValueError(f"Unsupported registry hive: {hive_id}")


def _run_service_command(command: list[str]) -> bool:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=6,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOGGER.debug("Service command failed: %s: %s", " ".join(command), exc)
        return False
    if result.returncode != 0:
        LOGGER.debug("Service command returned %s: %s", result.returncode, (result.stderr or result.stdout).strip())
        return False
    return True


# Backward-friendly alias for tests and future UI code.
BACKGROUND_LOAD_SETTINGS = BACKGROUND_LOAD_CONTROLS
