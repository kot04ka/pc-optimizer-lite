"""Sleep mode for inactive foreground-window applications."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import psutil

from .history_manager import HistoryManager
from .smart_process_manager import get_foreground_pid, get_visible_window_pids, is_related_to_pid
from .whitelist import Whitelist

LOGGER = logging.getLogger(__name__)

SKIP_SLEEP_NAMES = {
    "audiodg.exe",
    "discord.exe",
    "msedge.exe",
    "onedrive.exe",
    "obs64.exe",
    "spotify.exe",
    "steam.exe",
    "teams.exe",
    "telegram.exe",
    "vlc.exe",
    "wmplayer.exe",
    "zoom.exe",
}


@dataclass(slots=True)
class SleepEntry:
    """One currently sleeping process."""

    pid: int
    name: str
    exe: str
    slept_at: float
    reason: str
    previous_priority: int | None = None
    suspended: bool = False


@dataclass(slots=True)
class SleepAction:
    """Result of sleep/resume work."""

    pid: int
    name: str
    action: str
    success: bool
    message: str


class SleepManager:
    """Tracks window focus and suspends safe inactive apps when enabled."""

    def __init__(self, whitelist: Whitelist, history: HistoryManager) -> None:
        self.whitelist = whitelist
        self.history = history
        self._own_pid = os.getpid()
        self._last_focus_by_pid: dict[int, float] = {}
        self._sleeping: dict[int, SleepEntry] = {}
        self._last_io_by_pid: dict[int, tuple[int, int]] = {}

    @property
    def sleeping(self) -> list[SleepEntry]:
        """Return sleeping processes newest first."""

        return sorted(self._sleeping.values(), key=lambda entry: entry.slept_at, reverse=True)

    def poll(self, enabled: bool, idle_minutes: float, max_actions: int = 2) -> list[SleepAction]:
        """Update focus state, resume focused apps, and sleep eligible inactive apps."""

        now = time.monotonic()
        foreground_pid = get_foreground_pid()
        visible_pids = get_visible_window_pids()
        actions: list[SleepAction] = []

        if foreground_pid:
            self._last_focus_by_pid[foreground_pid] = now
            if foreground_pid in self._sleeping:
                actions.append(self.resume_process(foreground_pid, "foreground"))

        for pid in visible_pids:
            self._last_focus_by_pid.setdefault(pid, now)

        if not enabled:
            return actions

        for pid in list(visible_pids):
            if len([action for action in actions if action.action == "sleep"]) >= max_actions:
                break
            if pid == foreground_pid or pid in self._sleeping:
                continue
            last_focus = self._last_focus_by_pid.get(pid, now)
            if now - last_focus < idle_minutes * 60.0:
                continue
            action = self.sleep_process(pid, f"Inactive for {idle_minutes:.0f}+ min")
            if action:
                actions.append(action)
        return actions

    def sleep_process(self, pid: int, reason: str) -> SleepAction | None:
        """Set process to idle priority and suspend it if safety checks pass."""

        try:
            proc = psutil.Process(pid)
            name = proc.name()
            exe = _safe_exe(proc)
            if not self._is_sleep_safe(proc, name, exe):
                return None
            previous_priority = _safe_nice(proc)
            if os.name == "nt":
                proc.nice(psutil.IDLE_PRIORITY_CLASS)
            else:
                proc.nice(19)
            suspended = False
            try:
                proc.suspend()
                suspended = True
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                suspended = False
            entry = SleepEntry(
                pid=pid,
                name=name,
                exe=exe,
                slept_at=time.time(),
                reason=reason,
                previous_priority=previous_priority,
                suspended=suspended,
            )
            self._sleeping[pid] = entry
            self.history.add_event("sleep", f"Slept {name}", reason, "info")
            LOGGER.info("Slept pid=%s name=%s suspended=%s reason=%s", pid, name, suspended, reason)
            return SleepAction(pid, name, "sleep", True, reason)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess) as exc:
            LOGGER.debug("Sleep skipped for pid=%s: %s", pid, exc)
            return SleepAction(pid, str(pid), "sleep", False, str(exc))

    def sleep_process_from_snapshot(
        self,
        pid: int,
        name: str,
        exe: str,
        previous_priority: int | None,
        reason: str,
    ) -> SleepAction | None:
        """Sleep a process already vetted by a shared optimization snapshot."""

        if pid == self._own_pid:
            return None
        if name.lower() in SKIP_SLEEP_NAMES:
            return None
        if self.whitelist.is_whitelisted(name, exe):
            return None
        try:
            proc = psutil.Process(pid)
            if previous_priority is None:
                previous_priority = _safe_nice(proc)
            if os.name == "nt":
                proc.nice(psutil.IDLE_PRIORITY_CLASS)
            else:
                proc.nice(19)
            suspended = False
            try:
                proc.suspend()
                suspended = True
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                suspended = False
            entry = SleepEntry(
                pid=pid,
                name=name,
                exe=exe,
                slept_at=time.time(),
                reason=reason,
                previous_priority=previous_priority,
                suspended=suspended,
            )
            self._sleeping[pid] = entry
            self.history.add_event("sleep", f"Slept {name}", reason, "info")
            LOGGER.info("Slept from snapshot pid=%s name=%s suspended=%s reason=%s", pid, name, suspended, reason)
            return SleepAction(pid, name, "sleep", True, reason)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess) as exc:
            LOGGER.debug("Snapshot sleep skipped for pid=%s: %s", pid, exc)
            return SleepAction(pid, name or str(pid), "sleep", False, str(exc))

    def resume_process(self, pid: int, reason: str = "manual") -> SleepAction:
        """Resume a sleeping process and restore normal priority."""

        entry = self._sleeping.pop(pid, None)
        try:
            proc = psutil.Process(pid)
            name = proc.name()
            if entry and entry.suspended:
                proc.resume()
            if os.name == "nt":
                proc.nice(psutil.NORMAL_PRIORITY_CLASS)
            elif entry and entry.previous_priority is not None:
                proc.nice(entry.previous_priority)
            self.history.add_event("wake", f"Woke {name}", reason, "success")
            LOGGER.info("Resumed pid=%s name=%s reason=%s", pid, name, reason)
            return SleepAction(pid, name, "wake", True, reason)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess) as exc:
            LOGGER.warning("Resume failed for pid=%s: %s", pid, exc)
            return SleepAction(pid, entry.name if entry else str(pid), "wake", False, str(exc))

    def _is_sleep_safe(self, proc: psutil.Process, name: str, exe: str) -> bool:
        if proc.pid == self._own_pid:
            return False
        if name.lower() in SKIP_SLEEP_NAMES:
            return False
        if self.whitelist.is_whitelisted(name, exe):
            return False
        foreground_pid = get_foreground_pid()
        if foreground_pid and is_related_to_pid(proc.pid, foreground_pid):
            return False
        if _has_active_network(proc):
            return False
        if self._has_active_io(proc):
            return False
        return True

    def _has_active_io(self, proc: psutil.Process) -> bool:
        try:
            counters = proc.io_counters()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, AttributeError):
            return True
        current = (int(counters.read_bytes), int(counters.write_bytes))
        previous = self._last_io_by_pid.get(proc.pid)
        self._last_io_by_pid[proc.pid] = current
        if previous is None:
            return True
        return (current[0] - previous[0]) + (current[1] - previous[1]) > 1_000_000


def _has_active_network(proc: psutil.Process) -> bool:
    try:
        connections = proc.net_connections(kind="inet")
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, AttributeError):
        try:
            connections = proc.connections(kind="inet")
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, AttributeError):
            return True
    return any(getattr(conn, "status", "") == psutil.CONN_ESTABLISHED for conn in connections)


def _safe_exe(proc: psutil.Process) -> str:
    try:
        return proc.exe()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return ""


def _safe_nice(proc: psutil.Process) -> int | None:
    try:
        return int(proc.nice())
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, ValueError):
        return None
