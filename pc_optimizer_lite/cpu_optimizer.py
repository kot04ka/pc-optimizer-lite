"""Snapshot-based CPU relief for the unified optimization cycle.

This module never enumerates processes on its own. It receives the shared
optimization snapshot, changes only priority/affinity for selected processes,
and restores previous limits when load normalizes or a restore timeout expires.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from threading import Event
from typing import Iterable, Protocol

import psutil

from .config import AppConfig
from .history_manager import HistoryManager
from .process_safety import is_interactive_process
from .whitelist import Whitelist

LOGGER = logging.getLogger(__name__)


class CpuSnapshot(Protocol):
    """Minimal process snapshot interface consumed by the CPU optimizer."""

    pid: int
    name: str
    exe: str
    cpu_percent: float
    priority: int | None
    num_threads: int
    has_window: bool
    is_foreground_related: bool
    active_network: bool
    active_audio_hint: bool
    proc: psutil.Process | None


@dataclass(slots=True)
class CpuOptimizationChange:
    """One priority or affinity change made by CPU optimization."""

    pid: int
    name: str
    exe: str
    previous_priority: int | None
    new_priority_label: str
    previous_affinity: list[int] | None
    new_affinity: list[int] | None
    reason: str
    restored: bool = False
    success: bool = True
    detail: str = ""


@dataclass(slots=True)
class CpuOptimizationRecord:
    """State needed to restore one optimized process later."""

    pid: int
    name: str
    exe: str
    previous_priority: int | None
    previous_affinity: list[int] | None
    changed_at: float
    reason: str


class CpuOptimizer:
    """Applies reversible CPU relief from a prebuilt process snapshot."""

    def __init__(self, whitelist: Whitelist, history: HistoryManager) -> None:
        self.whitelist = whitelist
        self.history = history
        self._own_pid = os.getpid()
        self._records: dict[int, CpuOptimizationRecord] = {}

    @property
    def records(self) -> dict[int, CpuOptimizationRecord]:
        """Return currently optimized processes keyed by PID."""

        return dict(self._records)

    def optimize_snapshots(
        self,
        snapshots: Iterable[CpuSnapshot],
        config: AppConfig,
        *,
        cancel_event: Event | None = None,
        reason: str = "Unified CPU optimization",
        max_changes: int | None = None,
    ) -> list[CpuOptimizationChange]:
        """Apply priority/affinity relief to top CPU candidates from a snapshot."""

        if not config.cpu_optimizer_enabled:
            return []
        limit = max(1, int(max_changes or config.cpu_optimizer_max_processes))
        candidates = sorted(
            (item for item in snapshots if self._is_candidate(item, config)),
            key=lambda item: item.cpu_percent,
            reverse=True,
        )[:limit]

        changes: list[CpuOptimizationChange] = []
        for item in candidates:
            if cancel_event and cancel_event.is_set():
                break
            change = self._apply(item, config, reason)
            if change:
                changes.append(change)
        return changes

    def restore_due(
        self,
        *,
        total_cpu_percent: float,
        config: AppConfig,
        reason: str = "CPU load normalized",
    ) -> list[CpuOptimizationChange]:
        """Restore optimized processes when load is normal or records are stale."""

        if not self._records:
            return []
        now = time.monotonic()
        normalized = total_cpu_percent < max(1.0, config.cpu_threshold_percent - 5.0)
        disabled = not config.cpu_optimizer_enabled
        changes: list[CpuOptimizationChange] = []
        for record in list(self._records.values()):
            expired = now - record.changed_at >= config.cpu_optimizer_restore_after_seconds
            if disabled or normalized or expired:
                changes.append(self._restore_record(record, reason if not expired else "CPU optimization timeout"))
        return changes

    def restore_all(self, reason: str = "manual restore") -> list[CpuOptimizationChange]:
        """Restore all currently optimized processes."""

        return [self._restore_record(record, reason) for record in list(self._records.values())]

    def restore_interactive(self, reason: str = "interactive app focused") -> list[CpuOptimizationChange]:
        """Restore optimized processes that are now active or otherwise interactive."""

        changes: list[CpuOptimizationChange] = []
        for record in list(self._records.values()):
            if is_interactive_process(pid=record.pid, name=record.name):
                changes.append(self._restore_record(record, reason))
        return changes

    def _is_candidate(self, item: CpuSnapshot, config: AppConfig) -> bool:
        if item.pid == self._own_pid or item.pid in self._records:
            return False
        if item.cpu_percent < config.cpu_optimizer_min_process_cpu_percent:
            return False
        if is_interactive_process(
            pid=item.pid,
            name=item.name,
            has_window=item.has_window,
            is_foreground_related=item.is_foreground_related,
        ):
            return False
        if item.is_foreground_related or item.active_audio_hint or item.active_network:
            return False
        if item.priority is not None and not _is_normal_priority(item.priority):
            return False
        if self.whitelist.is_whitelisted(item.name, item.exe):
            return False
        return True

    def _apply(self, item: CpuSnapshot, config: AppConfig, reason: str) -> CpuOptimizationChange | None:
        try:
            proc = item.proc or psutil.Process(item.pid)
            if self.whitelist.is_whitelisted(item.name, item.exe):
                return None
            if is_interactive_process(
                pid=item.pid,
                name=item.name,
                has_window=item.has_window,
                is_foreground_related=item.is_foreground_related,
            ):
                return None
            previous_priority = item.priority if item.priority is not None else _safe_nice(proc)
            previous_affinity = _safe_affinity(proc)
            priority_label = _set_priority(proc, config.cpu_optimizer_priority_mode)
            new_affinity = None
            affinity_detail = ""
            if config.cpu_throttle_affinity_enabled and not _is_realtime_like(item):
                new_affinity = _limit_affinity(
                    proc,
                    previous_affinity,
                    item.num_threads,
                    min_cores=config.cpu_optimizer_affinity_min_cores,
                    ratio=config.cpu_optimizer_affinity_ratio,
                )
                if new_affinity is not None and previous_affinity is not None:
                    affinity_detail = f"; affinity {len(previous_affinity)}->{len(new_affinity)} cores"

            detail = (
                f"{item.name} (PID {item.pid}) CPU {item.cpu_percent:.1f}%: "
                f"priority {previous_priority}->{priority_label}{affinity_detail}. Reason: {reason}"
            )
            change = CpuOptimizationChange(
                pid=item.pid,
                name=item.name,
                exe=item.exe,
                previous_priority=previous_priority,
                new_priority_label=priority_label + affinity_detail,
                previous_affinity=previous_affinity,
                new_affinity=new_affinity,
                reason=reason,
                detail=detail,
            )
            self._records[item.pid] = CpuOptimizationRecord(
                pid=item.pid,
                name=item.name,
                exe=item.exe,
                previous_priority=previous_priority,
                previous_affinity=previous_affinity,
                changed_at=time.monotonic(),
                reason=reason,
            )
            self.history.add_event("cpu_optimizer", f"CPU optimized: {item.name}", detail, "warning")
            LOGGER.info(detail)
            return change
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError) as exc:
            detail = f"CPU optimization skipped for {item.name} (PID {item.pid}): {exc}"
            self.history.add_event("cpu_optimizer", f"CPU optimization skipped: {item.name}", detail, "warning")
            LOGGER.info(detail)
            return CpuOptimizationChange(
                pid=item.pid,
                name=item.name,
                exe=item.exe,
                previous_priority=None,
                new_priority_label="skipped",
                previous_affinity=None,
                new_affinity=None,
                reason=reason,
                success=False,
                detail=detail,
            )

    def _restore_record(self, record: CpuOptimizationRecord, reason: str) -> CpuOptimizationChange:
        try:
            proc = psutil.Process(record.pid)
            if record.previous_affinity is not None and hasattr(proc, "cpu_affinity"):
                proc.cpu_affinity(record.previous_affinity)
            if record.previous_priority is not None:
                proc.nice(record.previous_priority)
            elif os.name == "nt" and hasattr(psutil, "NORMAL_PRIORITY_CLASS"):
                proc.nice(psutil.NORMAL_PRIORITY_CLASS)
            detail = f"{record.name} (PID {record.pid}) CPU limits restored. Reason: {reason}"
            severity = "success"
            success = True
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError) as exc:
            detail = f"{record.name} (PID {record.pid}) restore skipped: {exc}"
            severity = "warning"
            success = False
        self._records.pop(record.pid, None)
        self.history.add_event("cpu_optimizer", f"CPU restored: {record.name}", detail, severity)
        LOGGER.info(detail)
        return CpuOptimizationChange(
            pid=record.pid,
            name=record.name,
            exe=record.exe,
            previous_priority=record.previous_priority,
            new_priority_label="restored",
            previous_affinity=record.previous_affinity,
            new_affinity=record.previous_affinity,
            reason=reason,
            restored=True,
            success=success,
            detail=detail,
        )


def _set_priority(proc: psutil.Process, mode: str) -> str:
    if os.name == "nt":
        if mode == "idle" and hasattr(psutil, "IDLE_PRIORITY_CLASS"):
            proc.nice(psutil.IDLE_PRIORITY_CLASS)
            return "IDLE_PRIORITY_CLASS"
        if hasattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS"):
            proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            return "BELOW_NORMAL_PRIORITY_CLASS"

    previous = _safe_nice(proc)
    target = 19 if mode == "idle" else 10
    if previous is not None:
        target = min(19, max(previous, target))
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
    if current is None or len(current) <= max(1, min_cores) or not hasattr(proc, "cpu_affinity"):
        return None
    if num_threads <= 1:
        return None
    keep = max(1, min(len(current), math.ceil(len(current) * ratio), len(current) - 1))
    keep = max(min_cores, keep)
    if keep >= len(current):
        return None
    new_affinity = current[:keep]
    proc.cpu_affinity(new_affinity)
    return new_affinity


def _is_realtime_like(item: CpuSnapshot) -> bool:
    return item.is_foreground_related or item.active_audio_hint or item.active_network


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


def _is_normal_priority(value: int) -> bool:
    if os.name == "nt" and hasattr(psutil, "NORMAL_PRIORITY_CLASS"):
        return value == int(psutil.NORMAL_PRIORITY_CLASS)
    return value == 0
