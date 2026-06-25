"""Modern PySide6 interface for PC Optimizer Lite."""

from __future__ import annotations

import ctypes
import logging
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Callable

import psutil
from PySide6.QtCore import QObject, QPointF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QCloseEvent, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .autostart import disable_autostart, enable_autostart, is_autostart_enabled
from .config import (
    DEFAULT_GITHUB_OWNER,
    DEFAULT_GITHUB_REPO,
    DEFAULT_UPDATE_CHECK_INTERVAL_HOURS,
    AppConfig,
    apply_automation_mode,
    save_config,
)
from .cpu_optimizer import CpuOptimizer
from .cpu_throttler import CpuThrottler
from .history_manager import HistoryManager
from .monitor import MonitorSnapshot, ProcessInfo, SystemMonitor, format_bytes
from .notifier import SystemNotifier
from .optimize_action import OptimizationResult, run_full_optimization, undo_optimization
from .optimizer import CleanupPlan, SystemOptimizer, open_file_location
from .ram_cleaner import MEMORY_PURGE_STANDBY_LIST, RamCleanMode, RamCleaner, RamCleanResult, is_admin, purge_memory_list
from .sleep_manager import SleepAction, SleepManager
from .smart_process_manager import CloseCandidate, SmartProcessManager
from .updater import UpdateCheckResult, UpdateError, check_for_updates, download_and_install_update, is_repository_configured
from .version import APP_VERSION
from .whitelist import Whitelist

LOGGER = logging.getLogger(__name__)


THEMES = {
    "dark": {
        "bg": "#101318",
        "panel": "#171b22",
        "panel_2": "#1f2630",
        "text": "#eef2f7",
        "muted": "#9aa6b2",
        "border": "#2b3440",
        "accent": "#60a5fa",
        "good": "#34d399",
        "warn": "#fbbf24",
        "bad": "#fb7185",
        "input": "#0f141b",
        "row": "#131821",
        "row_alt": "#18202b",
    },
    "light": {
        "bg": "#f5f7fb",
        "panel": "#ffffff",
        "panel_2": "#eef2f7",
        "text": "#111827",
        "muted": "#667085",
        "border": "#d7dde6",
        "accent": "#2563eb",
        "good": "#059669",
        "warn": "#d97706",
        "bad": "#e11d48",
        "input": "#ffffff",
        "row": "#ffffff",
        "row_alt": "#f1f5f9",
    },
}


class SnapshotBridge(QObject):
    """Thread-safe bridge from the monitor thread into Qt's GUI thread."""

    snapshot_received = Signal(object)


class OptimizationBridge(QObject):
    """Thread-safe bridge from the optimization worker into Qt."""

    progress_received = Signal(int, str, str)
    result_received = Signal(object)


class OptimizationWorker(QObject):
    """Runs the optimization cycle inside a Qt worker thread."""

    progress_received = Signal(int, str, str)
    result_received = Signal(object)

    def __init__(
        self,
        *,
        config: AppConfig,
        whitelist: Whitelist,
        optimizer: SystemOptimizer,
        history: HistoryManager,
        sleep_manager: SleepManager,
        ram_cleaner: RamCleaner,
        cpu_optimizer: CpuOptimizer,
        cancel_event: Event,
        eco_mode: bool,
        quiet: bool,
    ) -> None:
        super().__init__()
        self.config = config
        self.whitelist = whitelist
        self.optimizer = optimizer
        self.history = history
        self.sleep_manager = sleep_manager
        self.ram_cleaner = ram_cleaner
        self.cpu_optimizer = cpu_optimizer
        self.cancel_event = cancel_event
        self.eco_mode = eco_mode
        self.quiet = quiet
        self._last_progress_at = 0.0

    def run(self) -> None:
        """Execute optimization and emit exactly one result."""

        try:
            result = run_full_optimization(
                config=self.config,
                whitelist=self.whitelist,
                optimizer=self.optimizer,
                history=self.history,
                sleep_manager=self.sleep_manager,
                ram_cleaner=self.ram_cleaner,
                cpu_optimizer=self.cpu_optimizer,
                progress_callback=self._emit_progress,
                cancel_event=self.cancel_event,
                eco_mode=self.eco_mode,
                quiet=self.quiet,
            )
        except Exception as exc:
            LOGGER.exception("Optimization worker failed")
            result = OptimizationResult(eco_mode=self.eco_mode)
            result.errors.append(str(exc))
            result.cancelled = True
        self.result_received.emit(result)

    def _emit_progress(self, percent: int, step: str, detail: str) -> None:
        now = time.monotonic()
        if percent < 100 and not step.startswith("Шаг ") and now - self._last_progress_at < 0.25:
            return
        self._last_progress_at = now
        self.progress_received.emit(percent, step, detail)


class ProcessRefreshWorker(QObject):
    """Refreshes the process table away from the GUI thread."""

    result_received = Signal(object)
    error_received = Signal(str)

    def __init__(self, monitor: SystemMonitor, max_processes: int) -> None:
        super().__init__()
        self.monitor = monitor
        self.max_processes = max_processes

    def run(self) -> None:
        try:
            self.result_received.emit(self.monitor.get_processes(max_processes=self.max_processes))
        except Exception as exc:
            LOGGER.exception("Process refresh worker failed")
            self.error_received.emit(str(exc))


class CleanupWorker(QObject):
    """Scans or cleans temp/cache files without blocking Qt."""

    result_received = Signal(str, object)
    error_received = Signal(str)

    def __init__(self, optimizer: SystemOptimizer, mode: str, plan: CleanupPlan | None = None) -> None:
        super().__init__()
        self.optimizer = optimizer
        self.mode = mode
        self.plan = plan

    def run(self) -> None:
        backgrounded = _set_current_thread_background_mode(True)
        try:
            if self.mode == "scan":
                self.result_received.emit(self.mode, self.optimizer.scan_cleanup_files())
            else:
                self.result_received.emit(self.mode, self.optimizer.cleanup_temp_files(plan=self.plan, dry_run=False))
        except Exception as exc:
            LOGGER.exception("Cleanup worker failed")
            self.error_received.emit(str(exc))
        finally:
            if backgrounded:
                _set_current_thread_background_mode(False)


class RamCleanWorker(QObject):
    """Runs RAM cleanup in a worker thread."""

    result_received = Signal(object)
    error_received = Signal(str)

    def __init__(self, ram_cleaner: RamCleaner, mode: RamCleanMode, purge_standby: bool) -> None:
        super().__init__()
        self.ram_cleaner = ram_cleaner
        self.mode = mode
        self.purge_standby = purge_standby

    def run(self) -> None:
        backgrounded = _set_current_thread_background_mode(True)
        try:
            result = self.ram_cleaner.clean(self.mode)
            if self.purge_standby and is_admin():
                result.standby_purged = purge_memory_list(MEMORY_PURGE_STANDBY_LIST)
            self.result_received.emit(result)
        except Exception as exc:
            LOGGER.exception("RAM cleanup worker failed")
            self.error_received.emit(str(exc))
        finally:
            if backgrounded:
                _set_current_thread_background_mode(False)


class UpdateCheckWorker(QObject):
    """Checks GitHub Releases in the background."""

    result_received = Signal(object)

    def __init__(self, config: AppConfig, *, force: bool) -> None:
        super().__init__()
        self.owner = DEFAULT_GITHUB_OWNER
        self.repo = DEFAULT_GITHUB_REPO
        self.skipped_version = config.skipped_update_version
        self.force = force
        self.cache_ttl_seconds = DEFAULT_UPDATE_CHECK_INTERVAL_HOURS * 60 * 60

    def run(self) -> None:
        self.result_received.emit(
            check_for_updates(
                owner=self.owner,
                repo=self.repo,
                skipped_version=self.skipped_version,
                force=self.force,
                cache_ttl_seconds=self.cache_ttl_seconds,
            )
        )


class UpdateInstallWorker(QObject):
    """Downloads and stages an update in the background."""

    progress_received = Signal(int, str)
    result_received = Signal(object)
    error_received = Signal(str)

    def __init__(self, update: UpdateCheckResult) -> None:
        super().__init__()
        self.update = update

    def run(self) -> None:
        try:
            if self.update.asset is None:
                raise UpdateError("Release asset is missing.")
            self.result_received.emit(
                download_and_install_update(
                    self.update.asset,
                    progress_callback=lambda percent, message: self.progress_received.emit(percent, message),
                )
            )
        except Exception as exc:
            LOGGER.exception("Update install worker failed")
            self.error_received.emit(str(exc))


class HistoryGraphWidget(QWidget):
    """Tiny custom real-time CPU/RAM graph with no plotting dependency."""

    def __init__(self, palette: dict[str, str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.palette = palette
        self.cpu_values: deque[float] = deque(maxlen=60)
        self.ram_values: deque[float] = deque(maxlen=60)
        self.intervention_labels: deque[str | None] = deque(maxlen=60)
        self._pending_point: tuple[float, float] | None = None
        self._pending_intervention_label: str | None = None
        self._recent_interventions: deque[str] = deque(maxlen=5)
        self.max_points = 60
        self.fill_enabled = True
        self._ema_y_max = 100.0
        self._live_updates_enabled = True
        self.setMinimumHeight(170)

    def set_palette(self, palette: dict[str, str]) -> None:
        self.palette = palette
        self.update()

    def set_live_updates_enabled(self, enabled: bool) -> None:
        self._live_updates_enabled = enabled

    def set_lite_mode(self, enabled: bool) -> None:
        """Reduce retained points and paint work on weak machines."""

        max_points = 30 if enabled else 60
        if max_points != self.max_points:
            self.cpu_values = deque(list(self.cpu_values)[-max_points:], maxlen=max_points)
            self.ram_values = deque(list(self.ram_values)[-max_points:], maxlen=max_points)
            self.intervention_labels = deque(list(self.intervention_labels)[-max_points:], maxlen=max_points)
            self.max_points = max_points
        self.fill_enabled = not enabled
        self.update()

    def queue_point(self, cpu: float, ram: float) -> None:
        """Append a point at monitor cadence; no independent repaint timer."""

        self._pending_point = (max(0.0, min(cpu, 100.0)), max(0.0, min(ram, 100.0)))
        if self._live_updates_enabled:
            self._flush_pending_point()

    def mark_intervention(self, label: str) -> None:
        """Mark the latest graph point as a CPU responsiveness intervention."""

        clean_label = label.strip()
        if not clean_label:
            return
        self._recent_interventions.appendleft(clean_label)
        self.setToolTip("CPU responsiveness interventions:\n" + "\n".join(self._recent_interventions))
        if self.intervention_labels:
            self.intervention_labels[-1] = clean_label
        else:
            self._pending_intervention_label = clean_label
        if self.isVisible():
            self.update()

    def add_point(self, cpu: float, ram: float) -> None:
        """Compatibility helper used by tests."""

        self.queue_point(cpu, ram)
        self._flush_pending_point()

    def _flush_pending_point(self) -> None:
        if self._pending_point is None:
            return
        cpu, ram = self._pending_point
        self._pending_point = None
        self.cpu_values.append(cpu)
        self.ram_values.append(ram)
        self.intervention_labels.append(self._pending_intervention_label)
        self._pending_intervention_label = None
        target_max = max(30.0, min(100.0, max(cpu, ram) * 1.25))
        self._ema_y_max = max(max(cpu, ram, 30.0), self._ema_y_max * 0.88 + target_max * 0.12)
        if self.isVisible():
            self.update()

    def paintEvent(self, _: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(12, 12, -12, -12)
        painter.setPen(QPen(QColor(self.palette["border"]), 1))
        painter.setBrush(QColor(self.palette["panel"]))
        painter.drawRoundedRect(rect, 10, 10)

        inner = rect.adjusted(38, 32, -14, -24)
        self._draw_legend(painter, rect)
        for label_value, ratio in ((100, 0.0), (75, 0.25), (50, 0.5), (25, 0.75), (0, 1.0)):
            painter.setPen(QPen(QColor(self.palette["border"]), 1, Qt.PenStyle.DotLine))
            grid_y = inner.top() + inner.height() * ratio
            painter.drawLine(inner.left(), int(grid_y), inner.right(), int(grid_y))
            painter.setPen(QColor(self.palette["muted"]))
            painter.drawText(rect.left() + 8, int(grid_y) + 4, f"{label_value}%")

        x_axis_color = _mix(QColor(self.palette["muted"]), QColor(self.palette["text"]), 0.35)
        painter.setPen(QPen(x_axis_color, 1.3))
        painter.drawText(inner.left(), rect.bottom() - 6, "-60s")
        painter.drawText(inner.right() - 28, rect.bottom() - 6, "now")

        self._draw_interventions(painter, inner)
        self._draw_series(painter, inner, list(self.ram_values), QColor(self.palette["accent"]))
        self._draw_series(painter, inner, list(self.cpu_values), QColor(self.palette["good"]))

    def _draw_legend(self, painter: QPainter, rect) -> None:
        items = (("CPU", QColor(self.palette["good"])), ("RAM", QColor(self.palette["accent"])))
        x = rect.left() + 12
        y = rect.top() + 12
        for label, color in items:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(x, y + 4, 10, 10, 2, 2)
            painter.setPen(QPen(QColor(self.palette["text"]), 1))
            painter.drawText(x + 16, y + 14, label)
            x += 62

    def _draw_interventions(self, painter: QPainter, rect) -> None:
        labels = list(self.intervention_labels)
        if not labels:
            return
        step = rect.width() / max(1, self.max_points - 1)
        start_index = self.max_points - len(labels)
        color = QColor(self.palette["warn"])
        color.setAlpha(105)
        painter.setPen(QPen(color, 2))
        for index, label in enumerate(labels):
            if not label:
                continue
            x = rect.left() + (start_index + index) * step
            painter.drawLine(int(x), rect.top(), int(x), rect.bottom())

    def _draw_series(self, painter: QPainter, rect, values: list[float], color: QColor) -> None:
        if len(values) < 2:
            return
        step = rect.width() / max(1, self.max_points - 1)
        points = [
            QPointF(
                rect.left() + index * step,
                rect.bottom() - (max(0.0, min(value, 100.0)) / 100.0) * rect.height(),
            )
            for index, value in enumerate(values[-self.max_points :])
        ]
        if self.fill_enabled:
            area = QPainterPath(points[0])
            for point in points[1:]:
                area.lineTo(point)
            area.lineTo(points[-1].x(), rect.bottom())
            area.lineTo(points[0].x(), rect.bottom())
            area.closeSubpath()
            fill = QColor(color)
            fill.setAlpha(34)
            painter.fillPath(area, fill)
        painter.setPen(QPen(color, 2.2))
        for start, end in zip(points, points[1:]):
            painter.drawLine(start, end)


class MetricCard(QFrame):
    """Compact metric card with status color."""

    def __init__(self, title: str, icon_name: str, palette: dict[str, str]) -> None:
        super().__init__()
        self.title = title
        self.palette = palette
        self.icon_name = icon_name
        self._last_percent: float | None = None
        self._info_tooltip = ""
        self.setObjectName("MetricCard")
        self.setMinimumHeight(118)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        _add_shadow(self, palette)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(7)
        header = QHBoxLayout()
        header.setSpacing(8)
        self.icon_label = QLabel()
        self.icon_label.setPixmap(_feather_icon(icon_name, palette["accent"]).pixmap(22, 22))
        self.title_label = QLabel(title)
        self.title_label.setObjectName("CardTitle")
        self.title_label.setWordWrap(True)
        self.info_label = QLabel("i")
        self.info_label.setObjectName("InfoBadge")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.info_label.setVisible(False)
        header.addWidget(self.icon_label)
        header.addWidget(self.title_label)
        header.addWidget(self.info_label)
        header.addStretch(1)
        layout.addLayout(header)

        self.value_label = QLabel("--")
        self.value_label.setObjectName("CardValue")
        self.trend_label = QLabel("→")
        self.trend_label.setObjectName("TrendLabel")
        self.trend_label.setToolTip("Тренд за последние 30 сек")
        self.detail_label = QLabel("")
        self.detail_label.setObjectName("CardDetail")
        self.detail_label.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        value_row = QHBoxLayout()
        value_row.setSpacing(8)
        value_row.addWidget(self.value_label)
        value_row.addWidget(self.trend_label)
        value_row.addStretch(1)
        layout.addLayout(value_row)
        layout.addWidget(self.detail_label)
        layout.addWidget(self.progress)
        layout.addStretch(1)
        self.set_metric("--", "", 0.0)

    def set_palette(self, palette: dict[str, str]) -> None:
        self.palette = palette
        self.icon_label.setPixmap(_feather_icon(self.icon_name, palette["accent"]).pixmap(22, 22))

    def set_info_tooltip(self, text: str) -> None:
        """Show a small information marker in the header."""

        self._info_tooltip = text
        self.info_label.setVisible(bool(text))
        self.info_label.setToolTip(text)
        self.setToolTip(text)

    def set_metric(
        self,
        value: str,
        detail: str,
        percent: float | None,
        progress_text: str | None = None,
        tooltip: str | None = None,
    ) -> None:
        self.value_label.setText(value)
        self.detail_label.setText(detail)
        effective_tooltip = self._info_tooltip if tooltip is None else tooltip
        self.detail_label.setToolTip(effective_tooltip)
        self.setToolTip(effective_tooltip)
        if percent is None:
            self.progress.setValue(0)
            color = QColor(self.palette["muted"])
            self._set_trend(None)
        else:
            bounded = max(0.0, min(percent, 100.0))
            self.progress.setValue(round(bounded))
            color = _status_color(bounded, self.palette)
            self._set_trend(bounded)
        if progress_text is None:
            self.progress.setTextVisible(False)
            self.progress.setFormat("%p%")
        else:
            self.progress.setTextVisible(True)
            self.progress.setFormat(progress_text)
        self.setStyleSheet(
            f"""
            QFrame#MetricCard {{
                border: 1px solid {self.palette["border"]};
                border-left: 4px solid {color.name()};
                border-radius: 12px;
                background: {self.palette["panel"]};
            }}
            QProgressBar {{
                background: {self.palette["input"]};
                border: 1px solid {self.palette["border"]};
                border-radius: 5px;
                height: 12px;
                text-align: center;
                color: {self.palette["muted"]};
            }}
            QProgressBar::chunk {{ background: {color.name()}; border-radius: 4px; }}
            """
        )

    def _set_trend(self, percent: float | None) -> None:
        if percent is None:
            self.trend_label.setText("→")
            self.trend_label.setStyleSheet(f"color: {self.palette['muted']};")
            self.trend_label.setToolTip("Тренд за последние 30 сек: нет данных")
            return
        previous = self._last_percent
        self._last_percent = percent
        if previous is None:
            self.trend_label.setText("→")
            self.trend_label.setStyleSheet(f"color: {self.palette['muted']};")
            self.trend_label.setToolTip("Тренд за последние 30 сек: без изменений")
            return
        delta = percent - previous
        if delta > 1.0:
            self.trend_label.setText("↑")
            self.trend_label.setStyleSheet(f"color: {self.palette['bad']};")
            self.trend_label.setToolTip("Тренд за последние 30 сек: нагрузка растёт")
        elif delta < -1.0:
            self.trend_label.setText("↓")
            self.trend_label.setStyleSheet(f"color: {self.palette['good']};")
            self.trend_label.setToolTip("Тренд за последние 30 сек: нагрузка снижается")
        else:
            self.trend_label.setText("→")
            self.trend_label.setStyleSheet(f"color: {self.palette['muted']};")
            self.trend_label.setToolTip("Тренд за последние 30 сек: без заметного изменения")


class CollapsibleSection(QFrame):
    """Section with a persistent collapse toggle."""

    collapsed_changed = Signal(bool)

    def __init__(self, title: str, palette: dict[str, str], collapsed: bool = False) -> None:
        super().__init__()
        self._collapsed = False
        self.setObjectName("CollapsibleSection")
        _add_shadow(self, palette)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.toggle_button = QToolButton()
        self.toggle_button.setObjectName("CollapseButton")
        self.toggle_button.clicked.connect(self.toggle_collapsed)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("SectionTitle")
        header.addWidget(self.toggle_button)
        header.addWidget(self.title_label)
        header.addStretch(1)
        layout.addLayout(header)

        self.content = QWidget()
        self.content.setObjectName("CollapsibleContent")
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(8)
        layout.addWidget(self.content)
        self.set_collapsed(collapsed, emit=False)

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self._collapsed)

    def set_collapsed(self, collapsed: bool, *, emit: bool = True) -> None:
        self._collapsed = bool(collapsed)
        self.content.setVisible(not self._collapsed)
        self.toggle_button.setText("▼" if self._collapsed else "▲")
        self.toggle_button.setToolTip("Развернуть" if self._collapsed else "Свернуть")
        if emit:
            self.collapsed_changed.emit(self._collapsed)

    @property
    def collapsed(self) -> bool:
        return self._collapsed


class PCOptimizerQtWindow(QMainWindow):
    """Main PySide6 window."""

    def __init__(
        self,
        config: AppConfig,
        monitor: SystemMonitor,
        whitelist: Whitelist,
        optimizer: SystemOptimizer,
        notifier: SystemNotifier,
        history: HistoryManager,
        smart_manager: SmartProcessManager,
        sleep_manager: SleepManager,
        ram_cleaner: RamCleaner,
        cpu_optimizer: CpuOptimizer,
        cpu_throttler: CpuThrottler,
    ) -> None:
        super().__init__()
        self.config = config
        self.monitor = monitor
        self.whitelist = whitelist
        self.optimizer = optimizer
        self.notifier = notifier
        self.history = history
        self.smart_manager = smart_manager
        self.sleep_manager = sleep_manager
        self.ram_cleaner = ram_cleaner
        self.cpu_optimizer = cpu_optimizer
        self.cpu_throttler = cpu_throttler
        self.palette = THEMES[self.config.theme]
        self.bridge = SnapshotBridge()
        self.bridge.snapshot_received.connect(self._render_snapshot)
        self._process_rows: dict[int, ProcessInfo] = {}
        self._pending_close_candidates: dict[int, CloseCandidate] = {}
        self._high_cpu_since: float | None = None
        self._last_auto_priority_at = 0.0
        self._last_auto_close_at = 0.0
        self._optimization_cancel_event: Event | None = None
        self._optimization_thread: QThread | None = None
        self._optimization_worker_obj: OptimizationWorker | None = None
        self._optimization_quiet = False
        self._last_optimization_result: OptimizationResult | None = None
        self._last_periodic_optimization_at = time.monotonic()
        self._last_scheduled_cleanup_at = time.monotonic()
        self._last_auto_ram_clean_at = 0.0
        self._last_threshold_cpu_optimization_at = 0.0
        self._last_sleep_poll_at = 0.0
        self._foreground_monitor_interval = self.config.monitor_interval_seconds
        self._foreground_process_interval = self.config.process_refresh_seconds
        self._allow_close = False
        self._process_refresh_thread: QThread | None = None
        self._process_refresh_worker_obj: ProcessRefreshWorker | None = None
        self._cleanup_thread: QThread | None = None
        self._cleanup_worker_obj: CleanupWorker | None = None
        self._cleanup_context: dict[str, object] = {}
        self._deferred_cleanup_plan: tuple[CleanupPlan, dict[str, object]] | None = None
        self._ram_clean_thread: QThread | None = None
        self._ram_clean_worker_obj: RamCleanWorker | None = None
        self._ram_clean_context: dict[str, object] = {}
        self._update_thread: QThread | None = None
        self._update_worker_obj: UpdateCheckWorker | None = None
        self._update_install_thread: QThread | None = None
        self._update_install_worker_obj: UpdateInstallWorker | None = None
        self._update_check_manual = False
        self._pending_update: UpdateCheckResult | None = None
        self._update_action_mode = "check"
        self._syncing_controls = False

        self.setWindowTitle("PC Optimizer Lite")
        self.resize(1160, 760)
        self.setMinimumSize(980, 640)
        self.setWindowIcon(_app_icon(self.palette))
        self._build_ui()
        self._build_tray()
        self.apply_theme()

        self.monitor.add_callback(self._on_monitor_snapshot)
        self.activity_timer = QTimer(self)
        self.activity_timer.timeout.connect(self.refresh_activity)
        self.activity_timer.start(15000)
        self._apply_runtime_performance_mode()
        QTimer.singleShot(900, self._maybe_offer_lite_mode)
        QTimer.singleShot(2200, self._maybe_check_updates_on_startup)

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)
        self._arrange_metric_cards()

    def _arrange_metric_cards(self) -> None:
        if not hasattr(self, "card_grid") or not hasattr(self, "metric_cards"):
            return
        viewport_width = self.monitor_scroll.viewport().width() if hasattr(self, "monitor_scroll") else self.width()
        columns = 4 if viewport_width >= 1040 else 2 if viewport_width >= 620 else 1
        for card in self.metric_cards:
            self.card_grid.removeWidget(card)
        for index, card in enumerate(self.metric_cards):
            self.card_grid.addWidget(card, index // columns, index % columns)
        for column in range(4):
            self.card_grid.setColumnStretch(column, 1 if column < columns else 0)

    def _graph_updates_allowed(self) -> bool:
        return (
            hasattr(self, "graph_section")
            and not self.config.graph_collapsed
            and self.isVisible()
            and not self.isMinimized()
            and hasattr(self, "tabs")
            and self.tabs.currentWidget() == self.monitoring_tab
        )

    def _on_graph_section_collapsed(self, collapsed: bool) -> None:
        self.config.graph_collapsed = collapsed
        self.graph.set_live_updates_enabled(self._graph_updates_allowed())
        save_config(self.config)

    def _on_core_section_collapsed(self, collapsed: bool) -> None:
        self.config.core_table_collapsed = collapsed
        save_config(self.config)

    def _build_ui(self) -> None:
        self.tabs = QTabWidget(self)
        self.tabs.currentChanged.connect(lambda *_: self._sync_process_collection_mode())
        self.setCentralWidget(self.tabs)

        self.monitoring_tab = QWidget()
        self.processes_tab = QWidget()
        self.activity_tab = QWidget()
        self.whitelist_tab = QWidget()
        self.settings_tab = QWidget()

        self.tabs.addTab(self.monitoring_tab, _feather_icon("activity", self.palette["accent"]), "Мониторинг")
        self.tabs.addTab(self.processes_tab, _feather_icon("list", self.palette["accent"]), "Процессы")
        self.tabs.addTab(self.activity_tab, _feather_icon("clock", self.palette["accent"]), "Активность")
        self.tabs.addTab(self.whitelist_tab, _feather_icon("shield", self.palette["accent"]), "Исключения")
        self.tabs.addTab(self.settings_tab, _feather_icon("settings", self.palette["accent"]), "Настройки")

        self._build_monitoring_tab()
        self._build_processes_tab()
        self._build_activity_tab()
        self._build_whitelist_tab()
        self._build_settings_tab()

    def _build_monitoring_tab(self) -> None:
        root_layout = QVBoxLayout(self.monitoring_tab)
        root_layout.setContentsMargins(0, 0, 0, 0)
        self.monitor_scroll = QScrollArea()
        self.monitor_scroll.setObjectName("MonitorScroll")
        self.monitor_scroll.setWidgetResizable(True)
        self.monitor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 18, 18, 12)
        layout.setSpacing(14)
        self.monitor_scroll.setWidget(content)
        root_layout.addWidget(self.monitor_scroll, 1)

        self.update_banner = QFrame()
        self.update_banner.setObjectName("UpdateBanner")
        self.update_banner.setVisible(False)
        update_layout = QHBoxLayout(self.update_banner)
        update_layout.setContentsMargins(14, 10, 14, 10)
        update_layout.setSpacing(10)
        self.update_banner_label = QLabel("Доступно обновление")
        self.update_banner_label.setObjectName("UpdateBannerText")
        self.update_download_progress = QProgressBar()
        self.update_download_progress.setRange(0, 100)
        self.update_download_progress.setValue(0)
        self.update_download_progress.setVisible(False)
        self.update_banner_button = _button("Обновить", "arrow-down", self.install_pending_update, self.palette)
        self.update_later_button = _button("Позже", "x", self.hide_update_banner, self.palette)
        update_layout.addWidget(self.update_banner_label, 1)
        update_layout.addWidget(self.update_download_progress)
        update_layout.addWidget(self.update_banner_button)
        update_layout.addWidget(self.update_later_button)
        layout.addWidget(self.update_banner)

        optimize_panel = QFrame()
        optimize_panel.setObjectName("OptimizeHero")
        _add_shadow(optimize_panel, self.palette)
        optimize_layout = QVBoxLayout(optimize_panel)
        optimize_layout.setContentsMargins(18, 16, 18, 16)
        optimize_layout.setSpacing(8)
        title = QLabel("Оптимизация одним нажатием")
        title.setObjectName("HeroTitle")
        subtitle = QLabel("Плавный пошаговый цикл: проверка системы, освобождение ресурсов и понятный отчёт.")
        subtitle.setObjectName("HeroSubtitle")
        self.optimize_button = QPushButton("Оптимизировать")
        self.optimize_button.setObjectName("OptimizeButton")
        self.optimize_button.setIcon(_feather_icon("zap", self.palette["bg"]))
        self.optimize_button.setIconSize(QSize(22, 22))
        self.optimize_button.clicked.connect(self.start_full_optimization)
        self.optimize_progress = QProgressBar()
        self.optimize_progress.setRange(0, 100)
        self.optimize_progress.setValue(0)
        self.optimize_progress.setVisible(False)
        self.optimize_status = QLabel("● Готово к запуску")
        self.optimize_status.setObjectName("StatusText")
        self.optimize_cancel_button = _button("Прервать", "x", self.cancel_full_optimization, self.palette)
        self.optimize_cancel_button.setVisible(False)
        center = QHBoxLayout()
        center.addStretch(1)
        center.addWidget(self.optimize_button)
        center.addStretch(1)
        cancel_row = QHBoxLayout()
        cancel_row.addWidget(self.optimize_status)
        cancel_row.addStretch(1)
        cancel_row.addWidget(self.optimize_cancel_button)
        optimize_layout.addWidget(title, alignment=Qt.AlignmentFlag.AlignCenter)
        optimize_layout.addWidget(subtitle, alignment=Qt.AlignmentFlag.AlignCenter)
        optimize_layout.addLayout(center)
        optimize_layout.addWidget(self.optimize_progress)
        optimize_layout.addLayout(cancel_row)
        layout.addWidget(optimize_panel)

        self.card_grid = QGridLayout()
        self.card_grid.setContentsMargins(0, 0, 0, 0)
        self.card_grid.setHorizontalSpacing(16)
        self.card_grid.setVerticalSpacing(16)
        self.cpu_card = MetricCard("CPU", "cpu", self.palette)
        self.ram_card = MetricCard("RAM", "memory", self.palette)
        self.disk_card = MetricCard("Диск", "hard-drive", self.palette)
        self.swap_card = MetricCard("Файл подкачки", "hard-drive", self.palette)
        self.swap_card.set_info_tooltip(
            "Файл подкачки — резервное расширение ОЗУ на диске. "
            "Высокое использование (>70%) может говорить о нехватке физической памяти."
        )
        self.metric_cards = (self.cpu_card, self.ram_card, self.disk_card, self.swap_card)
        layout.addLayout(self.card_grid)
        self._arrange_metric_cards()

        self.graph = HistoryGraphWidget(self.palette)
        self.graph_section = CollapsibleSection("График CPU/RAM", self.palette, self.config.graph_collapsed)
        self.graph_section.collapsed_changed.connect(self._on_graph_section_collapsed)
        self.graph_section.content_layout.addWidget(self.graph)
        layout.addWidget(self.graph_section)
        self.graph.set_live_updates_enabled(not self.config.graph_collapsed)

        self.core_table = QTableWidget(0, 3)
        self.core_table.setHorizontalHeaderLabels(["Ядро", "Нагрузка", "%"])
        _configure_table(self.core_table, min_rows=3, max_height=190)
        self.core_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.core_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.core_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.core_section = CollapsibleSection("Ядра CPU", self.palette, self.config.core_table_collapsed)
        self.core_section.collapsed_changed.connect(self._on_core_section_collapsed)
        self.core_section.content_layout.addWidget(self.core_table)
        layout.addWidget(self.core_section)

        self.disk_table = QTableWidget(0, 7)
        self.disk_table.setHorizontalHeaderLabels(["Диск", "Точка", "ФС", "Занято", "Свободно", "Всего", "%"])
        _configure_table(self.disk_table, min_rows=3, max_height=190)
        layout.addWidget(self.disk_table, 1)

        bottom_bar = QFrame()
        bottom_bar.setObjectName("MonitorBottomBar")
        bottom_bar.setFixedHeight(62)
        bottom_layout = QHBoxLayout(bottom_bar)
        bottom_layout.setContentsMargins(18, 10, 18, 10)
        bottom_layout.setSpacing(10)
        ram_label = QLabel("RAM")
        ram_label.setObjectName("BottomBarLabel")
        ram_label.setToolTip("Меню выбирает лёгкую или глубокую очистку RAM")
        bottom_layout.addWidget(ram_label)
        bottom_layout.addWidget(self._build_ram_clean_button())
        bottom_layout.addWidget(_button("Очистить temp/cache", "trash", self.confirm_temp_cleanup, self.palette))
        bottom_layout.addWidget(_button("Свернуть в трей", "minimize", self.hide_to_tray, self.palette))
        bottom_layout.addStretch(1)
        root_layout.addWidget(bottom_bar, 0)

    def _build_ram_clean_button(self) -> QToolButton:
        button = QToolButton()
        button.setObjectName("RamCleanButton")
        button.setText("Очистить RAM")
        button.setIcon(_feather_icon("memory", self.palette["accent"]))
        button.setToolTip(
            "Лёгкая очистка освобождает неиспользуемую память безопасных неактивных процессов. "
            "Это не закрывает приложения, но может вызвать краткую подгрузку страниц при возврате к ним."
        )
        menu = QMenu(button)
        light_action = QAction("Лёгкая", button)
        light_action.triggered.connect(lambda: self.clean_ram(RamCleanMode.LIGHT))
        deep_action = QAction("Глубокая (нужны права администратора)", button)
        deep_action.triggered.connect(lambda: self.clean_ram(RamCleanMode.DEEP))
        menu.addAction(light_action)
        menu.addAction(deep_action)
        button.setMenu(menu)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        button.clicked.connect(lambda: self.clean_ram(RamCleanMode.LIGHT))
        return button

    def _build_processes_tab(self) -> None:
        layout = QVBoxLayout(self.processes_tab)
        layout.setContentsMargins(18, 18, 18, 18)
        toolbar = QHBoxLayout()
        for text, icon, callback in (
            ("Обновить", "refresh", self.refresh_process_table),
            ("Снизить влияние", "arrow-down", self.lower_selected_priority),
            ("Закрыть выбранный", "x", self.terminate_selected_process),
            ("Открыть папку", "folder", self.open_selected_process_location),
        ):
            toolbar.addWidget(_button(text, icon, callback, self.palette))
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.process_table = QTableWidget(0, 7)
        self.process_table.setHorizontalHeaderLabels(["PID", "Процесс", "CPU %", "RAM %", "RAM", "Приоритет", "Путь"])
        _configure_table(self.process_table, min_rows=8)
        self.process_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.process_table, 1)
        layout.addWidget(QLabel("Серым отмечены системные или пользовательские исключения: они не трогаются никакими функциями."))

    def _build_activity_tab(self) -> None:
        layout = QVBoxLayout(self.activity_tab)
        layout.setContentsMargins(18, 18, 18, 18)

        self.activity_table = QTableWidget(0, 3)
        self.activity_table.setHorizontalHeaderLabels(["Время", "Событие", "Детали"])
        _configure_table(self.activity_table, min_rows=5)
        layout.addWidget(QLabel("Лента событий"))
        layout.addWidget(self.activity_table, 1)

        self.sleep_table = QTableWidget(0, 6)
        self.sleep_table.setHorizontalHeaderLabels(["PID", "Процесс", "Уснул", "Причина", "Состояние", "Действие"])
        _configure_table(self.sleep_table, min_rows=3, max_height=170)
        layout.addWidget(QLabel("Спящие приложения"))
        layout.addWidget(self.sleep_table)

        self.closed_table = QTableWidget(0, 6)
        self.closed_table.setHorizontalHeaderLabels(["Время", "Процесс", "Причина", "Режим", "Путь", "Действие"])
        _configure_table(self.closed_table, min_rows=4)
        layout.addWidget(QLabel("История закрытых процессов"))
        layout.addWidget(self.closed_table)
        self.refresh_activity()

    def _build_whitelist_tab(self) -> None:
        layout = QVBoxLayout(self.whitelist_tab)
        layout.setContentsMargins(18, 18, 18, 18)
        lists = QHBoxLayout()

        left = QVBoxLayout()
        left.addWidget(QLabel("Имена процессов"))
        self.names_list = QListWidget()
        left.addWidget(self.names_list)
        lists.addLayout(left)

        right = QVBoxLayout()
        right.addWidget(QLabel("Пути к exe"))
        self.paths_list = QListWidget()
        right.addWidget(self.paths_list)
        lists.addLayout(right)
        layout.addLayout(lists, 1)

        controls = QHBoxLayout()
        self.whitelist_entry = QLineEdit()
        self.whitelist_entry.setPlaceholderText("например: render.exe")
        controls.addWidget(self.whitelist_entry, 1)
        controls.addWidget(_button("Добавить имя", "plus", self.add_whitelist_name, self.palette))
        controls.addWidget(_button("Добавить exe", "folder-plus", self.add_whitelist_path, self.palette))
        controls.addWidget(_button("Удалить выбранное", "trash", self.remove_whitelist_selected, self.palette))
        layout.addLayout(controls)
        self.refresh_whitelist_lists()

    def _build_settings_tab(self) -> None:
        layout = QVBoxLayout(self.settings_tab)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        scroll = QScrollArea()
        scroll.setObjectName("SettingsScroll")
        scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(2, 2, 10, 2)
        content_layout.setSpacing(18)

        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Тёмная", "dark")
        self.theme_combo.addItem("Светлая", "light")
        self.theme_combo.setCurrentIndex(0 if self.config.theme == "dark" else 1)
        self.theme_combo.currentIndexChanged.connect(lambda *_: self._change_theme_from_combo())

        self.automation_mode_combo = QComboBox()
        self.automation_mode_combo.addItem("Наблюдение", "observation")
        self.automation_mode_combo.addItem("Ручной", "manual")
        self.automation_mode_combo.addItem("Автопилот", "autopilot")
        self.automation_mode_combo.setCurrentIndex(
            {"observation": 0, "manual": 1, "autopilot": 2}.get(self.config.automation_mode, 0)
        )
        self.automation_mode_combo.currentIndexChanged.connect(lambda *_: self._preview_automation_mode())

        self.observation_only_check = _toggle("Режим только наблюдения: запретить авто-действия")
        self.observation_only_check.setChecked(self.config.observation_only_mode)
        self.autostart_check = _toggle("Запускать с Windows")
        self.autostart_check.setChecked(is_autostart_enabled())
        self.lite_mode_check = _toggle("Режим слабого ПК")
        self.lite_mode_check.setChecked(self.config.lite_mode_enabled)
        self.lite_mode_check.setToolTip(
            "Увеличивает интервалы опроса, упрощает график и по умолчанию отключает тяжёлые действия."
        )
        self.lite_mode_check.toggled.connect(lambda enabled: self._preview_lite_mode_defaults(enabled))
        self.interval_edit = QLineEdit(str(self.config.monitor_interval_seconds))
        self.process_interval_edit = QLineEdit(str(self.config.process_refresh_seconds))
        self.cpu_threshold_edit = QLineEdit(str(self.config.cpu_threshold_percent))
        self.cpu_sustain_edit = QLineEdit(str(self.config.cpu_sustain_seconds))
        self.ram_threshold_edit = QLineEdit(str(self.config.ram_threshold_percent))
        self.cooldown_edit = QLineEdit(str(self.config.notification_cooldown_seconds))
        self.max_priority_edit = QLineEdit(str(self.config.max_auto_priority_changes))
        self.auto_priority_check = _toggle("Автоматически понижать priority при критической нагрузке")
        self.auto_priority_check.setChecked(self.config.auto_lower_priority_enabled)

        self.auto_close_combo = QComboBox()
        self.auto_close_combo.addItem("Выключено", "off")
        self.auto_close_combo.addItem("Спрашивать подтверждение", "ask")
        self.auto_close_combo.addItem("Разрешить авто-закрытие", "auto")
        self.auto_close_combo.setCurrentIndex({"off": 0, "ask": 1, "auto": 2}.get(self.config.auto_close_mode, 1))
        self.close_background_edit = QLineEdit(str(self.config.auto_close_min_background_minutes))
        self.close_cpu_edit = QLineEdit(str(self.config.auto_close_cpu_threshold_percent))
        self.close_ram_edit = QLineEdit(str(self.config.auto_close_memory_threshold_percent))
        self.close_duplicates_edit = QLineEdit(str(self.config.auto_close_duplicate_count))

        self.sleep_enabled_check = _toggle("Разрешить режим сна для неактивных приложений")
        self.sleep_enabled_check.setChecked(self.config.sleep_enabled)
        self.sleep_after_edit = QLineEdit(str(self.config.sleep_after_minutes))
        self.sleep_check_edit = QLineEdit(str(self.config.sleep_check_seconds))
        self.ram_auto_clean_check = _toggle("Авто-очистка RAM лёгким режимом при превышении порога")
        self.ram_auto_clean_check.setChecked(self.config.ram_auto_clean_enabled)
        self.ram_auto_threshold_edit = QLineEdit(str(self.config.ram_auto_clean_threshold_percent))
        self.cpu_optimizer_check = _toggle("Включить мягкую CPU-защиту интерфейса")
        self.cpu_optimizer_check.setChecked(self.config.cpu_optimizer_enabled)
        self.cpu_optimizer_priority_combo = QComboBox()
        self.cpu_optimizer_priority_combo.addItem("Ниже обычного", "below_normal")
        self.cpu_optimizer_priority_combo.addItem("Минимальный", "idle")
        self.cpu_optimizer_priority_combo.setCurrentIndex(
            {"below_normal": 0, "idle": 1}.get(self.config.cpu_optimizer_priority_mode, 0)
        )
        self.cpu_optimizer_min_cpu_edit = QLineEdit(str(self.config.cpu_optimizer_min_process_cpu_percent))
        self.cpu_optimizer_max_edit = QLineEdit(str(self.config.cpu_optimizer_max_processes))
        self.cpu_optimizer_affinity_ratio_edit = QLineEdit(str(self.config.cpu_optimizer_affinity_ratio))
        self.cpu_optimizer_min_cores_edit = QLineEdit(str(self.config.cpu_optimizer_affinity_min_cores))
        self.cpu_optimizer_restore_edit = QLineEdit(str(self.config.cpu_optimizer_restore_after_seconds))
        self.cpu_throttle_check = _toggle("Автоматически вмешиваться при устойчивой CPU-нагрузке")
        self.cpu_throttle_check.setChecked(self.config.cpu_throttle_enabled)
        self.cpu_affinity_check = _toggle("Разрешить временно ограничивать ядра процесса")
        self.cpu_affinity_check.setChecked(self.config.cpu_throttle_affinity_enabled)
        self.cpu_limiter_check = _toggle("Коротко притормаживать тяжёлый процесс, если обычное снижение не помогает")
        self.cpu_limiter_check.setChecked(self.config.cpu_limiter_enabled)
        self.scheduled_cleanup_check = _toggle("Включить автоочистку по расписанию")
        self.scheduled_cleanup_check.setChecked(self.config.scheduled_cleanup_enabled)
        self.scheduled_cleanup_interval_edit = QLineEdit(str(self.config.scheduled_cleanup_interval_minutes))
        self.scheduled_cleanup_notify_check = _toggle("Тихое уведомление в трее после автоочистки")
        self.scheduled_cleanup_notify_check.setChecked(self.config.scheduled_cleanup_notify)
        self.auto_cleanup_cooldown_edit = QLineEdit(str(self.config.auto_cleanup_cooldown_minutes))
        self.cleanup_temp_check = _toggle("Temp текущего пользователя")
        self.cleanup_temp_check.setChecked(self.config.cleanup_temp_enabled)
        self.cleanup_windows_temp_check = _toggle("Windows Temp")
        self.cleanup_windows_temp_check.setChecked(self.config.cleanup_windows_temp_enabled)
        self.cleanup_browser_cache_check = _toggle("Кэши браузеров")
        self.cleanup_browser_cache_check.setChecked(self.config.cleanup_browser_cache_enabled)
        self.cleanup_prefetch_check = _toggle("Prefetch старше 7 дней")
        self.cleanup_prefetch_check.setChecked(self.config.cleanup_prefetch_enabled)
        self.cleanup_logs_check = _toggle("Логи Windows/WER старше N дней")
        self.cleanup_logs_check.setChecked(self.config.cleanup_logs_enabled)
        self.cleanup_logs_days_edit = QLineEdit(str(self.config.cleanup_logs_older_than_days))
        self.cleanup_recycle_bin_check = _toggle("Корзина Windows (только при явном включении)")
        self.cleanup_recycle_bin_check.setChecked(self.config.cleanup_recycle_bin_enabled)
        self.periodic_optimization_check = _toggle("Включить периодическую оптимизацию")
        self.periodic_optimization_check.setChecked(self.config.periodic_optimization_enabled)
        self.periodic_interval_edit = QLineEdit(str(self.config.periodic_optimization_interval_minutes))
        self.periodic_eco_check = _toggle("Облегчённый авторежим")
        self.periodic_eco_check.setChecked(self.config.periodic_optimization_eco_mode)
        self.periodic_notify_check = _toggle("Тихое уведомление в трее после автооптимизации")
        self.periodic_notify_check.setChecked(self.config.periodic_optimization_notify)
        self.optimization_step_checks: dict[str, QCheckBox] = {}
        for key, label, tooltip in (
            ("snapshot", "Шаг 1: снять снимок системы", "Один кэшированный список процессов для всего цикла."),
            ("classify", "Шаг 2: классифицировать процессы", "Разделяет процессы на группы: память, CPU, сон, закрытие."),
            ("ram", "Шаг 3: освободить неиспользуемую память приложений", "Безопасный EmptyWorkingSet для выбранных неактивных процессов."),
            ("standby", "Шаг 4: очистить Standby List", "Системный список ожидания очищается только с правами администратора."),
            ("cpu", "Шаг 5: снизить влияние тяжёлых процессов", "Временно меняет priority/affinity у безопасных кандидатов."),
            ("sleep", "Шаг 6: усыпить давно неактивные окна", "Переводит неактивные окна в щадящий режим без закрытия."),
            ("close", "Шаг 7: закрыть безопасные фоновые процессы", "Закрывает только консервативно выбранные процессы вне исключений."),
            ("cleanup", "Шаг 8: очистить temp/cache", "Удаляет файлы только из известных безопасных временных папок."),
        ):
            checkbox = _toggle(label)
            checkbox.setChecked(bool(getattr(self.config, f"optimize_step_{key}_enabled", True)))
            checkbox.setToolTip(tooltip)
            self.optimization_step_checks[key] = checkbox
        self.update_startup_check = _toggle("Проверять обновления при запуске")
        self.update_startup_check.setChecked(self.config.check_updates_on_startup)
        self.update_notify_check = _toggle("Уведомлять о новых версиях")
        self.update_notify_check.setChecked(self.config.update_notify_enabled)
        self.update_auto_install_check = _toggle("Автоматически устанавливать обновления")
        self.update_auto_install_check.setChecked(self.config.auto_install_updates)
        self.check_updates_button = _button("Проверить обновления", "refresh", self.check_updates_now, self.palette)
        self.check_updates_button.setObjectName("UpdateActionButton")
        self.check_updates_button.setProperty("updateAvailable", False)
        self._preview_automation_mode()

        monitoring_section, monitoring_form = _settings_section("Мониторинг", self.palette)
        _form_row(monitoring_form, "Тема", self.theme_combo)
        _form_row(monitoring_form, "Режим", self.automation_mode_combo)
        _form_row(monitoring_form, "", self.observation_only_check)
        _form_row(monitoring_form, "", self.autostart_check)
        _form_row(monitoring_form, "", self.lite_mode_check)
        _form_row(monitoring_form, "Интервал мониторинга, сек", self.interval_edit)
        _form_row(monitoring_form, "Интервал обновления процессов, сек", self.process_interval_edit)
        content_layout.addWidget(monitoring_section)

        notifications_section, notifications_form = _settings_section("Уведомления", self.palette)
        _form_row(notifications_form, "Порог CPU, %", self.cpu_threshold_edit)
        _form_row(notifications_form, "CPU выше порога, сек", self.cpu_sustain_edit)
        _form_row(notifications_form, "Порог RAM, %", self.ram_threshold_edit)
        _form_row(notifications_form, "Антиспам уведомлений, сек", self.cooldown_edit)
        content_layout.addWidget(notifications_section)

        close_section, close_form = _settings_section("Умное закрытие процессов", self.palette)
        _form_row(close_form, "Режим закрытия", self.auto_close_combo)
        _form_row(close_form, "Фон без окна, мин", self.close_background_edit)
        _form_row(close_form, "CPU процесса для закрытия, %", self.close_cpu_edit)
        _form_row(close_form, "RAM процесса для закрытия, %", self.close_ram_edit)
        _form_row(close_form, "Дубликатов процесса до проверки", self.close_duplicates_edit)
        _form_row(close_form, "", self.auto_priority_check)
        _form_row(close_form, "Макс. priority-изменений за раз", self.max_priority_edit)
        content_layout.addWidget(close_section)

        sleep_section, sleep_form = _settings_section("Режим сна", self.palette)
        _form_row(sleep_form, "", self.sleep_enabled_check)
        _form_row(sleep_form, "Сон после неактивности, мин", self.sleep_after_edit)
        _form_row(sleep_form, "Интервал проверки сна, сек", self.sleep_check_edit)
        content_layout.addWidget(sleep_section)

        ram_section, ram_form = _settings_section("Очистка RAM", self.palette)
        _form_row(ram_form, "", self.ram_auto_clean_check)
        _form_row(ram_form, "Порог авто-очистки RAM, %", self.ram_auto_threshold_edit)
        content_layout.addWidget(ram_section)

        cpu_section, cpu_form = _settings_section("Оптимизация CPU", self.palette)
        _form_row(cpu_form, "", self.cpu_optimizer_check)
        _form_row(cpu_form, "Приоритет для выбранных процессов", self.cpu_optimizer_priority_combo)
        _form_row(cpu_form, "CPU процесса от, %", self.cpu_optimizer_min_cpu_edit)
        _form_row(cpu_form, "Макс. процессов за цикл", self.cpu_optimizer_max_edit)
        _form_row(cpu_form, "", self.cpu_throttle_check)
        _form_row(cpu_form, "", self.cpu_affinity_check)
        _form_row(cpu_form, "", self.cpu_limiter_check)
        _form_row(cpu_form, "Доля ядер при affinity", self.cpu_optimizer_affinity_ratio_edit)
        _form_row(cpu_form, "Минимум ядер оставить", self.cpu_optimizer_min_cores_edit)
        _form_row(cpu_form, "Автовозврат через, сек", self.cpu_optimizer_restore_edit)
        hint = QLabel(
            "В норме проверяется только общий CPU; процессы сканируются только после устойчивого превышения порога. "
            "Изменения временные и автоматически возвращаются обратно."
        )
        hint.setObjectName("SettingsHint")
        cpu_form.addRow(hint)
        content_layout.addWidget(cpu_section)

        cleanup_section, cleanup_form = _settings_section("Автоматическая очистка", self.palette)
        _form_row(cleanup_form, "", self.scheduled_cleanup_check)
        _form_row(cleanup_form, "Интервал, мин", self.scheduled_cleanup_interval_edit)
        _form_row(cleanup_form, "", self.scheduled_cleanup_notify_check)
        _form_row(cleanup_form, "Кулдаун автоочистки, мин", self.auto_cleanup_cooldown_edit)
        _form_row(cleanup_form, "", self.cleanup_temp_check)
        _form_row(cleanup_form, "", self.cleanup_windows_temp_check)
        _form_row(cleanup_form, "", self.cleanup_browser_cache_check)
        _form_row(cleanup_form, "", self.cleanup_prefetch_check)
        _form_row(cleanup_form, "", self.cleanup_logs_check)
        _form_row(cleanup_form, "Возраст логов, дней", self.cleanup_logs_days_edit)
        _form_row(cleanup_form, "", self.cleanup_recycle_bin_check)
        cleanup_hint = QLabel("Очищаются только заранее известные temp/cache/log roots. Документы и произвольные папки не сканируются.")
        cleanup_hint.setObjectName("SettingsHint")
        cleanup_form.addRow(cleanup_hint)
        content_layout.addWidget(cleanup_section)

        periodic_section, periodic_form = _settings_section("Автооптимизация", self.palette)
        _form_row(periodic_form, "", self.periodic_optimization_check)
        _form_row(periodic_form, "Интервал автооптимизации, мин", self.periodic_interval_edit)
        _form_row(periodic_form, "", self.periodic_eco_check)
        _form_row(periodic_form, "", self.periodic_notify_check)
        content_layout.addWidget(periodic_section)

        steps_section, steps_form = _settings_section("Пошаговая оптимизация", self.palette)
        for checkbox in self.optimization_step_checks.values():
            _form_row(steps_form, "", checkbox)
        steps_hint = QLabel("Cancel останавливает цикл после текущего шага. В Lite Mode паузы между шагами длиннее.")
        steps_hint.setObjectName("SettingsHint")
        steps_form.addRow(steps_hint)
        content_layout.addWidget(steps_section)

        updates_section, updates_form = _settings_section("Обновления", self.palette)
        _form_row(updates_form, "", self.update_startup_check)
        _form_row(updates_form, "", self.update_notify_check)
        _form_row(updates_form, "", self.update_auto_install_check)
        _form_row(updates_form, "", self.check_updates_button)
        content_layout.addWidget(updates_section)

        content_layout.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        footer = QFrame()
        footer.setObjectName("SettingsFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(14, 12, 14, 12)
        footer_layout.addStretch(1)
        self.save_settings_button = QPushButton("Сохранить настройки")
        self.save_settings_button.setIcon(_feather_icon("save", self.palette["bg"]))
        self.save_settings_button.setObjectName("SaveButton")
        self.save_settings_button.setMinimumWidth(240)
        self.save_settings_button.clicked.connect(self.save_settings)
        footer_layout.addWidget(self.save_settings_button)
        layout.addWidget(footer)

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self._current_icon(), self)
        self.tray.setToolTip(f"PC Optimizer Lite — {self._mode_label()}")
        menu = QMenu()
        show_action = QAction(_feather_icon("eye", self.palette["accent"]), "Показать", self)
        show_action.triggered.connect(self.show_normal)
        exit_action = QAction(_feather_icon("x", self.palette["accent"]), "Выход", self)
        exit_action.triggered.connect(self.exit_app)
        menu.addAction(show_action)
        menu.addAction(exit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _mode_label(self) -> str:
        return {
            "observation": "Наблюдение",
            "manual": "Ручной режим",
            "autopilot": "Автопилот",
        }.get(self.config.automation_mode, "Наблюдение")

    def _current_icon(self) -> QIcon:
        if self._pending_update and self._pending_update.update_available:
            badge = self.palette["warn"]
        else:
            badge = self.palette["good"] if self.config.automation_mode == "autopilot" else ""
        return _app_icon(self.palette, badge)

    def _refresh_tray_state(self) -> None:
        icon = self._current_icon()
        self.setWindowIcon(icon)
        if hasattr(self, "tray"):
            self.tray.setIcon(icon)
            self.tray.setToolTip(f"PC Optimizer Lite — {self._mode_label()}")

    def _preview_automation_mode(self) -> None:
        if self._syncing_controls:
            return
        mode = self.automation_mode_combo.currentData()
        self.observation_only_check.setChecked(mode == "observation")
        if mode == "manual":
            self.auto_priority_check.setChecked(False)
            self.sleep_enabled_check.setChecked(False)
            self.ram_auto_clean_check.setChecked(False)
            self.cpu_throttle_check.setChecked(False)
            self.cpu_limiter_check.setChecked(False)
            self.scheduled_cleanup_check.setChecked(False)
            self.periodic_optimization_check.setChecked(False)
            self.auto_close_combo.setCurrentIndex(0)
        elif mode == "autopilot":
            self.auto_priority_check.setChecked(True)
            self.sleep_enabled_check.setChecked(True)
            self.ram_auto_clean_check.setChecked(True)
            self.cpu_optimizer_check.setChecked(True)
            self.cpu_throttle_check.setChecked(True)
            self.scheduled_cleanup_check.setChecked(True)
            self.scheduled_cleanup_notify_check.setChecked(True)
            self.periodic_optimization_check.setChecked(True)
            self.periodic_eco_check.setChecked(True)
            self.periodic_notify_check.setChecked(True)
            self.auto_close_combo.setCurrentIndex(2)

    def _preview_lite_mode_defaults(self, enabled: bool) -> None:
        if not enabled or self._syncing_controls:
            return
        self.interval_edit.setText(str(max(_safe_float(self.interval_edit.text(), 3.0), 3.5)))
        self.process_interval_edit.setText(str(max(_safe_float(self.process_interval_edit.text(), 6.0), 12.0)))
        self.cpu_optimizer_max_edit.setText(str(min(int(_safe_float(self.cpu_optimizer_max_edit.text(), 3)), 2)))
        self.cpu_throttle_check.setChecked(False)
        self.cpu_limiter_check.setChecked(False)
        self.auto_close_combo.setCurrentIndex(0)
        for key in ("sleep", "close", "cleanup"):
            checkbox = self.optimization_step_checks.get(key)
            if checkbox is not None:
                checkbox.setChecked(False)

    def _sync_controls_from_config(self) -> None:
        self._syncing_controls = True
        try:
            self.automation_mode_combo.setCurrentIndex(
                {"observation": 0, "manual": 1, "autopilot": 2}.get(self.config.automation_mode, 0)
            )
            self.observation_only_check.setChecked(self.config.observation_only_mode)
            self.auto_priority_check.setChecked(self.config.auto_lower_priority_enabled)
            self.cpu_threshold_edit.setText(str(self.config.cpu_threshold_percent))
            self.cpu_sustain_edit.setText(str(self.config.cpu_sustain_seconds))
            self.lite_mode_check.setChecked(self.config.lite_mode_enabled)
            self.interval_edit.setText(str(self.config.monitor_interval_seconds))
            self.process_interval_edit.setText(str(self.config.process_refresh_seconds))
            self.auto_close_combo.setCurrentIndex({"off": 0, "ask": 1, "auto": 2}.get(self.config.auto_close_mode, 0))
            self.sleep_enabled_check.setChecked(self.config.sleep_enabled)
            self.ram_auto_clean_check.setChecked(self.config.ram_auto_clean_enabled)
            self.cpu_optimizer_check.setChecked(self.config.cpu_optimizer_enabled)
            self.cpu_optimizer_priority_combo.setCurrentIndex(
                {"below_normal": 0, "idle": 1}.get(self.config.cpu_optimizer_priority_mode, 0)
            )
            self.cpu_optimizer_min_cpu_edit.setText(str(self.config.cpu_optimizer_min_process_cpu_percent))
            self.cpu_optimizer_max_edit.setText(str(self.config.cpu_optimizer_max_processes))
            self.cpu_optimizer_affinity_ratio_edit.setText(str(self.config.cpu_optimizer_affinity_ratio))
            self.cpu_optimizer_min_cores_edit.setText(str(self.config.cpu_optimizer_affinity_min_cores))
            self.cpu_optimizer_restore_edit.setText(str(self.config.cpu_optimizer_restore_after_seconds))
            self.cpu_throttle_check.setChecked(self.config.cpu_throttle_enabled)
            self.cpu_affinity_check.setChecked(self.config.cpu_throttle_affinity_enabled)
            self.cpu_limiter_check.setChecked(self.config.cpu_limiter_enabled)
            self.scheduled_cleanup_check.setChecked(self.config.scheduled_cleanup_enabled)
            self.scheduled_cleanup_interval_edit.setText(str(self.config.scheduled_cleanup_interval_minutes))
            self.scheduled_cleanup_notify_check.setChecked(self.config.scheduled_cleanup_notify)
            self.auto_cleanup_cooldown_edit.setText(str(self.config.auto_cleanup_cooldown_minutes))
            self.cleanup_temp_check.setChecked(self.config.cleanup_temp_enabled)
            self.cleanup_windows_temp_check.setChecked(self.config.cleanup_windows_temp_enabled)
            self.cleanup_browser_cache_check.setChecked(self.config.cleanup_browser_cache_enabled)
            self.cleanup_prefetch_check.setChecked(self.config.cleanup_prefetch_enabled)
            self.cleanup_logs_check.setChecked(self.config.cleanup_logs_enabled)
            self.cleanup_logs_days_edit.setText(str(self.config.cleanup_logs_older_than_days))
            self.cleanup_recycle_bin_check.setChecked(self.config.cleanup_recycle_bin_enabled)
            self.periodic_optimization_check.setChecked(self.config.periodic_optimization_enabled)
            self.periodic_interval_edit.setText(str(self.config.periodic_optimization_interval_minutes))
            self.periodic_eco_check.setChecked(self.config.periodic_optimization_eco_mode)
            self.periodic_notify_check.setChecked(self.config.periodic_optimization_notify)
            for key, checkbox in self.optimization_step_checks.items():
                checkbox.setChecked(bool(getattr(self.config, f"optimize_step_{key}_enabled", True)))
            self.update_startup_check.setChecked(self.config.check_updates_on_startup)
            self.update_notify_check.setChecked(self.config.update_notify_enabled)
            self.update_auto_install_check.setChecked(self.config.auto_install_updates)
            self.autostart_check.setChecked(is_autostart_enabled())
        finally:
            self._syncing_controls = False

    def _ensure_autopilot_consent(self) -> bool:
        if self.config.autopilot_consent_accepted:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("Автопилот")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("Автопилот будет сам выполнять безопасные действия в фоне.")
        box.setInformativeText(
            "PC Optimizer Lite сможет без отдельных подтверждений усыплять неактивные приложения, "
            "понижать priority, выполнять лёгкую автоочистку RAM/temp/cache, включать CPU throttling и закрывать "
            "только консервативно выбранные фоновые процессы вне whitelist. Во время автопилота будут только toast-уведомления "
            "и записи в историю."
        )
        checkbox = QCheckBox("Понимаю и согласен")
        box.setCheckBox(checkbox)
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Ok or not checkbox.isChecked():
            QMessageBox.information(self, "Автопилот", "Автопилот не включён без явного согласия.")
            return False
        self.config.autopilot_consent_accepted = True
        return True

    def apply_theme(self) -> None:
        self.palette = THEMES[self.config.theme]
        QApplication.instance().setStyleSheet(_qss(self.palette))
        self._refresh_tray_state()
        for card in (self.cpu_card, self.ram_card, self.disk_card, self.swap_card):
            card.set_palette(self.palette)
        self.graph.set_palette(self.palette)
        if hasattr(self, "save_settings_button"):
            self.save_settings_button.setIcon(_feather_icon("save", self.palette["bg"]))
        if hasattr(self, "check_updates_button"):
            version = self._pending_update.latest_version if self._pending_update else ""
            self._set_update_action_state(self._update_action_mode, version)
        if hasattr(self, "update_banner_button"):
            self.update_banner_button.setIcon(_feather_icon("arrow-down", self.palette["accent"]))
        if hasattr(self, "update_later_button"):
            self.update_later_button.setIcon(_feather_icon("x", self.palette["accent"]))

    def _change_theme_from_combo(self) -> None:
        self.config.theme = self.theme_combo.currentData()
        self.apply_theme()

    def _on_monitor_snapshot(self, snapshot: MonitorSnapshot) -> None:
        self.bridge.snapshot_received.emit(snapshot)

    def _render_snapshot(self, snapshot: MonitorSnapshot) -> None:
        visible = self.isVisible() and not self.isMinimized()
        monitoring_visible = visible and self.tabs.currentWidget() == self.monitoring_tab
        processes_visible = visible and self.tabs.currentWidget() == self.processes_tab
        if snapshot.processes:
            self.ram_cleaner.observe_processes(snapshot.processes)
        if monitoring_visible:
            self.graph.set_live_updates_enabled(self._graph_updates_allowed())
            if not self.config.graph_collapsed:
                self.graph.queue_point(snapshot.cpu_percent, snapshot.memory.percent)
            self.cpu_card.set_metric(f"{snapshot.cpu_percent:.1f}%", f"{len(snapshot.per_core_cpu_percent)} ядер", snapshot.cpu_percent)
            self.ram_card.set_metric(
                f"{snapshot.memory.percent:.1f}%",
                f"{format_bytes(snapshot.memory.used)} / {format_bytes(snapshot.memory.total)}",
                snapshot.memory.percent,
            )
            self.swap_card.set_metric(
                f"{snapshot.swap.percent:.1f}%",
                f"Page File: {format_bytes(snapshot.swap.used)} / {format_bytes(snapshot.swap.total)} ({snapshot.swap.percent:.0f}%)",
                snapshot.swap.percent,
            )
            max_disk = max((disk.percent for disk in snapshot.disks), default=0.0)
            self.disk_card.set_metric(
                f"{max_disk:.1f}%",
                f"R {format_bytes(snapshot.disk_io.read_bytes_per_second)}/s · W {format_bytes(snapshot.disk_io.write_bytes_per_second)}/s",
                max_disk,
            )
            if not self.config.core_table_collapsed:
                self._render_cores(snapshot.per_core_cpu_percent)
            self._render_disks(snapshot)
        if processes_visible and snapshot.processes:
            self._render_processes(snapshot.processes)
        self._handle_thresholds(snapshot)
        if not self.config.observation_only_mode:
            wake_actions = self.sleep_manager.resume_foreground_if_sleeping()
            if wake_actions:
                for action in wake_actions:
                    self._log_sleep_action(action)
                self.refresh_activity()
            self._maybe_poll_sleep_manager()
            self._maybe_cpu_throttle(snapshot)
            self._maybe_auto_ram_clean(snapshot)
            self._maybe_smart_close(snapshot)
            self._maybe_scheduled_auto_cleanup(snapshot)
            self._maybe_periodic_optimization(snapshot)

    def start_full_optimization(self) -> None:
        """Start one-click optimization in a Qt worker thread."""

        self._start_optimization(eco_mode=False, quiet=False)

    def _start_optimization(self, *, eco_mode: bool, quiet: bool) -> bool:
        """Start the shared optimization cycle, optionally in silent eco mode."""

        if self._optimization_thread and self._optimization_thread.isRunning():
            return False
        if not quiet and not self.config.optimize_consent_accepted and not self._show_optimization_consent():
            return False

        self._optimization_cancel_event = Event()
        self._optimization_quiet = quiet
        if not quiet:
            self.optimize_button.setEnabled(False)
            self.optimize_button.setText("Идёт оптимизация...")
            self.optimize_progress.setVisible(True)
            self.optimize_progress.setValue(0)
            self.optimize_cancel_button.setEnabled(True)
            self.optimize_cancel_button.setVisible(True)
            self.optimize_status.setText("● Запуск полного цикла...")

        thread = QThread(self)
        worker = OptimizationWorker(
            config=self.config,
            whitelist=self.whitelist,
            optimizer=self.optimizer,
            history=self.history,
            sleep_manager=self.sleep_manager,
            ram_cleaner=self.ram_cleaner,
            cpu_optimizer=self.cpu_optimizer,
            cancel_event=self._optimization_cancel_event,
            eco_mode=eco_mode,
            quiet=quiet,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress_received.connect(self._on_optimization_progress)
        worker.result_received.connect(self._on_optimization_result)
        worker.result_received.connect(thread.quit)
        worker.result_received.connect(worker.deleteLater)
        thread.finished.connect(self._on_optimization_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._optimization_thread = thread
        self._optimization_worker_obj = worker
        thread.start()
        return True

    def cancel_full_optimization(self) -> None:
        """Request cancellation of the current one-click cycle."""

        if self._optimization_cancel_event:
            self._optimization_cancel_event.set()
            if not self._optimization_quiet:
                self.optimize_status.setText("● Прерываю после текущего безопасного шага...")
                self.optimize_cancel_button.setEnabled(False)

    def _on_optimization_progress(self, percent: int, step: str, detail: str) -> None:
        if self._optimization_quiet:
            return
        self.optimize_progress.setValue(percent)
        self.optimize_status.setText(f"● {step}: {detail}")

    def _on_optimization_result(self, result: OptimizationResult) -> None:
        self._last_optimization_result = result
        self.refresh_activity()
        if not self._optimization_quiet or (
            self.isVisible() and not self.isMinimized() and self.tabs.currentWidget() == self.processes_tab
        ):
            self.refresh_process_table()
        if self._optimization_quiet:
            cleanup_bytes = result.cleanup_result.freed_bytes if result.cleanup_result else 0
            detail = (
                f"RAM {result.ram_before_percent:.1f}%→{result.ram_after_percent:.1f}%, "
                f"CPU {result.cpu_before:.1f}%→{result.cpu_after:.1f}%, disk {format_bytes(cleanup_bytes)}"
            )
            self.history.add_event("optimization", "Автооптимизация завершена", detail, "success" if not result.errors else "warning")
            if self.config.periodic_optimization_notify or self.config.automation_mode == "autopilot":
                self._notify_cleanup_summary(result.ram_freed_bytes, cleanup_bytes, key="auto_optimization_done")
            return
        self.optimize_button.setEnabled(True)
        self.optimize_button.setText("Оптимизировать")
        self.optimize_cancel_button.setEnabled(True)
        self.optimize_cancel_button.setVisible(False)
        self.optimize_progress.setValue(100 if not result.cancelled else self.optimize_progress.value())
        self.optimize_status.setText("● Оптимизация прервана" if result.cancelled else "● Оптимизация завершена")
        cleanup_bytes = result.cleanup_result.freed_bytes if result.cleanup_result else 0
        self.notifier.notify(
            "PC Optimizer Lite",
            (
                f"Оптимизация завершена: освобождено {format_bytes(result.ram_freed_bytes)} RAM, "
                f"закрыто {len(result.closed_entries)} процессов, очищено {format_bytes(cleanup_bytes)}"
            ),
            key="optimization_done",
        )
        self._show_optimization_report(result)

    def _on_optimization_thread_finished(self) -> None:
        self._optimization_thread = None
        self._optimization_worker_obj = None
        self._optimization_cancel_event = None
        self._optimization_quiet = False

    def _show_optimization_consent(self) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("Оптимизация одним нажатием")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("Кнопка выполнит полный цикл без внутренних подтверждений.")
        box.setInformativeText(
            "PC Optimizer Lite просканирует процессы, безопасно закроет только консервативно выбранные фоновые кандидаты, "
            "усыпит неактивные приложения, снизит влияние тяжёлых процессов и очистит temp/cache.\n\n"
            "Whitelist, активное окно, процессы с сетью/медиа-признаками и всё сомнительное не трогаются."
        )
        checkbox = QCheckBox("Понимаю и согласен")
        box.setCheckBox(checkbox)
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Ok or not checkbox.isChecked():
            QMessageBox.information(self, "Оптимизация", "Для первого запуска нужно отметить согласие.")
            return False
        self.config.optimize_consent_accepted = True
        save_config(self.config)
        return True

    def _show_optimization_report(self, result: OptimizationResult) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Отчёт оптимизации")
        box.setIcon(QMessageBox.Icon.Information if not result.errors else QMessageBox.Icon.Warning)
        box.setText("Было / Стало")
        box.setInformativeText(result.summary_text())
        box.setDetailedText(result.details_text())
        undo_button = box.addButton("Отменить все действия", QMessageBox.ButtonRole.ActionRole)
        close_button = box.addButton("Закрыть", QMessageBox.ButtonRole.AcceptRole)
        undo_button.setEnabled(
            bool(result.closed_entries)
            or any(action.success for action in result.slept_actions)
            or bool(result.priority_changes)
        )
        box.exec()
        if box.clickedButton() == undo_button:
            self.undo_last_optimization()
        elif box.clickedButton() == close_button:
            return

    def undo_last_optimization(self) -> None:
        if not self._last_optimization_result:
            return
        messages = undo_optimization(self._last_optimization_result, self.history, self.sleep_manager)
        QMessageBox.information(
            self,
            "Undo",
            "\n".join(messages[:16]) if messages else "Нечего отменять.",
        )
        self.refresh_activity()

    def _render_cores(self, values: list[float]) -> None:
        self.core_table.setRowCount(len(values))
        for row, value in enumerate(values):
            self.core_table.setItem(row, 0, QTableWidgetItem(f"Core {row + 1}"))
            progress = QProgressBar()
            progress.setRange(0, 100)
            progress.setValue(round(value))
            progress.setTextVisible(False)
            progress.setToolTip(f"{value:.0f}%")
            self.core_table.setCellWidget(row, 1, progress)
            percent_item = QTableWidgetItem(f"{value:.0f}%")
            percent_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            percent_item.setForeground(_status_color(value, self.palette))
            self.core_table.setItem(row, 2, percent_item)
            self.core_table.setRowHeight(row, 30)
        _fit_table_height(self.core_table, min_rows=3, max_height=190)

    def _render_disks(self, snapshot: MonitorSnapshot) -> None:
        self.disk_table.setRowCount(len(snapshot.disks))
        for row, disk in enumerate(snapshot.disks):
            values = (
                disk.device,
                disk.mountpoint,
                disk.fstype,
                format_bytes(disk.used),
                format_bytes(disk.free),
                format_bytes(disk.total),
                f"{disk.percent:.1f}",
            )
            for column, value in enumerate(values):
                self.disk_table.setItem(row, column, QTableWidgetItem(value))
            self.disk_table.setRowHeight(row, 30)
        _fit_table_height(self.disk_table, min_rows=3, max_height=190)

    def refresh_process_table(self) -> None:
        if self._process_refresh_thread and self._process_refresh_thread.isRunning():
            return
        thread = QThread(self)
        worker = ProcessRefreshWorker(self.monitor, max_processes=220)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.result_received.connect(self._on_process_refresh_result)
        worker.error_received.connect(self._on_process_refresh_error)
        worker.result_received.connect(thread.quit)
        worker.error_received.connect(thread.quit)
        worker.result_received.connect(worker.deleteLater)
        worker.error_received.connect(worker.deleteLater)
        thread.finished.connect(self._on_process_refresh_finished)
        thread.finished.connect(thread.deleteLater)
        self._process_refresh_thread = thread
        self._process_refresh_worker_obj = worker
        thread.start()

    def _on_process_refresh_result(self, processes: list[ProcessInfo]) -> None:
        self._render_processes(processes)

    def _on_process_refresh_error(self, message: str) -> None:
        LOGGER.warning("Process refresh failed: %s", message)

    def _on_process_refresh_finished(self) -> None:
        self._process_refresh_thread = None
        self._process_refresh_worker_obj = None

    def _render_processes(self, processes: list[ProcessInfo]) -> None:
        selected_pid: int | None = None
        selected_item = self.process_table.item(self.process_table.currentRow(), 0)
        if selected_item is not None:
            try:
                selected_pid = int(selected_item.data(Qt.ItemDataRole.UserRole) or selected_item.text())
            except (TypeError, ValueError):
                selected_pid = None

        self._process_rows = {process.pid: process for process in processes}
        self.process_table.setUpdatesEnabled(False)
        self.process_table.setRowCount(len(processes))
        for row, process in enumerate(processes):
            protected = self.whitelist.is_whitelisted(process.name, process.exe)
            values = (
                str(process.pid),
                process.name,
                f"{process.cpu_percent:.1f}",
                f"{process.memory_percent:.1f}",
                format_bytes(process.memory_rss),
                process.priority,
                process.exe,
            )
            for column, value in enumerate(values):
                item = self.process_table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.process_table.setItem(row, column, item)
                if item.text() != value:
                    item.setText(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, process.pid)
                item.setForeground(QColor(self.palette["muted"] if protected else self.palette["text"]))
            if selected_pid == process.pid:
                self.process_table.selectRow(row)
        self.process_table.setUpdatesEnabled(True)

    def _selected_process(self) -> ProcessInfo | None:
        row = self.process_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Процессы", "Выберите процесс в таблице.")
            return None
        item = self.process_table.item(row, 0)
        if item is None:
            return None
        pid = int(item.data(Qt.ItemDataRole.UserRole) or item.text())
        return self._process_rows.get(pid)

    def lower_selected_priority(self) -> None:
        process = self._selected_process()
        if not process:
            return
        action = self.optimizer.lower_priority_for_process(process.pid)
        if action.success:
            self.history.add_event("priority", f"Снижено влияние: {action.name}", action.message, "info")
        QMessageBox.information(self, "Снижение влияния процесса", action.message)
        self.refresh_process_table()
        self.refresh_activity()

    def terminate_selected_process(self) -> None:
        process = self._selected_process()
        if not process:
            return
        if self.whitelist.is_whitelisted(process.name, process.exe):
            QMessageBox.warning(self, "Защищённый процесс", "Этот процесс находится в исключениях.")
            return
        answer = QMessageBox.question(
            self,
            "Подтверждение закрытия",
            (
                f"Закрыть процесс {process.name} (PID {process.pid})?\n\n"
                "Несохранённые данные в этом приложении восстановить нельзя. "
                "PC Optimizer Lite отправит только terminate-сигнал и сохранит запись в историю."
            ),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        candidate = CloseCandidate(
            pid=process.pid,
            name=process.name,
            exe=process.exe,
            reason="manual",
            detail="Manual close from process table",
            cpu_percent=process.cpu_percent,
            memory_percent=process.memory_percent,
        )
        ok, message = self.smart_manager.close_candidate(candidate, "manual")
        QMessageBox.information(self, "Процессы", message)
        if ok:
            self.refresh_activity()
        self.refresh_process_table()

    def open_selected_process_location(self) -> None:
        process = self._selected_process()
        if not process:
            return
        if not open_file_location(process.exe):
            QMessageBox.warning(self, "Папка процесса", "Не удалось открыть расположение файла.")

    def _handle_thresholds(self, snapshot: MonitorSnapshot) -> None:
        now = time.monotonic()
        if snapshot.cpu_percent >= self.config.cpu_threshold_percent:
            if self._high_cpu_since is None:
                self._high_cpu_since = now
            if now - self._high_cpu_since >= self.config.cpu_sustain_seconds:
                self.notifier.notify(
                    "Высокая нагрузка CPU",
                    f"CPU {snapshot.cpu_percent:.0f}% держится дольше {self.config.cpu_sustain_seconds:.1f} сек.",
                    key="high_cpu",
                )
                if not self._maybe_threshold_cpu_optimization(snapshot):
                    self._maybe_auto_lower_priority(snapshot)
        else:
            self._high_cpu_since = None

        if snapshot.memory.percent >= self.config.ram_threshold_percent:
            self.notifier.notify(
                "Высокое использование RAM",
                f"RAM занята на {snapshot.memory.percent:.0f}%.",
                key="high_ram",
            )

    def _auto_cleanup_cooldown_seconds(self) -> float:
        return max(180.0, self.config.auto_cleanup_cooldown_minutes * 60.0)

    def _maybe_threshold_cpu_optimization(self, snapshot: MonitorSnapshot) -> bool:
        if self.config.observation_only_mode:
            return False
        if not (
            self.config.cpu_optimizer_enabled
            or self.config.cpu_throttle_enabled
            or self.config.auto_lower_priority_enabled
        ):
            return False
        if self._optimization_thread and self._optimization_thread.isRunning():
            return False
        now = time.monotonic()
        if now - self._last_threshold_cpu_optimization_at < self._auto_cleanup_cooldown_seconds():
            return False
        if self._user_recently_active(snapshot):
            return False
        started = self._start_optimization(eco_mode=True, quiet=True)
        if not started:
            return False
        self._last_threshold_cpu_optimization_at = now
        self.history.add_event(
            "optimization",
            "CPU threshold optimization started",
            f"CPU {snapshot.cpu_percent:.0f}% held above {self.config.cpu_threshold_percent:.0f}%",
            "info",
        )
        return True

    def _maybe_auto_lower_priority(self, snapshot: MonitorSnapshot) -> None:
        if (
            self.config.observation_only_mode
            or not self.config.auto_lower_priority_enabled
            or self.config.cpu_throttle_enabled
        ):
            return
        now = time.monotonic()
        if now - self._last_auto_priority_at < self.config.notification_cooldown_seconds:
            return
        processes = snapshot.processes
        if not processes:
            return
        actions = self.optimizer.lower_priority_for_heavy_processes(
            processes,
            limit=self.config.max_auto_priority_changes,
        )
        changed = [action for action in actions if action.success]
        if changed:
            self._last_auto_priority_at = now
            self.history.add_event("priority", "Auto priority relief", f"Changed {len(changed)} process(es)", "info")
            self.refresh_activity()

    def _maybe_cpu_throttle(self, snapshot: MonitorSnapshot) -> None:
        actions = self.cpu_throttler.observe(snapshot, self.config)
        if actions:
            for action in actions:
                if action.action in {"throttle", "limit"} and action.success:
                    self.graph.mark_intervention(action.detail)
            self.refresh_activity()

    def _maybe_smart_close(self, snapshot: MonitorSnapshot) -> None:
        if self.config.observation_only_mode or self.config.auto_close_mode == "off" or not snapshot.processes:
            return
        now = time.monotonic()
        if now - self._last_auto_close_at < self.config.notification_cooldown_seconds:
            return
        candidates = self.smart_manager.find_candidates(
            snapshot.processes,
            min_background_minutes=self.config.auto_close_min_background_minutes,
            cpu_threshold=self.config.auto_close_cpu_threshold_percent,
            memory_threshold=self.config.auto_close_memory_threshold_percent,
            duplicate_count=self.config.auto_close_duplicate_count,
        )
        for candidate in candidates[:2]:
            self.smart_manager.mark_prompted(candidate.pid)
            if self.config.auto_close_mode == "ask":
                answer = QMessageBox.question(
                    self,
                    "Умное закрытие",
                    (
                        f"Закрыть {candidate.name} (PID {candidate.pid})?\n\n"
                        f"{candidate.detail}\n\n"
                        "Whitelist уже проверен. Несохранённые данные восстановить нельзя."
                    ),
                )
                if answer != QMessageBox.StandardButton.Yes:
                    continue
            ok, message = self.smart_manager.close_candidate(candidate, self.config.auto_close_mode)
            self._last_auto_close_at = now
            self.history.add_event(
                "smart_close",
                f"Smart close: {candidate.name}",
                message,
                "warning" if ok else "error",
            )
        if candidates:
            self.refresh_activity()

    def _maybe_periodic_optimization(self, snapshot: MonitorSnapshot) -> None:
        if self.config.observation_only_mode or not self.config.periodic_optimization_enabled:
            return
        if self._optimization_thread and self._optimization_thread.isRunning():
            return
        now = time.monotonic()
        interval_seconds = max(15.0, self.config.periodic_optimization_interval_minutes) * 60.0
        if now - self._last_periodic_optimization_at < interval_seconds:
            return
        self._last_periodic_optimization_at = now
        if self._user_recently_active(snapshot):
            LOGGER.info("Periodic optimization deferred: recent user activity or active CPU load")
            return
        started = self._start_optimization(
            eco_mode=self.config.periodic_optimization_eco_mode,
            quiet=True,
        )
        if started:
            self.history.add_event(
                "optimization",
                "Periodic optimization started",
                "Silent eco mode" if self.config.periodic_optimization_eco_mode else "Silent full mode",
                "info",
            )

    def _user_recently_active(self, snapshot: MonitorSnapshot) -> bool:
        idle_seconds = _seconds_since_last_input()
        if idle_seconds < 30.0:
            return True
        return idle_seconds < 120.0 and snapshot.cpu_percent >= max(70.0, self.config.cpu_threshold_percent - 10.0)

    def _maybe_poll_sleep_manager(self) -> None:
        now = time.monotonic()
        if now - self._last_sleep_poll_at < self.config.sleep_check_seconds:
            return
        self._last_sleep_poll_at = now
        self._poll_sleep_manager()

    def _poll_sleep_manager(self) -> None:
        enabled = self.config.sleep_enabled and not self.config.observation_only_mode
        actions = self.sleep_manager.poll(
            enabled=enabled,
            idle_minutes=self.config.sleep_after_minutes,
            max_actions=self.config.max_sleep_actions_per_cycle,
        )
        if actions:
            for action in actions:
                self._log_sleep_action(action)
            self.refresh_activity()

    def _log_sleep_action(self, action: SleepAction) -> None:
        if action.success:
            self.notifier.notify("PC Optimizer Lite", f"{action.name}: {action.action}", key=f"sleep_{action.pid}")

    def confirm_temp_cleanup(self) -> None:
        self._start_cleanup_scan(automatic=False, notify=True)

    def _start_cleanup_scan(self, *, automatic: bool, notify: bool) -> None:
        if self._cleanup_thread and self._cleanup_thread.isRunning():
            return
        self._start_cleanup_worker(
            mode="scan",
            plan=None,
            context={"automatic": automatic, "notify": notify},
        )

    def _start_cleanup_worker(self, *, mode: str, plan: CleanupPlan | None, context: dict[str, object]) -> None:
        thread = QThread(self)
        worker = CleanupWorker(self.optimizer, mode=mode, plan=plan)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.result_received.connect(self._on_cleanup_worker_result)
        worker.error_received.connect(self._on_cleanup_worker_error)
        worker.result_received.connect(thread.quit)
        worker.error_received.connect(thread.quit)
        worker.result_received.connect(worker.deleteLater)
        worker.error_received.connect(worker.deleteLater)
        thread.finished.connect(self._on_cleanup_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._cleanup_context = dict(context)
        self._cleanup_thread = thread
        self._cleanup_worker_obj = worker
        self.statusBar().showMessage("Очистка temp/cache выполняется в фоне..." if mode == "cleanup" else "Сканирую temp/cache в фоне...", 2500)
        thread.start()

    def _on_cleanup_worker_result(self, mode: str, payload: object) -> None:
        context = dict(self._cleanup_context)
        if mode == "scan":
            self._deferred_cleanup_plan = (payload, context)  # type: ignore[assignment]
            return
        self._handle_cleanup_result(payload, context)

    def _on_cleanup_worker_error(self, message: str) -> None:
        context = dict(self._cleanup_context)
        LOGGER.warning("Cleanup failed: %s", message)
        self.history.add_event("cleanup", "Ошибка очистки", message, "error")
        if not bool(context.get("automatic")):
            QMessageBox.critical(self, "Очистка", f"Не удалось выполнить очистку:\n{message}")
        self.refresh_activity()

    def _on_cleanup_thread_finished(self) -> None:
        self._cleanup_thread = None
        self._cleanup_worker_obj = None
        deferred = self._deferred_cleanup_plan
        self._deferred_cleanup_plan = None
        if deferred is not None:
            plan, context = deferred
            self._handle_cleanup_plan(plan, context)

    def _handle_cleanup_plan(self, plan: CleanupPlan, context: dict[str, object]) -> None:
        automatic = bool(context.get("automatic"))
        notify = bool(context.get("notify"))
        if plan.file_count == 0 and plan.total_bytes == 0:
            detail = "В известных temp/cache папках нечего очищать."
            self.history.add_event("cleanup", "Очистка temp/cache", detail, "info")
            if not automatic:
                QMessageBox.information(self, "Очистка", detail)
            elif notify:
                self._notify_cleanup_summary(0, 0, key="scheduled_cleanup_empty")
            self.refresh_activity()
            return
        if not automatic:
            category_text = _format_categories(plan)
            answer = QMessageBox.question(
                self,
                "Подтверждение очистки",
                (
                    f"Найдено: {plan.file_count} файлов, {format_bytes(plan.total_bytes)}.\n\n"
                    f"{category_text}\n\n"
                    "Очистить? Будут удалены только заранее известные temp/cache директории."
                ),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._start_cleanup_worker(mode="cleanup", plan=plan, context=context)

    def _notify_cleanup_summary(self, ram_bytes: int, disk_bytes: int, *, key: str) -> bool:
        message = f"Очищено: освобождено {format_bytes(ram_bytes)} RAM / {format_bytes(disk_bytes)} диска"
        return self.notifier.notify("PC Optimizer Lite", message, key=key)

    def _handle_cleanup_result(self, result, context: dict[str, object]) -> None:
        automatic = bool(context.get("automatic"))
        notify = bool(context.get("notify"))
        severity = "warning" if result.errors else "success"
        message = f"Очищено: {result.deleted_files} файлов, освобождено {format_bytes(result.freed_bytes)}"
        details = f"{message}; ошибок/пропусков: {len(result.errors)}"
        self.history.add_event(
            "cleanup",
            "Автоочистка temp/cache" if automatic else "Очистка temp/cache",
            details,
            severity,
        )
        if notify or not automatic:
            if automatic:
                self._notify_cleanup_summary(0, result.freed_bytes, key="scheduled_cleanup")
            else:
                self.notifier.notify("PC Optimizer Lite", message, key="manual_cleanup")
        if not automatic:
            deleted_summary = _format_result_categories(result.categories)
            QMessageBox.information(
                self,
                "Очистка завершена",
                (
                    f"{message}.\n\n"
                    f"{deleted_summary}\n\n"
                    f"Ошибок/пропусков: {len(result.errors)}"
                ),
            )
        self.refresh_activity()

    def clean_ram(
        self,
        mode: RamCleanMode = RamCleanMode.LIGHT,
        automatic: bool = False,
        *,
        notify: bool = True,
        event_title: str | None = None,
        purge_standby: bool = False,
    ) -> None:
        """Run RAM cleanup and show/report the result."""

        if self._ram_clean_thread and self._ram_clean_thread.isRunning():
            return
        if not automatic and not self._show_ram_clean_warning_once():
            return
        thread = QThread(self)
        worker = RamCleanWorker(self.ram_cleaner, mode=mode, purge_standby=purge_standby)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.result_received.connect(self._on_ram_clean_result)
        worker.error_received.connect(self._on_ram_clean_error)
        worker.result_received.connect(thread.quit)
        worker.error_received.connect(thread.quit)
        worker.result_received.connect(worker.deleteLater)
        worker.error_received.connect(worker.deleteLater)
        thread.finished.connect(self._on_ram_clean_finished)
        thread.finished.connect(thread.deleteLater)
        self._ram_clean_context = {
            "automatic": automatic,
            "notify": notify,
            "event_title": event_title or "",
            "mode": mode.value,
        }
        self._ram_clean_thread = thread
        self._ram_clean_worker_obj = worker
        self.statusBar().showMessage("Очистка RAM выполняется в фоне...", 2500)
        thread.start()

    def _on_ram_clean_result(self, result: RamCleanResult) -> None:
        context = dict(self._ram_clean_context)
        automatic = bool(context.get("automatic"))
        notify = bool(context.get("notify"))
        event_title = str(context.get("event_title") or "")
        severity = "warning" if result.errors else "success"
        detail = (
            f"Было {result.ram_percent_before:.1f}% → стало {result.ram_percent_after:.1f}%; "
            f"освобождено {format_bytes(result.freed_bytes)}; "
            f"процессов обработано: {len([item for item in result.process_results if item.success])}; "
            f"standby list: {'очищен' if result.standby_purged else 'не тронут'}"
        )
        self.history.add_event(
            "ram_clean",
            event_title or ("Автоочистка RAM" if automatic else f"Очистка RAM: {result.mode.value}"),
            detail,
            severity,
        )
        if not automatic:
            QMessageBox.information(
                self,
                "Очистка RAM",
                _format_ram_clean_report(result),
            )
        if notify or not automatic:
            if automatic:
                self._notify_cleanup_summary(result.freed_bytes, 0, key="auto_ram_clean")
            else:
                self.notifier.notify(
                    "PC Optimizer Lite",
                    f"Освобождено {format_bytes(result.freed_bytes)} RAM (было {result.ram_percent_before:.1f}% → стало {result.ram_percent_after:.1f}%)",
                    key="manual_ram_clean",
                )
        self.refresh_activity()

    def _on_ram_clean_error(self, message: str) -> None:
        context = dict(self._ram_clean_context)
        LOGGER.warning("RAM cleanup failed: %s", message)
        self.history.add_event("ram_clean", "Ошибка очистки RAM", message, "error")
        if not bool(context.get("automatic")):
            QMessageBox.critical(self, "Очистка RAM", f"Не удалось выполнить очистку RAM:\n{message}")
        self.refresh_activity()

    def _on_ram_clean_finished(self) -> None:
        self._ram_clean_thread = None
        self._ram_clean_worker_obj = None

    def _show_ram_clean_warning_once(self) -> bool:
        if self.config.ram_clean_warning_seen:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("Очистка RAM")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText("Очистка RAM не закрывает приложения.")
        box.setInformativeText(
            "PC Optimizer Lite попросит Windows освободить неиспользуемую память безопасных неактивных процессов вне whitelist. "
            "Windows выгрузит неиспользуемые страницы из физической RAM; при возврате к приложению возможна краткая подгрузка."
        )
        checkbox = QCheckBox("Понимаю, больше не показывать")
        box.setCheckBox(checkbox)
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Ok:
            return False
        if checkbox.isChecked():
            self.config.ram_clean_warning_seen = True
            save_config(self.config)
        return True

    def _maybe_auto_ram_clean(self, snapshot: MonitorSnapshot) -> None:
        if self.config.observation_only_mode or not self.config.ram_auto_clean_enabled:
            return
        if snapshot.memory.percent < self.config.ram_auto_clean_threshold_percent:
            return
        now = time.monotonic()
        if now - self._last_auto_ram_clean_at < self._auto_cleanup_cooldown_seconds():
            return
        if self._user_recently_active(snapshot) and snapshot.memory.percent < self.config.ram_auto_clean_threshold_percent + 5.0:
            return
        self._last_auto_ram_clean_at = now
        self.clean_ram(RamCleanMode.LIGHT, automatic=True, event_title="RAM threshold auto-clean")

    def _maybe_scheduled_auto_cleanup(self, snapshot: MonitorSnapshot) -> None:
        if not self.config.scheduled_cleanup_enabled:
            return
        now = time.monotonic()
        interval_seconds = max(10.0, self.config.scheduled_cleanup_interval_minutes) * 60.0
        if now - self._last_scheduled_cleanup_at < interval_seconds:
            return
        self._last_scheduled_cleanup_at = now
        if _seconds_since_last_input() < 30.0:
            LOGGER.info("Scheduled cleanup deferred: recent user input")
            return
        self._start_cleanup_scan(
            automatic=True,
            notify=self.config.scheduled_cleanup_notify,
        )

    def refresh_activity(self) -> None:
        events = self.history.get_events()
        self.activity_table.setRowCount(len(events[:80]))
        for row, event in enumerate(events[:80]):
            values = (_format_time(event.timestamp), event.title, event.detail)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if event.severity == "error":
                    item.setForeground(QColor(self.palette["bad"]))
                elif event.severity == "warning":
                    item.setForeground(QColor(self.palette["warn"]))
                elif event.severity == "success":
                    item.setForeground(QColor(self.palette["good"]))
                self.activity_table.setItem(row, column, item)

        sleeping = self.sleep_manager.sleeping
        self.sleep_table.setRowCount(len(sleeping))
        for row, entry in enumerate(sleeping):
            values = (
                str(entry.pid),
                entry.name,
                _format_time(entry.slept_at),
                entry.reason,
                "suspended" if entry.suspended else "idle priority",
            )
            for column, value in enumerate(values):
                self.sleep_table.setItem(row, column, QTableWidgetItem(value))
            button = _button("Разбудить", "play", lambda pid=entry.pid: self.wake_process(pid), self.palette)
            self.sleep_table.setCellWidget(row, 5, button)

        closed = self.history.get_closed_processes()
        self.closed_table.setRowCount(len(closed))
        for row, entry in enumerate(closed):
            values = (
                _format_time(entry.timestamp),
                entry.name,
                entry.reason,
                entry.mode,
                entry.exe,
            )
            for column, value in enumerate(values):
                self.closed_table.setItem(row, column, QTableWidgetItem(str(value)))
            button_text = "↩ Открыть снова" if not entry.restored_at else "Открыто"
            button = _button(button_text, "rotate", lambda entry_id=entry.id: self.restore_closed_process(entry_id), self.palette)
            button.setEnabled(bool(entry.exe) and not bool(entry.restored_at))
            self.closed_table.setCellWidget(row, 5, button)

    def wake_process(self, pid: int) -> None:
        action = self.sleep_manager.resume_process(pid, "manual")
        QMessageBox.information(self, "Сон", action.message)
        self.refresh_activity()

    def restore_closed_process(self, entry_id: str) -> None:
        ok, message = self.history.restore_process(entry_id)
        QMessageBox.information(self, "История", message)
        if ok:
            self.refresh_activity()

    def refresh_whitelist_lists(self) -> None:
        self.names_list.clear()
        self.names_list.addItems(sorted(self.whitelist.user_names))
        self.paths_list.clear()
        self.paths_list.addItems(sorted(self.whitelist.user_paths))

    def add_whitelist_name(self) -> None:
        value = self.whitelist_entry.text().strip()
        if value and self.whitelist.add_name(value):
            save_config(self.config)
            self.history.add_event("whitelist", "Whitelist updated", f"Added process name: {value}", "info")
        self.whitelist_entry.clear()
        self.refresh_whitelist_lists()
        self.refresh_activity()

    def add_whitelist_path(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Выберите exe", "", "Executable files (*.exe);;All files (*.*)")
        if path and self.whitelist.add_path(path):
            save_config(self.config)
            self.history.add_event("whitelist", "Whitelist updated", f"Added path: {path}", "info")
        self.refresh_whitelist_lists()
        self.refresh_activity()

    def remove_whitelist_selected(self) -> None:
        removed = False
        for item in self.names_list.selectedItems():
            removed = self.whitelist.remove_name(item.text()) or removed
        for item in self.paths_list.selectedItems():
            removed = self.whitelist.remove_path(item.text()) or removed
        if removed:
            save_config(self.config)
            self.history.add_event("whitelist", "Whitelist updated", "Removed selected item(s)", "info")
        self.refresh_whitelist_lists()
        self.refresh_activity()

    def save_settings(self) -> None:
        try:
            self.config.theme = self.theme_combo.currentData()
            self.config.automation_mode = self.automation_mode_combo.currentData()
            if self.config.automation_mode == "autopilot" and not self._ensure_autopilot_consent():
                self.automation_mode_combo.setCurrentIndex(0)
                self.config.automation_mode = "observation"
                self._preview_automation_mode()
                return
            self.config.observation_only_mode = self.observation_only_check.isChecked()
            self.config.lite_mode_enabled = self.lite_mode_check.isChecked()
            self.config.monitor_interval_seconds = float(self.interval_edit.text())
            self.config.process_refresh_seconds = float(self.process_interval_edit.text())
            self.config.cpu_threshold_percent = float(self.cpu_threshold_edit.text())
            self.config.cpu_sustain_seconds = float(self.cpu_sustain_edit.text())
            self.config.ram_threshold_percent = float(self.ram_threshold_edit.text())
            self.config.notification_cooldown_seconds = float(self.cooldown_edit.text())
            self.config.max_auto_priority_changes = int(float(self.max_priority_edit.text()))
            self.config.auto_lower_priority_enabled = self.auto_priority_check.isChecked()
            self.config.auto_close_mode = self.auto_close_combo.currentData()
            self.config.auto_close_min_background_minutes = float(self.close_background_edit.text())
            self.config.auto_close_cpu_threshold_percent = float(self.close_cpu_edit.text())
            self.config.auto_close_memory_threshold_percent = float(self.close_ram_edit.text())
            self.config.auto_close_duplicate_count = int(float(self.close_duplicates_edit.text()))
            self.config.sleep_enabled = self.sleep_enabled_check.isChecked()
            self.config.sleep_after_minutes = float(self.sleep_after_edit.text())
            self.config.sleep_check_seconds = float(self.sleep_check_edit.text())
            self.config.ram_auto_clean_enabled = self.ram_auto_clean_check.isChecked()
            self.config.ram_auto_clean_threshold_percent = float(self.ram_auto_threshold_edit.text())
            self.config.cpu_optimizer_enabled = self.cpu_optimizer_check.isChecked()
            self.config.cpu_optimizer_priority_mode = self.cpu_optimizer_priority_combo.currentData()
            self.config.cpu_optimizer_min_process_cpu_percent = float(self.cpu_optimizer_min_cpu_edit.text())
            self.config.cpu_optimizer_max_processes = int(float(self.cpu_optimizer_max_edit.text()))
            self.config.cpu_optimizer_affinity_ratio = float(self.cpu_optimizer_affinity_ratio_edit.text())
            self.config.cpu_optimizer_affinity_min_cores = int(float(self.cpu_optimizer_min_cores_edit.text()))
            self.config.cpu_optimizer_restore_after_seconds = float(self.cpu_optimizer_restore_edit.text())
            self.config.cpu_throttle_enabled = self.cpu_throttle_check.isChecked()
            self.config.cpu_throttle_affinity_enabled = self.cpu_affinity_check.isChecked()
            self.config.cpu_limiter_enabled = self.cpu_limiter_check.isChecked()
            self.config.scheduled_cleanup_enabled = self.scheduled_cleanup_check.isChecked()
            self.config.scheduled_cleanup_interval_minutes = float(self.scheduled_cleanup_interval_edit.text())
            self.config.scheduled_cleanup_notify = self.scheduled_cleanup_notify_check.isChecked()
            self.config.auto_cleanup_cooldown_minutes = float(self.auto_cleanup_cooldown_edit.text())
            self.config.cleanup_temp_enabled = self.cleanup_temp_check.isChecked()
            self.config.cleanup_windows_temp_enabled = self.cleanup_windows_temp_check.isChecked()
            self.config.cleanup_browser_cache_enabled = self.cleanup_browser_cache_check.isChecked()
            self.config.cleanup_prefetch_enabled = self.cleanup_prefetch_check.isChecked()
            self.config.cleanup_logs_enabled = self.cleanup_logs_check.isChecked()
            self.config.cleanup_logs_older_than_days = int(float(self.cleanup_logs_days_edit.text()))
            self.config.cleanup_recycle_bin_enabled = self.cleanup_recycle_bin_check.isChecked()
            self.config.periodic_optimization_enabled = self.periodic_optimization_check.isChecked()
            self.config.periodic_optimization_interval_minutes = float(self.periodic_interval_edit.text())
            self.config.periodic_optimization_eco_mode = self.periodic_eco_check.isChecked()
            self.config.periodic_optimization_notify = self.periodic_notify_check.isChecked()
            for key, checkbox in self.optimization_step_checks.items():
                setattr(self.config, f"optimize_step_{key}_enabled", checkbox.isChecked())
            self.config.check_updates_on_startup = self.update_startup_check.isChecked()
            self.config.update_notify_enabled = self.update_notify_check.isChecked()
            self.config.auto_install_updates = self.update_auto_install_check.isChecked()
        except ValueError:
            QMessageBox.critical(self, "Настройки", "Проверьте числовые значения.")
            return
        apply_automation_mode(self.config)
        self._sync_controls_from_config()
        try:
            if self.autostart_check.isChecked():
                command = enable_autostart()
                autostart_detail = f"Autostart enabled: {command}"
            else:
                disable_autostart()
                autostart_detail = "Autostart disabled"
        except OSError as exc:
            QMessageBox.warning(self, "Автозапуск", str(exc))
            self.autostart_check.setChecked(is_autostart_enabled())
            autostart_detail = f"Autostart unchanged: {exc}"
        save_config(self.config)
        self._sync_controls_from_config()
        self.notifier.cooldown_seconds = self.config.notification_cooldown_seconds
        self.monitor.interval_seconds = self.config.monitor_interval_seconds
        self.monitor.process_refresh_seconds = self.config.process_refresh_seconds
        self._foreground_monitor_interval = self.config.monitor_interval_seconds
        self._foreground_process_interval = self.config.process_refresh_seconds
        self._apply_runtime_performance_mode()
        if not self.isVisible() or self.isMinimized():
            self._enter_background_mode()
        self.apply_theme()
        self.history.add_event("settings", "Settings saved", f"Configuration persisted. {autostart_detail}", "success")
        QMessageBox.information(self, "Настройки", "Настройки сохранены.")
        self.refresh_activity()

    def check_updates_now(self) -> None:
        if self._pending_update and self._pending_update.update_available:
            self.install_pending_update()
            return
        self._start_update_check(manual=True)

    def _maybe_check_updates_on_startup(self) -> None:
        if self.config.check_updates_on_startup:
            self._start_update_check(manual=False)

    def _start_update_check(self, *, manual: bool) -> None:
        if self._update_thread and self._update_thread.isRunning():
            return
        if not is_repository_configured(DEFAULT_GITHUB_OWNER, DEFAULT_GITHUB_REPO):
            if manual:
                QMessageBox.information(
                    self,
                    "Обновления",
                    "Репозиторий обновлений не настроен.",
                )
            return
        thread = QThread(self)
        worker = UpdateCheckWorker(self.config, force=manual)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.result_received.connect(self._on_update_check_result)
        worker.result_received.connect(thread.quit)
        worker.result_received.connect(worker.deleteLater)
        thread.finished.connect(self._on_update_check_finished)
        thread.finished.connect(thread.deleteLater)
        self._update_check_manual = manual
        self._update_thread = thread
        self._update_worker_obj = worker
        if manual:
            self._set_update_action_state("checking")
            self.statusBar().showMessage("Проверяю GitHub Releases...", 2500)
        thread.start()

    def _on_update_check_result(self, result: UpdateCheckResult) -> None:
        manual = self._update_check_manual
        if not result.configured:
            if manual:
                QMessageBox.information(self, "Обновления", "GitHub репозиторий пока не настроен.")
                self._set_update_action_state("check")
            return
        if result.skipped and not manual:
            return
        if not result.update_available:
            self._pending_update = None
            self._set_update_action_state("check")
            if manual:
                if not result.message.startswith("Update check failed quietly"):
                    latest = result.latest_version or APP_VERSION
                    QMessageBox.information(self, "Обновления", f"У вас последняя версия {latest}.")
            return
        self._pending_update = result
        self._set_update_action_state("install", result.latest_version)
        self._show_update_banner(result)
        self._refresh_tray_state()
        if self.config.auto_install_updates:
            self._start_update_install(result)
            return
        if self.config.update_notify_enabled:
            self.notifier.notify(
                "PC Optimizer Lite",
                f"Доступна версия {result.latest_version}. Откройте приложение и нажмите «Обновить».",
                key=f"update_{result.latest_version}",
            )
        if manual:
            self.statusBar().showMessage(f"Доступна версия {result.latest_version}", 3500)

    def _on_update_check_finished(self) -> None:
        self._update_thread = None
        self._update_worker_obj = None

    def _prompt_update(self, result: UpdateCheckResult) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Доступно обновление")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"Доступна версия {result.latest_version}. Обновить?")
        box.setInformativeText(result.release_name or result.release_url or "Новый релиз найден в GitHub Releases.")
        update_button = box.addButton("Обновить", QMessageBox.ButtonRole.AcceptRole)
        later_button = box.addButton("Позже", QMessageBox.ButtonRole.RejectRole)
        skip_button = box.addButton("Пропустить эту версию", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(update_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked == update_button:
            self._start_update_install(result)
        elif clicked == skip_button:
            self.config.skipped_update_version = result.latest_version
            save_config(self.config)
            self.history.add_event("updates", "Update skipped", f"Version {result.latest_version}", "info")
            self.refresh_activity()
        elif clicked == later_button:
            self.statusBar().showMessage("Обновление отложено.", 2500)

    def _set_update_action_state(self, mode: str, version: str = "") -> None:
        if not hasattr(self, "check_updates_button"):
            return
        self._update_action_mode = mode
        button = self.check_updates_button
        if mode == "checking":
            button.setText("Проверяю...")
            button.setEnabled(False)
            button.setProperty("updateAvailable", False)
            button.setIcon(_feather_icon("refresh", self.palette["accent"]))
        elif mode == "install":
            button.setText(f"Обновить до {version}" if version else "Обновить")
            button.setEnabled(True)
            button.setProperty("updateAvailable", True)
            button.setIcon(_feather_icon("arrow-down", self.palette["bg"]))
        elif mode == "downloading":
            button.setText("Загрузка... 0%")
            button.setEnabled(False)
            button.setProperty("updateAvailable", True)
            button.setIcon(_feather_icon("arrow-down", self.palette["bg"]))
        else:
            button.setText("Проверить обновления")
            button.setEnabled(True)
            button.setProperty("updateAvailable", False)
            button.setIcon(_feather_icon("refresh", self.palette["accent"]))
        button.style().unpolish(button)
        button.style().polish(button)
        button.update()

    def _show_update_banner(self, result: UpdateCheckResult) -> None:
        self.update_banner_label.setText(f"Доступна версия {result.latest_version} — Обновить")
        self.update_download_progress.setValue(0)
        self.update_download_progress.setVisible(False)
        self.update_banner_button.setEnabled(True)
        self.update_banner_button.setText(f"Обновить до {result.latest_version}")
        self.update_banner.setVisible(True)

    def hide_update_banner(self) -> None:
        self.update_banner.setVisible(False)

    def install_pending_update(self) -> None:
        if not self._pending_update:
            return
        self._start_update_install(self._pending_update)

    def _start_update_install(self, result: UpdateCheckResult) -> None:
        if self._update_install_thread and self._update_install_thread.isRunning():
            return
        thread = QThread(self)
        worker = UpdateInstallWorker(result)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress_received.connect(self._on_update_install_progress)
        worker.result_received.connect(self._on_update_install_result)
        worker.error_received.connect(self._on_update_install_error)
        worker.result_received.connect(thread.quit)
        worker.error_received.connect(thread.quit)
        worker.result_received.connect(worker.deleteLater)
        worker.error_received.connect(worker.deleteLater)
        thread.finished.connect(self._on_update_install_finished)
        thread.finished.connect(thread.deleteLater)
        self._update_install_thread = thread
        self._update_install_worker_obj = worker
        self.update_banner.setVisible(True)
        self.update_download_progress.setValue(0)
        self.update_download_progress.setVisible(True)
        self.update_banner_button.setEnabled(False)
        self.update_banner_button.setText("Загрузка...")
        self._set_update_action_state("downloading")
        self.statusBar().showMessage("Скачиваю и подготавливаю обновление...", 4000)
        thread.start()

    def _on_update_install_progress(self, percent: int, message: str) -> None:
        percent = max(0, min(100, percent))
        self.update_download_progress.setVisible(True)
        self.update_download_progress.setValue(percent)
        progress_text = f"Загрузка... {percent}%"
        self.update_banner_label.setText(progress_text)
        self.update_banner_button.setText(progress_text)
        if hasattr(self, "check_updates_button"):
            self.check_updates_button.setText(progress_text)

    def _on_update_install_result(self, script_path: Path) -> None:
        self.history.add_event(
            "updates",
            "Update staged",
            f"Replacement script started: {script_path}",
            "success",
        )
        QMessageBox.information(
            self,
            "Обновление",
            "Установка, приложение перезапустится.",
        )
        self.update_banner_label.setText("Установка, приложение перезапустится")
        self.update_banner_button.setText("Установка...")
        if hasattr(self, "check_updates_button"):
            self.check_updates_button.setText("Установка...")
        self._allow_close = True
        QApplication.instance().quit()

    def _on_update_install_error(self, message: str) -> None:
        self.history.add_event("updates", "Update failed", message, "error")
        self.update_banner_button.setEnabled(True)
        self.update_banner_button.setText(
            f"Обновить до {self._pending_update.latest_version}" if self._pending_update else "Обновить"
        )
        self.update_download_progress.setVisible(False)
        if self._pending_update:
            self._set_update_action_state("install", self._pending_update.latest_version)
        else:
            self._set_update_action_state("check")
        QMessageBox.warning(self, "Обновление", message)
        self.refresh_activity()

    def _on_update_install_finished(self) -> None:
        self._update_install_thread = None
        self._update_install_worker_obj = None

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_normal()

    def show_normal(self) -> None:
        self._enter_foreground_mode()
        self.show()
        self.raise_()
        self.activateWindow()
        self._sync_process_collection_mode()

    def hide_to_tray(self) -> None:
        self._enter_background_mode()
        self.hide()

    def hideEvent(self, event: object) -> None:
        super().hideEvent(event)
        if not self._allow_close:
            self._enter_background_mode()

    def showEvent(self, event: object) -> None:
        super().showEvent(event)
        self._enter_foreground_mode()

    def _apply_runtime_performance_mode(self) -> None:
        self.graph.set_lite_mode(self.config.lite_mode_enabled)
        self._foreground_monitor_interval = max(
            3.5 if self.config.lite_mode_enabled else 2.0,
            self.config.monitor_interval_seconds,
        )
        self._foreground_process_interval = max(
            self._foreground_monitor_interval,
            12.0 if self.config.lite_mode_enabled else self.config.process_refresh_seconds,
            self.config.process_refresh_seconds,
        )
        if hasattr(self, "activity_timer"):
            self.activity_timer.setInterval(25000 if self.config.lite_mode_enabled else 15000)
        if self.isVisible() and not self.isMinimized():
            self._enter_foreground_mode()

    def _maybe_offer_lite_mode(self) -> None:
        if self.config.lite_mode_enabled or self.config.lite_mode_prompted:
            return
        logical_cores = psutil.cpu_count(logical=True) or 1
        total_ram = psutil.virtual_memory().total
        if logical_cores > 2 and total_ram > 4 * 1024**3:
            return
        self.config.lite_mode_prompted = True
        answer = QMessageBox.question(
            self,
            "Режим слабого ПК",
            (
                "Похоже, у компьютера немного ресурсов. Включить режим слабого ПК?\n\n"
                "Он реже опрашивает систему, упрощает график и делает паузы между шагами оптимизации длиннее."
            ),
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.config.lite_mode_enabled = True
            self.config.monitor_interval_seconds = max(self.config.monitor_interval_seconds, 3.5)
            self.config.process_refresh_seconds = max(self.config.process_refresh_seconds, 12.0)
            self.config.cpu_optimizer_max_processes = min(self.config.cpu_optimizer_max_processes, 2)
            self.config.optimize_step_sleep_enabled = False
            self.config.optimize_step_close_enabled = False
            self.config.optimize_step_cleanup_enabled = False
            save_config(self.config)
            self._sync_controls_from_config()
            self._apply_runtime_performance_mode()
            self.history.add_event("settings", "Lite Mode enabled", "Enabled automatically after weak PC prompt.", "info")
            self.refresh_activity()
        else:
            save_config(self.config)

    def _enter_background_mode(self) -> None:
        self.monitor.interval_seconds = max(18.0 if self.config.lite_mode_enabled else 10.0, self.config.monitor_interval_seconds, 12.0)
        self.monitor.process_refresh_seconds = max(90.0 if self.config.lite_mode_enabled else 60.0, self.monitor.interval_seconds)
        self.monitor.set_process_collection_enabled(False)
        self.graph.set_live_updates_enabled(False)
        if hasattr(self, "activity_timer"):
            self.activity_timer.setInterval(45000 if self.config.lite_mode_enabled else 30000)

    def _enter_foreground_mode(self) -> None:
        self.monitor.interval_seconds = self._foreground_monitor_interval
        self.monitor.process_refresh_seconds = self._foreground_process_interval
        self.graph.set_lite_mode(self.config.lite_mode_enabled)
        self.graph.set_live_updates_enabled(self._graph_updates_allowed())
        self._sync_process_collection_mode()
        if hasattr(self, "activity_timer"):
            self.activity_timer.setInterval(25000 if self.config.lite_mode_enabled else 15000)

    def _sync_process_collection_mode(self) -> None:
        visible = self.isVisible() and not self.isMinimized()
        enabled = visible and hasattr(self, "tabs") and self.tabs.currentWidget() == self.processes_tab
        self.monitor.set_process_collection_enabled(enabled)
        if hasattr(self, "graph"):
            self.graph.set_live_updates_enabled(self._graph_updates_allowed())

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._allow_close:
            event.accept()
            return
        event.ignore()
        self.hide_to_tray()
        if self.tray.isVisible():
            self.tray.showMessage(
                "PC Optimizer Lite",
                "Приложение работает в фоне.",
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )

    def exit_app(self) -> None:
        self._allow_close = True
        if self._optimization_cancel_event:
            self._optimization_cancel_event.set()
        if self._optimization_thread and self._optimization_thread.isRunning():
            self._optimization_thread.quit()
            self._optimization_thread.wait(1500)
        for thread in (
            self._process_refresh_thread,
            self._cleanup_thread,
            self._ram_clean_thread,
            self._update_thread,
            self._update_install_thread,
        ):
            if thread and thread.isRunning():
                thread.quit()
                thread.wait(1500)
        self.cpu_throttler.restore_all("app exit")
        self.cpu_optimizer.restore_all("app exit")
        for entry in list(self.sleep_manager.sleeping):
            self.sleep_manager.resume_process(entry.pid, "app exit")
        self.monitor.stop()
        self.tray.hide()
        QApplication.instance().quit()


def run_app(config: AppConfig) -> int:
    """Run the PySide6 application."""

    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    palette = THEMES[config.theme]
    app.setWindowIcon(_app_icon(palette))

    history = HistoryManager()
    whitelist = Whitelist(config)
    monitor = SystemMonitor(
        interval_seconds=config.monitor_interval_seconds,
        process_refresh_seconds=config.process_refresh_seconds,
    )
    optimizer = SystemOptimizer(whitelist, config)
    notifier = SystemNotifier(cooldown_seconds=config.notification_cooldown_seconds)
    smart_manager = SmartProcessManager(whitelist, history)
    sleep_manager = SleepManager(whitelist, history)
    ram_cleaner = RamCleaner(whitelist)
    cpu_optimizer = CpuOptimizer(whitelist, history)
    cpu_throttler = CpuThrottler(whitelist, history)
    window = PCOptimizerQtWindow(
        config=config,
        monitor=monitor,
        whitelist=whitelist,
        optimizer=optimizer,
        notifier=notifier,
        history=history,
        smart_manager=smart_manager,
        sleep_manager=sleep_manager,
        ram_cleaner=ram_cleaner,
        cpu_optimizer=cpu_optimizer,
        cpu_throttler=cpu_throttler,
    )
    app.aboutToQuit.connect(monitor.stop)
    app.aboutToQuit.connect(cpu_throttler.restore_all)
    app.aboutToQuit.connect(cpu_optimizer.restore_all)
    if config.window_starts_hidden:
        window._enter_background_mode()
    monitor.start()
    if not config.window_starts_hidden:
        window.show()
    return int(app.exec())


def _seconds_since_last_input() -> float:
    """Return seconds since last keyboard/mouse input on Windows, or a safe idle value."""

    if not hasattr(ctypes, "windll"):
        return 3600.0

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    try:
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):  # type: ignore[attr-defined]
            return 3600.0
        tick_count = ctypes.windll.kernel32.GetTickCount()  # type: ignore[attr-defined]
        return max(0.0, (int(tick_count) - int(info.dwTime)) / 1000.0)
    except Exception:
        return 3600.0


def _set_current_thread_background_mode(enabled: bool) -> bool:
    """Ask Windows to run the current worker thread in background mode."""

    if not hasattr(ctypes, "windll"):
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        priority = 0x00010000 if enabled else 0x00020000
        return bool(kernel32.SetThreadPriority(kernel32.GetCurrentThread(), priority))
    except Exception:
        return False


def _add_shadow(widget: QWidget, palette: dict[str, str]) -> None:
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(18)
    shadow.setOffset(0, 6)
    color = QColor("#000000")
    color.setAlpha(75 if palette is THEMES["dark"] else 35)
    shadow.setColor(color)
    widget.setGraphicsEffect(shadow)


def _configure_table(table: QTableWidget, min_rows: int = 3, max_height: int | None = None) -> None:
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    table.setShowGrid(False)
    table.setMinimumHeight(_table_height(table, min_rows))
    if max_height:
        table.setMaximumHeight(max_height)


def _fit_table_height(table: QTableWidget, min_rows: int = 3, max_height: int | None = None) -> None:
    visible_rows = max(min_rows, min(max(table.rowCount(), 1), 6))
    height = _table_height(table, visible_rows)
    table.setMinimumHeight(height)
    if max_height:
        table.setMaximumHeight(max(max_height, height))


def _table_height(table: QTableWidget, rows: int) -> int:
    header_height = table.horizontalHeader().height() or 36
    return header_height + rows * 32 + 18


def _button(text: str, icon_name: str, callback: Callable[[], None], palette: dict[str, str]) -> QPushButton:
    button = QPushButton(text)
    button.setIcon(_feather_icon(icon_name, palette["accent"]))
    button.clicked.connect(callback)
    return button


def _toggle(text: str) -> QCheckBox:
    checkbox = QCheckBox(text)
    checkbox.setObjectName("ToggleSwitch")
    checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
    return checkbox


def _settings_section(title: str, palette: dict[str, str]) -> tuple[QFrame, QFormLayout]:
    frame = QFrame()
    frame.setObjectName("SettingsSection")
    _add_shadow(frame, palette)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 16, 18, 18)
    layout.setSpacing(12)
    title_label = QLabel(title)
    title_label.setObjectName("SectionTitle")
    layout.addWidget(title_label)
    form = QFormLayout()
    form.setContentsMargins(0, 0, 0, 0)
    form.setSpacing(10)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    layout.addLayout(form)
    return frame, form


def _form_row(form: QFormLayout, label: str, widget: QWidget) -> None:
    if label:
        label_widget = QLabel(label)
        label_widget.setObjectName("FormLabel")
        form.addRow(label_widget, widget)
    else:
        form.addRow(widget)


def _status_color(percent: float, palette: dict[str, str]) -> QColor:
    value = max(0.0, min(percent, 100.0))
    if value < 60.0:
        return QColor(palette["good"])
    if value <= 85.0:
        return QColor(palette["warn"])
    return QColor(palette["bad"])


def _mix(a: QColor, b: QColor, ratio: float) -> QColor:
    ratio = max(0.0, min(1.0, ratio))
    return QColor(
        round(a.red() + (b.red() - a.red()) * ratio),
        round(a.green() + (b.green() - a.green()) * ratio),
        round(a.blue() + (b.blue() - a.blue()) * ratio),
    )


def _with_alpha(color: str, alpha: int) -> str:
    value = QColor(color)
    value.setAlpha(max(0, min(alpha, 255)))
    return value.name(QColor.NameFormat.HexArgb)


def _safe_float(value: str, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _qss(palette: dict[str, str]) -> str:
    return f"""
    QWidget {{
        background: {palette["bg"]};
        color: {palette["text"]};
        font-family: "Segoe UI", Arial, sans-serif;
        font-size: 13px;
    }}
    QTabWidget::pane {{
        border: 1px solid {palette["border"]};
        border-radius: 12px;
        background: {palette["bg"]};
    }}
    QTabBar::tab {{
        background: {palette["panel"]};
        color: {palette["muted"]};
        border: 1px solid {palette["border"]};
        border-bottom: none;
        padding: 10px 16px;
        margin-right: 4px;
        border-top-left-radius: 10px;
        border-top-right-radius: 10px;
    }}
    QTabBar::tab:selected {{
        color: {palette["text"]};
        background: {palette["panel_2"]};
    }}
    QTableWidget, QListWidget {{
        background: {palette["panel"]};
        border: 1px solid {palette["border"]};
        border-radius: 10px;
    }}
    QFrame#MetricCard, QFrame#SettingsSection, QFrame#CollapsibleSection {{
        background: {palette["panel"]};
        border: 1px solid {palette["border"]};
        border-radius: 12px;
    }}
    QFrame#SettingsFooter {{
        background: {palette["panel"]};
        border: 1px solid {palette["border"]};
        border-radius: 12px;
    }}
    QFrame#MonitorBottomBar {{
        background: {palette["panel"]};
        border-top: 1px solid {palette["border"]};
        border-left: none;
        border-right: none;
        border-bottom: none;
    }}
    QWidget#CollapsibleContent {{
        background: transparent;
        border: none;
    }}
    QScrollArea#MonitorScroll, QScrollArea#SettingsScroll {{
        background: transparent;
        border: none;
    }}
    QTableWidget {{
        gridline-color: {palette["border"]};
        alternate-background-color: {palette["row_alt"]};
        selection-background-color: {palette["accent"]};
        padding: 0px;
    }}
    QHeaderView::section {{
        background: {palette["panel_2"]};
        color: {palette["text"]};
        border: none;
        border-bottom: 1px solid {palette["border"]};
        padding: 8px;
    }}
    QScrollBar:vertical {{
        background: transparent;
        width: 8px;
        margin: 4px 2px 4px 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {_with_alpha(palette["muted"], 120)};
        border-radius: 4px;
        min-height: 28px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {_with_alpha(palette["accent"], 170)};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        height: 0px;
        background: transparent;
        border: none;
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 8px;
        margin: 2px 4px 2px 4px;
    }}
    QScrollBar::handle:horizontal {{
        background: {_with_alpha(palette["muted"], 120)};
        border-radius: 4px;
        min-width: 28px;
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
        width: 0px;
        background: transparent;
        border: none;
    }}
    QPushButton, QToolButton {{
        background: {palette["panel_2"]};
        color: {palette["text"]};
        border: 1px solid {palette["border"]};
        border-radius: 9px;
        padding: 8px 12px;
    }}
    QPushButton:hover, QToolButton:hover {{
        border-color: {palette["accent"]};
        background: {palette["input"]};
    }}
    QPushButton:pressed, QToolButton:pressed {{
        border-color: {_mix(QColor(palette["accent"]), QColor(palette["text"]), 0.28).name()};
        background: {_mix(QColor(palette["panel_2"]), QColor(palette["accent"]), 0.18).name()};
        padding-top: 9px;
        padding-bottom: 7px;
    }}
    QPushButton:disabled, QToolButton:disabled {{
        color: {palette["muted"]};
        background: {palette["panel"]};
    }}
    QPushButton#UpdateActionButton[updateAvailable="true"] {{
        background: {palette["accent"]};
        color: {palette["bg"]};
        border: none;
        font-weight: 750;
    }}
    QPushButton#UpdateActionButton[updateAvailable="true"]:hover {{
        background: {_mix(QColor(palette["accent"]), QColor(palette["good"]), 0.18).name()};
    }}
    QPushButton#SaveButton {{
        background: {palette["accent"]};
        color: {palette["bg"]};
        border: none;
        border-radius: 10px;
        padding: 10px 18px;
        font-weight: 750;
    }}
    QPushButton#SaveButton:hover {{
        background: {_mix(QColor(palette["accent"]), QColor(palette["good"]), 0.16).name()};
    }}
    QLineEdit, QComboBox {{
        background: {palette["input"]};
        color: {palette["text"]};
        border: 1px solid {palette["border"]};
        border-radius: 8px;
        padding: 7px;
    }}
    QLabel#FormLabel {{
        color: {palette["muted"]};
        background: transparent;
        border: none;
        padding: 0px 8px 0px 0px;
    }}
    QLabel#SectionTitle {{
        font-size: 15px;
        font-weight: 750;
        color: {palette["text"]};
        background: transparent;
        border: none;
        padding-bottom: 4px;
    }}
    QLabel#BottomBarLabel {{
        color: {palette["muted"]};
        background: transparent;
        border: none;
        font-weight: 700;
        padding: 0px 4px 0px 0px;
    }}
    QLabel#SettingsHint {{
        color: {palette["muted"]};
        background: transparent;
        border: none;
        padding-top: 4px;
    }}
    QCheckBox#ToggleSwitch {{
        background: transparent;
        border: none;
        color: {palette["text"]};
        spacing: 10px;
        padding: 3px 0px;
    }}
    QCheckBox#ToggleSwitch::indicator {{
        width: 42px;
        height: 22px;
        border-radius: 11px;
        background: {palette["input"]};
        border: 1px solid {palette["border"]};
    }}
    QCheckBox#ToggleSwitch::indicator:checked {{
        background: {palette["accent"]};
        border-color: {palette["accent"]};
    }}
    QCheckBox#ToggleSwitch::indicator:hover {{
        border-color: {palette["accent"]};
    }}
    QProgressBar {{
        background: {palette["input"]};
        border: 1px solid {palette["border"]};
        border-radius: 5px;
        height: 12px;
        text-align: center;
    }}
    QProgressBar::chunk {{
        background: {palette["accent"]};
        border-radius: 4px;
    }}
    QLabel#CardTitle {{
        color: {palette["muted"]};
        font-weight: 600;
    }}
    QLabel#CardValue {{
        font-size: 26px;
        font-weight: 700;
        color: {palette["text"]};
    }}
    QLabel#CardDetail {{
        color: {palette["muted"]};
    }}
    QLabel#InfoBadge {{
        background: {palette["panel_2"]};
        color: {palette["muted"]};
        border: 1px solid {palette["border"]};
        border-radius: 8px;
        min-width: 16px;
        max-width: 16px;
        min-height: 16px;
        max-height: 16px;
        font-size: 11px;
        font-weight: 800;
        qproperty-alignment: AlignCenter;
    }}
    QLabel#TrendLabel {{
        font-size: 18px;
        font-weight: 750;
        background: transparent;
        border: none;
    }}
    QFrame#OptimizeHero {{
        background: {palette["panel"]};
        border: 1px solid {palette["border"]};
        border-radius: 14px;
    }}
    QFrame#UpdateBanner {{
        background: {_with_alpha(palette["warn"], 35)};
        border: 1px solid {palette["warn"]};
        border-radius: 10px;
    }}
    QLabel#UpdateBannerText {{
        color: {palette["text"]};
        font-weight: 700;
        background: transparent;
        border: none;
    }}
    QLabel#HeroTitle {{
        font-size: 22px;
        font-weight: 750;
        color: {palette["text"]};
    }}
    QLabel#HeroSubtitle {{
        color: {palette["muted"]};
    }}
    QLabel#StatusText {{
        color: {palette["muted"]};
        background: transparent;
        border: none;
        padding: 0px;
    }}
    QPushButton#OptimizeButton {{
        background: {palette["accent"]};
        color: {palette["bg"]};
        border: none;
        border-radius: 18px;
        padding: 15px 28px;
        font-size: 19px;
        font-weight: 800;
        min-width: 340px;
    }}
    QPushButton#OptimizeButton:hover {{
        background: {_mix(QColor(palette["accent"]), QColor(palette["good"]), 0.18).name()};
    }}
    QPushButton#OptimizeButton:pressed {{
        background: {_mix(QColor(palette["accent"]), QColor(palette["text"]), 0.18).name()};
    }}
    QToolButton#RamCleanButton {{
        font-weight: 650;
    }}
    QToolButton#CollapseButton {{
        min-width: 28px;
        max-width: 28px;
        min-height: 26px;
        max-height: 26px;
        padding: 0px;
        border-radius: 7px;
        font-weight: 800;
    }}
    """


def _format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")


def _format_categories(plan: CleanupPlan) -> str:
    if not plan.categories:
        return "Категории: нет данных"
    return "\n".join(
        f"{category}: {summary.files} файлов, {format_bytes(summary.bytes)}"
        for category, summary in sorted(plan.categories.items())
    )


def _format_result_categories(categories) -> str:
    if not categories:
        return "Категории: нет данных"
    return "\n".join(
        f"{category}: {summary.files} файлов, {format_bytes(summary.bytes)}"
        for category, summary in sorted(categories.items())
    )


def _format_ram_clean_report(result: RamCleanResult) -> str:
    successful = [item for item in result.process_results if item.success]
    lines = [
        (
            "Было занято RAM: "
            f"{format_bytes(result.ram_used_before)} ({result.ram_percent_before:.1f}%) → "
            f"{format_bytes(result.ram_used_after)} ({result.ram_percent_after:.1f}%)"
        ),
        f"Освобождено: {format_bytes(result.freed_bytes)}",
        f"Обработано процессов: {len(successful)}",
    ]
    if result.mode == RamCleanMode.DEEP:
        if result.admin_required:
            lines.append("Глубокая очистка системных списков не выполнена: нужны права администратора.")
        else:
            lines.append(f"Standby List: {'очищен' if result.standby_purged else 'не очищен'}")
            lines.append(f"Modified Page List: {'очищен' if result.modified_purged else 'не очищен'}")
    if successful:
        lines.append("")
        lines.append("Процессы с уменьшением working set:")
        for item in sorted(successful, key=lambda value: value.freed_bytes, reverse=True)[:20]:
            if item.freed_bytes <= 0:
                continue
            lines.append(
                f"- {item.name}: {format_bytes(item.before_rss)} → "
                f"{format_bytes(item.after_rss)} (-{format_bytes(item.freed_bytes)})"
            )
    if result.errors:
        lines.append("")
        lines.append("Ошибки/пропуски:")
        lines.extend(f"- {error}" for error in result.errors[:8])
    return "\n".join(lines)


def _app_icon(palette: dict[str, str], badge_color: str = "") -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor(palette["accent"]))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("white"), 4))
    painter.drawRoundedRect(14, 18, 36, 28, 6, 6)
    painter.drawLine(23, 31, 31, 39)
    painter.drawLine(31, 39, 43, 25)
    if badge_color:
        painter.setPen(QPen(QColor("white"), 3))
        painter.setBrush(QColor(badge_color))
        painter.drawEllipse(43, 43, 16, 16)
    painter.end()
    return QIcon(pixmap)


def _feather_icon(name: str, color: str) -> QIcon:
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), 2.4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if name in {"activity", "cpu"}:
        painter.drawLine(4, 17, 10, 17)
        painter.drawLine(10, 17, 13, 9)
        painter.drawLine(13, 9, 18, 24)
        painter.drawLine(18, 24, 22, 14)
        painter.drawLine(22, 14, 28, 14)
    elif name in {"memory", "hard-drive"}:
        painter.drawRoundedRect(5, 9, 22, 14, 4, 4)
        painter.drawLine(10, 23, 10, 27)
        painter.drawLine(16, 23, 16, 27)
        painter.drawLine(22, 23, 22, 27)
    elif name == "thermometer":
        painter.drawLine(16, 6, 16, 20)
        painter.drawEllipse(11, 18, 10, 10)
    elif name == "shield":
        painter.drawPolygon([QPointF(16, 5), QPointF(26, 9), QPointF(23, 24), QPointF(16, 28), QPointF(9, 24), QPointF(6, 9)])
    elif name in {"settings", "refresh"}:
        painter.drawEllipse(10, 10, 12, 12)
        painter.drawLine(16, 4, 16, 8)
        painter.drawLine(16, 24, 16, 28)
        painter.drawLine(4, 16, 8, 16)
        painter.drawLine(24, 16, 28, 16)
    elif name in {"trash", "x"}:
        painter.drawLine(10, 10, 22, 22)
        painter.drawLine(22, 10, 10, 22)
    elif name in {"plus", "folder-plus"}:
        painter.drawLine(16, 8, 16, 24)
        painter.drawLine(8, 16, 24, 16)
    elif name == "folder":
        painter.drawRoundedRect(4, 10, 24, 16, 4, 4)
        painter.drawLine(5, 12, 13, 12)
    elif name == "arrow-down":
        painter.drawLine(16, 7, 16, 23)
        painter.drawLine(9, 17, 16, 24)
        painter.drawLine(23, 17, 16, 24)
    elif name in {"play", "rotate"}:
        painter.drawPolygon([QPointF(12, 8), QPointF(24, 16), QPointF(12, 24)])
    elif name == "clock":
        painter.drawEllipse(6, 6, 20, 20)
        painter.drawLine(16, 10, 16, 17)
        painter.drawLine(16, 17, 21, 20)
    elif name == "eye":
        painter.drawEllipse(7, 11, 18, 10)
        painter.drawEllipse(13, 13, 6, 6)
    elif name == "minimize":
        painter.drawLine(8, 20, 24, 20)
    elif name == "zap":
        painter.drawPolygon([QPointF(18, 4), QPointF(8, 18), QPointF(16, 18), QPointF(14, 28), QPointF(24, 14), QPointF(16, 14)])
    else:
        painter.drawEllipse(8, 8, 16, 16)
    painter.end()
    return QIcon(pixmap)

