"""Safe page-file analysis and explicit Windows reset helpers."""

from __future__ import annotations

import ctypes
import os
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PageFileStatus:
    """Read-only page-file state used for recommendations."""

    total_ram_bytes: int
    pagefile_total_bytes: int
    pagefile_used_bytes: int
    pagefile_percent: float
    automatic_managed: bool | None


@dataclass(frozen=True, slots=True)
class PageFileAdvice:
    """Non-destructive recommendation for the user."""

    action: str
    title: str
    detail: str
    requires_admin: bool = False
    reboot_required: bool = False


def recommend_pagefile_action(status: PageFileStatus) -> PageFileAdvice:
    """Return a conservative recommendation without changing the system."""

    ram_gb = status.total_ram_bytes / 1024**3 if status.total_ram_bytes else 0.0
    pagefile_gb = status.pagefile_total_bytes / 1024**3 if status.pagefile_total_bytes else 0.0
    if status.automatic_managed is True:
        return PageFileAdvice(
            action="none",
            title="Windows уже управляет файлом подкачки",
            detail="Это самый безопасный режим по умолчанию. PC Optimizer Lite ничего не меняет автоматически.",
        )
    if status.pagefile_total_bytes <= 0 and ram_gb <= 8:
        return PageFileAdvice(
            action="enable_auto",
            title="Рекомендуется включить автоматическое управление файлом подкачки",
            detail=(
                "Файл подкачки отключён, а физической RAM немного. Лучше вернуть режим Windows по умолчанию, "
                "чтобы система не падала при пиках памяти. Изменение применяется только после подтверждения."
            ),
            requires_admin=True,
            reboot_required=True,
        )
    if status.automatic_managed is False and status.pagefile_percent >= 70.0:
        return PageFileAdvice(
            action="enable_auto",
            title="Рекомендуется вернуть автоматическое управление Windows",
            detail=(
                f"Файл подкачки задан вручную и сейчас используется на {status.pagefile_percent:.0f}%. "
                "Автоматическое управление безопаснее подстраивает размер под нагрузку."
            ),
            requires_admin=True,
            reboot_required=True,
        )
    if status.automatic_managed is False and 0 < pagefile_gb < max(2.0, ram_gb * 0.25):
        return PageFileAdvice(
            action="enable_auto",
            title="Рекомендуется увеличить слишком маленький файл подкачки",
            detail=(
                f"Текущий размер около {pagefile_gb:.1f} ГБ при {ram_gb:.1f} ГБ RAM. "
                "Безопасный вариант — вернуть автоматическое управление Windows."
            ),
            requires_admin=True,
            reboot_required=True,
        )
    return PageFileAdvice(
        action="none",
        title="Файл подкачки выглядит нормально",
        detail="Автоматических изменений не требуется. Пользовательские ручные настройки не трогаются.",
    )


def build_enable_auto_pagefile_command() -> str:
    """Build the PowerShell command that restores Windows automatic management."""

    return (
        "$ErrorActionPreference = 'Stop'; "
        "$system = Get-CimInstance -ClassName Win32_ComputerSystem; "
        "Set-CimInstance -InputObject $system -Property @{AutomaticManagedPagefile=$true}"
    )


def get_windows_pagefile_auto_managed(timeout_seconds: float = 2.5) -> bool | None:
    """Read whether Windows automatic page-file management is enabled."""

    if os.name != "nt":
        return None
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "(Get-CimInstance -ClassName Win32_ComputerSystem).AutomaticManagedPagefile",
        ],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def is_running_as_admin() -> bool:
    """Return whether the current process has Windows administrator rights."""

    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def set_windows_auto_pagefile() -> None:
    """Restore Windows automatic page-file management after explicit user consent."""

    if os.name != "nt":
        raise RuntimeError("Page-file changes are supported only on Windows.")
    if not is_running_as_admin():
        raise PermissionError("Administrator rights are required to change page-file settings.")
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            build_enable_auto_pagefile_command(),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "PowerShell command failed.").strip()
        raise RuntimeError(message)
