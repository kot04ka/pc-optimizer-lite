"""Targeted RAM cleanup for Windows.

The cleaner does not close applications. In light mode it calls the Windows
EmptyWorkingSet API for conservative candidates so Windows can page out unused
physical memory. Deep mode additionally tries to purge system memory lists and
requires administrator rights.
"""

from __future__ import annotations

import ctypes
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum

import psutil

from .monitor import ProcessInfo
from .sleep_manager import SKIP_SLEEP_NAMES
from .smart_process_manager import get_foreground_pid, get_visible_window_pids, is_related_to_pid
from .whitelist import Whitelist

LOGGER = logging.getLogger(__name__)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_QUOTA = 0x0100
PROCESS_VM_OPERATION = 0x0008
SYSTEM_MEMORY_LIST_INFORMATION = 0x50
MEMORY_FLUSH_MODIFIED_LIST = 3
MEMORY_PURGE_STANDBY_LIST = 4


class RamCleanMode(str, Enum):
    """RAM cleanup intensity."""

    LIGHT = "light"
    DEEP = "deep"


@dataclass(slots=True)
class RamProcessCleanResult:
    """Before/after RAM cleanup for one process."""

    pid: int
    name: str
    exe: str
    before_rss: int
    after_rss: int
    success: bool
    message: str = ""

    @property
    def freed_bytes(self) -> int:
        """Return best-effort RSS reduction."""

        return max(0, self.before_rss - self.after_rss)


@dataclass(slots=True)
class RamCleanResult:
    """Full RAM cleanup report."""

    mode: RamCleanMode
    ram_used_before: int
    ram_used_after: int
    ram_percent_before: float
    ram_percent_after: float
    process_results: list[RamProcessCleanResult] = field(default_factory=list)
    standby_purged: bool = False
    modified_purged: bool = False
    admin_required: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def freed_bytes(self) -> int:
        """Return memory freed from system perspective."""

        return max(0, self.ram_used_before - self.ram_used_after)


class RamCleaner:
    """Conservative Windows RAM cleaner."""

    def __init__(self, whitelist: Whitelist) -> None:
        self.whitelist = whitelist
        self._own_pid = os.getpid()
        self._low_cpu_since: dict[int, float] = {}
        self._last_known_processes: list[ProcessInfo] = []

    def observe_processes(self, processes: list[ProcessInfo]) -> None:
        """Track low-CPU processes over time for later targeted cleanup."""

        now = time.monotonic()
        self._last_known_processes = list(processes)
        seen: set[int] = set()
        for process in processes:
            seen.add(process.pid)
            if process.cpu_percent < 1.0:
                self._low_cpu_since.setdefault(process.pid, now)
            else:
                self._low_cpu_since.pop(process.pid, None)
        for pid in list(self._low_cpu_since):
            if pid not in seen:
                self._low_cpu_since.pop(pid, None)

    def clean(
        self,
        mode: RamCleanMode = RamCleanMode.LIGHT,
        min_low_cpu_seconds: float = 120.0,
        *,
        batch_size: int = 8,
        pause_seconds: float = 0.07,
    ) -> RamCleanResult:
        """Run targeted RAM cleanup and return a detailed report."""

        before = psutil.virtual_memory()
        result = RamCleanResult(
            mode=mode,
            ram_used_before=before.used,
            ram_used_after=before.used,
            ram_percent_before=float(before.percent),
            ram_percent_after=float(before.percent),
        )
        candidates = self.find_candidates(min_low_cpu_seconds=min_low_cpu_seconds)
        for index, proc in enumerate(candidates, start=1):
            item = self._empty_working_set(proc)
            result.process_results.append(item)
            if pause_seconds and index % max(1, int(batch_size)) == 0:
                time.sleep(max(0.0, float(pause_seconds)))

        if mode == RamCleanMode.DEEP:
            if not is_admin():
                result.admin_required = True
                result.errors.append("Deep RAM cleanup requires administrator rights.")
            else:
                result.modified_purged = purge_memory_list(MEMORY_FLUSH_MODIFIED_LIST)
                result.standby_purged = purge_memory_list(MEMORY_PURGE_STANDBY_LIST)

        time.sleep(0.4)
        after = psutil.virtual_memory()
        result.ram_used_after = after.used
        result.ram_percent_after = float(after.percent)
        LOGGER.info(
            "RAM cleanup mode=%s before=%s after=%s processes=%s standby=%s modified=%s errors=%s",
            mode.value,
            result.ram_used_before,
            result.ram_used_after,
            len(result.process_results),
            result.standby_purged,
            result.modified_purged,
            len(result.errors),
        )
        return result

    def find_candidates(self, min_low_cpu_seconds: float = 120.0) -> list[psutil.Process]:
        """Return non-whitelisted, inactive process candidates sorted by RSS."""

        foreground_pid = get_foreground_pid()
        visible_pids = get_visible_window_pids()
        now = time.monotonic()
        candidates: list[tuple[int, psutil.Process]] = []
        scanned = 0
        for proc in psutil.process_iter(attrs=("pid", "name", "exe", "memory_info", "cpu_percent")):
            scanned += 1
            if scanned % 40 == 0:
                time.sleep(0.02)
            try:
                pid = int(proc.info.get("pid") or proc.pid)
                name = str(proc.info.get("name") or proc.name())
                exe = str(proc.info.get("exe") or "")
                if not self._is_candidate_safe(proc, name, exe, foreground_pid, visible_pids):
                    continue
                low_since = self._low_cpu_since.get(pid)
                no_window = pid not in visible_pids
                low_cpu_long_enough = bool(low_since and now - low_since >= min_low_cpu_seconds)
                current_cpu_low = float(proc.cpu_percent(interval=None) or 0.0) < 1.0
                if not (no_window or low_cpu_long_enough or current_cpu_low):
                    continue
                memory_info = proc.info.get("memory_info") or proc.memory_info()
                candidates.append((int(getattr(memory_info, "rss", 0) or 0), proc))
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
        return [proc for _, proc in sorted(candidates, key=lambda item: item[0], reverse=True)[:80]]

    def _is_candidate_safe(
        self,
        proc: psutil.Process,
        name: str,
        exe: str,
        foreground_pid: int | None,
        visible_pids: set[int],
    ) -> bool:
        if proc.pid == self._own_pid:
            return False
        if self.whitelist.is_whitelisted(name, exe):
            return False
        if name.lower() in SKIP_SLEEP_NAMES:
            return False
        if foreground_pid and is_related_to_pid(proc.pid, foreground_pid):
            return False
        if _has_active_network(proc):
            return False
        if proc.pid in visible_pids and proc.pid == foreground_pid:
            return False
        return True

    @staticmethod
    def _empty_working_set(proc: psutil.Process) -> RamProcessCleanResult:
        try:
            name = proc.name()
            exe = _safe_exe(proc)
            before = int(proc.memory_info().rss)
            success, message = empty_working_set(proc.pid)
            time.sleep(0.02)
            try:
                after = int(proc.memory_info().rss)
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                after = before
            return RamProcessCleanResult(
                pid=proc.pid,
                name=name,
                exe=exe,
                before_rss=before,
                after_rss=after,
                success=success,
                message=message,
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess) as exc:
            return RamProcessCleanResult(
                pid=getattr(proc, "pid", 0),
                name=str(getattr(proc, "pid", "?")),
                exe="",
                before_rss=0,
                after_rss=0,
                success=False,
                message=str(exc),
            )


def empty_working_set(pid: int) -> tuple[bool, str]:
    """Call EmptyWorkingSet for one process."""

    if os.name != "nt":
        return False, "EmptyWorkingSet is only available on Windows"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    open_process.restype = ctypes.c_void_p
    handle = open_process(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA | PROCESS_VM_OPERATION,
        False,
        int(pid),
    )
    if not handle:
        return False, f"OpenProcess failed: {ctypes.get_last_error()}"
    try:
        psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
        psapi.EmptyWorkingSet.restype = ctypes.c_bool
        if not psapi.EmptyWorkingSet(handle):
            return False, f"EmptyWorkingSet failed: {ctypes.get_last_error()}"
        return True, "OK"
    finally:
        kernel32.CloseHandle(handle)


def purge_memory_list(command: int) -> bool:
    """Best-effort NtSetSystemInformation memory-list purge."""

    if os.name != "nt":
        return False
    try:
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        cmd = ctypes.c_int(command)
        status = ntdll.NtSetSystemInformation(
            SYSTEM_MEMORY_LIST_INFORMATION,
            ctypes.byref(cmd),
            ctypes.sizeof(cmd),
        )
        return int(status) == 0
    except Exception:
        LOGGER.debug("NtSetSystemInformation memory purge failed", exc_info=True)
        return False


def is_admin() -> bool:
    """Return True when the current process is elevated."""

    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _has_active_network(proc: psutil.Process) -> bool:
    try:
        connections = proc.net_connections(kind="inet")
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, AttributeError):
        return True
    return any(getattr(conn, "status", "") == psutil.CONN_ESTABLISHED for conn in connections)


def _safe_exe(proc: psutil.Process) -> str:
    try:
        return proc.exe()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return ""
