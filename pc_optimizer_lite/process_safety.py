"""Shared guards for processes that must stay responsive to user input."""

from __future__ import annotations

from .smart_process_manager import get_foreground_pid, is_related_to_pid

INTERACTIVE_INPUT_PROCESS_NAMES = {
    "brave.exe",
    "chrome.exe",
    "code.exe",
    "discord.exe",
    "excel.exe",
    "firefox.exe",
    "idea64.exe",
    "msedge.exe",
    "notepad.exe",
    "notepad++.exe",
    "opera.exe",
    "outlook.exe",
    "powerpnt.exe",
    "pycharm64.exe",
    "signal.exe",
    "slack.exe",
    "telegram.exe",
    "teams.exe",
    "vivaldi.exe",
    "whatsapp.exe",
    "winword.exe",
    "zoom.exe",
}


def is_known_interactive_process_name(name: str) -> bool:
    """Return True for apps where priority/affinity changes hurt input latency."""

    return name.strip().lower() in INTERACTIVE_INPUT_PROCESS_NAMES


def is_pid_foreground_related(pid: int, foreground_pid: int | None = None) -> bool:
    """Return True when a PID is the active app or in its parent/child chain."""

    active_pid = get_foreground_pid() if foreground_pid is None else foreground_pid
    if not active_pid:
        return False
    return is_related_to_pid(pid, active_pid)


def is_interactive_process(
    *,
    pid: int,
    name: str,
    has_window: bool = False,
    is_foreground_related: bool = False,
    foreground_pid: int | None = None,
    check_current_foreground: bool = True,
) -> bool:
    """Return True for processes that should never be priority/affinity-limited."""

    return (
        bool(is_foreground_related)
        or bool(has_window)
        or is_known_interactive_process_name(name)
        or (check_current_foreground and is_pid_foreground_related(pid, foreground_pid))
    )
