"""Candidate detection for conservative smart process closing."""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass

import psutil

from .history_manager import HistoryManager
from .monitor import ProcessInfo
from .whitelist import Whitelist

try:
    import win32gui
    import win32process
except ModuleNotFoundError:  # pragma: no cover - optional Windows integration
    win32gui = None  # type: ignore[assignment]
    win32process = None  # type: ignore[assignment]

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class CloseCandidate:
    """A process that may be closed after policy and user confirmation."""

    pid: int
    name: str
    exe: str
    reason: str
    detail: str
    cpu_percent: float
    memory_percent: float
    mode_hint: str = "ask"


class SmartProcessManager:
    """Finds and closes only non-whitelisted low-confidence background clutter."""

    def __init__(self, whitelist: Whitelist, history: HistoryManager) -> None:
        self.whitelist = whitelist
        self.history = history
        self._own_pid = os.getpid()
        self._first_seen_without_window: dict[int, float] = {}
        self._last_candidate_prompt: dict[int, float] = {}

    def find_candidates(
        self,
        processes: list[ProcessInfo],
        min_background_minutes: float,
        cpu_threshold: float,
        memory_threshold: float,
        duplicate_count: int,
    ) -> list[CloseCandidate]:
        """Return close candidates after whitelist, foreground, and relation checks."""

        now = time.monotonic()
        visible_pids = get_visible_window_pids()
        foreground_pid = get_foreground_pid()
        by_key: dict[str, list[ProcessInfo]] = defaultdict(list)
        candidates: list[CloseCandidate] = []

        for process in processes:
            key = (process.exe or process.name).lower()
            if key:
                by_key[key].append(process)

            if not self._is_safe_to_consider(process, foreground_pid):
                continue

            has_window = process.pid in visible_pids
            if has_window:
                self._first_seen_without_window.pop(process.pid, None)
                continue

            first_seen = self._first_seen_without_window.setdefault(process.pid, now)
            old_enough = now - first_seen >= min_background_minutes * 60.0
            resource_heavy = (
                process.cpu_percent >= cpu_threshold or process.memory_percent >= memory_threshold
            )
            if old_enough and resource_heavy:
                candidates.append(
                    CloseCandidate(
                        pid=process.pid,
                        name=process.name,
                        exe=process.exe,
                        reason="background_no_window",
                        detail=(
                            f"No visible window for {min_background_minutes:.0f}+ min; "
                            f"CPU {process.cpu_percent:.1f}%, RAM {process.memory_percent:.1f}%"
                        ),
                        cpu_percent=process.cpu_percent,
                        memory_percent=process.memory_percent,
                    )
                )

        candidates.extend(
            self._find_duplicate_candidates(
                by_key=by_key,
                foreground_pid=foreground_pid,
                duplicate_count=duplicate_count,
                cpu_threshold=cpu_threshold,
                memory_threshold=memory_threshold,
            )
        )

        unique: dict[int, CloseCandidate] = {}
        for candidate in candidates:
            if time.monotonic() - self._last_candidate_prompt.get(candidate.pid, 0.0) < 180.0:
                continue
            unique[candidate.pid] = candidate
        return sorted(unique.values(), key=lambda item: (item.cpu_percent, item.memory_percent), reverse=True)

    def mark_prompted(self, pid: int) -> None:
        """Avoid repeatedly asking about the same process."""

        self._last_candidate_prompt[pid] = time.monotonic()

    def close_candidate(self, candidate: CloseCandidate, mode: str) -> tuple[bool, str]:
        """Terminate one candidate and persist history. Never uses kill()."""

        try:
            proc = psutil.Process(candidate.pid)
            name = proc.name()
            exe = _safe_exe(proc)
            if not self._is_safe_to_touch(proc, name, exe, get_foreground_pid()):
                return False, "Process is protected or related to the active app"
            proc.terminate()
            self.history.add_closed_process(
                pid=candidate.pid,
                name=name,
                exe=exe,
                reason=candidate.detail,
                mode=mode,
            )
            LOGGER.info("Smart close requested for pid=%s name=%s reason=%s", candidate.pid, name, candidate.detail)
            return True, "Terminate signal sent"
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess) as exc:
            LOGGER.warning("Smart close failed for pid=%s: %s", candidate.pid, exc)
            return False, str(exc)

    def _find_duplicate_candidates(
        self,
        by_key: dict[str, list[ProcessInfo]],
        foreground_pid: int | None,
        duplicate_count: int,
        cpu_threshold: float,
        memory_threshold: float,
    ) -> list[CloseCandidate]:
        candidates: list[CloseCandidate] = []
        hung_pids = get_hung_window_pids()
        for group in by_key.values():
            if len(group) < duplicate_count:
                continue
            sorted_group = sorted(group, key=lambda item: (item.cpu_percent, item.memory_percent), reverse=True)
            for process in sorted_group[duplicate_count - 1 :]:
                if not self._is_safe_to_consider(process, foreground_pid):
                    continue
                if process.pid in hung_pids or process.cpu_percent >= cpu_threshold or process.memory_percent >= memory_threshold:
                    candidates.append(
                        CloseCandidate(
                            pid=process.pid,
                            name=process.name,
                            exe=process.exe,
                            reason="duplicate_or_hung",
                            detail=(
                                f"Duplicate process group has {len(group)} copies; "
                                f"status={process.status}; CPU {process.cpu_percent:.1f}%, "
                                f"RAM {process.memory_percent:.1f}%"
                            ),
                            cpu_percent=process.cpu_percent,
                            memory_percent=process.memory_percent,
                        )
                    )
        return candidates

    def _is_safe_to_consider(self, process: ProcessInfo, foreground_pid: int | None) -> bool:
        if process.pid == self._own_pid:
            return False
        if self.whitelist.is_whitelisted(process.name, process.exe):
            return False
        if foreground_pid and is_related_to_pid(process.pid, foreground_pid):
            return False
        return True

    def _is_safe_to_touch(
        self,
        proc: psutil.Process,
        name: str,
        exe: str,
        foreground_pid: int | None,
    ) -> bool:
        if proc.pid == self._own_pid:
            return False
        if self.whitelist.is_whitelisted(name, exe):
            return False
        if foreground_pid and is_related_to_pid(proc.pid, foreground_pid):
            return False
        return True


def get_visible_window_pids() -> set[int]:
    """Return PIDs with visible top-level windows when pywin32 is available."""

    if win32gui is None or win32process is None:
        return set()
    pids: set[int] = set()

    def callback(hwnd: int, _: object) -> bool:
        try:
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid:
                    pids.add(int(pid))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        LOGGER.debug("EnumWindows failed while collecting visible windows", exc_info=True)
    return pids


def get_hung_window_pids() -> set[int]:
    """Return PIDs with windows reported as hung/not responding."""

    if win32gui is None or win32process is None:
        return set()
    pids: set[int] = set()

    def callback(hwnd: int, _: object) -> bool:
        try:
            if win32gui.IsWindowVisible(hwnd) and win32gui.IsHungAppWindow(hwnd):
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid:
                    pids.add(int(pid))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        LOGGER.debug("EnumWindows failed while collecting hung windows", exc_info=True)
    return pids


def get_foreground_pid() -> int | None:
    """Return the active foreground window PID."""

    if win32gui is None or win32process is None:
        return None
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return int(pid) if pid else None
    except Exception:
        return None


def is_related_to_pid(pid: int, active_pid: int) -> bool:
    """Return True when pid is parent/child-related to active_pid."""

    if pid == active_pid:
        return True
    try:
        proc = psutil.Process(pid)
        active = psutil.Process(active_pid)
        active_children = {child.pid for child in active.children(recursive=True)}
        if pid in active_children:
            return True
        parent = proc.parent()
        while parent is not None:
            if parent.pid == active_pid:
                return True
            parent = parent.parent()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return True
    return False


def _safe_exe(proc: psutil.Process) -> str:
    try:
        return proc.exe()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return ""
