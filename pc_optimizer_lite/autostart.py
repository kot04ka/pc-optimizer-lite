"""Current-user Windows autostart integration."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import __app_name__

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = __app_name__


def get_launch_command() -> str:
    """Return the command stored in HKCU Run for tray startup."""

    if getattr(sys, "frozen", False):
        args = [sys.executable, "--tray"]
    else:
        script = Path(sys.argv[0]).resolve()
        args = [sys.executable, str(script), "--tray"]
    return subprocess.list2cmdline(args)


def enable_autostart() -> str:
    """Enable PC Optimizer Lite autostart for the current Windows user."""

    if os.name != "nt":
        raise OSError("Autostart through HKCU Run is available only on Windows")
    import winreg

    command = get_launch_command()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
    return command


def disable_autostart() -> None:
    """Disable PC Optimizer Lite autostart for the current Windows user."""

    if os.name != "nt":
        return
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return


def is_autostart_enabled() -> bool:
    """Return True when the current launch command is registered in HKCU Run."""

    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return False
    return str(value).strip() == get_launch_command()
