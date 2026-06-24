"""Tkinter GUI and optional system tray integration."""

from __future__ import annotations

import logging
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .config import AppConfig, save_config
from .monitor import MonitorSnapshot, ProcessInfo, SystemMonitor, format_bytes
from .notifier import SystemNotifier
from .optimizer import SystemOptimizer, open_file_location
from .whitelist import Whitelist

LOGGER = logging.getLogger(__name__)


class PCOptimizerApp:
    """Main tkinter application."""

    def __init__(
        self,
        root: tk.Tk,
        config: AppConfig,
        monitor: SystemMonitor,
        whitelist: Whitelist,
        optimizer: SystemOptimizer,
        notifier: SystemNotifier,
    ) -> None:
        self.root = root
        self.config = config
        self.monitor = monitor
        self.whitelist = whitelist
        self.optimizer = optimizer
        self.notifier = notifier
        self._process_rows: dict[str, ProcessInfo] = {}
        self._snapshot_queue: queue.Queue[MonitorSnapshot] = queue.Queue(maxsize=3)
        self._high_cpu_since: float | None = None
        self._last_auto_priority_at = 0.0
        self._tray = TrayController(self.root, self.show_window, self.exit_app)

        self.root.title("PC Optimizer Lite")
        self.root.geometry("980x640")
        self.root.minsize(820, 520)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        self._build_styles()
        self._build_ui()
        self.monitor.add_callback(self._on_monitor_snapshot)
        self.root.after(250, self._process_snapshot_queue)
        self.root.after(1000, self.refresh_process_table)

    def run(self) -> None:
        """Start background services and enter the GUI loop."""

        self.monitor.start()
        if self.config.window_starts_hidden:
            self.hide_to_tray()
        self.root.mainloop()

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Danger.Horizontal.TProgressbar", troughcolor="#f2f2f2", background="#d9534f")
        style.configure("Normal.Horizontal.TProgressbar", troughcolor="#f2f2f2", background="#4f8ef7")

    def _build_ui(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.monitoring_tab = ttk.Frame(notebook)
        self.processes_tab = ttk.Frame(notebook)
        self.whitelist_tab = ttk.Frame(notebook)
        self.settings_tab = ttk.Frame(notebook)

        notebook.add(self.monitoring_tab, text="Мониторинг")
        notebook.add(self.processes_tab, text="Процессы")
        notebook.add(self.whitelist_tab, text="Исключения")
        notebook.add(self.settings_tab, text="Настройки")

        self._build_monitoring_tab()
        self._build_processes_tab()
        self._build_whitelist_tab()
        self._build_settings_tab()

    def _build_monitoring_tab(self) -> None:
        metrics = ttk.Frame(self.monitoring_tab)
        metrics.pack(fill=tk.X, padx=8, pady=8)

        self.cpu_label = ttk.Label(metrics, text="CPU: -- %", font=("Segoe UI", 14, "bold"))
        self.cpu_label.grid(row=0, column=0, sticky=tk.W, padx=(0, 12), pady=4)
        self.cpu_progress = ttk.Progressbar(metrics, maximum=100, length=280)
        self.cpu_progress.grid(row=0, column=1, sticky=tk.EW, pady=4)

        self.ram_label = ttk.Label(metrics, text="RAM: -- %", font=("Segoe UI", 14, "bold"))
        self.ram_label.grid(row=1, column=0, sticky=tk.W, padx=(0, 12), pady=4)
        self.ram_progress = ttk.Progressbar(metrics, maximum=100, length=280)
        self.ram_progress.grid(row=1, column=1, sticky=tk.EW, pady=4)

        self.disk_io_label = ttk.Label(metrics, text="Диск I/O: --")
        self.disk_io_label.grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(8, 4))


        metrics.columnconfigure(1, weight=1)

        cores_frame = ttk.LabelFrame(self.monitoring_tab, text="CPU по ядрам")
        cores_frame.pack(fill=tk.X, padx=8, pady=8)
        self.cores_container = ttk.Frame(cores_frame)
        self.cores_container.pack(fill=tk.X, padx=8, pady=8)
        self.core_bars: list[ttk.Progressbar] = []
        self.core_labels: list[ttk.Label] = []

        disk_frame = ttk.LabelFrame(self.monitoring_tab, text="Диски")
        disk_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.disk_tree = ttk.Treeview(
            disk_frame,
            columns=("device", "mount", "type", "used", "free", "total", "percent"),
            show="headings",
            height=7,
        )
        for column, title, width in (
            ("device", "Диск", 140),
            ("mount", "Точка", 110),
            ("type", "ФС", 70),
            ("used", "Занято", 110),
            ("free", "Свободно", 110),
            ("total", "Всего", 110),
            ("percent", "%", 70),
        ):
            self.disk_tree.heading(column, text=title)
            self.disk_tree.column(column, width=width, anchor=tk.W)
        self.disk_tree.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        actions = ttk.Frame(self.monitoring_tab)
        actions.pack(fill=tk.X, padx=8, pady=(0, 8))
        ttk.Button(actions, text="Очистить temp...", command=self.confirm_temp_cleanup).pack(side=tk.LEFT)
        ttk.Button(actions, text="Свернуть в трей", command=self.hide_to_tray).pack(side=tk.LEFT, padx=8)

    def _build_processes_tab(self) -> None:
        toolbar = ttk.Frame(self.processes_tab)
        toolbar.pack(fill=tk.X, padx=8, pady=8)
        ttk.Button(toolbar, text="Обновить", command=self.refresh_process_table).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Понизить priority", command=self.lower_selected_priority).pack(
            side=tk.LEFT, padx=8
        )
        ttk.Button(toolbar, text="Завершить выбранный...", command=self.terminate_selected_process).pack(
            side=tk.LEFT
        )
        ttk.Button(toolbar, text="Открыть папку", command=self.open_selected_process_location).pack(
            side=tk.LEFT, padx=8
        )

        self.process_tree = ttk.Treeview(
            self.processes_tab,
            columns=("pid", "name", "cpu", "mem", "rss", "priority", "path"),
            show="headings",
        )
        for column, title, width in (
            ("pid", "PID", 70),
            ("name", "Процесс", 180),
            ("cpu", "CPU %", 80),
            ("mem", "RAM %", 80),
            ("rss", "RAM", 100),
            ("priority", "Priority", 90),
            ("path", "Путь", 340),
        ):
            self.process_tree.heading(column, text=title)
            self.process_tree.column(column, width=width, anchor=tk.W)
        self.process_tree.tag_configure("whitelisted", foreground="#777777")
        self.process_tree.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self.process_hint = ttk.Label(
            self.processes_tab,
            text="Серым отмечены системные или пользовательские исключения: они не меняются автоматически.",
        )
        self.process_hint.pack(fill=tk.X, padx=8, pady=(0, 8))

    def _build_whitelist_tab(self) -> None:
        top = ttk.Frame(self.whitelist_tab)
        top.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)
        top.rowconfigure(0, weight=1)

        names_frame = ttk.LabelFrame(top, text="Имена процессов")
        names_frame.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 4))
        self.names_list = tk.Listbox(names_frame, height=14)
        self.names_list.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        paths_frame = ttk.LabelFrame(top, text="Пути к exe")
        paths_frame.grid(row=0, column=1, sticky=tk.NSEW, padx=(4, 0))
        self.paths_list = tk.Listbox(paths_frame, height=14)
        self.paths_list.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        controls = ttk.Frame(self.whitelist_tab)
        controls.pack(fill=tk.X, padx=8, pady=(0, 8))
        self.whitelist_entry = ttk.Entry(controls)
        self.whitelist_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(controls, text="Добавить имя", command=self.add_whitelist_name).pack(side=tk.LEFT, padx=4)
        ttk.Button(controls, text="Добавить exe...", command=self.add_whitelist_path).pack(side=tk.LEFT, padx=4)
        ttk.Button(controls, text="Удалить выбранное", command=self.remove_whitelist_selected).pack(
            side=tk.LEFT, padx=4
        )
        self.refresh_whitelist_lists()

    def _build_settings_tab(self) -> None:
        form = ttk.Frame(self.settings_tab)
        form.pack(fill=tk.X, padx=16, pady=16)
        form.columnconfigure(1, weight=1)

        self.interval_var = tk.StringVar(value=str(self.config.monitor_interval_seconds))
        self.process_interval_var = tk.StringVar(value=str(self.config.process_refresh_seconds))
        self.cpu_threshold_var = tk.StringVar(value=str(self.config.cpu_threshold_percent))
        self.cpu_sustain_var = tk.StringVar(value=str(self.config.cpu_sustain_seconds))
        self.ram_threshold_var = tk.StringVar(value=str(self.config.ram_threshold_percent))
        self.cooldown_var = tk.StringVar(value=str(self.config.notification_cooldown_seconds))
        self.max_priority_var = tk.StringVar(value=str(self.config.max_auto_priority_changes))
        self.auto_priority_var = tk.BooleanVar(value=self.config.auto_lower_priority_enabled)

        rows = (
            ("Интервал мониторинга, сек", self.interval_var),
            ("Интервал обновления процессов, сек", self.process_interval_var),
            ("Порог CPU, %", self.cpu_threshold_var),
            ("CPU должен быть выше порога, сек", self.cpu_sustain_var),
            ("Порог RAM, %", self.ram_threshold_var),
            ("Антиспам уведомлений, сек", self.cooldown_var),
            ("Макс. priority-изменений за раз", self.max_priority_var),
        )
        for row, (label, variable) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky=tk.W, pady=5)
            ttk.Entry(form, textvariable=variable, width=16).grid(row=row, column=1, sticky=tk.W, pady=5)

        ttk.Checkbutton(
            form,
            text="Автоматически понижать priority тяжёлых процессов при критической нагрузке",
            variable=self.auto_priority_var,
        ).grid(row=len(rows), column=0, columnspan=2, sticky=tk.W, pady=8)

        ttk.Button(form, text="Сохранить настройки", command=self.save_settings).grid(
            row=len(rows) + 1, column=0, sticky=tk.W, pady=12
        )

    def _on_monitor_snapshot(self, snapshot: MonitorSnapshot) -> None:
        try:
            if self._snapshot_queue.full():
                self._snapshot_queue.get_nowait()
            self._snapshot_queue.put_nowait(snapshot)
        except queue.Full:
            pass
        self._handle_thresholds(snapshot)

    def _handle_thresholds(self, snapshot: MonitorSnapshot) -> None:
        now = time.monotonic()
        if snapshot.cpu_percent >= self.config.cpu_threshold_percent:
            if self._high_cpu_since is None:
                self._high_cpu_since = now
            high_for = now - self._high_cpu_since
            if high_for >= self.config.cpu_sustain_seconds:
                self.notifier.notify(
                    "Высокая нагрузка CPU",
                    f"CPU {snapshot.cpu_percent:.0f}% держится дольше {self.config.cpu_sustain_seconds:.0f} сек.",
                    key="high_cpu",
                )
                self._maybe_auto_lower_priority(snapshot)
        else:
            self._high_cpu_since = None

        if snapshot.memory.percent >= self.config.ram_threshold_percent:
            self.notifier.notify(
                "Высокое использование RAM",
                f"RAM занята на {snapshot.memory.percent:.0f}%.",
                key="high_ram",
            )

    def _maybe_auto_lower_priority(self, snapshot: MonitorSnapshot) -> None:
        if not self.config.auto_lower_priority_enabled:
            return
        now = time.monotonic()
        if now - self._last_auto_priority_at < self.config.notification_cooldown_seconds:
            return
        processes = snapshot.processes or self.monitor.get_processes(max_processes=80)
        actions = self.optimizer.lower_priority_for_heavy_processes(
            processes,
            limit=self.config.max_auto_priority_changes,
        )
        if actions:
            self._last_auto_priority_at = now
            changed = [action for action in actions if action.success]
            if changed:
                self.notifier.notify(
                    "PC Optimizer Lite",
                    f"Понижен priority у {len(changed)} тяжёлых процессов.",
                    key="priority_changed",
                )

    def _process_snapshot_queue(self) -> None:
        latest: MonitorSnapshot | None = None
        while True:
            try:
                latest = self._snapshot_queue.get_nowait()
            except queue.Empty:
                break
        if latest:
            self._render_snapshot(latest)
        self.root.after(250, self._process_snapshot_queue)

    def _render_snapshot(self, snapshot: MonitorSnapshot) -> None:
        self.cpu_label.configure(text=f"CPU: {snapshot.cpu_percent:.1f} %")
        self.cpu_progress.configure(value=snapshot.cpu_percent)
        self.ram_label.configure(
            text=(
                f"RAM: {snapshot.memory.percent:.1f} % "
                f"({format_bytes(snapshot.memory.used)} / {format_bytes(snapshot.memory.total)}, "
                f"доступно {format_bytes(snapshot.memory.available)})"
            )
        )
        self.ram_progress.configure(value=snapshot.memory.percent)
        self.disk_io_label.configure(
            text=(
                "Диск I/O: "
                f"чтение {format_bytes(snapshot.disk_io.read_bytes_per_second)}/с, "
                f"запись {format_bytes(snapshot.disk_io.write_bytes_per_second)}/с"
            )
        )
        self._render_core_bars(snapshot.per_core_cpu_percent)
        self._render_disks(snapshot)
        if snapshot.processes:
            self._render_processes(snapshot.processes)

    def _render_core_bars(self, values: list[float]) -> None:
        while len(self.core_bars) < len(values):
            index = len(self.core_bars)
            label = ttk.Label(self.cores_container, text=f"Core {index + 1}")
            label.grid(row=index, column=0, sticky=tk.W, padx=(0, 8), pady=2)
            bar = ttk.Progressbar(self.cores_container, maximum=100)
            bar.grid(row=index, column=1, sticky=tk.EW, pady=2)
            value_label = ttk.Label(self.cores_container, text="-- %", width=8)
            value_label.grid(row=index, column=2, sticky=tk.E, padx=(8, 0), pady=2)
            self.core_labels.append(value_label)
            self.core_bars.append(bar)
        self.cores_container.columnconfigure(1, weight=1)
        for index, value in enumerate(values):
            self.core_bars[index].configure(value=value)
            self.core_labels[index].configure(text=f"{value:.0f} %")

    def _render_disks(self, snapshot: MonitorSnapshot) -> None:
        self.disk_tree.delete(*self.disk_tree.get_children())
        for disk in snapshot.disks:
            self.disk_tree.insert(
                "",
                tk.END,
                values=(
                    disk.device,
                    disk.mountpoint,
                    disk.fstype,
                    format_bytes(disk.used),
                    format_bytes(disk.free),
                    format_bytes(disk.total),
                    f"{disk.percent:.1f}",
                ),
            )

    def refresh_process_table(self) -> None:
        try:
            processes = self.monitor.get_processes(max_processes=200)
            self._render_processes(processes)
        except Exception:
            LOGGER.exception("Failed to refresh process table")
            messagebox.showerror("Ошибка", "Не удалось обновить список процессов.")

    def _render_processes(self, processes: list[ProcessInfo]) -> None:
        self.process_tree.delete(*self.process_tree.get_children())
        self._process_rows.clear()
        for process in processes:
            item_id = str(process.pid)
            whitelisted = self.whitelist.is_whitelisted(process.name, process.exe)
            self._process_rows[item_id] = process
            self.process_tree.insert(
                "",
                tk.END,
                iid=item_id,
                values=(
                    process.pid,
                    process.name,
                    f"{process.cpu_percent:.1f}",
                    f"{process.memory_percent:.1f}",
                    format_bytes(process.memory_rss),
                    process.priority,
                    process.exe,
                ),
                tags=("whitelisted",) if whitelisted else (),
            )

    def _get_selected_process(self) -> ProcessInfo | None:
        selection = self.process_tree.selection()
        if not selection:
            messagebox.showinfo("Процессы", "Выберите процесс в таблице.")
            return None
        return self._process_rows.get(selection[0])

    def lower_selected_priority(self) -> None:
        process = self._get_selected_process()
        if not process:
            return
        action = self.optimizer.lower_priority_for_process(process.pid)
        messagebox.showinfo("Priority", action.message)
        self.refresh_process_table()

    def terminate_selected_process(self) -> None:
        process = self._get_selected_process()
        if not process:
            return
        if self.whitelist.is_whitelisted(process.name, process.exe):
            messagebox.showwarning("Защищённый процесс", "Этот процесс находится в исключениях.")
            return
        confirmed = messagebox.askyesno(
            "Подтверждение",
            (
                f"Завершить процесс {process.name} (PID {process.pid})?\n\n"
                "PC Optimizer Lite никогда не завершает процессы автоматически; "
                "это действие выполнится только по вашему подтверждению."
            ),
        )
        if not confirmed:
            return
        action = self.optimizer.terminate_process_after_confirmation(process.pid)
        messagebox.showinfo("Процессы", action.message)
        self.refresh_process_table()

    def open_selected_process_location(self) -> None:
        process = self._get_selected_process()
        if not process:
            return
        if not open_file_location(process.exe):
            messagebox.showwarning("Папка процесса", "Не удалось открыть расположение файла.")

    def refresh_whitelist_lists(self) -> None:
        self.names_list.delete(0, tk.END)
        for name in sorted(self.whitelist.user_names):
            self.names_list.insert(tk.END, name)
        self.paths_list.delete(0, tk.END)
        for path in sorted(self.whitelist.user_paths):
            self.paths_list.insert(tk.END, path)

    def add_whitelist_name(self) -> None:
        value = self.whitelist_entry.get().strip()
        if not value:
            return
        if self.whitelist.add_name(value):
            save_config(self.config)
            LOGGER.info("User added whitelist process name: %s", value)
        self.whitelist_entry.delete(0, tk.END)
        self.refresh_whitelist_lists()

    def add_whitelist_path(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите exe",
            filetypes=(("Executable files", "*.exe"), ("All files", "*.*")),
        )
        if not path:
            return
        if self.whitelist.add_path(path):
            save_config(self.config)
            LOGGER.info("User added whitelist path: %s", path)
        self.refresh_whitelist_lists()

    def remove_whitelist_selected(self) -> None:
        removed = False
        name_selection = self.names_list.curselection()
        if name_selection:
            removed = self.whitelist.remove_name(self.names_list.get(name_selection[0])) or removed
        path_selection = self.paths_list.curselection()
        if path_selection:
            removed = self.whitelist.remove_path(self.paths_list.get(path_selection[0])) or removed
        if removed:
            save_config(self.config)
            LOGGER.info("User removed whitelist item")
        self.refresh_whitelist_lists()

    def save_settings(self) -> None:
        try:
            self.config.monitor_interval_seconds = float(self.interval_var.get())
            self.config.process_refresh_seconds = float(self.process_interval_var.get())
            self.config.cpu_threshold_percent = float(self.cpu_threshold_var.get())
            self.config.cpu_sustain_seconds = float(self.cpu_sustain_var.get())
            self.config.ram_threshold_percent = float(self.ram_threshold_var.get())
            self.config.notification_cooldown_seconds = float(self.cooldown_var.get())
            self.config.max_auto_priority_changes = int(float(self.max_priority_var.get()))
            self.config.auto_lower_priority_enabled = bool(self.auto_priority_var.get())
        except ValueError:
            messagebox.showerror("Настройки", "Проверьте числовые значения.")
            return

        save_config(self.config)
        self.notifier.cooldown_seconds = self.config.notification_cooldown_seconds
        self.monitor.interval_seconds = self.config.monitor_interval_seconds
        self.monitor.process_refresh_seconds = self.config.process_refresh_seconds
        messagebox.showinfo("Настройки", "Настройки сохранены.")

    def confirm_temp_cleanup(self) -> None:
        roots = self.optimizer.get_safe_temp_roots()
        if not roots:
            messagebox.showinfo("Очистка temp", "Временные папки не найдены.")
            return

        dry_run = self.optimizer.cleanup_temp_files(roots=roots, dry_run=True)
        root_text = "\n".join(str(root) for root in roots)
        confirmed = messagebox.askyesno(
            "Очистка temp",
            (
                f"Будут очищены только известные временные папки:\n{root_text}\n\n"
                f"Оценка: {dry_run.deleted_files} файлов, {format_bytes(dry_run.freed_bytes)}.\n"
                "Удаление пользовательских папок и системных файлов не выполняется.\n\n"
                "Продолжить?"
            ),
        )
        if not confirmed:
            return
        result = self.optimizer.cleanup_temp_files(roots=roots, dry_run=False)
        messagebox.showinfo(
            "Очистка temp",
            (
                f"Удалено файлов: {result.deleted_files}\n"
                f"Удалено папок: {result.deleted_dirs}\n"
                f"Освобождено: {format_bytes(result.freed_bytes)}\n"
                f"Ошибок/пропусков: {len(result.errors)}"
            ),
        )

    def hide_to_tray(self) -> None:
        if self._tray.start():
            self.root.withdraw()
        else:
            self.root.iconify()

    def show_window(self) -> None:
        self.root.after(0, self._show_window_on_main_thread)

    def _show_window_on_main_thread(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def exit_app(self) -> None:
        self.root.after(0, self._exit_on_main_thread)

    def _exit_on_main_thread(self) -> None:
        LOGGER.info("Application exit requested")
        self._tray.stop()
        self.monitor.stop()
        self.root.destroy()


class TrayController:
    """Optional pystray integration with graceful fallback."""

    def __init__(self, root: tk.Tk, on_show: callable, on_exit: callable) -> None:
        self.root = root
        self.on_show = on_show
        self.on_exit = on_exit
        self._icon = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """Start tray icon if pystray and Pillow are installed."""

        if self._icon is not None:
            return True
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            LOGGER.info("pystray/Pillow are unavailable; using window minimize fallback")
            return False

        image = Image.new("RGB", (64, 64), "#4f8ef7")
        draw = ImageDraw.Draw(image)
        draw.rectangle((14, 18, 50, 46), fill="white")
        draw.rectangle((20, 24, 44, 40), fill="#4f8ef7")
        menu = pystray.Menu(
            pystray.MenuItem("Показать", lambda *_: self.on_show()),
            pystray.MenuItem("Выход", lambda *_: self.on_exit()),
        )
        self._icon = pystray.Icon("pc_optimizer_lite", image, "PC Optimizer Lite", menu)
        self._thread = threading.Thread(target=self._icon.run, name="pc-optimizer-tray", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """Stop tray icon if it exists."""

        if self._icon is None:
            return
        try:
            self._icon.stop()
        except Exception:
            LOGGER.debug("Failed to stop tray icon cleanly", exc_info=True)
        self._icon = None
