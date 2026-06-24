"""Core safety tests for PC Optimizer Lite."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pc_optimizer_lite.autostart import get_launch_command
from pc_optimizer_lite.config import AppConfig, apply_automation_mode, load_config, save_config, sanitize_config
from pc_optimizer_lite.cpu_optimizer import CpuOptimizer
from pc_optimizer_lite.cpu_throttler import CpuThrottler
from pc_optimizer_lite.history_manager import HistoryManager
from pc_optimizer_lite.monitor import DiskIOInfo, MemoryInfo, MonitorSnapshot, ProcessInfo, SwapInfo
from pc_optimizer_lite.optimize_action import ProcessOptimizationSnapshot, _classify_processes, _step_enabled
from pc_optimizer_lite.optimizer import SystemOptimizer
from pc_optimizer_lite.ram_cleaner import RamCleanMode, RamCleanResult
from pc_optimizer_lite.smart_process_manager import SmartProcessManager
from pc_optimizer_lite.updater import is_newer_version, is_repository_configured
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
        self.assertEqual(config.auto_close_mode, "auto")

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


if __name__ == "__main__":
    unittest.main()
