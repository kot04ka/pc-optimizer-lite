"""Core safety tests for PC Optimizer Lite."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from pc_optimizer_lite.autostart import get_launch_command
from pc_optimizer_lite.config import (
    AppConfig,
    HardwareProfile,
    apply_automation_mode,
    apply_optimal_preset,
    load_config,
    save_config,
    sanitize_config,
)
from pc_optimizer_lite.cpu_optimizer import CpuOptimizer
from pc_optimizer_lite.cpu_throttler import CpuThrottler
from pc_optimizer_lite.history_manager import HistoryManager
from pc_optimizer_lite.monitor import DiskIOInfo, MemoryInfo, MonitorSnapshot, ProcessInfo, SwapInfo, SystemMonitor
from pc_optimizer_lite.optimize_action import ProcessOptimizationSnapshot, _classify_processes, _step_enabled
from pc_optimizer_lite.optimizer import CleanupTarget, SystemOptimizer
from pc_optimizer_lite.pagefile import (
    PageFileStatus,
    build_enable_auto_pagefile_command,
    recommend_pagefile_action,
)
from pc_optimizer_lite.ram_cleaner import RamCleanMode, RamCleanResult
from pc_optimizer_lite.sleep_manager import SKIP_SLEEP_NAMES, SleepManager
from pc_optimizer_lite.smart_process_manager import SmartProcessManager
from pc_optimizer_lite.updater import _replacement_script, is_newer_version, is_repository_configured
from pc_optimizer_lite.whitelist import Whitelist


class ConfigTests(unittest.TestCase):
    def test_monitor_interval_is_clamped(self) -> None:
        config = sanitize_config(AppConfig(monitor_interval_seconds=0.1))
        self.assertGreaterEqual(config.monitor_interval_seconds, 2.0)

    def test_config_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = AppConfig(user_whitelist_names=["Example.exe"])
            save_config(config, path)
            loaded = load_config(path)
            self.assertIn("example.exe", loaded.user_whitelist_names)

    def test_old_cpu_defaults_are_migrated_to_probalance_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                '{"cpu_threshold_percent": 90.0, "cpu_sustain_seconds": 10.0, '
                '"cpu_optimizer_min_process_cpu_percent": 10.0}\n',
                encoding="utf-8",
            )
            loaded = load_config(path)
            self.assertEqual(loaded.cpu_threshold_percent, 85.0)
            self.assertEqual(loaded.cpu_sustain_seconds, 2.8)
            self.assertEqual(loaded.cpu_optimizer_min_process_cpu_percent, 20.0)

    def test_autopilot_mode_enables_auto_features(self) -> None:
        config = apply_automation_mode(AppConfig(automation_mode="autopilot"))
        self.assertFalse(config.observation_only_mode)
        self.assertTrue(config.sleep_enabled)
        self.assertTrue(config.ram_auto_clean_enabled)
        self.assertTrue(config.cpu_throttle_enabled)
        self.assertTrue(config.scheduled_cleanup_enabled)
        self.assertTrue(config.scheduled_cleanup_notify)
        self.assertTrue(config.periodic_optimization_enabled)
        self.assertTrue(config.periodic_optimization_notify)
        self.assertEqual(config.auto_close_mode, "off")

    def test_optimal_balanced_preset_enables_safe_autonomy_without_auto_close(self) -> None:
        config = apply_optimal_preset(
            AppConfig(),
            HardwareProfile(cpu_cores=6, ram_bytes=16 * 1024**3, disk_kind="ssd"),
        )

        self.assertEqual(config.optimal_preset_tier, "balanced")
        self.assertEqual(config.automation_mode, "autopilot")
        self.assertFalse(config.observation_only_mode)
        self.assertTrue(config.ram_auto_clean_enabled)
        self.assertEqual(config.ram_auto_clean_threshold_percent, 80.0)
        self.assertEqual(config.cpu_threshold_percent, 85.0)
        self.assertEqual(config.cpu_sustain_seconds, 3.0)
        self.assertTrue(config.cpu_throttle_enabled)
        self.assertTrue(config.scheduled_cleanup_enabled)
        self.assertEqual(config.scheduled_cleanup_interval_minutes, 15.0)
        self.assertTrue(config.sleep_enabled)
        self.assertEqual(config.sleep_after_minutes, 15.0)
        self.assertTrue(config.scheduled_cleanup_notify)
        self.assertTrue(config.periodic_optimization_notify)
        self.assertEqual(config.auto_close_mode, "off")

    def test_optimal_low_end_preset_uses_lite_mode_and_slower_polling(self) -> None:
        config = apply_optimal_preset(
            AppConfig(),
            HardwareProfile(cpu_cores=2, ram_bytes=4 * 1024**3, disk_kind="hdd"),
        )

        self.assertEqual(config.optimal_preset_tier, "low")
        self.assertTrue(config.lite_mode_enabled)
        self.assertGreaterEqual(config.monitor_interval_seconds, 3.5)
        self.assertGreaterEqual(config.process_refresh_seconds, 12.0)
        self.assertLessEqual(config.cpu_optimizer_max_processes, 2)
        self.assertEqual(config.auto_close_mode, "off")

    def test_auto_cleanup_settings_are_clamped(self) -> None:
        config = sanitize_config(
            AppConfig(
                auto_cleanup_cooldown_minutes=0.1,
                cleanup_logs_older_than_days=0,
                cleanup_prefetch_enabled=True,
                cleanup_recycle_bin_enabled=True,
            )
        )
        self.assertGreaterEqual(config.auto_cleanup_cooldown_minutes, 3.0)
        self.assertGreaterEqual(config.cleanup_logs_older_than_days, 1)
        self.assertTrue(config.cleanup_prefetch_enabled)
        self.assertTrue(config.cleanup_recycle_bin_enabled)

    def test_lite_mode_uses_slower_polling(self) -> None:
        config = sanitize_config(
            AppConfig(
                lite_mode_enabled=True,
                monitor_interval_seconds=1.0,
                process_refresh_seconds=2.0,
                cpu_optimizer_max_processes=8,
            )
        )
        self.assertGreaterEqual(config.monitor_interval_seconds, 3.5)
        self.assertGreaterEqual(config.process_refresh_seconds, 12.0)
        self.assertLessEqual(config.cpu_optimizer_max_processes, 2)

    def test_autostart_command_uses_tray_flag(self) -> None:
        self.assertIn("--tray", get_launch_command())

    def test_update_repository_placeholders_are_not_configured(self) -> None:
        self.assertFalse(is_repository_configured("YOUR_GITHUB_OWNER", "YOUR_GITHUB_REPO"))
        self.assertTrue(is_repository_configured("owner", "repo"))

    def test_update_version_compare(self) -> None:
        self.assertTrue(is_newer_version("v1.2.1", "1.2.0"))
        self.assertFalse(is_newer_version("1.2.0", "1.2.0"))


class WhitelistTests(unittest.TestCase):
    def test_default_system_process_is_protected(self) -> None:
        whitelist = Whitelist(AppConfig())
        self.assertTrue(whitelist.is_whitelisted("csrss.exe"))

    def test_user_path_is_protected(self) -> None:
        config = AppConfig()
        whitelist = Whitelist(config)
        whitelist.add_path(r"C:\Apps\demo.exe")
        self.assertTrue(whitelist.is_whitelisted("demo.exe", r"C:\Apps\demo.exe"))


class CleanupTests(unittest.TestCase):
    def test_cleanup_rejects_unsafe_root(self) -> None:
        whitelist = Whitelist(AppConfig())
        optimizer = SystemOptimizer(whitelist)
        result = optimizer.cleanup_temp_files(roots=[Path.cwd()], dry_run=True)
        self.assertGreaterEqual(len(result.errors), 1)

    def test_cleanup_dry_run_counts_temp_file(self) -> None:
        whitelist = Whitelist(AppConfig())
        optimizer = SystemOptimizer(whitelist)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "sample.tmp"
            file_path.write_bytes(b"12345")
            original_get_roots = optimizer.get_safe_temp_roots
            optimizer.get_safe_temp_roots = lambda: [root]  # type: ignore[method-assign]
            try:
                result = optimizer.cleanup_temp_files(roots=[root], dry_run=True)
            finally:
                optimizer.get_safe_temp_roots = original_get_roots  # type: ignore[method-assign]
            self.assertEqual(result.deleted_files, 1)
            self.assertTrue(file_path.exists())
            self.assertIn("Temp", result.categories)

    def test_cleanup_targets_respect_disabled_categories(self) -> None:
        config = AppConfig(
            cleanup_temp_enabled=False,
            cleanup_browser_cache_enabled=False,
            cleanup_windows_temp_enabled=False,
            cleanup_logs_enabled=False,
        )
        whitelist = Whitelist(config)
        optimizer = SystemOptimizer(whitelist, config)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            temp_root = root / "temp"
            browser_root = root / "browser"
            windows_root = root / "windows-temp"
            for path in (temp_root, browser_root, windows_root):
                path.mkdir()
            original_get_roots = optimizer.get_safe_temp_roots
            original_browser_targets = optimizer._browser_cache_targets
            optimizer.get_safe_temp_roots = lambda: [temp_root]  # type: ignore[method-assign]
            optimizer._browser_cache_targets = lambda: [  # type: ignore[method-assign]
                CleanupTarget(browser_root, "Browser cache"),
                CleanupTarget(windows_root, "Windows temp"),
            ]
            try:
                self.assertEqual(optimizer.get_safe_cleanup_targets(), [])
            finally:
                optimizer.get_safe_temp_roots = original_get_roots  # type: ignore[method-assign]
                optimizer._browser_cache_targets = original_browser_targets  # type: ignore[method-assign]

    def test_cleanup_scan_yields_between_batches(self) -> None:
        whitelist = Whitelist(AppConfig())
        optimizer = SystemOptimizer(whitelist)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(3):
                (root / f"sample-{index}.tmp").write_bytes(b"12345")
            original_get_roots = optimizer.get_safe_temp_roots
            optimizer.get_safe_temp_roots = lambda: [root]  # type: ignore[method-assign]
            pauses: list[float] = []
            try:
                plan = optimizer.scan_cleanup_files(
                    targets=[CleanupTarget(root, "Temp")],
                    batch_size=1,
                    pause_seconds=0.01,
                    sleep_func=pauses.append,
                )
            finally:
                optimizer.get_safe_temp_roots = original_get_roots  # type: ignore[method-assign]
            self.assertEqual(plan.file_count, 3)
            self.assertGreaterEqual(len(pauses), 2)


class UpdaterSafetyTests(unittest.TestCase):
    def test_replacement_script_waits_with_timeout_and_retries_locked_exe(self) -> None:
        script = _replacement_script(
            current=Path(r"C:\Apps\PC Optimizer Lite\PC Optimizer Lite.exe"),
            staged=Path(r"C:\Apps\PC Optimizer Lite\PC Optimizer Lite_new.exe"),
            pid=4242,
        )

        self.assertIn("$deadline", script)
        self.assertIn("Timed out waiting for process 4242", script)
        self.assertIn("for ($attempt = 1", script)
        self.assertIn("Move-Item -LiteralPath $new", script)
        self.assertIn("$backup", script)
        self.assertIn("Get-ProcessesByExecutablePath", script)
        self.assertIn("Stop-ProcessesByExecutablePath $backup", script)
        self.assertIn("pc_optimizer_lite_update_cleanup", script)
        self.assertIn("$removed = $false", script)
        self.assertIn("for ($attempt = 1; $attempt -le 120", script)
        self.assertIn("Backup cleanup deferred", script)
        self.assertIn("Rename-Item -LiteralPath $old", script)
        self.assertIn("-ErrorAction Stop", script)
        self.assertIn("$replaced = $true", script)
        self.assertIn("Update replacement failed", script)
        self.assertLess(script.index("if (-not $replaced)"), script.index("Start-Process"))


class PageFileTests(unittest.TestCase):
    def test_recommendation_enables_windows_auto_management_when_pagefile_disabled_on_low_ram(self) -> None:
        advice = recommend_pagefile_action(
            PageFileStatus(
                total_ram_bytes=4 * 1024**3,
                pagefile_total_bytes=0,
                pagefile_used_bytes=0,
                pagefile_percent=0.0,
                automatic_managed=False,
            )
        )

        self.assertEqual(advice.action, "enable_auto")
        self.assertTrue(advice.requires_admin)
        self.assertIn("автоматическое управление", advice.title.lower())

    def test_recommendation_is_read_only_when_windows_already_manages_pagefile(self) -> None:
        advice = recommend_pagefile_action(
            PageFileStatus(
                total_ram_bytes=16 * 1024**3,
                pagefile_total_bytes=8 * 1024**3,
                pagefile_used_bytes=1 * 1024**3,
                pagefile_percent=12.5,
                automatic_managed=True,
            )
        )

        self.assertEqual(advice.action, "none")
        self.assertFalse(advice.requires_admin)

    def test_enable_auto_pagefile_command_uses_windows_system_setting(self) -> None:
        command = build_enable_auto_pagefile_command()
        self.assertIn("Win32_ComputerSystem", command)
        self.assertIn("AutomaticManagedPagefile=$true", command)


class InstallerTests(unittest.TestCase):
    def test_perform_install_copies_onedir_payload_and_removes_stale_update_files(self) -> None:
        from installer import installer_app

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = root / "payload"
            internal = payload / "_internal"
            internal.mkdir(parents=True)
            (payload / "PC Optimizer Lite.exe").write_bytes(b"new exe")
            (internal / "python312.dll").write_bytes(b"dll")
            local_app_data = root / "LocalAppData"
            app_data = root / "AppData"
            install_dir = local_app_data / "Programs" / "PC Optimizer Lite"
            install_dir.mkdir(parents=True)
            (install_dir / "PC Optimizer Lite_new.exe").write_bytes(b"stale")
            (install_dir / "apply_pc_optimizer_lite_update.ps1").write_text("stale", encoding="utf-8")

            old_local = os.environ.get("LOCALAPPDATA")
            old_appdata = os.environ.get("APPDATA")
            original_resource_path = installer_app.resource_path
            original_create_shortcut = installer_app.create_shortcut
            original_set_run_value = installer_app.set_run_value
            original_write_uninstaller = installer_app.write_uninstaller
            original_register_uninstall = installer_app.register_uninstall
            original_stop_installed_copy = installer_app.stop_installed_copy
            original_desktop_shortcut = installer_app.desktop_shortcut
            original_start_menu_dir = installer_app.start_menu_dir
            try:
                os.environ["LOCALAPPDATA"] = str(local_app_data)
                os.environ["APPDATA"] = str(app_data)
                installer_app.resource_path = lambda relative: payload if relative == "payload" else payload / relative  # type: ignore[assignment]
                installer_app.create_shortcut = lambda *_args, **_kwargs: None  # type: ignore[assignment]
                installer_app.set_run_value = lambda *_args, **_kwargs: None  # type: ignore[assignment]
                installer_app.write_uninstaller = lambda target_dir: target_dir / "Uninstall.ps1"  # type: ignore[assignment]
                installer_app.register_uninstall = lambda *_args, **_kwargs: None  # type: ignore[assignment]
                installer_app.stop_installed_copy = lambda *_args, **_kwargs: None  # type: ignore[assignment]
                installer_app.desktop_shortcut = lambda: root / "Desktop" / "PC Optimizer Lite.lnk"  # type: ignore[assignment]
                installer_app.start_menu_dir = lambda: root / "StartMenu" / "PC Optimizer Lite"  # type: ignore[assignment]

                target = installer_app.perform_install(create_desktop=False, autostart=False)
            finally:
                if old_local is None:
                    os.environ.pop("LOCALAPPDATA", None)
                else:
                    os.environ["LOCALAPPDATA"] = old_local
                if old_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = old_appdata
                installer_app.resource_path = original_resource_path  # type: ignore[assignment]
                installer_app.create_shortcut = original_create_shortcut  # type: ignore[assignment]
                installer_app.set_run_value = original_set_run_value  # type: ignore[assignment]
                installer_app.write_uninstaller = original_write_uninstaller  # type: ignore[assignment]
                installer_app.register_uninstall = original_register_uninstall  # type: ignore[assignment]
                installer_app.stop_installed_copy = original_stop_installed_copy  # type: ignore[assignment]
                installer_app.desktop_shortcut = original_desktop_shortcut  # type: ignore[assignment]
                installer_app.start_menu_dir = original_start_menu_dir  # type: ignore[assignment]

            self.assertEqual(target, install_dir / "PC Optimizer Lite.exe")
            self.assertEqual(target.read_bytes(), b"new exe")
            self.assertTrue((install_dir / "_internal" / "python312.dll").exists())
            self.assertFalse((install_dir / "PC Optimizer Lite_new.exe").exists())
            self.assertFalse((install_dir / "apply_pc_optimizer_lite_update.ps1").exists())


class SleepManagerTests(unittest.TestCase):
    def test_browser_names_are_not_absolute_sleep_skips(self) -> None:
        self.assertNotIn("msedge.exe", SKIP_SLEEP_NAMES)
        self.assertNotIn("chrome.exe", SKIP_SLEEP_NAMES)
        self.assertNotIn("firefox.exe", SKIP_SLEEP_NAMES)

    def test_focus_usage_stats_track_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig()
            manager = SleepManager(Whitelist(config), HistoryManager(path=Path(tmp) / "history.json"))
            manager._record_focus_transition(10, 100.0)  # noqa: SLF001
            manager._record_focus_transition(20, 160.0)  # noqa: SLF001
            stats = manager.usage_stats.get(10)
            self.assertIsNotNone(stats)
            self.assertEqual(stats.focus_count, 1)  # type: ignore[union-attr]
            self.assertEqual(stats.total_focus_seconds, 60.0)  # type: ignore[union-attr]


class HistoryTests(unittest.TestCase):
    def test_closed_process_history_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            history = HistoryManager(path=path)
            entry = history.add_closed_process(
                pid=123,
                name="demo.exe",
                exe=r"C:\Apps\demo.exe",
                reason="test",
                mode="manual",
            )
            reloaded = HistoryManager(path=path)
            self.assertEqual(reloaded.get_closed_process(entry.id).name, "demo.exe")  # type: ignore[union-attr]


class SmartProcessTests(unittest.TestCase):
    def test_whitelisted_process_is_not_close_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(user_whitelist_names=["demo.exe"])
            whitelist = Whitelist(config)
            history = HistoryManager(path=Path(tmp) / "history.json")
            manager = SmartProcessManager(whitelist, history)
            process = ProcessInfo(
                pid=999999,
                name="demo.exe",
                exe="",
                username="",
                status="running",
                cpu_percent=90.0,
                memory_percent=50.0,
                memory_rss=1,
                priority="normal",
            )
            candidates = manager.find_candidates(
                [process],
                min_background_minutes=1.0,
                cpu_threshold=1.0,
                memory_threshold=1.0,
                duplicate_count=2,
            )
            self.assertEqual(candidates, [])


class SystemOptimizerInputGuardTests(unittest.TestCase):
    def test_foreground_process_table_entry_is_not_priority_candidate(self) -> None:
        config = AppConfig()
        optimizer = SystemOptimizer(Whitelist(config), config)
        foreground = ProcessInfo(
            pid=4001,
            name="Telegram.exe",
            exe=r"C:\Users\User\AppData\Roaming\Telegram Desktop\Telegram.exe",
            username="",
            status="running",
            cpu_percent=95.0,
            memory_percent=3.0,
            memory_rss=1024,
            priority="normal",
            has_window=True,
            is_foreground_related=True,
        )
        worker = ProcessInfo(
            pid=4002,
            name="worker.exe",
            exe=r"C:\Apps\worker.exe",
            username="",
            status="running",
            cpu_percent=80.0,
            memory_percent=3.0,
            memory_rss=1024,
            priority="normal",
            has_window=False,
            is_foreground_related=False,
        )

        candidates = optimizer.suggest_heavy_processes([foreground, worker], cpu_percent=20.0, limit=5)

        self.assertEqual([item.pid for item in candidates], [4002])

    def test_lower_priority_refuses_foreground_related_process(self) -> None:
        import pc_optimizer_lite.optimizer as optimizer_module

        class FakeProcess:
            pid = 5001

            def __init__(self, pid: int) -> None:
                self.pid = pid
                self.nice_calls: list[int] = []

            def name(self) -> str:
                return "Discord.exe"

            def exe(self) -> str:
                return r"C:\Users\User\AppData\Local\Discord\Discord.exe"

            def nice(self, value: int | None = None) -> int:
                if value is not None:
                    self.nice_calls.append(value)
                return 0

        class FakePsutil:
            BELOW_NORMAL_PRIORITY_CLASS = 0x4000
            AccessDenied = Exception
            NoSuchProcess = Exception
            ZombieProcess = Exception

            def __init__(self) -> None:
                self.proc = FakeProcess(5001)

            def Process(self, pid: int) -> FakeProcess:  # noqa: N802 - psutil-style API
                self.proc.pid = pid
                return self.proc

        fake_psutil = FakePsutil()
        original_psutil = optimizer_module.psutil
        original_get_foreground_pid = getattr(optimizer_module, "get_foreground_pid", None)
        original_is_related_to_pid = getattr(optimizer_module, "is_related_to_pid", None)
        optimizer_module.psutil = fake_psutil  # type: ignore[assignment]
        optimizer_module.get_foreground_pid = lambda: 5001  # type: ignore[attr-defined]
        optimizer_module.is_related_to_pid = lambda pid, active_pid: pid == active_pid  # type: ignore[attr-defined]
        try:
            action = SystemOptimizer(Whitelist(AppConfig())).lower_priority_for_process(5001)
        finally:
            optimizer_module.psutil = original_psutil  # type: ignore[assignment]
            if original_get_foreground_pid is None:
                delattr(optimizer_module, "get_foreground_pid")
            else:
                optimizer_module.get_foreground_pid = original_get_foreground_pid  # type: ignore[attr-defined]
            if original_is_related_to_pid is None:
                delattr(optimizer_module, "is_related_to_pid")
            else:
                optimizer_module.is_related_to_pid = original_is_related_to_pid  # type: ignore[attr-defined]

        self.assertFalse(action.success)
        self.assertIn("active", action.message.lower())
        self.assertEqual(fake_psutil.proc.nice_calls, [])


class SystemMonitorStartupTests(unittest.TestCase):
    def test_startup_grace_defers_first_process_collection(self) -> None:
        monitor = SystemMonitor(interval_seconds=2.0, process_refresh_seconds=2.0, startup_grace_seconds=5.0)
        monitor.set_process_collection_enabled(True)
        monitor._started_at = 100.0  # noqa: SLF001
        monitor._last_process_refresh = 0.0  # noqa: SLF001
        self.assertFalse(monitor._should_collect_processes(now=103.0))  # noqa: SLF001
        self.assertTrue(monitor._should_collect_processes(now=106.0))  # noqa: SLF001


class OneClickOptimizationTests(unittest.TestCase):
    def test_optimization_step_flag_disables_step(self) -> None:
        config = AppConfig(optimize_step_cleanup_enabled=False)
        self.assertFalse(_step_enabled(config, "cleanup", eco_mode=False))
        self.assertFalse(_step_enabled(AppConfig(), "cleanup", eco_mode=True))

    def test_active_network_process_is_not_safe_close(self) -> None:
        config = AppConfig(
            auto_close_cpu_threshold_percent=1.0,
            auto_close_memory_threshold_percent=1.0,
            auto_close_min_background_minutes=1.0,
        )
        snapshot = ProcessOptimizationSnapshot(
            pid=12345,
            name="worker.exe",
            exe=r"C:\Apps\worker.exe",
            username=r"PC\User",
            cpu_percent=80.0,
            memory_percent=20.0,
            rss=1024,
            priority=None,
            has_window=False,
            is_foreground_related=False,
            active_network=True,
            active_audio_hint=False,
            hung_window=False,
            age_seconds=3600.0,
            last_focus_age_seconds=None,
        )
        plan = _classify_processes(config, [snapshot])
        self.assertEqual(plan.safe_close, [])
        self.assertEqual(plan.lower_priority, [snapshot])


class RamCleanerTests(unittest.TestCase):
    def test_ram_clean_result_freed_bytes(self) -> None:
        result = RamCleanResult(
            mode=RamCleanMode.LIGHT,
            ram_used_before=2_000,
            ram_used_after=1_250,
            ram_percent_before=80.0,
            ram_percent_after=50.0,
        )
        self.assertEqual(result.freed_bytes, 750)


class CpuThrottlerTests(unittest.TestCase):
    def test_normal_cpu_does_not_iterate_processes(self) -> None:
        import pc_optimizer_lite.cpu_throttler as cpu_throttler_module

        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(cpu_throttle_enabled=True, observation_only_mode=False)
            whitelist = Whitelist(config)
            history = HistoryManager(path=Path(tmp) / "history.json")
            throttler = CpuThrottler(whitelist, history)
            snapshot = MonitorSnapshot(
                timestamp=0.0,
                cpu_percent=10.0,
                per_core_cpu_percent=[10.0],
                memory=MemoryInfo(total=1, available=1, used=0, percent=10.0),
                swap=SwapInfo(total=0, used=0, free=0, percent=0.0),
                disks=[],
                disk_io=DiskIOInfo(),
            )
            original = cpu_throttler_module.psutil.process_iter

            def fail_process_iter(*_: object, **__: object) -> list[object]:
                raise AssertionError("process_iter must not run while total CPU is normal")

            cpu_throttler_module.psutil.process_iter = fail_process_iter  # type: ignore[assignment]
            try:
                self.assertEqual(throttler.observe(snapshot, config), [])
            finally:
                cpu_throttler_module.psutil.process_iter = original  # type: ignore[assignment]

    def test_whitelist_is_not_throttle_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(user_whitelist_names=["protected.exe"])
            whitelist = Whitelist(config)
            history = HistoryManager(path=Path(tmp) / "history.json")
            throttler = CpuThrottler(whitelist, history)
            protected = ProcessInfo(
                pid=777001,
                name="protected.exe",
                exe="",
                username="",
                status="running",
                cpu_percent=99.0,
                memory_percent=5.0,
                memory_rss=1,
                priority="normal",
            )
            worker = ProcessInfo(
                pid=777002,
                name="worker.exe",
                exe=r"C:\Apps\worker.exe",
                username="",
                status="running",
                cpu_percent=70.0,
                memory_percent=5.0,
                memory_rss=1,
                priority="normal",
            )
            candidates = throttler.select_candidates([protected, worker])
            self.assertEqual([item.name for item in candidates], ["worker.exe"])

    def test_foreground_and_windowed_processes_are_not_throttle_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig()
            whitelist = Whitelist(config)
            history = HistoryManager(path=Path(tmp) / "history.json")
            throttler = CpuThrottler(whitelist, history)
            active_messenger = ProcessInfo(
                pid=777101,
                name="Discord.exe",
                exe=r"C:\Users\User\AppData\Local\Discord\Discord.exe",
                username="",
                status="running",
                cpu_percent=99.0,
                memory_percent=5.0,
                memory_rss=1,
                priority="normal",
                has_window=True,
                is_foreground_related=True,
            )
            background_window = ProcessInfo(
                pid=777102,
                name="Telegram.exe",
                exe=r"C:\Users\User\AppData\Roaming\Telegram Desktop\Telegram.exe",
                username="",
                status="running",
                cpu_percent=88.0,
                memory_percent=5.0,
                memory_rss=1,
                priority="normal",
                has_window=True,
                is_foreground_related=False,
            )
            worker = ProcessInfo(
                pid=777103,
                name="worker.exe",
                exe=r"C:\Apps\worker.exe",
                username="",
                status="running",
                cpu_percent=70.0,
                memory_percent=5.0,
                memory_rss=1,
                priority="normal",
                has_window=False,
                is_foreground_related=False,
            )

            candidates = throttler.select_candidates([active_messenger, background_window, worker])

            self.assertEqual([item.pid for item in candidates], [777103])


class CpuOptimizerTests(unittest.TestCase):
    def test_whitelist_is_not_snapshot_cpu_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(user_whitelist_names=["protected.exe"])
            whitelist = Whitelist(config)
            history = HistoryManager(path=Path(tmp) / "history.json")
            optimizer = CpuOptimizer(whitelist, history)
            protected = ProcessOptimizationSnapshot(
                pid=1001,
                name="protected.exe",
                exe="",
                username="",
                cpu_percent=99.0,
                memory_percent=1.0,
                rss=1,
                priority=None,
                has_window=False,
                is_foreground_related=False,
                active_network=False,
                active_audio_hint=False,
                hung_window=False,
                age_seconds=100.0,
                last_focus_age_seconds=None,
            )
            worker = ProcessOptimizationSnapshot(
                pid=1002,
                name="worker.exe",
                exe=r"C:\Apps\worker.exe",
                username="",
                cpu_percent=50.0,
                memory_percent=1.0,
                rss=1,
                priority=None,
                has_window=False,
                is_foreground_related=False,
                active_network=False,
                active_audio_hint=False,
                hung_window=False,
                age_seconds=100.0,
                last_focus_age_seconds=None,
            )
            self.assertFalse(optimizer._is_candidate(protected, config))  # noqa: SLF001
            self.assertTrue(optimizer._is_candidate(worker, config))  # noqa: SLF001

    def test_windowed_interactive_snapshot_is_not_cpu_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig()
            whitelist = Whitelist(config)
            history = HistoryManager(path=Path(tmp) / "history.json")
            optimizer = CpuOptimizer(whitelist, history)
            telegram = ProcessOptimizationSnapshot(
                pid=2001,
                name="Telegram.exe",
                exe=r"C:\Users\User\AppData\Roaming\Telegram Desktop\Telegram.exe",
                username="",
                cpu_percent=99.0,
                memory_percent=1.0,
                rss=1,
                priority=None,
                has_window=True,
                is_foreground_related=False,
                active_network=False,
                active_audio_hint=False,
                hung_window=False,
                age_seconds=100.0,
                last_focus_age_seconds=600.0,
            )

            self.assertFalse(optimizer._is_candidate(telegram, config))  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
