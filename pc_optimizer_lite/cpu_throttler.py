"""ProBalance-style CPU responsiveness control.

The normal path is intentionally tiny: observe total CPU only. Process
enumeration happens only after total CPU stays above the configured threshold
long enough to justify intervention.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import psutil

from .config import AppConfig
from .history_manager import HistoryManager
from .monitor import MonitorSnapshot, ProcessInfo
from .process_safety import is_interactive_process, is_known_interactive_process_name
from .smart_process_manager import get_foreground_pid, get_visible_window_pids
from .whitelist import Whitelist

LOGGER = logging.getLogger(__name__)
MIN_SAMPLE_SECONDS = 0.5
SCAN_COOLDOWN_SECONDS = 3.0
RESTORE_HYSTERESIS_PERCENT = 0.0

REALTIME_HINT_NAMES = {
    "audacity.exe",
    "audiodg.exe",
    "discord.exe",
    "firefox.exe",
    "msedge.exe",
    "obs.exe",
    "obs64.exe",
    "spotify.exe",
    "teams.exe",
    "telegram.exe",
    "vlc.exe",
    "wmplayer.exe",
    "zoom.exe",
}


@dataclass(slots=True)
class ThrottleAction:
    """One CPU priority/affinity action or restoration."""

    pid: int
    name: str
    action: str
    success: bool
    detail: str
    severity: str = "info"


@dataclass(slots=True)
class ThrottledProcess:
    """State needed to restore a ProBalance intervention."""

    pid: int
    name: str
    exe: str
    previous_priority: int | None
    previous_affinity: list[int] | None
    throttled_at: float
    reason: str
    last_cpu_percent: float
    last_limited_at: float = 0.0


@dataclass(slots=True)
class _CpuTimeSample:
    create_time: float
    cpu_time: float


@dataclass(slots=True)
class _Candidate:
    proc: psutil.Process
    pid: int
    name: str
    exe: str
    cpu_percent: float
    priority: int
    num_threads: int


class CpuThrottler:
    """Dynamically lower culprit priority only during sustained CPU pressure."""

    def __init__(self, whitelist: Whitelist, history: HistoryManager) -> None:
        self.whitelist = whitelist
        self.history = history
        self._own_pid = os.getpid()
        self._high_since: float | None = None
        self._last_scan_at = 0.0
        self._baseline_at = 0.0
        self._baseline: dict[int, _CpuTimeSample] = {}
        self._records: dict[int, ThrottledProcess] = {}

    @property
    def throttled(self) -> dict[int, ThrottledProcess]:
        """Return currently throttled processes keyed by PID."""

        return dict(self._records)

    def observe(self, snapshot: MonitorSnapshot, config: AppConfig) -> list[ThrottleAction]:
        """Observe one cheap system snapshot and react only on sustained overload."""

        if not config.cpu_throttle_enabled or config.observation_only_mode:
            self._high_since = None
            self._baseline.clear()
            return self.restore_all("CPU responsiveness control disabled") if self._records else []

        now = time.monotonic()
        foreground_pid = get_foreground_pid()
        restored = self.restore_interactive(foreground_pid=foreground_pid)
        if restored:
            return restored
        threshold = float(config.cpu_threshold_percent)
        if snapshot.cpu_percent < max(0.0, threshold - RESTORE_HYSTERESIS_PERCENT):
            self._high_since = None
            self._baseline.clear()
            if self._records:
                return self.restore_all("CPU load normalized")
            return []

        if self._high_since is None:
            self._high_since = now
            return []

        if now - self._high_since < config.cpu_sustain_seconds:
            return []

        if self._records and config.cpu_limiter_enabled:
            action = self._maybe_cpu_limit(config, now)
            if action:
                return [action]

        if len(self._records) >= max(1, int(config.cpu_optimizer_max_processes)):
            return []

        if now - self._last_scan_at < SCAN_COOLDOWN_SECONDS:
            return []

        if not self._baseline:
            self._capture_baseline(now)
            return []

        elapsed = now - self._baseline_at
        if elapsed < MIN_SAMPLE_SECONDS:
            return []

        self._last_scan_at = now
        action = self._apply_to_current_culprit(config, elapsed)
        self._baseline.clear()
        return [action] if action else []

    def select_candidates(self, processes: list[ProcessInfo], limit: int = 3) -> list[ProcessInfo]:
        """Return top non-whitelisted normal-priority candidates from a provided list."""

        candidates: list[ProcessInfo] = []
        for process in sorted(processes, key=lambda item: item.cpu_percent, reverse=True):
            if len(candidates) >= limit:
                break
            if process.pid == self._own_pid:
                continue
            if process.pid in self._records:
                continue
            if self.whitelist.is_whitelisted(process.name, process.exe):
                continue
            if is_interactive_process(
                pid=process.pid,
                name=process.name,
                has_window=bool(getattr(process, "has_window", False)),
                is_foreground_related=bool(getattr(process, "is_foreground_related", False)),
                check_current_foreground=False,
            ):
                continue
            if not _is_process_info_normal_priority(process.priority):
                continue
            candidates.append(process)
        return candidates

    def restore_all(self, reason: str = "manual restore") -> list[ThrottleAction]:
        """Restore every process touched by the responsiveness controller."""

        return [self._restore_process(entry, reason) for entry in list(self._records.values())]

    def restore_interactive(
        self,
        *,
        foreground_pid: int | None = None,
        reason: str = "interactive app focused",
    ) -> list[ThrottleAction]:
        """Restore throttled apps as soon as they become interactive again."""

        actions: list[ThrottleAction] = []
        for entry in list(self._records.values()):
            if is_interactive_process(pid=entry.pid, name=entry.name, foreground_pid=foreground_pid):
                actions.append(self._restore_process(entry, reason))
        return actions

    def _capture_baseline(self, now: float) -> None:
        """Capture process CPU times only after sustained total CPU pressure."""

        baseline: dict[int, _CpuTimeSample] = {}
        for proc in psutil.process_iter(attrs=("pid", "create_time")):
            try:
                pid = int(proc.info.get("pid") or proc.pid)
                if pid == self._own_pid or pid in self._records:
                    continue
                times = proc.cpu_times()
                baseline[pid] = _CpuTimeSample(
                    create_time=float(proc.info.get("create_time") or 0.0),
                    cpu_time=float(times.user + times.system),
                )
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except Exception:
                LOGGER.debug("Failed to baseline process %s", getattr(proc, "pid", "?"), exc_info=True)
        self._baseline = baseline
        self._baseline_at = now
        self._last_scan_at = now
        LOGGER.debug("CPU ProBalance baseline captured for %s processes", len(baseline))

    def _apply_to_current_culprit(self, config: AppConfig, elapsed: float) -> ThrottleAction | None:
        foreground_pid = get_foreground_pid()
        foreground_related = _foreground_related_pids(foreground_pid)
        visible_pids = get_visible_window_pids()
        candidates = self._collect_culprits(config, elapsed, foreground_related, visible_pids)
        for candidate in candidates[:8]:
            if _has_realtime_hint(candidate.name):
                continue
            if _has_active_network(candidate.proc):
                continue
            action = self._apply_throttle(candidate, config)
            if action.success:
                return action
        return None

    def _collect_culprits(
        self,
        config: AppConfig,
        elapsed: float,
        foreground_related: set[int],
        visible_pids: set[int],
    ) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        min_process_cpu = float(config.cpu_optimizer_min_process_cpu_percent)
        attrs = ("pid", "name", "exe", "create_time", "num_threads")
        for proc in psutil.process_iter(attrs=attrs):
            try:
                info = proc.info
                pid = int(info.get("pid") or proc.pid)
                if pid == self._own_pid or pid in self._records or pid in foreground_related or pid in visible_pids:
                    continue
                baseline = self._baseline.get(pid)
                if baseline is None:
                    continue
                create_time = float(info.get("create_time") or 0.0)
                if baseline.create_time and create_time and abs(create_time - baseline.create_time) > 0.01:
                    continue
                name = str(info.get("name") or "")
                exe = str(info.get("exe") or "")
                if is_known_interactive_process_name(name):
                    continue
                if self.whitelist.is_whitelisted(name, exe):
                    continue
                priority = _safe_nice(proc)
                if priority is None or not _is_normal_priority(priority):
                    continue
                times = proc.cpu_times()
                cpu_percent = max(0.0, ((float(times.user + times.system) - baseline.cpu_time) / elapsed) * 100.0)
                if cpu_percent < min_process_cpu:
                    continue
                candidates.append(
                    _Candidate(
                        proc=proc,
                        pid=pid,
                        name=name,
                        exe=exe,
                        cpu_percent=cpu_percent,
                        priority=priority,
                        num_threads=int(info.get("num_threads") or 0),
                    )
                )
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except Exception:
                LOGGER.debug("Failed to inspect CPU culprit %s", getattr(proc, "pid", "?"), exc_info=True)
        return sorted(candidates, key=lambda item: item.cpu_percent, reverse=True)

    def _apply_throttle(self, candidate: _Candidate, config: AppConfig) -> ThrottleAction:
        try:
            if self.whitelist.is_whitelisted(candidate.name, candidate.exe):
                return ThrottleAction(candidate.pid, candidate.name, "throttle", False, "Process is protected", "warning")
            if is_interactive_process(pid=candidate.pid, name=candidate.name):
                return ThrottleAction(
                    candidate.pid,
                    candidate.name,
                    "throttle",
                    False,
                    "Process is active or interactive",
                    "warning",
                )
            previous_affinity = _safe_affinity(candidate.proc)
            priority_label = _set_priority(candidate.proc, config.cpu_optimizer_priority_mode)
            affinity_detail = ""
            new_affinity = None
            if config.cpu_throttle_affinity_enabled:
                new_affinity = _limit_affinity(
                    candidate.proc,
                    previous_affinity,
                    candidate.num_threads,
                    min_cores=config.cpu_optimizer_affinity_min_cores,
                    ratio=config.cpu_optimizer_affinity_ratio,
                )
                if new_affinity is not None and previous_affinity is not None:
                    affinity_detail = f"; affinity {len(previous_affinity)}->{len(new_affinity)} cores"

            detail = (
                f"ProBalance-style intervention: {candidate.name} (PID {candidate.pid}) "
                f"used {candidate.cpu_percent:.1f}% CPU; priority {candidate.priority}->{priority_label}"
                f"{affinity_detail}."
            )
            self._records[candidate.pid] = ThrottledProcess(
                pid=candidate.pid,
                name=candidate.name,
                exe=candidate.exe,
                previous_priority=candidate.priority,
                previous_affinity=previous_affinity,
                throttled_at=time.monotonic(),
                reason="sustained high total CPU",
                last_cpu_percent=candidate.cpu_percent,
            )
            self.history.add_event("cpu_probalance", f"Priority lowered: {candidate.name}", detail, "warning")
            LOGGER.info(detail)
            return ThrottleAction(candidate.pid, candidate.name, "throttle", True, detail, "warning")
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError) as exc:
            detail = f"CPU intervention skipped for {candidate.name} (PID {candidate.pid}): {exc}"
            LOGGER.info(detail)
            return ThrottleAction(candidate.pid, candidate.name, "throttle", False, detail, "warning")

    def _restore_process(self, entry: ThrottledProcess, reason: str) -> ThrottleAction:
        try:
            proc = psutil.Process(entry.pid)
            if entry.previous_affinity is not None and hasattr(proc, "cpu_affinity"):
                proc.cpu_affinity(entry.previous_affinity)
            if entry.previous_priority is not None:
                proc.nice(entry.previous_priority)
            detail = f"{entry.name} (PID {entry.pid}) priority/affinity restored: {reason}"
            success = True
            severity = "success"
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError) as exc:
            detail = f"{entry.name} (PID {entry.pid}) restore skipped: {exc}"
            success = False
            severity = "warning"
        self._records.pop(entry.pid, None)
        self.history.add_event("cpu_probalance", f"Priority restored: {entry.name}", detail, severity)
        LOGGER.info(detail)
        return ThrottleAction(entry.pid, entry.name, "restore", success, detail, severity)

    def _maybe_cpu_limit(self, config: AppConfig, now: float) -> ThrottleAction | None:
        cooldown = max(1.0, float(config.cpu_limiter_cooldown_seconds))
        for entry in sorted(self._records.values(), key=lambda item: item.last_cpu_percent, reverse=True):
            if now - entry.last_limited_at < cooldown:
                continue
            try:
                proc = psutil.Process(entry.pid)
                if (
                    self.whitelist.is_whitelisted(entry.name, entry.exe)
                    or is_interactive_process(pid=entry.pid, name=entry.name)
                    or _has_active_network(proc)
                ):
                    continue
                proc.suspend()
                time.sleep(config.cpu_limiter_suspend_milliseconds / 1000.0)
                proc.resume()
                entry.last_limited_at = time.monotonic()
                detail = (
                    f"CPU limiter pulse: {entry.name} (PID {entry.pid}) suspended for "
                    f"{config.cpu_limiter_suspend_milliseconds} ms after priority relief was insufficient."
                )
                self.history.add_event("cpu_limiter", f"CPU limited: {entry.name}", detail, "warning")
                LOGGER.info(detail)
                return ThrottleAction(entry.pid, entry.name, "limit", True, detail, "warning")
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError) as exc:
                detail = f"CPU limiter skipped for {entry.name} (PID {entry.pid}): {exc}"
                LOGGER.info(detail)
                return ThrottleAction(entry.pid, entry.name, "limit", False, detail, "warning")
        return None


def _foreground_related_pids(foreground_pid: int | None) -> set[int]:
    if not foreground_pid:
        return set()
    related = {foreground_pid}
    try:
        active = psutil.Process(foreground_pid)
        related.update(child.pid for child in active.children(recursive=True))
        parent = active.parent()
        while parent is not None:
            related.add(parent.pid)
            parent = parent.parent()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        related.add(foreground_pid)
    return related


def _set_priority(proc: psutil.Process, mode: str) -> str:
    if os.name == "nt":
        if mode == "idle" and hasattr(psutil, "IDLE_PRIORITY_CLASS"):
            proc.nice(psutil.IDLE_PRIORITY_CLASS)
            return "IDLE_PRIORITY_CLASS"
        if hasattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS"):
            proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            return "BELOW_NORMAL_PRIORITY_CLASS"
    target = 19 if mode == "idle" else 10
    proc.nice(target)
    return f"nice {target}"


def _limit_affinity(
    proc: psutil.Process,
    current: list[int] | None,
    num_threads: int,
    *,
    min_cores: int,
    ratio: float,
) -> list[int] | None:
    if current is None or len(current) <= max(1, min_cores) or num_threads <= 1:
        return None
    if not hasattr(proc, "cpu_affinity"):
        return None
    keep = max(min_cores, int(round(len(current) * ratio)))
    keep = min(keep, len(current) - 1)
    if keep >= len(current):
        return None
    new_affinity = current[:keep]
    try:
        proc.cpu_affinity(new_affinity)
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, AttributeError, OSError):
        return None
    return new_affinity


def _is_normal_priority(value: int) -> bool:
    if os.name == "nt" and hasattr(psutil, "NORMAL_PRIORITY_CLASS"):
        return value == int(psutil.NORMAL_PRIORITY_CLASS)
    return value == 0


def _is_process_info_normal_priority(value: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"normal", "normal_priority_class", "0"}:
        return True
    if os.name == "nt" and hasattr(psutil, "NORMAL_PRIORITY_CLASS"):
        return normalized == str(int(psutil.NORMAL_PRIORITY_CLASS))
    return False


def _safe_nice(proc: psutil.Process) -> int | None:
    try:
        return int(proc.nice())
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, ValueError):
        return None


def _safe_affinity(proc: psutil.Process) -> list[int] | None:
    try:
        if hasattr(proc, "cpu_affinity"):
            return list(proc.cpu_affinity())
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, AttributeError, OSError):
        return None
    return None


def _has_realtime_hint(name: str) -> bool:
    lowered = name.lower()
    return lowered in REALTIME_HINT_NAMES or "audio" in lowered or "video" in lowered


def _has_active_network(proc: psutil.Process) -> bool:
    try:
        connections = proc.net_connections(kind="inet")
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, AttributeError):
        try:
            connections = proc.connections(kind="inet")
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, AttributeError):
            return True
    return any(getattr(conn, "status", "") == psutil.CONN_ESTABLISHED for conn in connections)
