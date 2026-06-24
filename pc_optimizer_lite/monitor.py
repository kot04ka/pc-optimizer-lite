"""Low-overhead system monitoring built on psutil."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import psutil

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class MemoryInfo:
    """RAM usage snapshot."""

    total: int
    available: int
    used: int
    percent: float


@dataclass(slots=True)
class SwapInfo:
    """Page file / swap usage snapshot."""

    total: int
    used: int
    free: int
    percent: float


@dataclass(slots=True)
class DiskUsageInfo:
    """Disk space usage for one mounted partition."""

    device: str
    mountpoint: str
    fstype: str
    total: int
    used: int
    free: int
    percent: float


@dataclass(slots=True)
class DiskIOInfo:
    """Aggregate disk I/O speed estimate."""

    read_bytes_per_second: float = 0.0
    write_bytes_per_second: float = 0.0


@dataclass(slots=True)
class ProcessInfo:
    """Selected process metrics used by GUI and optimizer."""

    pid: int
    name: str
    exe: str
    username: str
    status: str
    cpu_percent: float
    memory_percent: float
    memory_rss: int
    priority: str


@dataclass(slots=True)
class MonitorSnapshot:
    """Complete system snapshot."""

    timestamp: float
    cpu_percent: float
    per_core_cpu_percent: list[float]
    memory: MemoryInfo
    swap: SwapInfo
    disks: list[DiskUsageInfo]
    disk_io: DiskIOInfo
    processes: list[ProcessInfo] = field(default_factory=list)


SnapshotCallback = Callable[[MonitorSnapshot], None]


def format_bytes(value: int | float) -> str:
    """Convert bytes to a compact human-readable value."""

    units = ("B", "KB", "MB", "GB", "TB", "PB")
    number = float(value)
    for unit in units:
        if abs(number) < 1024.0:
            return f"{number:.1f} {unit}"
        number /= 1024.0
    return f"{number:.1f} EB"


class SystemMonitor:
    """Polls system metrics in a background thread."""

    def __init__(
        self,
        interval_seconds: float = 3.0,
        process_refresh_seconds: float = 6.0,
        max_processes: int = 80,
    ) -> None:
        self.interval_seconds = max(2.0, float(interval_seconds))
        self.process_refresh_seconds = max(self.interval_seconds, float(process_refresh_seconds))
        self.max_processes = max_processes
        self._callbacks: list[SnapshotCallback] = []
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_disk_io: tuple[float, psutil._common.sdiskio] | None = None
        self._last_process_refresh = 0.0
        self._last_processes: list[ProcessInfo] = []
        self._last_snapshot: MonitorSnapshot | None = None
        self._lock = threading.Lock()
        self.process_collection_enabled = False

        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)

    @property
    def last_snapshot(self) -> MonitorSnapshot | None:
        """Return the latest snapshot observed by the background worker."""

        with self._lock:
            return self._last_snapshot

    def add_callback(self, callback: SnapshotCallback) -> None:
        """Subscribe to monitor snapshots."""

        self._callbacks.append(callback)

    def set_process_collection_enabled(self, enabled: bool) -> None:
        """Enable expensive process-table refreshes only when they are visible."""

        enabled = bool(enabled)
        if enabled and not self.process_collection_enabled:
            self._last_process_refresh = 0.0
        if not enabled:
            self._last_processes = []
        self.process_collection_enabled = enabled

    def start(self) -> None:
        """Start the polling thread."""

        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="pc-optimizer-monitor", daemon=True)
        self._thread.start()
        LOGGER.info("System monitor started with %.1fs interval", self.interval_seconds)

    def stop(self) -> None:
        """Stop the polling thread."""

        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.interval_seconds + 1.0)
        LOGGER.info("System monitor stopped")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            started_at = time.monotonic()
            try:
                include_processes = (
                    self.process_collection_enabled
                    and started_at - self._last_process_refresh >= self.process_refresh_seconds
                )
                snapshot = self.collect_snapshot(include_processes=include_processes)
                with self._lock:
                    self._last_snapshot = snapshot
                for callback in list(self._callbacks):
                    try:
                        callback(snapshot)
                    except Exception:
                        LOGGER.exception("Monitor callback failed")
            except Exception:
                LOGGER.exception("System monitor iteration failed")

            elapsed = time.monotonic() - started_at
            self._stop_event.wait(max(0.25, self.interval_seconds - elapsed))

    def collect_snapshot(self, include_processes: bool = False) -> MonitorSnapshot:
        """Collect one system snapshot without blocking the GUI thread."""

        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disks = self._collect_disks()
        processes = self._last_processes
        now = time.time()
        if include_processes:
            processes = self.get_processes(max_processes=self.max_processes)
            self._last_processes = processes
            self._last_process_refresh = time.monotonic()

        return MonitorSnapshot(
            timestamp=now,
            cpu_percent=psutil.cpu_percent(interval=None),
            per_core_cpu_percent=list(psutil.cpu_percent(interval=None, percpu=True)),
            memory=MemoryInfo(
                total=memory.total,
                available=memory.available,
                used=memory.used,
                percent=memory.percent,
            ),
            swap=SwapInfo(
                total=swap.total,
                used=swap.used,
                free=swap.free,
                percent=swap.percent,
            ),
            disks=disks,
            disk_io=self._collect_disk_io(),
            processes=processes,
        )

    def get_processes(self, max_processes: int = 150) -> list[ProcessInfo]:
        """Return processes sorted by CPU and memory usage."""

        processes: list[ProcessInfo] = []
        attrs = ("pid", "name", "exe", "username", "status", "memory_percent", "memory_info")
        for proc in psutil.process_iter(attrs=attrs):
            try:
                info = proc.info
                memory_info = info.get("memory_info")
                processes.append(
                    ProcessInfo(
                        pid=int(info.get("pid") or proc.pid),
                        name=str(info.get("name") or ""),
                        exe=str(info.get("exe") or ""),
                        username=str(info.get("username") or ""),
                        status=str(info.get("status") or ""),
                        cpu_percent=float(proc.cpu_percent(interval=None)),
                        memory_percent=float(info.get("memory_percent") or 0.0),
                        memory_rss=int(getattr(memory_info, "rss", 0) or 0),
                        priority=self._get_priority_label(proc),
                    )
                )
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except Exception:
                LOGGER.debug("Failed to inspect process %s", getattr(proc, "pid", "?"), exc_info=True)

        return sorted(processes, key=lambda item: (item.cpu_percent, item.memory_percent), reverse=True)[
            :max_processes
        ]

    def _collect_disks(self) -> list[DiskUsageInfo]:
        disks: list[DiskUsageInfo] = []
        for partition in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(partition.mountpoint)
            except (PermissionError, OSError):
                continue
            disks.append(
                DiskUsageInfo(
                    device=partition.device,
                    mountpoint=partition.mountpoint,
                    fstype=partition.fstype,
                    total=usage.total,
                    used=usage.used,
                    free=usage.free,
                    percent=usage.percent,
                )
            )
        return disks

    def _collect_disk_io(self) -> DiskIOInfo:
        try:
            counters = psutil.disk_io_counters()
        except Exception:
            return DiskIOInfo()
        if not counters:
            return DiskIOInfo()

        now = time.monotonic()
        previous = self._last_disk_io
        self._last_disk_io = (now, counters)
        if not previous:
            return DiskIOInfo()

        prev_time, prev_counters = previous
        delta = max(0.001, now - prev_time)
        return DiskIOInfo(
            read_bytes_per_second=max(0.0, (counters.read_bytes - prev_counters.read_bytes) / delta),
            write_bytes_per_second=max(0.0, (counters.write_bytes - prev_counters.write_bytes) / delta),
        )

    @staticmethod
    def _get_priority_label(proc: psutil.Process) -> str:
        try:
            value = proc.nice()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            return "n/a"
        return str(value)
