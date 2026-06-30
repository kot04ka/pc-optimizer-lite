"""Entry point for PC Optimizer Lite."""

from __future__ import annotations

import logging
import os
import sys

import psutil

from pc_optimizer_lite.config import load_config, save_config, setup_logging


def main() -> int:
    """Launch the application."""

    config = load_config()
    setup_logging()
    _lower_own_priority()
    save_config(config)
    if any(arg in {"--tray", "--minimized"} for arg in sys.argv[1:]):
        config.window_starts_hidden = True
    logging.getLogger(__name__).info("PC Optimizer Lite starting")

    try:
        return _run_pyside_app(config)
    except Exception as exc:
        if isinstance(exc, ModuleNotFoundError) and exc.name == "PySide6":
            logging.getLogger(__name__).warning("PySide6 is unavailable; falling back to tkinter")
            return _run_tkinter_app(config)
        return _show_startup_error(exc)


def _run_tkinter_app(config) -> int:
    import tkinter as tk

    from pc_optimizer_lite.gui import PCOptimizerApp
    from pc_optimizer_lite.monitor import SystemMonitor
    from pc_optimizer_lite.notifier import SystemNotifier
    from pc_optimizer_lite.optimizer import SystemOptimizer
    from pc_optimizer_lite.whitelist import Whitelist

    root = tk.Tk()
    whitelist = Whitelist(config)
    monitor = SystemMonitor(
        interval_seconds=config.monitor_interval_seconds,
        process_refresh_seconds=config.process_refresh_seconds,
    )
    monitor.set_process_collection_enabled(False)
    optimizer = SystemOptimizer(whitelist)
    notifier = SystemNotifier(cooldown_seconds=config.notification_cooldown_seconds)
    app = PCOptimizerApp(root, config, monitor, whitelist, optimizer, notifier)
    app.run()
    return 0


def _run_pyside_app(config) -> int:
    from pc_optimizer_lite.pyside_gui import run_app

    return run_app(config)


def _show_startup_error(startup_exc: Exception) -> int:
    logging.getLogger(__name__).exception("Fatal application error")
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("PC Optimizer Lite", _format_startup_error(startup_exc))
        root.destroy()
    except Exception:
        print(_format_startup_error(startup_exc))
    return 1


def _format_startup_error(exc: Exception) -> str:
    if isinstance(exc, ModuleNotFoundError):
        if exc.name == "psutil":
            return "Не установлен psutil. Выполните: pip install -r requirements.txt"
        if exc.name == "PySide6":
            return "Не установлен PySide6. Выполните: pip install -r requirements.txt"
    return f"Ошибка запуска: {exc}"


def _lower_own_priority() -> None:
    """Lower app priority so the passive monitor never competes for CPU."""

    try:
        proc = psutil.Process()
        if os.name == "nt" and hasattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS"):
            proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            current = proc.nice()
            proc.nice(min(19, max(int(current), 10)))
        logging.getLogger(__name__).info("Own process priority lowered")
    except Exception:
        logging.getLogger(__name__).debug("Could not lower own process priority", exc_info=True)


def _is_broken_tk_error(exc: Exception) -> bool:
    if isinstance(exc, ModuleNotFoundError) and exc.name in {"tkinter", "_tkinter"}:
        return True
    message = str(exc).lower()
    return "init.tcl" in message or "tcl" in message and "installed properly" in message


if __name__ == "__main__":
    raise SystemExit(main())
