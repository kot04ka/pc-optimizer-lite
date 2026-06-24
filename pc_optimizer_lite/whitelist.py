"""Process whitelist management.

The whitelist is intentionally conservative: protected Windows system processes
are never changed or terminated by the application.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import AppConfig

DEFAULT_PROCESS_WHITELIST: frozenset[str] = frozenset(
    {
        "audiodg.exe",
        "csrss.exe",
        "dwm.exe",
        "explorer.exe",
        "fontdrvhost.exe",
        "idle",
        "lsass.exe",
        "lsm.exe",
        "memory compression",
        "msmpeng.exe",
        "registry",
        "secure system",
        "services.exe",
        "sihost.exe",
        "smss.exe",
        "spoolsv.exe",
        "system",
        "system idle process",
        "taskhostw.exe",
        "userinit.exe",
        "wininit.exe",
        "winlogon.exe",
        "wmiprvse.exe",
        "wudfhost.exe",
        "svchost.exe",
        "conhost.exe",
    }
)


def normalize_process_name(name: str | None) -> str:
    """Normalize process names for case-insensitive matching."""

    return (name or "").strip().lower()


def normalize_path(path: str | None) -> str:
    """Normalize executable paths for case-insensitive matching on Windows."""

    if not path:
        return ""
    return os.path.normcase(str(Path(path).expanduser()))


class Whitelist:
    """Combines built-in protected processes with user-defined exclusions."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    @property
    def default_names(self) -> set[str]:
        """Return built-in protected process names."""

        return set(DEFAULT_PROCESS_WHITELIST)

    @property
    def user_names(self) -> set[str]:
        """Return user-defined protected process names."""

        return {normalize_process_name(name) for name in self._config.user_whitelist_names if name.strip()}

    @property
    def user_paths(self) -> set[str]:
        """Return user-defined protected executable paths."""

        return {normalize_path(path) for path in self._config.user_whitelist_paths if path.strip()}

    @property
    def all_names(self) -> set[str]:
        """Return all protected process names."""

        return self.default_names | self.user_names

    def is_whitelisted(self, name: str | None, exe_path: str | None = None) -> bool:
        """Return True when the process should never be touched automatically."""

        normalized_name = normalize_process_name(name)
        normalized_path = normalize_path(exe_path)
        if normalized_name and normalized_name in self.all_names:
            return True
        return bool(normalized_path and normalized_path in self.user_paths)

    def add_name(self, name: str) -> bool:
        """Add a user process name to the whitelist."""

        normalized = normalize_process_name(name)
        if not normalized or normalized in self.user_names:
            return False
        self._config.user_whitelist_names.append(normalized)
        self._config.user_whitelist_names = sorted(self.user_names)
        return True

    def add_path(self, path: str) -> bool:
        """Add a user executable path to the whitelist."""

        normalized = normalize_path(path)
        if not normalized or normalized in self.user_paths:
            return False
        self._config.user_whitelist_paths.append(normalized)
        self._config.user_whitelist_paths = sorted(self.user_paths)
        return True

    def remove_name(self, name: str) -> bool:
        """Remove a user-defined process name from the whitelist."""

        normalized = normalize_process_name(name)
        if normalized not in self.user_names:
            return False
        self._config.user_whitelist_names = sorted(self.user_names - {normalized})
        return True

    def remove_path(self, path: str) -> bool:
        """Remove a user-defined executable path from the whitelist."""

        normalized = normalize_path(path)
        if normalized not in self.user_paths:
            return False
        self._config.user_whitelist_paths = sorted(self.user_paths - {normalized})
        return True
