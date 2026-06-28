"""Sleep mode for inactive foreground-window applications."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import psutil

from .history_manager import HistoryManager
from .safety.activity_detector import choose_sleep_strategy, get_cursor_window_pid, get_window_titles_by_pid
from .smart_process_manager import get_foreground_pid, get_visible_window_pids, is_related_to_pid
from .whitelist import Whitelist

LOGGER = logging.getLogger(__name__)

SKIP_SLEEP_NAMES = {
    "audiodg.exe",
    "discord.exe",
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
    strategy: str = "suspend"


@dataclass(slots=True)
class AppUsageStats:
    """Observed foreground usage for one windowed process."""

    pid: int
    name: str = ""
    exe: str = ""
    focus_count: int = 0
    total_focus_seconds: float = 0.0
    last_focused_at: float = 0.0
    current_focus_started_at: float | None = None


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
        self._usage_stats: dict[int, AppUsageStats] = {}
        self._foreground_pid: int | None = None

    @property
    def sleeping(self) -> list[SleepEntry]:
        """Return sleeping processes newest first."""

        return sorted(self._sleeping.values(), key=lambda entry: entry.slept_at, reverse=True)

    @property
    def usage_stats(self) -> dict[int, AppUsageStats]:
        """Return foreground usage counters keyed by pid."""

        return self._usage_stats

    def resume_foreground_if_sleeping(self) -> list[SleepAction]:
        """Resume a suspended app as soon as Windows reports it as foreground."""

        foreground_pid = get_foreground_pid()
        return self._resume_for_active_pid(foreground_pid, "foreground")

    def resume_user_target_if_sleeping(self) -> list[SleepAction]:
        """Resume sleeping apps targeted by focus or by the mouse cursor."""

        actions = self.resume_foreground_if_sleeping()
        resumed_pids = {action.pid for action in actions if action.success}
        cursor_pid = get_cursor_window_pid()
        if cursor_pid and cursor_pid not in resumed_pids:
            actions.extend(self._resume_for_active_pid(cursor_pid, "cursor"))
        return actions

    def poll(self, enabled: bool, idle_minutes: float, max_actions: int = 2) -> list[SleepAction]:
        """Update focus state, resume focused apps, and sleep eligible inactive apps."""

        now = time.monotonic()
        foreground_pid = get_foreground_pid()
        visible_pids = get_visible_window_pids()
        actions: list[SleepAction] = []
        self._record_focus_transition(foreground_pid, now)

        if foreground_pid:
            self._update_usage_metadata(foreground_pid)
            self._last_focus_by_pid[foreground_pid] = now
            actions.extend(self._resume_for_active_pid(foreground_pid, "foreground"))

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
            if self._is_recently_reused(pid, now, idle_minutes):
                continue
            action = self.sleep_process(
                pid,
                f"Inactive for {idle_minutes:.0f}+ min",
                has_visible_window=pid in visible_pids,
            )
            if action:
                actions.append(action)
        return actions

    def sleep_process(
        self,
        pid: int,
        reason: str,
        *,
        has_visible_window: bool | None = None,
        window_title: str = "",
    ) -> SleepAction | None:
        """Apply the selected sleep strategy after safety checks pass."""

        try:
            proc = psutil.Process(pid)
            name = proc.name()
            exe = _safe_exe(proc)
            if not self._is_sleep_safe(proc, name, exe):
                return None
            if has_visible_window is None:
                has_visible_window = pid in get_visible_window_pids()
            if has_visible_window and not window_title:
                window_title = get_window_titles_by_pid({pid}).get(pid, "")
            decision = choose_sleep_strategy(
                name=name,
                has_visible_window=bool(has_visible_window),
                window_title=window_title,
            )
            if decision.strategy == "suspend" and (_has_active_network(proc) or self._has_active_io(proc)):
                return None
            previous_priority = _safe_nice(proc)
            if os.name == "nt":
                proc.nice(psutil.IDLE_PRIORITY_CLASS)
            else:
                proc.nice(19)
            suspended = False
            if decision.strategy == "suspend":
                try:
                    proc.suspend()
                    suspended = True
                except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                    suspended = False
            sleep_reason = _combine_reason(reason, decision.reason)
            entry = SleepEntry(
                pid=pid,
                name=name,
                exe=exe,
                slept_at=time.time(),
                reason=sleep_reason,
                previous_priority=previous_priority,
                suspended=suspended,
                strategy=decision.strategy,
            )
            self._sleeping[pid] = entry
            self.history.add_event("sleep", f"Slept {name}", sleep_reason, "info")
            LOGGER.info(
                "Slept pid=%s name=%s strategy=%s suspended=%s reason=%s",
                pid,
                name,
                decision.strategy,
                suspended,
                sleep_reason,
            )
            return SleepAction(pid, name, "sleep", True, sleep_reason)
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
        *,
        has_visible_window: bool = False,
        window_title: str = "",
    ) -> SleepAction | None:
        """Sleep a process already vetted by a shared optimization snapshot."""

        if pid == self._own_pid:
            return None
        if name.lower() in SKIP_SLEEP_NAMES:
            return None
        if self.whitelist.is_whitelisted(name, exe):
            return None
        try:
            decision = choose_sleep_strategy(
                name=name,
                has_visible_window=has_visible_window,
                window_title=window_title,
            )
            proc = psutil.Process(pid)
            if decision.strategy == "suspend" and (_has_active_network(proc) or self._has_active_io(proc)):
                return None
            if previous_priority is None:
                previous_priority = _safe_nice(proc)
            if os.name == "nt":
                proc.nice(psutil.IDLE_PRIORITY_CLASS)
            else:
                proc.nice(19)
            suspended = False
            if decision.strategy == "suspend":
                try:
                    proc.suspend()
                    suspended = True
                except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                    suspended = False
            sleep_reason = _combine_reason(reason, decision.reason)
            entry = SleepEntry(
                pid=pid,
                name=name,
                exe=exe,
                slept_at=time.time(),
                reason=sleep_reason,
                previous_priority=previous_priority,
                suspended=suspended,
                strategy=decision.strategy,
            )
            self._sleeping[pid] = entry
            self.history.add_event("sleep", f"Slept {name}", sleep_reason, "info")
            LOGGER.info(
                "Slept from snapshot pid=%s name=%s strategy=%s suspended=%s reason=%s",
                pid,
                name,
                decision.strategy,
                suspended,
                sleep_reason,
            )
            return SleepAction(pid, name, "sleep", True, sleep_reason)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess) as exc:
            LOGGER.debug("Snapshot sleep skipped for pid=%s: %s", pid, exc)
            return SleepAction(pid, name or str(pid), "sleep", False, str(exc))

    def resume_process(self, pid: int, reason: str = "manual") -> SleepAction:
        """Resume a sleeping process and restore normal priority."""

        entry = self._sleeping.get(pid)
        try:
            proc = psutil.Process(pid)
            name = proc.name()
            if entry and entry.suspended:
                proc.resume()
            if os.name == "nt":
                proc.nice(psutil.NORMAL_PRIORITY_CLASS)
            elif entry and entry.previous_priority is not None:
                proc.nice(entry.previous_priority)
            self._sleeping.pop(pid, None)
            self.history.add_event("wake", f"Woke {name}", reason, "success")
            LOGGER.info("Resumed pid=%s name=%s reason=%s", pid, name, reason)
            return SleepAction(pid, name, "wake", True, reason)
        except (psutil.NoSuchProcess, psutil.ZombieProcess) as exc:
            self._sleeping.pop(pid, None)
            LOGGER.warning("Resume skipped for missing pid=%s: %s", pid, exc)
            return SleepAction(pid, entry.name if entry else str(pid), "wake", False, str(exc))
        except psutil.AccessDenied as exc:
            LOGGER.warning("Resume failed for pid=%s: %s", pid, exc)
            return SleepAction(pid, entry.name if entry else str(pid), "wake", False, str(exc))

    def _resume_for_active_pid(self, active_pid: int | None, reason: str) -> list[SleepAction]:
        if not active_pid:
            return []
        wake_pids: list[int] = []
        if active_pid in self._sleeping:
            wake_pids.append(active_pid)
        for pid in list(self._sleeping):
            if pid == active_pid:
                continue
            if is_related_to_pid(pid, active_pid):
                wake_pids.append(pid)
        return [
            self.resume_process(pid, reason if pid == active_pid else f"{reason} related")
            for pid in wake_pids
        ]

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
        return True

    def _record_focus_transition(self, foreground_pid: int | None, now: float) -> None:
        if foreground_pid == self._foreground_pid:
            return
        previous_pid = self._foreground_pid
        if previous_pid is not None:
            previous_stats = self._usage_stats.get(previous_pid)
            if previous_stats and previous_stats.current_focus_started_at is not None:
                previous_stats.total_focus_seconds += max(0.0, now - previous_stats.current_focus_started_at)
                previous_stats.current_focus_started_at = None
        self._foreground_pid = foreground_pid
        if foreground_pid is None:
            return
        stats = self._usage_stats.setdefault(foreground_pid, AppUsageStats(pid=foreground_pid))
        stats.focus_count += 1
        stats.last_focused_at = now
        stats.current_focus_started_at = now

    def _update_usage_metadata(self, pid: int) -> None:
        stats = self._usage_stats.setdefault(pid, AppUsageStats(pid=pid))
        if stats.name and stats.exe:
            return
        try:
            proc = psutil.Process(pid)
            stats.name = proc.name()
            stats.exe = _safe_exe(proc)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            return

    def _is_recently_reused(self, pid: int, now: float, idle_minutes: float) -> bool:
        stats = self._usage_stats.get(pid)
        if stats is None or stats.focus_count < 3:
            return False
        reuse_window_seconds = max(idle_minutes * 60.0, 300.0) * 2.0
        return now - stats.last_focused_at < reuse_window_seconds

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


def _combine_reason(reason: str, policy_reason: str) -> str:
    if not policy_reason:
        return reason
    return f"{reason}; {policy_reason}"
