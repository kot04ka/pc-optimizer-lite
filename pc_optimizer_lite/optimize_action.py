"""Low-overhead one-click and automatic optimization flow.

The optimizer intentionally takes one process snapshot per cycle and then
passes that cached data through classification, RAM cleanup, CPU relief, sleep
and close actions. This avoids repeated psutil.process_iter() passes while the
machine is already under load.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Event
from typing import Callable

import psutil

from .config import AppConfig
from .cpu_optimizer import CpuOptimizationChange, CpuOptimizer
from .history_manager import ClosedProcessEntry, HistoryManager
from .monitor import format_bytes
from .optimizer import CleanupResult, SystemOptimizer
from .ram_cleaner import (
    MEMORY_PURGE_STANDBY_LIST,
    RamCleanMode,
    RamCleanResult,
    RamProcessCleanResult,
    empty_working_set,
    is_admin,
    purge_memory_list,
)
from .sleep_manager import SKIP_SLEEP_NAMES, SleepAction, SleepManager
from .smart_process_manager import get_foreground_pid, get_hung_window_pids, get_visible_window_pids
from .whitelist import Whitelist

LOGGER = logging.getLogger(__name__)

ProgressCallback = Callable[[int, str, str], None]
YIELD_EVERY_PROCESSES = 10
YIELD_SECONDS = 0.06
PROGRESS_MIN_INTERVAL_SECONDS = 0.25
MAX_PROCESS_SNAPSHOTS = 260
MAX_RAM_CLEAN_PROCESSES = 60

MEDIA_OR_BACKGROUND_NAMES = {
    "audiodg.exe",
    "discord.exe",
    "firefox.exe",
    "chrome.exe",
    "msedge.exe",
    "obs64.exe",
    "onedrive.exe",
    "spotify.exe",
    "steam.exe",
    "teams.exe",
    "telegram.exe",
    "vlc.exe",
    "wmplayer.exe",
    "zoom.exe",
}

RISKY_USEFUL_NAMES = {
    "antimalware service executable",
    "msmpeng.exe",
    "searchindexer.exe",
    "trustedinstaller.exe",
    "tiworker.exe",
    "wuauclt.exe",
    "usoclient.exe",
    "windowsmodulesinstallerworker.exe",
}

OPTIMIZATION_STEP_ORDER = (
    ("snapshot", "снимок системы", "Собираю один безопасный снапшот процессов"),
    ("classify", "классификация процессов", "Разделяю процессы на безопасные группы действий"),
    ("ram", "очистка оперативной памяти", "Освобождаю неиспользуемую память приложений"),
    ("standby", "очистка Standby List", "Пробую очистить системный список ожидания"),
    ("cpu", "CPU-оптимизация", "Снижаю влияние тяжёлых фоновых процессов"),
    ("sleep", "сон неактивных окон", "Перевожу давно неактивные окна в щадящий режим"),
    ("close", "безопасное закрытие", "Закрываю только консервативно выбранные фоновые процессы"),
    ("cleanup", "очистка temp/cache", "Удаляю известные временные и cache-файлы"),
)


@dataclass(slots=True)
class ProcessOptimizationSnapshot:
    """A cached process snapshot used by one optimization cycle."""

    pid: int
    name: str
    exe: str
    username: str
    cpu_percent: float
    memory_percent: float
    rss: int
    priority: int | None
    has_window: bool
    is_foreground_related: bool
    active_network: bool
    active_audio_hint: bool
    hung_window: bool
    age_seconds: float
    last_focus_age_seconds: float | None
    num_threads: int = 0
    create_time: float = 0.0
    proc: psutil.Process | None = field(default=None, repr=False, compare=False)
    network_checked: bool = False


@dataclass(slots=True)
class PriorityChange:
    """One priority/affinity change that may be restored by Undo."""

    pid: int
    name: str
    previous_priority: int | None
    new_priority_label: str
    previous_affinity: list[int] | None = None


@dataclass(slots=True)
class OptimizationPlan:
    """Classified actions for a full optimization cycle."""

    safe_close: list[ProcessOptimizationSnapshot] = field(default_factory=list)
    sleep: list[ProcessOptimizationSnapshot] = field(default_factory=list)
    lower_priority: list[ProcessOptimizationSnapshot] = field(default_factory=list)
    ram_clean: list[ProcessOptimizationSnapshot] = field(default_factory=list)
    untouched: list[ProcessOptimizationSnapshot] = field(default_factory=list)


@dataclass(slots=True)
class OptimizationResult:
    """Full optimization summary shown to the user and stored in activity history."""

    cancelled: bool = False
    eco_mode: bool = False
    cpu_before: float = 0.0
    cpu_after: float = 0.0
    ram_before_percent: float = 0.0
    ram_after_percent: float = 0.0
    ram_freed_bytes: int = 0
    closed_entries: list[ClosedProcessEntry] = field(default_factory=list)
    slept_actions: list[SleepAction] = field(default_factory=list)
    priority_changes: list[PriorityChange] = field(default_factory=list)
    ram_clean_result: RamCleanResult | None = None
    cleanup_result: CleanupResult | None = None
    standby_purged: bool = False
    plan: OptimizationPlan = field(default_factory=OptimizationPlan)
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    self_cpu_average_percent: float = 0.0
    self_cpu_peak_percent: float = 0.0
    scanned_processes: int = 0
    executed_steps: list[str] = field(default_factory=list)
    skipped_steps: list[str] = field(default_factory=list)

    def summary_text(self) -> str:
        """Return a compact report body."""

        cleanup_bytes = self.cleanup_result.freed_bytes if self.cleanup_result else 0
        cleanup_files = self.cleanup_result.deleted_files if self.cleanup_result else 0
        prefix = "Оптимизация прервана после текущего шага.\n\n" if self.cancelled else ""
        mode = "Облегчённый режим" if self.eco_mode else "Полный режим"
        return (
            f"{prefix}{mode}.\n"
            f"Освобождено RAM: {format_bytes(max(0, self.ram_freed_bytes))} "
            f"({self.ram_before_percent:.1f}% -> {self.ram_after_percent:.1f}%).\n"
            f"CPU: {self.cpu_before:.1f}% -> {self.cpu_after:.1f}%.\n"
            f"Закрыто процессов: {len(self.closed_entries)}.\n"
            f"Усыплено приложений: {len([item for item in self.slept_actions if item.success])}.\n"
            f"Понижен priority: {len(self.priority_changes)}.\n"
            f"Очищено temp/cache: {cleanup_files} файлов, {format_bytes(cleanup_bytes)}.\n"
            f"Цикл: {self.duration_seconds:.1f}s, CPU самого оптимизатора avg/peak "
            f"{self.self_cpu_average_percent:.1f}%/{self.self_cpu_peak_percent:.1f}%."
        )

    def details_text(self) -> str:
        """Return an expanded report for the modal details section."""

        lines: list[str] = [f"Просканировано процессов: {self.scanned_processes}"]
        if self.executed_steps:
            lines.append("Выполненные шаги: " + ", ".join(self.executed_steps))
        if self.skipped_steps:
            lines.append("Пропущенные шаги: " + ", ".join(self.skipped_steps))
        if self.ram_clean_result:
            touched = [item for item in self.ram_clean_result.process_results if item.success]
            lines.append(f"Память приложений обработана: {len(touched)}")
            lines.extend(
                f"  - {item.name}: {format_bytes(item.before_rss)} -> {format_bytes(item.after_rss)}"
                for item in touched[:25]
            )
        if self.standby_purged:
            lines.append("Standby List очищен.")
        if self.closed_entries:
            lines.append("Закрыто:")
            lines.extend(f"  - {entry.name} ({entry.exe or 'unknown path'})" for entry in self.closed_entries)
        if self.slept_actions:
            lines.append("Усыплено:")
            lines.extend(f"  - {action.name}: {action.message}" for action in self.slept_actions if action.success)
        if self.priority_changes:
            lines.append("Priority/affinity:")
            lines.extend(f"  - {change.name}: {change.new_priority_label}" for change in self.priority_changes)
        if self.cleanup_result and self.cleanup_result.categories:
            lines.append("Очистка:")
            lines.extend(
                f"  - {category}: {summary.files} files, {format_bytes(summary.bytes)}"
                for category, summary in sorted(self.cleanup_result.categories.items())
            )
        if self.errors:
            lines.append("Ошибки/пропуски:")
            lines.extend(f"  - {error}" for error in self.errors[:40])
        if len(lines) == 1:
            lines.append("Подробных действий не потребовалось.")
        return "\n".join(lines)


class _CycleProbe:
    """Tracks optimization duration and self CPU use without blocking."""

    def __init__(self) -> None:
        self.process = psutil.Process(os.getpid())
        self.cpu_count = max(1, psutil.cpu_count(logical=True) or 1)
        self.started_at = time.monotonic()
        self.cpu_started = self._cpu_seconds()
        self.cpu_peak = 0.0
        try:
            self.process.cpu_percent(interval=None)
        except Exception:
            pass

    def sample(self) -> None:
        try:
            raw_percent = float(self.process.cpu_percent(interval=None))
            self.cpu_peak = max(self.cpu_peak, min(100.0, raw_percent / self.cpu_count))
        except Exception:
            pass

    def finish(self, result: OptimizationResult) -> None:
        elapsed = max(0.001, time.monotonic() - self.started_at)
        cpu_delta = max(0.0, self._cpu_seconds() - self.cpu_started)
        result.duration_seconds = elapsed
        result.self_cpu_average_percent = min(100.0, (cpu_delta / elapsed) * 100.0 / self.cpu_count)
        result.self_cpu_peak_percent = self.cpu_peak

    def _cpu_seconds(self) -> float:
        try:
            times = self.process.cpu_times()
            return float(times.user + times.system)
        except Exception:
            return 0.0


def run_full_optimization(
    config: AppConfig,
    whitelist: Whitelist,
    optimizer: SystemOptimizer,
    history: HistoryManager,
    sleep_manager: SleepManager,
    progress_callback: ProgressCallback,
    cancel_event: Event,
    *,
    ram_cleaner: object | None = None,
    cpu_optimizer: CpuOptimizer | None = None,
    eco_mode: bool = False,
    quiet: bool = False,
) -> OptimizationResult:
    """Run a low-overhead optimization cycle as explicit, paced steps."""

    del ram_cleaner  # Compatibility slot: RAM cleanup now uses the shared snapshot directly.
    cpu_optimizer = cpu_optimizer or CpuOptimizer(whitelist, history)
    result = OptimizationResult(eco_mode=eco_mode)
    probe = _CycleProbe()
    before_memory = psutil.virtual_memory()
    result.ram_before_percent = float(before_memory.percent)
    result.cpu_before = float(psutil.cpu_percent(interval=None))
    snapshots: list[ProcessOptimizationSnapshot] = []
    total_steps = len(OPTIMIZATION_STEP_ORDER)

    with _self_priority_throttled():
        if _step_enabled(config, "snapshot", eco_mode):
            _step_progress(progress_callback, 1, total_steps, "снимок системы", "Собираю один кэшированный список процессов")
            snapshots = _scan_processes(whitelist, sleep_manager, cancel_event, eco_mode=eco_mode)
            result.scanned_processes = len(snapshots)
            result.executed_steps.append("снимок системы")
        else:
            _skip_step(result, progress_callback, 1, total_steps, "снимок системы")
        probe.sample()
        if finished := _finish_if_cancelled(result, before_memory, probe, history, quiet, cancel_event):
            return finished
        _pause_between_steps(config, cancel_event)

        if _step_enabled(config, "classify", eco_mode) and snapshots:
            _step_progress(progress_callback, 2, total_steps, "классификация процессов", "Работаю только с уже снятым снапшотом")
            result.plan = _classify_processes(config, snapshots, eco_mode=eco_mode)
            cpu_optimizer.restore_due(
                total_cpu_percent=result.cpu_before,
                config=config,
                reason="Pre-cycle CPU restore check",
            )
            result.executed_steps.append("классификация")
        else:
            _skip_step(result, progress_callback, 2, total_steps, "классификация процессов")
        probe.sample()
        if finished := _finish_if_cancelled(result, before_memory, probe, history, quiet, cancel_event):
            return finished
        _pause_between_steps(config, cancel_event)

        if _step_enabled(config, "ram", eco_mode):
            _step_progress(progress_callback, 3, total_steps, "очистка оперативной памяти", "Освобождаю неиспользуемую память приложений")
            result.ram_clean_result = _clean_ram_from_plan(result.plan.ram_clean, cancel_event)
            result.executed_steps.append("очистка RAM")
        else:
            _skip_step(result, progress_callback, 3, total_steps, "очистка оперативной памяти")
        probe.sample()
        if finished := _finish_if_cancelled(result, before_memory, probe, history, quiet, cancel_event):
            return finished
        _pause_between_steps(config, cancel_event)

        if _step_enabled(config, "standby", eco_mode):
            _step_progress(progress_callback, 4, total_steps, "очистка Standby List", "Выполняю только при наличии прав администратора")
            if is_admin():
                result.standby_purged = purge_memory_list(MEMORY_PURGE_STANDBY_LIST)
            else:
                result.skipped_steps.append("Standby List: нужны права администратора")
            result.executed_steps.append("Standby List")
        else:
            _skip_step(result, progress_callback, 4, total_steps, "очистка Standby List")
        probe.sample()
        if finished := _finish_if_cancelled(result, before_memory, probe, history, quiet, cancel_event):
            return finished
        _pause_between_steps(config, cancel_event)

        if _step_enabled(config, "cpu", eco_mode):
            _step_progress(progress_callback, 5, total_steps, "CPU-оптимизация", "Меняю priority/affinity только у безопасных кандидатов")
            _optimize_cpu(result, result.plan.lower_priority, cpu_optimizer, config, cancel_event)
            result.executed_steps.append("CPU-оптимизация")
        else:
            _skip_step(result, progress_callback, 5, total_steps, "CPU-оптимизация")
        probe.sample()
        if finished := _finish_if_cancelled(result, before_memory, probe, history, quiet, cancel_event):
            return finished
        _pause_between_steps(config, cancel_event)

        if _step_enabled(config, "sleep", eco_mode):
            _step_progress(progress_callback, 6, total_steps, "сон неактивных окон", "Перевожу давно неактивные окна в щадящий режим")
            _sleep_processes(result, result.plan.sleep, sleep_manager, progress_callback, cancel_event)
            result.executed_steps.append("сон окон")
        else:
            _skip_step(result, progress_callback, 6, total_steps, "сон неактивных окон")
        probe.sample()
        if finished := _finish_if_cancelled(result, before_memory, probe, history, quiet, cancel_event):
            return finished
        _pause_between_steps(config, cancel_event)

        if _step_enabled(config, "close", eco_mode):
            _step_progress(progress_callback, 7, total_steps, "безопасное закрытие", "Закрываю только консервативно выбранные фоновые процессы")
            _close_processes(result, result.plan.safe_close, whitelist, history, progress_callback, cancel_event)
            result.executed_steps.append("закрытие процессов")
        else:
            _skip_step(result, progress_callback, 7, total_steps, "безопасное закрытие")
        probe.sample()
        if finished := _finish_if_cancelled(result, before_memory, probe, history, quiet, cancel_event):
            return finished
        _pause_between_steps(config, cancel_event)

        if _step_enabled(config, "cleanup", eco_mode):
            _step_progress(progress_callback, 8, total_steps, "очистка temp/cache", "Сканирую и очищаю только известные безопасные папки")
            try:
                cleanup_plan = optimizer.scan_cleanup_files()
                if not cancel_event.is_set():
                    result.cleanup_result = optimizer.cleanup_temp_files(plan=cleanup_plan, dry_run=False)
                    history.add_event(
                        "one_click_cleanup",
                        "Очистка temp/cache",
                        (
                            f"Очищено {result.cleanup_result.deleted_files} файлов, "
                            f"освобождено {format_bytes(result.cleanup_result.freed_bytes)}"
                        ),
                        "success",
                    )
                result.executed_steps.append("очистка temp/cache")
            except Exception as exc:
                LOGGER.exception("One-click cleanup failed")
                result.errors.append(f"Cleanup failed: {exc}")
        else:
            _skip_step(result, progress_callback, 8, total_steps, "очистка temp/cache")
        probe.sample()
        if finished := _finish_if_cancelled(result, before_memory, probe, history, quiet, cancel_event):
            return finished

    result = _finish_cycle(result, before_memory, probe, history, quiet)
    _progress(progress_callback, 100, "Готово", "Оптимизация завершена", force=True)
    return result


def _step_enabled(config: AppConfig, key: str, eco_mode: bool) -> bool:
    """Return whether an optimization step should run in this cycle."""

    if eco_mode and key in {"close", "cleanup"}:
        return False
    return bool(getattr(config, f"optimize_step_{key}_enabled", True))


def _step_progress(
    callback: ProgressCallback,
    index: int,
    total: int,
    title: str,
    detail: str,
) -> None:
    """Emit user-facing step progress."""

    percent = round(((index - 1) / max(1, total)) * 100)
    _progress(callback, percent, f"Шаг {index}/{total}: {title}", detail, force=True)


def _skip_step(
    result: OptimizationResult,
    callback: ProgressCallback,
    index: int,
    total: int,
    title: str,
) -> None:
    """Record and display a skipped step."""

    result.skipped_steps.append(title)
    _step_progress(callback, index, total, title, "Пропущено настройками")


def _pause_between_steps(config: AppConfig, cancel_event: Event) -> None:
    """Give weaker machines a short breather between optimization steps."""

    pause_seconds = 0.38 if getattr(config, "lite_mode_enabled", False) else 0.18
    deadline = time.monotonic() + pause_seconds
    while not cancel_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining))


def _finish_if_cancelled(
    result: OptimizationResult,
    before_memory: psutil._common.svmem,
    probe: _CycleProbe,
    history: HistoryManager,
    quiet: bool,
    cancel_event: Event,
) -> OptimizationResult | None:
    """Finish after the current step when cancellation was requested."""

    if not cancel_event.is_set():
        return None
    result.cancelled = True
    return _finish_cycle(result, before_memory, probe, history, quiet)


def undo_optimization(result: OptimizationResult, history: HistoryManager, sleep_manager: SleepManager) -> list[str]:
    """Best-effort undo: reopen closed apps, wake sleeping apps, restore priority/affinity."""

    messages: list[str] = []
    for entry in result.closed_entries:
        ok, message = history.restore_process(entry.id)
        messages.append(f"{entry.name}: {message if ok else 'restore failed: ' + message}")

    for action in result.slept_actions:
        if action.success:
            wake = sleep_manager.resume_process(action.pid, "one-click undo")
            messages.append(f"{action.name}: {wake.message}")

    for change in result.priority_changes:
        try:
            proc = psutil.Process(change.pid)
            if change.previous_affinity is not None and hasattr(proc, "cpu_affinity"):
                proc.cpu_affinity(change.previous_affinity)
            if os.name == "nt":
                if change.previous_priority is not None:
                    proc.nice(change.previous_priority)
                else:
                    proc.nice(psutil.NORMAL_PRIORITY_CLASS)
            elif change.previous_priority is not None:
                proc.nice(change.previous_priority)
            messages.append(f"{change.name}: priority restored")
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError) as exc:
            messages.append(f"{change.name}: restore failed: {exc}")

    history.add_event("one_click_undo", "One-click undo finished", "; ".join(messages[:8]), "warning")
    return messages


def _scan_processes(
    whitelist: Whitelist,
    sleep_manager: SleepManager,
    cancel_event: Event,
    *,
    eco_mode: bool = False,
) -> list[ProcessOptimizationSnapshot]:
    visible_pids = get_visible_window_pids()
    foreground_pid = get_foreground_pid()
    foreground_related = _foreground_related_pids(foreground_pid)
    hung_pids = set() if eco_mode else get_hung_window_pids()
    now = time.time()
    snapshots: list[ProcessOptimizationSnapshot] = []
    own_pid = os.getpid()
    identity_attrs = ("pid", "name", "exe")
    detail_attrs = (
        ("memory_percent", "memory_info", "num_threads")
        if eco_mode
        else ("username", "memory_percent", "memory_info", "create_time", "num_threads")
    )

    for index, proc in enumerate(psutil.process_iter(attrs=identity_attrs)):
        if cancel_event.is_set():
            break
        if index and index % YIELD_EVERY_PROCESSES == 0:
            time.sleep(YIELD_SECONDS)
        try:
            info = proc.info
            pid = int(info.get("pid") or proc.pid)
            name = str(info.get("name") or "")
            exe = str(info.get("exe") or "")
            if pid == own_pid or whitelist.is_whitelisted(name, exe):
                continue
            detail = proc.as_dict(attrs=detail_attrs, ad_value=None)
            memory_info = detail.get("memory_info")
            last_focus = sleep_manager._last_focus_by_pid.get(pid)  # noqa: SLF001 - app-owned cache.
            create_time = float(detail.get("create_time") or now)
            snapshots.append(
                ProcessOptimizationSnapshot(
                    pid=pid,
                    name=name,
                    exe=exe,
                    username=str(detail.get("username") or ""),
                    cpu_percent=float(proc.cpu_percent(interval=None) or 0.0),
                    memory_percent=float(detail.get("memory_percent") or 0.0),
                    rss=int(getattr(memory_info, "rss", 0) or 0),
                    priority=_safe_nice(proc),
                    has_window=pid in visible_pids,
                    is_foreground_related=pid in foreground_related,
                    active_network=False,
                    active_audio_hint=_has_audio_hint(name),
                    hung_window=pid in hung_pids,
                    age_seconds=max(0.0, now - create_time),
                    last_focus_age_seconds=(time.monotonic() - last_focus) if last_focus else None,
                    num_threads=int(detail.get("num_threads") or 0),
                    create_time=create_time,
                    proc=proc,
                )
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except Exception:
            LOGGER.debug("Failed to scan process %s", getattr(proc, "pid", "?"), exc_info=True)

    return sorted(snapshots, key=lambda item: (item.cpu_percent, item.rss), reverse=True)[:MAX_PROCESS_SNAPSHOTS]


def _classify_processes(
    config: AppConfig,
    snapshots: list[ProcessOptimizationSnapshot],
    *,
    eco_mode: bool = False,
) -> OptimizationPlan:
    plan = OptimizationPlan()
    min_age = config.auto_close_min_background_minutes * 60.0
    idle_age = config.sleep_after_minutes * 60.0

    for index, item in enumerate(snapshots):
        if index and index % YIELD_EVERY_PROCESSES == 0:
            time.sleep(YIELD_SECONDS)

        if item.is_foreground_related or item.active_audio_hint:
            plan.untouched.append(item)
            continue

        if _is_ram_candidate(item):
            plan.ram_clean.append(item)

        if _is_sleep_candidate(item, idle_age):
            _ensure_network_checked(item)
            if not item.active_network:
                plan.sleep.append(item)
                if eco_mode:
                    continue

        if not eco_mode and _is_safe_close_candidate(item, config, min_age):
            _ensure_network_checked(item)
            if not item.active_network:
                plan.safe_close.append(item)
                continue

        if _is_priority_candidate(item, config):
            _ensure_network_checked(item)
            plan.lower_priority.append(item)
            continue

        plan.untouched.append(item)

    plan.ram_clean = sorted(plan.ram_clean, key=lambda item: item.rss, reverse=True)[:MAX_RAM_CLEAN_PROCESSES]
    plan.safe_close = sorted(plan.safe_close, key=lambda item: (item.cpu_percent, item.memory_percent), reverse=True)[:6]
    plan.sleep = sorted(plan.sleep, key=lambda item: item.last_focus_age_seconds or 0.0, reverse=True)[:6]
    plan.lower_priority = sorted(plan.lower_priority, key=lambda item: item.cpu_percent, reverse=True)[:4]
    return plan


def _clean_ram_from_plan(
    candidates: list[ProcessOptimizationSnapshot],
    cancel_event: Event,
) -> RamCleanResult:
    before = psutil.virtual_memory()
    result = RamCleanResult(
        mode=RamCleanMode.LIGHT,
        ram_used_before=before.used,
        ram_used_after=before.used,
        ram_percent_before=float(before.percent),
        ram_percent_after=float(before.percent),
    )

    for index, item in enumerate(candidates):
        if cancel_event.is_set():
            break
        if index and index % YIELD_EVERY_PROCESSES == 0:
            time.sleep(YIELD_SECONDS)
        success, message = empty_working_set(item.pid)
        after_rss = item.rss
        if success and item.proc is not None:
            try:
                after_rss = int(item.proc.memory_info().rss)
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                after_rss = item.rss
        result.process_results.append(
            RamProcessCleanResult(
                pid=item.pid,
                name=item.name,
                exe=item.exe,
                before_rss=item.rss,
                after_rss=after_rss,
                success=success,
                message=message,
            )
        )

    after = psutil.virtual_memory()
    result.ram_used_after = after.used
    result.ram_percent_after = float(after.percent)
    return result


def _optimize_cpu(
    result: OptimizationResult,
    candidates: list[ProcessOptimizationSnapshot],
    cpu_optimizer: CpuOptimizer,
    config: AppConfig,
    cancel_event: Event,
) -> None:
    changes = cpu_optimizer.optimize_snapshots(
        candidates,
        config,
        cancel_event=cancel_event,
        reason="Unified optimization cycle",
        max_changes=config.cpu_optimizer_max_processes,
    )
    for change in changes:
        if change.success and not change.restored:
            result.priority_changes.append(_priority_change_from_cpu(change))
        elif not change.success:
            result.errors.append(change.detail or f"CPU optimization skipped for {change.name}")


def _priority_change_from_cpu(change: CpuOptimizationChange) -> PriorityChange:
    return PriorityChange(
        pid=change.pid,
        name=change.name,
        previous_priority=change.previous_priority,
        new_priority_label=change.new_priority_label,
        previous_affinity=change.previous_affinity,
    )


def _close_processes(
    result: OptimizationResult,
    candidates: list[ProcessOptimizationSnapshot],
    whitelist: Whitelist,
    history: HistoryManager,
    progress_callback: ProgressCallback,
    cancel_event: Event,
) -> None:
    for index, item in enumerate(candidates):
        if cancel_event.is_set():
            return
        if index and index % YIELD_EVERY_PROCESSES == 0:
            time.sleep(YIELD_SECONDS)
        _progress(progress_callback, 78 + min(8, index * 2), "Close", item.name)
        try:
            proc = item.proc or psutil.Process(item.pid)
            name = _safe_name(proc) or item.name
            exe = _safe_exe(proc) or item.exe
            if whitelist.is_whitelisted(name, exe) or item.is_foreground_related or item.active_network:
                continue
            proc.terminate()
            entry = history.add_closed_process(
                pid=item.pid,
                name=name,
                exe=exe,
                reason="One-click safe background close",
                mode="one-click",
            )
            result.closed_entries.append(entry)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess) as exc:
            result.errors.append(f"Close skipped for {item.name}: {exc}")


def _sleep_processes(
    result: OptimizationResult,
    candidates: list[ProcessOptimizationSnapshot],
    sleep_manager: SleepManager,
    progress_callback: ProgressCallback,
    cancel_event: Event,
) -> None:
    for index, item in enumerate(candidates):
        if cancel_event.is_set():
            return
        if index and index % YIELD_EVERY_PROCESSES == 0:
            time.sleep(YIELD_SECONDS)
        _progress(progress_callback, 68 + min(8, index * 2), "Sleep", item.name)
        action = sleep_manager.sleep_process_from_snapshot(
            pid=item.pid,
            name=item.name,
            exe=item.exe,
            previous_priority=item.priority,
            reason="Optimization inactive app sleep",
        )
        if action:
            result.slept_actions.append(action)


def _lower_priority(
    result: OptimizationResult,
    candidates: list[ProcessOptimizationSnapshot],
    whitelist: Whitelist,
    progress_callback: ProgressCallback,
    cancel_event: Event,
) -> None:
    for index, item in enumerate(candidates):
        if cancel_event.is_set():
            return
        if index and index % YIELD_EVERY_PROCESSES == 0:
            time.sleep(YIELD_SECONDS)
        _progress(progress_callback, 55 + min(10, index * 2), "CPU", item.name)
        try:
            proc = item.proc or psutil.Process(item.pid)
            name = _safe_name(proc) or item.name
            exe = _safe_exe(proc) or item.exe
            if whitelist.is_whitelisted(name, exe):
                continue
            previous_affinity = _safe_affinity(proc)
            previous = item.priority if item.priority is not None else _safe_nice(proc)
            new_label = _lower_process_priority(proc)
            affinity_detail = _limit_affinity(proc, previous_affinity, item.num_threads)
            result.priority_changes.append(
                PriorityChange(
                    pid=item.pid,
                    name=name,
                    previous_priority=previous,
                    new_priority_label=new_label + affinity_detail,
                    previous_affinity=previous_affinity,
                )
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError) as exc:
            result.errors.append(f"CPU relief skipped for {item.name}: {exc}")


def _finish_cycle(
    result: OptimizationResult,
    before_memory: psutil._common.svmem,
    probe: _CycleProbe,
    history: HistoryManager,
    quiet: bool,
) -> OptimizationResult:
    after_memory = psutil.virtual_memory()
    result.cpu_after = float(psutil.cpu_percent(interval=None))
    result.ram_after_percent = float(after_memory.percent)
    result.ram_freed_bytes = max(0, int(after_memory.available - before_memory.available))
    probe.sample()
    probe.finish(result)
    severity = "warning" if result.cancelled or result.errors else "success"
    title = "Eco auto optimization finished" if result.eco_mode else "One-click optimization finished"
    history.add_event("optimization", title, result.summary_text(), severity)
    LOGGER.info(
        "Optimization cycle quiet=%s eco=%s scanned=%s duration=%.3fs self_cpu_avg=%.2f self_cpu_peak=%.2f "
        "closed=%s slept=%s priority=%s ram_touched=%s temp_freed=%s errors=%s",
        quiet,
        result.eco_mode,
        result.scanned_processes,
        result.duration_seconds,
        result.self_cpu_average_percent,
        result.self_cpu_peak_percent,
        len(result.closed_entries),
        len([item for item in result.slept_actions if item.success]),
        len(result.priority_changes),
        len(result.ram_clean_result.process_results) if result.ram_clean_result else 0,
        result.cleanup_result.freed_bytes if result.cleanup_result else 0,
        len(result.errors),
    )
    return result


@contextmanager
def _self_priority_throttled():
    proc = psutil.Process(os.getpid())
    previous: int | None = None
    try:
        previous = _safe_nice(proc)
        if os.name == "nt" and hasattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS"):
            proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        elif previous is not None:
            proc.nice(min(19, max(previous, 10)))
    except Exception:
        LOGGER.debug("Could not lower optimizer self-priority", exc_info=True)
    try:
        yield
    finally:
        try:
            if previous is not None:
                proc.nice(previous)
            elif os.name == "nt" and hasattr(psutil, "NORMAL_PRIORITY_CLASS"):
                proc.nice(psutil.NORMAL_PRIORITY_CLASS)
        except Exception:
            LOGGER.debug("Could not restore optimizer self-priority", exc_info=True)


def _foreground_related_pids(foreground_pid: int | None) -> set[int]:
    if not foreground_pid:
        return set()
    related = {foreground_pid}
    try:
        proc = psutil.Process(foreground_pid)
        related.update(child.pid for child in proc.children(recursive=True))
        parent = proc.parent()
        while parent is not None:
            related.add(parent.pid)
            parent = parent.parent()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return related
    return related


def _is_ram_candidate(item: ProcessOptimizationSnapshot) -> bool:
    if item.is_foreground_related or item.active_audio_hint:
        return False
    if item.name.lower() in SKIP_SLEEP_NAMES:
        return False
    return item.rss >= 60 * 1024 * 1024 and (not item.has_window or item.cpu_percent < 2.0)


def _is_safe_close_candidate(
    item: ProcessOptimizationSnapshot,
    config: AppConfig,
    min_age_seconds: float,
) -> bool:
    if item.has_window or item.active_network or _looks_system_or_service(item) or not item.exe:
        return False
    if item.age_seconds < min_age_seconds and not item.hung_window:
        return False
    resource_heavy = (
        item.cpu_percent >= config.auto_close_cpu_threshold_percent
        or item.memory_percent >= config.auto_close_memory_threshold_percent
    )
    return item.hung_window or resource_heavy


def _is_sleep_candidate(item: ProcessOptimizationSnapshot, idle_age_seconds: float) -> bool:
    if not item.has_window or item.hung_window or item.name.lower() in SKIP_SLEEP_NAMES:
        return False
    if item.last_focus_age_seconds is None:
        return False
    return item.last_focus_age_seconds >= idle_age_seconds


def _is_priority_candidate(item: ProcessOptimizationSnapshot, config: AppConfig) -> bool:
    if not config.cpu_optimizer_enabled:
        return False
    if item.cpu_percent < config.cpu_optimizer_min_process_cpu_percent:
        return False
    if item.priority is not None and not _is_normal_priority(item.priority):
        return False
    return item.active_network or item.has_window or _looks_system_or_service(item) or item.cpu_percent >= config.cpu_threshold_percent / 2.0


def _ensure_network_checked(item: ProcessOptimizationSnapshot) -> None:
    if item.network_checked:
        return
    item.network_checked = True
    if item.proc is None:
        return
    item.active_network = _has_active_network(item.proc)


def _looks_system_or_service(item: ProcessOptimizationSnapshot) -> bool:
    name = item.name.lower()
    exe = item.exe.lower()
    if name in RISKY_USEFUL_NAMES:
        return True
    if "\\windows\\" in exe or "\\program files\\windows" in exe:
        return True
    if not item.username:
        return True
    lowered_user = item.username.lower()
    return "system" in lowered_user or "local service" in lowered_user or "network service" in lowered_user


def _lower_process_priority(proc: psutil.Process) -> str:
    if os.name == "nt" and hasattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS"):
        proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        return "BELOW_NORMAL_PRIORITY_CLASS"
    previous = _safe_nice(proc)
    new_value = min(19, max(previous if previous is not None else 0, 10))
    proc.nice(new_value)
    return f"nice {new_value}"


def _limit_affinity(proc: psutil.Process, previous_affinity: list[int] | None, num_threads: int) -> str:
    if previous_affinity is None or len(previous_affinity) <= 2 or num_threads <= 1 or not hasattr(proc, "cpu_affinity"):
        return ""
    try:
        keep = max(1, len(previous_affinity) // 2)
        new_affinity = previous_affinity[:keep]
        proc.cpu_affinity(new_affinity)
        return f"; affinity {len(previous_affinity)}->{len(new_affinity)}"
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, AttributeError, OSError):
        return ""


def _has_active_network(proc: psutil.Process) -> bool:
    try:
        connections = proc.net_connections(kind="inet")
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, AttributeError):
        return True
    return any(getattr(conn, "status", "") == psutil.CONN_ESTABLISHED for conn in connections)


def _has_audio_hint(name: str) -> bool:
    return name.lower() in MEDIA_OR_BACKGROUND_NAMES


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


def _safe_exe(proc: psutil.Process) -> str:
    try:
        return proc.exe()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return ""


def _safe_name(proc: psutil.Process) -> str:
    try:
        return proc.name()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return ""


def _progress(
    callback: ProgressCallback,
    percent: int,
    step: str,
    detail: str,
    *,
    force: bool = False,
) -> None:
    now = time.monotonic()
    last_emit = getattr(callback, "_last_emit_at", 0.0)
    if not force and now - last_emit < PROGRESS_MIN_INTERVAL_SECONDS:
        return
    try:
        callback(max(0, min(100, percent)), step, detail)
        try:
            setattr(callback, "_last_emit_at", now)
        except Exception:
            pass
    except Exception:
        LOGGER.debug("Progress callback failed", exc_info=True)
