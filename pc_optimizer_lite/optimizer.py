"""Safe optimization actions.

This module never kills whitelisted/system processes. Priority changes are
limited, logged, and reversible by the operating system or user.
"""

from __future__ import annotations

import logging
import ctypes
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .config import AppConfig
from .whitelist import Whitelist

try:
    import psutil
except ModuleNotFoundError:  # pragma: no cover - exercised only without dependencies installed
    psutil = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from .monitor import ProcessInfo

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class OptimizerAction:
    """Result of one optimizer operation."""

    pid: int
    name: str
    action: str
    success: bool
    message: str


@dataclass(slots=True)
class CleanupResult:
    """Summary of a temporary-file cleanup run."""

    deleted_files: int = 0
    deleted_dirs: int = 0
    freed_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    categories: dict[str, "CleanupCategorySummary"] = field(default_factory=dict)


@dataclass(slots=True)
class CleanupTarget:
    """A known safe cleanup root and its category label."""

    path: Path
    category: str


@dataclass(slots=True)
class CleanupItem:
    """One file selected for cleanup."""

    path: Path
    size: int
    category: str


@dataclass(slots=True)
class CleanupCategorySummary:
    """Cleanup counts for one category."""

    files: int = 0
    bytes: int = 0


@dataclass(slots=True)
class CleanupPlan:
    """Full transparent cleanup plan shown before deleting anything."""

    items: list[CleanupItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    categories: dict[str, CleanupCategorySummary] = field(default_factory=dict)
    recycle_bin_files: int = 0
    recycle_bin_bytes: int = 0

    @property
    def file_count(self) -> int:
        """Return number of files in this plan."""

        return len(self.items) + self.recycle_bin_files

    @property
    def total_bytes(self) -> int:
        """Return total bytes in this plan."""

        return sum(item.size for item in self.items) + self.recycle_bin_bytes


class SystemOptimizer:
    """Applies conservative, user-auditable resource relief actions."""

    def __init__(self, whitelist: Whitelist, config: AppConfig | None = None) -> None:
        self.whitelist = whitelist
        self.config = config or AppConfig()
        self._own_pid = os.getpid()

    def suggest_heavy_processes(
        self,
        processes: list["ProcessInfo"],
        cpu_percent: float = 20.0,
        memory_percent: float = 10.0,
        limit: int = 10,
    ) -> list["ProcessInfo"]:
        """Return non-whitelisted resource-heavy processes."""

        suggestions = [
            process
            for process in processes
            if process.pid != self._own_pid
            and not self.whitelist.is_whitelisted(process.name, process.exe)
            and (process.cpu_percent >= cpu_percent or process.memory_percent >= memory_percent)
        ]
        return sorted(suggestions, key=lambda item: (item.cpu_percent, item.memory_percent), reverse=True)[
            :limit
        ]

    def lower_priority_for_process(self, pid: int) -> OptimizerAction:
        """Lower one process priority if it is not protected."""

        if psutil is None:
            return OptimizerAction(pid, str(pid), "lower_priority", False, "psutil is not installed")

        try:
            proc = psutil.Process(pid)
            name = proc.name()
            exe = _safe_exe(proc)
            if pid == self._own_pid:
                return OptimizerAction(pid, name, "lower_priority", False, "Application process is protected")
            if self.whitelist.is_whitelisted(name, exe):
                return OptimizerAction(pid, name, "lower_priority", False, "Process is whitelisted")

            if os.name == "nt":
                proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
                priority_label = "BELOW_NORMAL_PRIORITY_CLASS"
            else:
                current_nice = proc.nice()
                proc.nice(min(19, int(current_nice) + 5))
                priority_label = str(proc.nice())

            LOGGER.info("Lowered priority for pid=%s name=%s to %s", pid, name, priority_label)
            return OptimizerAction(pid, name, "lower_priority", True, f"Priority set to {priority_label}")
        except _psutil_process_errors() as exc:
            LOGGER.warning("Failed to lower priority for pid=%s: %s", pid, exc)
            return OptimizerAction(pid, str(pid), "lower_priority", False, str(exc))
        except Exception as exc:
            LOGGER.exception("Unexpected priority change failure for pid=%s", pid)
            return OptimizerAction(pid, str(pid), "lower_priority", False, str(exc))

    def lower_priority_for_heavy_processes(
        self,
        processes: list["ProcessInfo"],
        limit: int = 3,
    ) -> list[OptimizerAction]:
        """Lower priority for the top heavy non-whitelisted processes."""

        actions: list[OptimizerAction] = []
        for process in self.suggest_heavy_processes(processes, limit=limit):
            actions.append(self.lower_priority_for_process(process.pid))
        return actions

    def terminate_process_after_confirmation(self, pid: int) -> OptimizerAction:
        """Terminate one non-whitelisted process after the GUI has confirmed it."""

        if psutil is None:
            return OptimizerAction(pid, str(pid), "terminate", False, "psutil is not installed")

        try:
            proc = psutil.Process(pid)
            name = proc.name()
            exe = _safe_exe(proc)
            if pid == self._own_pid:
                return OptimizerAction(pid, name, "terminate", False, "Application process is protected")
            if self.whitelist.is_whitelisted(name, exe):
                return OptimizerAction(pid, name, "terminate", False, "Process is whitelisted")
            proc.terminate()
            LOGGER.info("User requested process termination: pid=%s name=%s", pid, name)
            return OptimizerAction(pid, name, "terminate", True, "Terminate signal sent")
        except _psutil_process_errors() as exc:
            LOGGER.warning("Failed to terminate pid=%s: %s", pid, exc)
            return OptimizerAction(pid, str(pid), "terminate", False, str(exc))
        except Exception as exc:
            LOGGER.exception("Unexpected termination failure for pid=%s", pid)
            return OptimizerAction(pid, str(pid), "terminate", False, str(exc))

    def get_safe_temp_roots(self) -> list[Path]:
        """Return known temporary directories that may be cleaned with confirmation."""

        candidates = {Path(tempfile.gettempdir())}
        if os.name == "nt":
            for env_name in ("TEMP", "TMP"):
                value = os.environ.get(env_name)
                if value:
                    candidates.add(Path(value))
            local_app_data = os.environ.get("LOCALAPPDATA")
            if local_app_data:
                candidates.add(Path(local_app_data) / "Temp")

        roots: list[Path] = []
        for candidate in candidates:
            try:
                resolved = candidate.expanduser().resolve()
            except OSError:
                continue
            if resolved.exists() and resolved.is_dir() and resolved not in roots:
                roots.append(resolved)
        return roots

    def get_safe_cleanup_targets(self) -> list[CleanupTarget]:
        """Return known temporary/cache directories that may be cleaned with confirmation."""

        targets: list[CleanupTarget] = []
        if self.config.cleanup_temp_enabled:
            targets.extend(CleanupTarget(path=root, category="Temp") for root in self.get_safe_temp_roots())
        targets.extend(self._browser_cache_targets())
        if self.config.cleanup_prefetch_enabled:
            targets.extend(self._prefetch_targets())
        if self.config.cleanup_logs_enabled:
            targets.extend(self._log_targets())
        unique: dict[Path, CleanupTarget] = {}
        for target in targets:
            if not self._category_enabled(target.category):
                continue
            try:
                resolved = target.path.expanduser().resolve()
            except OSError:
                continue
            if resolved.exists() and resolved.is_dir():
                unique[resolved] = CleanupTarget(resolved, target.category)
        return list(unique.values())

    def scan_cleanup_files(
        self,
        targets: list[CleanupTarget] | None = None,
        roots: list[Path] | None = None,
        *,
        batch_size: int = 40,
        pause_seconds: float = 0.05,
        sleep_func: Callable[[float], object] = time.sleep,
    ) -> CleanupPlan:
        """Collect a transparent file list before cleanup."""

        selected_targets = targets
        if selected_targets is None:
            if roots is not None:
                selected_targets = [CleanupTarget(root, "Temp") for root in roots]
            else:
                selected_targets = self.get_safe_cleanup_targets()

        allowed_roots = [target.path.resolve() for target in self.get_safe_cleanup_targets()]
        plan = CleanupPlan()
        for target in selected_targets:
            if not self._category_enabled(target.category):
                continue
            try:
                root = target.path.resolve()
            except OSError as exc:
                plan.errors.append(f"{target.path}: {exc}")
                continue
            if not _is_under_any_root(root, allowed_roots):
                plan.errors.append(f"Skipped unsafe path: {root}")
                continue
            self._scan_root(
                root,
                target.category,
                allowed_roots,
                plan,
                batch_size=max(1, int(batch_size)),
                pause_seconds=max(0.0, float(pause_seconds)),
                sleep_func=sleep_func,
            )
        if self.config.cleanup_recycle_bin_enabled:
            recycle_files, recycle_bytes = _query_recycle_bin()
            if recycle_files > 0 or recycle_bytes > 0:
                plan.recycle_bin_files = recycle_files
                plan.recycle_bin_bytes = recycle_bytes
                plan.categories["Recycle Bin"] = CleanupCategorySummary(
                    files=recycle_files,
                    bytes=recycle_bytes,
                )
        return plan

    def cleanup_temp_files(
        self,
        roots: list[Path] | None = None,
        dry_run: bool = False,
        plan: CleanupPlan | None = None,
        *,
        batch_size: int = 30,
        pause_seconds: float = 0.06,
        sleep_func: Callable[[float], object] = time.sleep,
    ) -> CleanupResult:
        """Delete files only from known temp/cache directories after user confirmation."""

        cleanup_plan = plan or self.scan_cleanup_files(
            roots=roots,
            batch_size=batch_size,
            pause_seconds=pause_seconds,
            sleep_func=sleep_func,
        )
        result = CleanupResult(errors=list(cleanup_plan.errors))
        if dry_run:
            for category, summary in cleanup_plan.categories.items():
                result.categories[category] = CleanupCategorySummary(
                    files=summary.files,
                    bytes=summary.bytes,
                )
            result.deleted_files = cleanup_plan.file_count
            result.freed_bytes = cleanup_plan.total_bytes
            return result

        allowed_roots = [target.path.resolve() for target in self.get_safe_cleanup_targets()]
        for index, item in enumerate(cleanup_plan.items, start=1):
            self._remove_planned_file(item, allowed_roots, result)
            if pause_seconds and index % max(1, int(batch_size)) == 0:
                sleep_func(pause_seconds)
        if self.config.cleanup_recycle_bin_enabled and (
            cleanup_plan.recycle_bin_files > 0 or cleanup_plan.recycle_bin_bytes > 0
        ):
            self._empty_recycle_bin(cleanup_plan, result)
        self._remove_empty_dirs_after_cleanup(allowed_roots, result)

        LOGGER.info(
            "Cleanup completed dry_run=%s files=%s dirs=%s freed=%s errors=%s categories=%s",
            dry_run,
            result.deleted_files,
            result.deleted_dirs,
            result.freed_bytes,
            len(result.errors),
            {key: {"files": value.files, "bytes": value.bytes} for key, value in result.categories.items()},
        )
        return result

    def _scan_root(
        self,
        root: Path,
        category: str,
        allowed_roots: list[Path],
        plan: CleanupPlan,
        *,
        batch_size: int,
        pause_seconds: float,
        sleep_func: Callable[[float], object],
    ) -> None:
        if not root.exists() or not root.is_dir():
            return

        processed = 0
        for dirpath, _, filenames in os.walk(root):
            current_dir = Path(dirpath)
            if not _is_under_any_root(current_dir, allowed_roots):
                continue

            for filename in filenames:
                path = current_dir / filename
                try:
                    resolved = path.resolve()
                    if not _is_under_any_root(resolved, allowed_roots) or path.is_symlink():
                        continue
                    size = path.stat().st_size
                    item_category = _categorize_cleanup_path(path, category)
                    if not self._category_enabled(item_category):
                        continue
                    if not self._should_include_cleanup_file(path, item_category):
                        continue
                except OSError as exc:
                    plan.errors.append(f"{path}: {exc}")
                    continue
                plan.items.append(CleanupItem(path=resolved, size=size, category=item_category))
                summary = plan.categories.setdefault(item_category, CleanupCategorySummary())
                summary.files += 1
                summary.bytes += size
                processed += 1
                if pause_seconds and processed % batch_size == 0:
                    sleep_func(pause_seconds)

    @staticmethod
    def _remove_planned_file(
        item: CleanupItem,
        allowed_roots: list[Path],
        result: CleanupResult,
    ) -> None:
        try:
            if not _is_under_any_root(item.path, allowed_roots) or item.path.is_symlink():
                return
            item.path.unlink()
            result.deleted_files += 1
            result.freed_bytes += item.size
            summary = result.categories.setdefault(item.category, CleanupCategorySummary())
            summary.files += 1
            summary.bytes += item.size
        except OSError as exc:
            result.errors.append(f"{item.path}: {exc}")

    def _remove_empty_dirs_after_cleanup(self, allowed_roots: list[Path], result: CleanupResult) -> None:
        for root in allowed_roots:
            for dirpath, dirnames, _ in os.walk(root, topdown=False):
                for dirname in dirnames:
                    path = Path(dirpath) / dirname
                    self._remove_empty_dir(path, allowed_roots, result)

    @staticmethod
    def _remove_empty_dir(path: Path, allowed_roots: list[Path], result: CleanupResult) -> None:
        try:
            resolved = path.resolve()
            if not _is_under_any_root(resolved, allowed_roots) or path.is_symlink() or any(path.iterdir()):
                return
            path.rmdir()
            result.deleted_dirs += 1
        except OSError:
            pass

    def _browser_cache_targets(self) -> list[CleanupTarget]:
        targets: list[CleanupTarget] = []
        local_app_data = os.environ.get("LOCALAPPDATA")
        roaming_app_data = os.environ.get("APPDATA")
        if local_app_data and self.config.cleanup_browser_cache_enabled:
            local = Path(local_app_data)
            chromium_roots = (
                local / "Google" / "Chrome" / "User Data",
                local / "Microsoft" / "Edge" / "User Data",
                local / "BraveSoftware" / "Brave-Browser" / "User Data",
                local / "Opera Software" / "Opera Stable",
            )
            for root in chromium_roots:
                if not _safe_is_dir(root):
                    continue
                for cache_name in ("Cache", "Code Cache", "GPUCache"):
                    try:
                        matches = list(root.glob(f"**/{cache_name}"))
                    except OSError:
                        continue
                    targets.extend(
                        CleanupTarget(path=path, category="Browser cache")
                        for path in matches
                        if _safe_is_dir(path)
                    )
        if roaming_app_data and self.config.cleanup_browser_cache_enabled:
            firefox_profiles = Path(roaming_app_data) / "Mozilla" / "Firefox" / "Profiles"
            if _safe_is_dir(firefox_profiles):
                targets.extend(
                    CleanupTarget(path=path, category="Browser cache")
                    for path in firefox_profiles.glob("*/cache2")
                    if _safe_is_dir(path)
                )
        windows_temp = os.environ.get("WINDIR")
        if windows_temp and self.config.cleanup_windows_temp_enabled:
            targets.append(CleanupTarget(Path(windows_temp) / "Temp", "Windows temp"))
        return targets

    def _prefetch_targets(self) -> list[CleanupTarget]:
        windir = os.environ.get("WINDIR")
        if not windir:
            return []
        return [CleanupTarget(Path(windir) / "Prefetch", "Prefetch")]

    def _log_targets(self) -> list[CleanupTarget]:
        targets: list[CleanupTarget] = []
        windir = os.environ.get("WINDIR")
        if windir:
            targets.append(CleanupTarget(Path(windir) / "Logs", "Logs"))
        program_data = os.environ.get("ProgramData")
        if program_data:
            targets.append(CleanupTarget(Path(program_data) / "Microsoft" / "Windows" / "WER", "Logs"))
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            targets.append(CleanupTarget(Path(local_app_data) / "Microsoft" / "Windows" / "WER", "Logs"))
        return targets

    def _category_enabled(self, category: str) -> bool:
        if category == "Temp":
            return self.config.cleanup_temp_enabled
        if category == "Windows temp":
            return self.config.cleanup_windows_temp_enabled
        if category == "Browser cache":
            return self.config.cleanup_browser_cache_enabled
        if category == "Prefetch":
            return self.config.cleanup_prefetch_enabled
        if category == "Logs":
            return self.config.cleanup_logs_enabled
        return True

    def _should_include_cleanup_file(self, path: Path, category: str) -> bool:
        if category == "Prefetch":
            return path.suffix.lower() == ".pf" and _is_older_than_days(path, 7)
        if category == "Logs":
            return path.suffix.lower() in {".log", ".old", ".etl", ".tmp", ".bak"} and _is_older_than_days(
                path,
                self.config.cleanup_logs_older_than_days,
            )
        return True

    @staticmethod
    def _empty_recycle_bin(plan: CleanupPlan, result: CleanupResult) -> None:
        success, message = _empty_recycle_bin()
        if not success:
            if message:
                result.errors.append(message)
            return
        result.deleted_files += plan.recycle_bin_files
        result.freed_bytes += plan.recycle_bin_bytes
        result.categories["Recycle Bin"] = CleanupCategorySummary(
            files=plan.recycle_bin_files,
            bytes=plan.recycle_bin_bytes,
        )


def estimate_directory_size(path: Path) -> int:
    """Return a best-effort size estimate for a directory tree."""

    total = 0
    for dirpath, _, filenames in os.walk(path):
        for filename in filenames:
            try:
                total += (Path(dirpath) / filename).stat().st_size
            except OSError:
                continue
    return total


def open_file_location(path: str) -> bool:
    """Open a process executable location in Explorer or the platform file manager."""

    if not path:
        return False
    target = Path(path)
    try:
        if os.name == "nt":
            os.startfile(str(target.parent))  # type: ignore[attr-defined]
        else:
            opener = shutil.which("xdg-open") or shutil.which("open")
            if not opener:
                return False
            os.spawnlp(os.P_NOWAIT, opener, opener, str(target.parent))
        return True
    except OSError:
        LOGGER.exception("Failed to open file location for %s", path)
        return False


def _safe_exe(proc: object) -> str:
    try:
        return proc.exe()  # type: ignore[attr-defined]
    except _psutil_process_errors():
        return ""


def _psutil_process_errors() -> tuple[type[BaseException], ...]:
    if psutil is None:
        return (Exception,)
    return (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess)


def _is_under_any_root(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _categorize_cleanup_path(path: Path, default_category: str) -> str:
    lowered = str(path).lower()
    suffix = path.suffix.lower()
    if default_category in {"Browser cache", "Prefetch", "Logs", "Windows temp"}:
        return default_category
    if any(name in lowered for name in ("chrome", "edge", "firefox", "opera", "brave")) and "cache" in lowered:
        return "Browser cache"
    if "prefetch" in lowered and suffix == ".pf":
        return "Prefetch"
    if suffix in {".log", ".etl", ".tmp"} and "windows" in lowered:
        return "Windows temp"
    if suffix in {".log", ".old"}:
        return "Logs"
    return default_category


def _is_older_than_days(path: Path, days: int) -> bool:
    try:
        age_seconds = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age_seconds >= max(1, int(days)) * 86400


def _query_recycle_bin() -> tuple[int, int]:
    if os.name != "nt":
        return (0, 0)

    class SHQUERYRBINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("i64Size", ctypes.c_longlong),
            ("i64NumItems", ctypes.c_longlong),
        ]

    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    try:
        result = ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))  # type: ignore[attr-defined]
    except Exception:
        return (0, 0)
    if int(result) != 0:
        return (0, 0)
    return (max(0, int(info.i64NumItems)), max(0, int(info.i64Size)))


def _empty_recycle_bin() -> tuple[bool, str]:
    if os.name != "nt":
        return (False, "Recycle Bin cleanup is only available on Windows.")
    flags = 0x00000001 | 0x00000002 | 0x00000004
    try:
        result = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, flags)  # type: ignore[attr-defined]
    except Exception as exc:
        return (False, str(exc))
    if int(result) != 0:
        return (False, f"Recycle Bin cleanup failed: {int(result)}")
    return (True, "OK")


def _safe_is_dir(path: Path) -> bool:
    try:
        return path.exists() and path.is_dir()
    except OSError:
        return False
