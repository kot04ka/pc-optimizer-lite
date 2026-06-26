"""Activity and data-risk policy for sleep mode."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Literal

try:
    import win32gui
    import win32process
except ModuleNotFoundError:  # pragma: no cover - optional Windows integration
    win32gui = None  # type: ignore[assignment]
    win32process = None  # type: ignore[assignment]

LOGGER = logging.getLogger(__name__)

SleepStrategy = Literal["priority", "suspend"]

DATA_RISK_PROCESS_NAMES = {
    "acrord32.exe",
    "code.exe",
    "devenv.exe",
    "excel.exe",
    "idea64.exe",
    "notepad.exe",
    "notepad++.exe",
    "outlook.exe",
    "photoshop.exe",
    "powerpnt.exe",
    "pycharm64.exe",
    "sublime_text.exe",
    "winword.exe",
    "wordpad.exe",
}

UNSAVED_TITLE_MARKERS = (
    "*",
    "untitled",
    "unsaved",
    "document1",
    "book1",
    "presentation1",
    "без имени",
    "несохран",
)


@dataclass(frozen=True, slots=True)
class SleepStrategyDecision:
    """Chosen sleep strategy and the policy reason behind it."""

    strategy: SleepStrategy
    reason: str


def choose_sleep_strategy(
    *,
    name: str,
    has_visible_window: bool,
    window_title: str = "",
) -> SleepStrategyDecision:
    """Choose a conservative sleep strategy for one already-eligible process."""

    normalized_name = name.strip().lower()
    if has_visible_window:
        if window_title_has_unsaved_marker(window_title):
            return SleepStrategyDecision("priority", "visible window with unsaved data marker")
        if normalized_name in DATA_RISK_PROCESS_NAMES:
            return SleepStrategyDecision("priority", "visible data-risk application")
        return SleepStrategyDecision("priority", "visible interactive window")
    if normalized_name in DATA_RISK_PROCESS_NAMES:
        return SleepStrategyDecision("priority", "known data-risk application")
    return SleepStrategyDecision("suspend", "headless safe candidate")


def window_title_has_unsaved_marker(window_title: str) -> bool:
    """Return True when a title looks like it contains unsaved work."""

    normalized = window_title.strip().lower()
    if not normalized:
        return False
    if normalized.startswith("*"):
        return True
    return any(marker in normalized for marker in UNSAVED_TITLE_MARKERS[1:])


def get_window_titles_by_pid(pids: Iterable[int] | None = None) -> dict[int, str]:
    """Return first visible top-level window title for each requested PID."""

    if win32gui is None or win32process is None:
        return {}
    requested = {int(pid) for pid in pids} if pids is not None else None
    titles: dict[int, str] = {}

    def callback(hwnd: int, _: object) -> bool:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = str(win32gui.GetWindowText(hwnd) or "").strip()
            if not title:
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            pid = int(pid)
            if requested is not None and pid not in requested:
                return True
            titles.setdefault(pid, title)
        except Exception:
            LOGGER.debug("Window title lookup failed for hwnd=%s", hwnd, exc_info=True)
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        LOGGER.debug("EnumWindows failed while collecting window titles", exc_info=True)
    return titles


def get_cursor_window_pid() -> int | None:
    """Return the PID for the window currently under the mouse cursor."""

    if win32gui is None or win32process is None:
        return None
    try:
        hwnd = win32gui.WindowFromPoint(win32gui.GetCursorPos())
        if not hwnd:
            return None
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return int(pid) if pid else None
    except Exception:
        LOGGER.debug("Cursor window PID lookup failed", exc_info=True)
        return None
