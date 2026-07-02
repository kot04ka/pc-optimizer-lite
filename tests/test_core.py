"""Core safety tests for PC Optimizer Lite."""

from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pc_optimizer_lite import updater as updater_module
from pc_optimizer_lite.autostart import get_launch_command
from pc_optimizer_lite.background_load import (
    AUTO_PAUSE_BACKGROUND_LOAD_IDS,
    SAFE_BACKGROUND_LOAD_PRESET,
    BackgroundLoadManager,
)
from pc_optimizer_lite.config import (
    AppConfig,
    HardwareProfile,
    apply_automation_mode,
    apply_optimal_preset,
    export_config,
    import_config,
    load_config,
    save_config,
    sanitize_config,
)
from pc_optimizer_lite.cpu_optimizer import CpuOptimizer
from pc_optimizer_lite.cpu_throttler import CpuThrottler
from pc_optimizer_lite.history_manager import HistoryManager
from pc_optimizer_lite.monitor import DiskIOInfo, MemoryInfo, MonitorSnapshot, ProcessInfo, SwapInfo, SystemMonitor
from pc_optimizer_lite.optimize_action import OptimizationResult, ProcessOptimizationSnapshot, _classify_processes, _step_enabled
from pc_optimizer_lite.optimizer import CleanupTarget, SystemOptimizer
from pc_optimizer_lite.pagefile import (
    PageFileStatus,
    build_enable_auto_pagefile_command,
    recommend_pagefile_action,
)
from pc_optimizer_lite.ram_cleaner import RamCleanMode, RamCleanResult
from pc_optimizer_lite.sleep_manager import SKIP_SLEEP_NAMES, SleepManager
from pc_optimizer_lite.smart_process_manager import SmartProcessManager
from pc_optimizer_lite.updater import (
    _select_windows_installer_asset,
    install_downloaded_update,
    is_newer_version,
    is_repository_configured,
)
from pc_optimizer_lite.visual_effects import VISUAL_EFFECT_SETTINGS, VisualEffectSetting, VisualEffectsManager
from pc_optimizer_lite.whitelist import Whitelist
from pc_optimizer_lite.runtime_policy import sleep_wake_poll_policy
from pc_optimizer_lite.ui_model import (
    DEFAULT_NAV_PAGES,
    PROMPT_DARK_TOKENS,
    SETTINGS_LAYOUT,
    TOPBAR_ACTIONS_LABEL,
    build_design_palette,
    evaluate_system_health,
)


class UiModelTests(unittest.TestCase):
    def test_prompt_navigation_order_and_labels_are_stable(self) -> None:
        self.assertEqual([page.page_id for page in DEFAULT_NAV_PAGES], ["overview", "processes", "activity", "exceptions", "settings"])
        self.assertEqual([page.title for page in DEFAULT_NAV_PAGES], ["Обзор", "Процессы", "Активность", "Исключения", "Настройки"])
        self.assertEqual(DEFAULT_NAV_PAGES[0].topbar_title, "Обзор системы")
        self.assertEqual(DEFAULT_NAV_PAGES[1].topbar_title, "Процессы")

    def test_prompt_dark_tokens_are_used_by_design_palette(self) -> None:
        palette = build_design_palette("dark")

        self.assertEqual(palette["bg"], PROMPT_DARK_TOKENS.background)
        self.assertEqual(palette["panel"], PROMPT_DARK_TOKENS.surface)
        self.assertEqual(palette["panel_2"], PROMPT_DARK_TOKENS.surface_elevated)
        self.assertEqual(palette["panel_hover"], PROMPT_DARK_TOKENS.surface_hover)
        self.assertEqual(palette["accent"], PROMPT_DARK_TOKENS.accent_blue)
        self.assertEqual(palette["accent_hover"], PROMPT_DARK_TOKENS.accent_blue_hover)
        self.assertEqual(palette["good"], PROMPT_DARK_TOKENS.success)

    def test_system_health_status_warns_about_pressure(self) -> None:
        good = evaluate_system_health(cpu_percent=22.0, ram_percent=42.0, disk_percent=54.0, swap_percent=2.0)
        warning = evaluate_system_health(cpu_percent=42.0, ram_percent=72.0, disk_percent=83.0, swap_percent=11.0)
        danger = evaluate_system_health(cpu_percent=94.0, ram_percent=91.0, disk_percent=88.0, swap_percent=76.0)

        self.assertEqual(good.severity, "good")
        self.assertEqual(warning.severity, "warn")
        self.assertEqual(danger.severity, "bad")
        self.assertIn("Нагрузка", danger.title)

    def test_settings_layout_avoids_horizontal_overflow(self) -> None:
        self.assertLessEqual(SETTINGS_LAYOUT.field_max_width, 360)
        self.assertLessEqual(SETTINGS_LAYOUT.nav_width, 168)
        self.assertEqual(SETTINGS_LAYOUT.min_content_width, 0)
        self.assertEqual(TOPBAR_ACTIONS_LABEL, "Действия")


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

    def test_saved_config_contains_schema_version_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            save_config(AppConfig(theme="light"), path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], payload["config_version"])

    def test_broken_config_loads_optimal_preset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{broken json", encoding="utf-8")
            with self.assertLogs("pc_optimizer_lite.config", level="WARNING") as logs:
                loaded = load_config(path)
            self.assertIn("Failed to load config", "\n".join(logs.output))
            self.assertTrue(loaded.optimal_preset_applied)
            self.assertEqual(loaded.automation_mode, "autopilot")

    def test_config_export_import_roundtrip_uses_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings-export.json"
            original = AppConfig(
                theme="light",
                cpu_threshold_percent=250.0,
                user_whitelist_names=["Demo.EXE"],
                automation_mode="manual",
            )
            export_config(original, path)
            loaded = import_config(path)
            self.assertEqual(loaded.theme, "light")
            self.assertEqual(loaded.cpu_threshold_percent, 100.0)
            self.assertIn("demo.exe", loaded.user_whitelist_names)
            self.assertEqual(loaded.automation_mode, "manual")

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
        self.assertTrue(config.visual_effects_low_power_enabled)
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

    def test_ultra_lite_mode_uses_very_slow_polling(self) -> None:
        config = sanitize_config(
            AppConfig(
                ultra_lite_mode_enabled=True,
                monitor_interval_seconds=1.0,
                process_refresh_seconds=2.0,
                cpu_optimizer_max_processes=8,
            )
        )

        self.assertGreaterEqual(config.monitor_interval_seconds, 10.0)
        self.assertGreaterEqual(config.process_refresh_seconds, 30.0)
        self.assertLessEqual(config.cpu_optimizer_max_processes, 1)

    def test_background_load_settings_are_sanitized(self) -> None:
        config = sanitize_config(
            AppConfig(
                background_load_control_enabled=True,
                background_load_restore_on_exit=False,
                background_load_auto_on_load=True,
                background_load_disabled_ids=["windows_widgets", 42, "game_dvr"],
                background_load_auto_pause_enabled=True,
                background_load_pause_cpu_threshold_percent=250.0,
                background_load_pause_idle_seconds=1.0,
                background_load_pause_cooldown_seconds=5.0,
            )
        )

        self.assertTrue(config.background_load_control_enabled)
        self.assertFalse(config.background_load_restore_on_exit)
        self.assertTrue(config.background_load_auto_on_load)
        self.assertEqual(config.background_load_disabled_ids, ["windows_widgets", "game_dvr"])
        self.assertTrue(config.background_load_auto_pause_enabled)
        self.assertEqual(config.background_load_pause_cpu_threshold_percent, 100.0)
        self.assertGreaterEqual(config.background_load_pause_idle_seconds, 10.0)
        self.assertGreaterEqual(config.background_load_pause_cooldown_seconds, 60.0)

    def test_optimal_low_end_preset_enables_background_load_relief(self) -> None:
        config = apply_optimal_preset(
            AppConfig(),
            HardwareProfile(cpu_cores=2, ram_bytes=4 * 1024**3, disk_kind="hdd"),
        )

        self.assertTrue(config.background_load_control_enabled)
        self.assertTrue(config.background_load_auto_pause_enabled)
        self.assertTrue(config.ultra_lite_mode_enabled)
        self.assertEqual(
            set(config.background_load_disabled_ids),
            set(SAFE_BACKGROUND_LOAD_PRESET) | set(AUTO_PAUSE_BACKGROUND_LOAD_IDS),
        )

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
    def test_selects_setup_installer_asset_and_uses_release_hash(self) -> None:
        release_hash = "a" * 64
        setup_hash = "b" * 64
        asset = _select_windows_installer_asset(
            [
                {
                    "name": "PC-Optimizer-Lite.exe",
                    "browser_download_url": "https://example.invalid/onefile.exe",
                    "size": 10,
                },
                {
                    "name": "PC-Optimizer-Lite-Setup.exe",
                    "browser_download_url": "https://example.invalid/setup.exe",
                    "size": 20,
                },
                {
                    "name": "PC-Optimizer-Lite-windows-x64.zip",
                    "browser_download_url": "https://example.invalid/onedir.zip",
                    "size": 30,
                },
            ],
            (
                f"PC-Optimizer-Lite-windows-x64.zip sha256: {release_hash}\n"
                f"PC-Optimizer-Lite-Setup.exe sha256: {setup_hash}\n"
            ),
        )

        self.assertIsNotNone(asset)
        self.assertEqual(asset.name, "PC-Optimizer-Lite-Setup.exe")  # type: ignore[union-attr]
        self.assertEqual(asset.url, "https://example.invalid/setup.exe")  # type: ignore[union-attr]
        self.assertEqual(asset.size, 20)  # type: ignore[union-attr]
        self.assertEqual(asset.sha256, setup_hash)  # type: ignore[union-attr]

    def test_install_downloaded_update_launches_silent_setup_from_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_dir = root / "PC Optimizer Lite"
            install_dir.mkdir()
            current_exe = install_dir / "PC Optimizer Lite.exe"
            current_exe.write_bytes(b"old")
            setup = root / "PC-Optimizer-Lite-Setup.exe"
            setup.write_bytes(b"new")
            calls: dict[str, object] = {}

            original_popen = updater_module.subprocess.Popen
            original_chdir = updater_module.os.chdir

            def fake_popen(args: list[str], **kwargs: object) -> object:
                calls["args"] = args
                calls["kwargs"] = kwargs
                return object()

            def fake_chdir(path: str | os.PathLike[str]) -> None:
                calls["chdir"] = Path(path)

            try:
                updater_module.subprocess.Popen = fake_popen  # type: ignore[assignment]
                updater_module.os.chdir = fake_chdir  # type: ignore[assignment]
                launched = install_downloaded_update(setup, current_exe=current_exe)
            finally:
                updater_module.subprocess.Popen = original_popen  # type: ignore[assignment]
                updater_module.os.chdir = original_chdir  # type: ignore[assignment]

            self.assertEqual(launched, setup)
            self.assertFalse((install_dir / "apply_pc_optimizer_lite_update.ps1").exists())
            self.assertEqual(calls["chdir"], Path(tempfile.gettempdir()))
            args = calls["args"]
            self.assertIsInstance(args, list)
            self.assertEqual(args[0], str(setup))  # type: ignore[index]
            self.assertIn("/VERYSILENT", args)  # type: ignore[arg-type]
            self.assertIn("/SUPPRESSMSGBOXES", args)  # type: ignore[arg-type]
            self.assertIn("/NORESTART", args)  # type: ignore[arg-type]
            kwargs = calls["kwargs"]
            self.assertIsInstance(kwargs, dict)
            self.assertEqual(kwargs.get("cwd"), tempfile.gettempdir())  # type: ignore[union-attr]

    def test_updater_does_not_generate_self_replacement_script(self) -> None:
        root = Path(__file__).resolve().parents[1]
        updater_source = (root / "pc_optimizer_lite" / "updater.py").read_text(encoding="utf-8")

        self.assertNotIn("apply_pc_optimizer_lite_update.ps1", updater_source)
        self.assertNotIn("Rename-Item -LiteralPath $appDir", updater_source)
        self.assertNotIn("Move-Item -LiteralPath $newAppDir", updater_source)
        self.assertNotIn("Update directory replacement failed", updater_source)


class PackagingTests(unittest.TestCase):
    def test_build_script_builds_only_onedir_app_and_zip_asset(self) -> None:
        root = Path(__file__).resolve().parents[1]
        build_script = (root / "build.ps1").read_text(encoding="utf-8")

        self.assertNotIn("--onefile", build_script)
        self.assertNotIn("PC Optimizer Lite.spec", build_script)
        self.assertIn("PC Optimizer Lite Onedir.spec", build_script)
        self.assertIn("PC-Optimizer-Lite-windows-x64.zip", build_script)

    def test_github_release_uploads_onedir_zip_not_portable_exe(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")

        self.assertIn("PC-Optimizer-Lite-windows-x64.zip", workflow)
        self.assertIn("PC-Optimizer-Lite-Setup.exe", workflow)
        self.assertNotIn("PC-Optimizer-Lite.exe", workflow)

    def test_inno_installer_supports_silent_update_flow(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "installer" / "PC Optimizer Lite.iss").read_text(encoding="utf-8")

        self.assertIn("AppId={{6E9D6384-2E39-420D-A63B-2C195A5C5A09}", script)
        self.assertIn(r"DefaultDirName={localappdata}\Programs\{#MyAppName}", script)
        self.assertIn("CloseApplications=yes", script)
        self.assertIn("RestartApplications=yes", script)
        self.assertIn('Filename: "{app}\\{#MyAppExeName}"; Flags: nowait skipifnotsilent', script)
        self.assertNotIn("RestartApplications=no", script)

    def test_project_does_not_disable_windows_defender_or_antivirus(self) -> None:
        root = Path(__file__).resolve().parents[1]
        banned_tokens = (
            "DisableRealtimeMonitoring",
            "Set-MpPreference",
            "Add-MpPreference",
        )
        skipped_dirs = {".git", ".venv", "__pycache__", "build", "dist", "installer_output", "tests"}
        scanned = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in root.rglob("*")
            if path.is_file()
            and not any(part in skipped_dirs for part in path.parts)
            and path.suffix.lower() in {".py", ".ps1", ".iss", ".yml", ".yaml", ".bat", ".toml", ".spec"}
        )

        for token in banned_tokens:
            self.assertNotIn(token, scanned)


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
    def test_cleanup_pyinstaller_temp_removes_only_mei_directories(self) -> None:
        from installer import installer_app

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mei = root / "_MEI12345"
            other = root / "ordinary-temp"
            mei.mkdir()
            other.mkdir()
            (mei / "python311.dll").write_bytes(b"stale")
            (other / "keep.txt").write_text("keep", encoding="utf-8")

            installer_app.cleanup_pyinstaller_temp(root)

            self.assertFalse(mei.exists())
            self.assertTrue(other.exists())
            self.assertTrue((other / "keep.txt").exists())

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
            old_internal = install_dir / "_internal"
            old_internal.mkdir()
            (install_dir / "PC Optimizer Lite_new.exe").write_bytes(b"stale")
            (install_dir / "apply_pc_optimizer_lite_update.ps1").write_text("stale", encoding="utf-8")
            (install_dir / "old_only.dll").write_bytes(b"old")
            (old_internal / "python311.dll").write_bytes(b"old dll")

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
            self.assertFalse((install_dir / "_internal" / "python311.dll").exists())
            self.assertFalse((install_dir / "old_only.dll").exists())
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

    def test_windowed_process_uses_priority_sleep_without_suspend(self) -> None:
        import pc_optimizer_lite.sleep_manager as sleep_module

        class FakeProcess:
            pid = 9101

            def __init__(self) -> None:
                self.nice_calls: list[int] = []
                self.suspend_calls = 0

            def name(self) -> str:
                return "notepad.exe"

            def exe(self) -> str:
                return r"C:\Windows\System32\notepad.exe"

            def nice(self, value: int | None = None) -> int:
                if value is not None:
                    self.nice_calls.append(value)
                return 32

            def suspend(self) -> None:
                self.suspend_calls += 1

            def io_counters(self):
                return type("IOCounters", (), {"read_bytes": 0, "write_bytes": 0})()

            def net_connections(self, kind: str = "inet") -> list[object]:
                return []

        class FakePsutil:
            IDLE_PRIORITY_CLASS = 0x40
            NORMAL_PRIORITY_CLASS = 0x20
            AccessDenied = Exception
            NoSuchProcess = Exception
            ZombieProcess = Exception

            def __init__(self, proc: FakeProcess) -> None:
                self.proc = proc

            def Process(self, pid: int) -> FakeProcess:  # noqa: N802 - psutil-style API
                self.proc.pid = pid
                return self.proc

        with tempfile.TemporaryDirectory() as tmp:
            fake_proc = FakeProcess()
            original_psutil = sleep_module.psutil
            original_get_foreground_pid = sleep_module.get_foreground_pid
            original_get_visible_window_pids = sleep_module.get_visible_window_pids
            sleep_module.psutil = FakePsutil(fake_proc)  # type: ignore[assignment]
            sleep_module.get_foreground_pid = lambda: None  # type: ignore[assignment]
            sleep_module.get_visible_window_pids = lambda: {9101}  # type: ignore[assignment]
            try:
                manager = SleepManager(Whitelist(AppConfig()), HistoryManager(path=Path(tmp) / "history.json"))
                manager._last_io_by_pid[9101] = (0, 0)  # noqa: SLF001 - seed stable IO for this policy test.
                action = manager.sleep_process(9101, "test")
            finally:
                sleep_module.psutil = original_psutil  # type: ignore[assignment]
                sleep_module.get_foreground_pid = original_get_foreground_pid  # type: ignore[assignment]
                sleep_module.get_visible_window_pids = original_get_visible_window_pids  # type: ignore[assignment]

            self.assertIsNotNone(action)
            self.assertTrue(action.success)  # type: ignore[union-attr]
            self.assertEqual(fake_proc.nice_calls, [0x40])
            self.assertEqual(fake_proc.suspend_calls, 0)
            self.assertEqual(manager.sleeping[0].strategy, "priority")
            self.assertFalse(manager.sleeping[0].suspended)

    def test_headless_safe_process_uses_deep_suspend(self) -> None:
        import pc_optimizer_lite.sleep_manager as sleep_module

        class FakeProcess:
            pid = 9201

            def __init__(self) -> None:
                self.nice_calls: list[int] = []
                self.suspend_calls = 0

            def name(self) -> str:
                return "worker.exe"

            def exe(self) -> str:
                return r"C:\Apps\worker.exe"

            def nice(self, value: int | None = None) -> int:
                if value is not None:
                    self.nice_calls.append(value)
                return 32

            def suspend(self) -> None:
                self.suspend_calls += 1

            def io_counters(self):
                return type("IOCounters", (), {"read_bytes": 0, "write_bytes": 0})()

            def net_connections(self, kind: str = "inet") -> list[object]:
                return []

        class FakePsutil:
            IDLE_PRIORITY_CLASS = 0x40
            NORMAL_PRIORITY_CLASS = 0x20
            CONN_ESTABLISHED = "ESTABLISHED"
            AccessDenied = Exception
            NoSuchProcess = Exception
            ZombieProcess = Exception

            def __init__(self, proc: FakeProcess) -> None:
                self.proc = proc

            def Process(self, pid: int) -> FakeProcess:  # noqa: N802 - psutil-style API
                self.proc.pid = pid
                return self.proc

        with tempfile.TemporaryDirectory() as tmp:
            fake_proc = FakeProcess()
            original_psutil = sleep_module.psutil
            original_get_foreground_pid = sleep_module.get_foreground_pid
            original_get_visible_window_pids = sleep_module.get_visible_window_pids
            sleep_module.psutil = FakePsutil(fake_proc)  # type: ignore[assignment]
            sleep_module.get_foreground_pid = lambda: None  # type: ignore[assignment]
            sleep_module.get_visible_window_pids = lambda: set()  # type: ignore[assignment]
            try:
                manager = SleepManager(Whitelist(AppConfig()), HistoryManager(path=Path(tmp) / "history.json"))
                manager._last_io_by_pid[9201] = (0, 0)  # noqa: SLF001 - seed stable IO for this policy test.
                action = manager.sleep_process(9201, "test")
            finally:
                sleep_module.psutil = original_psutil  # type: ignore[assignment]
                sleep_module.get_foreground_pid = original_get_foreground_pid  # type: ignore[assignment]
                sleep_module.get_visible_window_pids = original_get_visible_window_pids  # type: ignore[assignment]

            self.assertIsNotNone(action)
            self.assertTrue(action.success)  # type: ignore[union-attr]
            self.assertEqual(fake_proc.nice_calls, [0x40])
            self.assertEqual(fake_proc.suspend_calls, 1)
            self.assertEqual(manager.sleeping[0].strategy, "suspend")
            self.assertTrue(manager.sleeping[0].suspended)

    def test_active_network_process_is_not_slept(self) -> None:
        import pc_optimizer_lite.sleep_manager as sleep_module

        class FakeProcess:
            pid = 9301

            def __init__(self) -> None:
                self.nice_calls: list[int] = []
                self.suspend_calls = 0

            def name(self) -> str:
                return "sync-worker.exe"

            def exe(self) -> str:
                return r"C:\Apps\sync-worker.exe"

            def nice(self, value: int | None = None) -> int:
                if value is not None:
                    self.nice_calls.append(value)
                return 32

            def suspend(self) -> None:
                self.suspend_calls += 1

            def net_connections(self, kind: str = "inet") -> list[object]:
                return [SimpleNamespace(status="ESTABLISHED")]

        class FakePsutil:
            IDLE_PRIORITY_CLASS = 0x40
            NORMAL_PRIORITY_CLASS = 0x20
            CONN_ESTABLISHED = "ESTABLISHED"
            AccessDenied = Exception
            NoSuchProcess = Exception
            ZombieProcess = Exception

            def __init__(self, proc: FakeProcess) -> None:
                self.proc = proc

            def Process(self, pid: int) -> FakeProcess:  # noqa: N802 - psutil-style API
                self.proc.pid = pid
                return self.proc

        with tempfile.TemporaryDirectory() as tmp:
            fake_proc = FakeProcess()
            original_psutil = sleep_module.psutil
            original_get_foreground_pid = sleep_module.get_foreground_pid
            sleep_module.psutil = FakePsutil(fake_proc)  # type: ignore[assignment]
            sleep_module.get_foreground_pid = lambda: None  # type: ignore[assignment]
            try:
                manager = SleepManager(Whitelist(AppConfig()), HistoryManager(path=Path(tmp) / "history.json"))
                action = manager.sleep_process(9301, "test", has_visible_window=False)
            finally:
                sleep_module.psutil = original_psutil  # type: ignore[assignment]
                sleep_module.get_foreground_pid = original_get_foreground_pid  # type: ignore[assignment]

            self.assertIsNone(action)
            self.assertEqual(fake_proc.nice_calls, [])
            self.assertEqual(fake_proc.suspend_calls, 0)
            self.assertEqual(manager.sleeping, [])

    def test_media_process_name_is_not_slept(self) -> None:
        import pc_optimizer_lite.sleep_manager as sleep_module

        class FakeProcess:
            pid = 9401

            def __init__(self) -> None:
                self.nice_calls: list[int] = []
                self.suspend_calls = 0

            def name(self) -> str:
                return "spotify.exe"

            def exe(self) -> str:
                return r"C:\Users\User\AppData\Roaming\Spotify\spotify.exe"

            def nice(self, value: int | None = None) -> int:
                if value is not None:
                    self.nice_calls.append(value)
                return 32

            def suspend(self) -> None:
                self.suspend_calls += 1

        class FakePsutil:
            AccessDenied = Exception
            NoSuchProcess = Exception
            ZombieProcess = Exception

            def __init__(self, proc: FakeProcess) -> None:
                self.proc = proc

            def Process(self, pid: int) -> FakeProcess:  # noqa: N802 - psutil-style API
                self.proc.pid = pid
                return self.proc

        with tempfile.TemporaryDirectory() as tmp:
            fake_proc = FakeProcess()
            original_psutil = sleep_module.psutil
            sleep_module.psutil = FakePsutil(fake_proc)  # type: ignore[assignment]
            try:
                manager = SleepManager(Whitelist(AppConfig()), HistoryManager(path=Path(tmp) / "history.json"))
                action = manager.sleep_process(9401, "test", has_visible_window=False)
            finally:
                sleep_module.psutil = original_psutil  # type: ignore[assignment]

            self.assertIsNone(action)
            self.assertEqual(fake_proc.nice_calls, [])
            self.assertEqual(fake_proc.suspend_calls, 0)
            self.assertEqual(manager.sleeping, [])

    def test_foreground_related_sleeping_process_wakes_immediately(self) -> None:
        import pc_optimizer_lite.sleep_manager as sleep_module
        from pc_optimizer_lite.sleep_manager import SleepEntry

        class FakeProcess:
            def __init__(self, pid: int) -> None:
                self.pid = pid
                self.resume_calls = 0
                self.nice_calls: list[int] = []

            def name(self) -> str:
                return "worker.exe"

            def resume(self) -> None:
                self.resume_calls += 1

            def nice(self, value: int | None = None) -> int:
                if value is not None:
                    self.nice_calls.append(value)
                return 0

        class FakePsutil:
            NORMAL_PRIORITY_CLASS = 0x20
            AccessDenied = Exception
            NoSuchProcess = Exception
            ZombieProcess = Exception

            def __init__(self, proc: FakeProcess) -> None:
                self.proc = proc

            def Process(self, pid: int) -> FakeProcess:  # noqa: N802 - psutil-style API
                self.proc.pid = pid
                return self.proc

        with tempfile.TemporaryDirectory() as tmp:
            fake_proc = FakeProcess(1001)
            original_psutil = sleep_module.psutil
            original_get_foreground_pid = sleep_module.get_foreground_pid
            original_is_related_to_pid = sleep_module.is_related_to_pid
            sleep_module.psutil = FakePsutil(fake_proc)  # type: ignore[assignment]
            sleep_module.get_foreground_pid = lambda: 2002  # type: ignore[assignment]
            sleep_module.is_related_to_pid = lambda pid, active_pid: pid == 1001 and active_pid == 2002  # type: ignore[assignment]
            try:
                manager = SleepManager(Whitelist(AppConfig()), HistoryManager(path=Path(tmp) / "history.json"))
                manager._sleeping[1001] = SleepEntry(  # noqa: SLF001
                    pid=1001,
                    name="worker.exe",
                    exe=r"C:\Apps\worker.exe",
                    slept_at=100.0,
                    reason="test",
                    previous_priority=8,
                    suspended=True,
                    strategy="suspend",
                )
                actions = manager.resume_foreground_if_sleeping()
            finally:
                sleep_module.psutil = original_psutil  # type: ignore[assignment]
                sleep_module.get_foreground_pid = original_get_foreground_pid  # type: ignore[assignment]
                sleep_module.is_related_to_pid = original_is_related_to_pid  # type: ignore[assignment]

            self.assertEqual([action.pid for action in actions], [1001])
            self.assertEqual(fake_proc.resume_calls, 1)
            self.assertEqual(fake_proc.nice_calls, [0x20])
            self.assertEqual(manager.sleeping, [])

    def test_cursor_target_sleeping_process_wakes_immediately(self) -> None:
        import pc_optimizer_lite.sleep_manager as sleep_module
        from pc_optimizer_lite.sleep_manager import SleepEntry

        class FakeProcess:
            def __init__(self, pid: int) -> None:
                self.pid = pid
                self.resume_calls = 0
                self.nice_calls: list[int] = []

            def name(self) -> str:
                return "worker.exe"

            def resume(self) -> None:
                self.resume_calls += 1

            def nice(self, value: int | None = None) -> int:
                if value is not None:
                    self.nice_calls.append(value)
                return 0

        class FakePsutil:
            NORMAL_PRIORITY_CLASS = 0x20
            AccessDenied = Exception
            NoSuchProcess = Exception
            ZombieProcess = Exception

            def __init__(self, proc: FakeProcess) -> None:
                self.proc = proc

            def Process(self, pid: int) -> FakeProcess:  # noqa: N802 - psutil-style API
                self.proc.pid = pid
                return self.proc

        with tempfile.TemporaryDirectory() as tmp:
            fake_proc = FakeProcess(1001)
            original_psutil = sleep_module.psutil
            original_get_foreground_pid = sleep_module.get_foreground_pid
            original_get_cursor_window_pid = sleep_module.get_cursor_window_pid
            sleep_module.psutil = FakePsutil(fake_proc)  # type: ignore[assignment]
            sleep_module.get_foreground_pid = lambda: None  # type: ignore[assignment]
            sleep_module.get_cursor_window_pid = lambda: 1001  # type: ignore[assignment]
            try:
                manager = SleepManager(Whitelist(AppConfig()), HistoryManager(path=Path(tmp) / "history.json"))
                manager._sleeping[1001] = SleepEntry(  # noqa: SLF001
                    pid=1001,
                    name="worker.exe",
                    exe=r"C:\Apps\worker.exe",
                    slept_at=100.0,
                    reason="test",
                    previous_priority=8,
                    suspended=True,
                    strategy="suspend",
                )
                actions = manager.resume_user_target_if_sleeping()
            finally:
                sleep_module.psutil = original_psutil  # type: ignore[assignment]
                sleep_module.get_foreground_pid = original_get_foreground_pid  # type: ignore[assignment]
                sleep_module.get_cursor_window_pid = original_get_cursor_window_pid  # type: ignore[assignment]

            self.assertEqual([action.pid for action in actions], [1001])
            self.assertEqual(fake_proc.resume_calls, 1)
            self.assertEqual(fake_proc.nice_calls, [0x20])
            self.assertEqual(manager.sleeping, [])

    def test_unsaved_window_title_is_never_deep_suspended(self) -> None:
        from pc_optimizer_lite.safety.activity_detector import choose_sleep_strategy

        decision = choose_sleep_strategy(
            name="notepad.exe",
            has_visible_window=True,
            window_title="*Untitled - Notepad",
        )

        self.assertEqual(decision.strategy, "priority")
        self.assertIn("unsaved", decision.reason)


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

    def test_low_resource_inactive_window_is_not_sleep_candidate(self) -> None:
        config = AppConfig(sleep_after_minutes=15.0)
        idle_low_resource = ProcessOptimizationSnapshot(
            pid=6001,
            name="notes.exe",
            exe=r"C:\Apps\notes.exe",
            username="",
            cpu_percent=0.0,
            memory_percent=0.2,
            rss=20 * 1024 * 1024,
            priority=None,
            has_window=True,
            is_foreground_related=False,
            active_network=False,
            active_audio_hint=False,
            hung_window=False,
            age_seconds=3600.0,
            last_focus_age_seconds=3600.0,
        )
        idle_heavy = ProcessOptimizationSnapshot(
            pid=6002,
            name="renderer.exe",
            exe=r"C:\Apps\renderer.exe",
            username="",
            cpu_percent=3.0,
            memory_percent=3.0,
            rss=250 * 1024 * 1024,
            priority=None,
            has_window=True,
            is_foreground_related=False,
            active_network=False,
            active_audio_hint=False,
            hung_window=False,
            age_seconds=3600.0,
            last_focus_age_seconds=3600.0,
        )

        plan = _classify_processes(config, [idle_low_resource, idle_heavy])

        self.assertEqual([item.pid for item in plan.sleep], [6002])

    def test_visible_browser_keepalive_network_can_still_priority_sleep(self) -> None:
        config = AppConfig(sleep_after_minutes=15.0)
        browser = ProcessOptimizationSnapshot(
            pid=6010,
            name="chrome.exe",
            exe=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            username=r"PC\User",
            cpu_percent=2.0,
            memory_percent=4.0,
            rss=400 * 1024 * 1024,
            priority=None,
            has_window=True,
            is_foreground_related=False,
            active_network=True,
            active_audio_hint=False,
            hung_window=False,
            age_seconds=7200.0,
            last_focus_age_seconds=3600.0,
        )

        plan = _classify_processes(config, [browser])

        self.assertEqual(plan.sleep, [browser])


class RuntimePolicyTests(unittest.TestCase):
    def test_sleep_wake_poll_disabled_without_autopilot_or_sleeping_apps(self) -> None:
        enabled, interval = sleep_wake_poll_policy(
            AppConfig(observation_only_mode=True, sleep_enabled=False),
            sleeping_count=0,
            background=True,
        )

        self.assertFalse(enabled)
        self.assertEqual(interval, 0)

    def test_sleep_wake_poll_stays_on_for_sleeping_apps_in_background(self) -> None:
        enabled, interval = sleep_wake_poll_policy(
            AppConfig(observation_only_mode=True, sleep_enabled=False, lite_mode_enabled=True),
            sleeping_count=1,
            background=True,
        )

        self.assertTrue(enabled)
        self.assertGreaterEqual(interval, 2500)


class VisualEffectsTests(unittest.TestCase):
    def test_low_power_effect_set_covers_common_windows_animations(self) -> None:
        names = {setting.name for setting in VISUAL_EFFECT_SETTINGS}

        self.assertGreaterEqual(len(names), 12)
        self.assertIn("ui_effects", names)
        self.assertIn("menu_fade", names)
        self.assertIn("tooltip_fade", names)
        self.assertIn("drop_shadow", names)

    def test_visual_effects_manager_restores_captured_values(self) -> None:
        class FakeAdapter:
            available = True

            def __init__(self) -> None:
                self.values = {setting.name: True for setting in VISUAL_EFFECT_SETTINGS}
                self.set_calls: list[tuple[str, bool]] = []

            def get_bool(self, setting: VisualEffectSetting) -> bool | None:
                return self.values[setting.name]

            def set_bool(self, setting: VisualEffectSetting, enabled: bool) -> bool:
                self.values[setting.name] = enabled
                self.set_calls.append((setting.name, enabled))
                return True

        adapter = FakeAdapter()
        manager = VisualEffectsManager(adapter)  # type: ignore[arg-type]

        self.assertTrue(manager.apply_low_power())
        self.assertTrue(manager.active)
        self.assertTrue(all(value is False for value in adapter.values.values()))

        self.assertTrue(manager.restore())
        self.assertFalse(manager.active)
        self.assertTrue(all(value is True for value in adapter.values.values()))


class BackgroundLoadTests(unittest.TestCase):
    def test_manager_restores_captured_registry_values(self) -> None:
        adapter = _FakeBackgroundLoadAdapter(
            registry_values={
                "windows_widgets": 1,
                "windows_news": 0,
                "xbox_game_bar": 1,
                "game_dvr": 1,
                "game_recording": 1,
            }
        )
        manager = BackgroundLoadManager(adapter)

        count = manager.apply_disabled_set(SAFE_BACKGROUND_LOAD_PRESET)

        self.assertEqual(count, len(SAFE_BACKGROUND_LOAD_PRESET))
        self.assertTrue(manager.active)
        for control_id in SAFE_BACKGROUND_LOAD_PRESET:
            self.assertEqual(adapter.registry_values[control_id], 0 if control_id != "windows_news" else 2)

        self.assertTrue(manager.restore())
        self.assertFalse(manager.active)
        self.assertEqual(adapter.registry_values["windows_widgets"], 1)
        self.assertEqual(adapter.registry_values["windows_news"], 0)
        self.assertEqual(adapter.registry_values["xbox_game_bar"], 1)
        self.assertEqual(adapter.registry_values["game_dvr"], 1)
        self.assertEqual(adapter.registry_values["game_recording"], 1)

    def test_manager_deletes_registry_values_that_were_missing_before_apply(self) -> None:
        adapter = _FakeBackgroundLoadAdapter(registry_values={})
        manager = BackgroundLoadManager(adapter)

        manager.apply_disabled_set({"windows_widgets"})
        self.assertEqual(adapter.registry_values["windows_widgets"], 0)

        self.assertTrue(manager.restore())
        self.assertNotIn("windows_widgets", adapter.registry_values)

    def test_auto_pause_pauses_and_resumes_services_only_when_high_load_and_idle(self) -> None:
        adapter = _FakeBackgroundLoadAdapter(registry_values={})
        manager = BackgroundLoadManager(adapter)

        low_actions = manager.update_auto_pause(
            AUTO_PAUSE_BACKGROUND_LOAD_IDS,
            cpu_percent=50.0,
            idle_seconds=60.0,
            threshold_percent=80.0,
            required_idle_seconds=30.0,
        )
        self.assertEqual(low_actions, [])
        self.assertFalse(adapter.paused_services)

        pause_actions = manager.update_auto_pause(
            AUTO_PAUSE_BACKGROUND_LOAD_IDS,
            cpu_percent=91.0,
            idle_seconds=45.0,
            threshold_percent=80.0,
            required_idle_seconds=30.0,
        )
        self.assertEqual(pause_actions, ["paused:WSearch", "paused:DoSvc"])
        self.assertEqual(adapter.paused_services, {"WSearch", "DoSvc"})

        resume_actions = manager.update_auto_pause(
            AUTO_PAUSE_BACKGROUND_LOAD_IDS,
            cpu_percent=25.0,
            idle_seconds=45.0,
            threshold_percent=80.0,
            required_idle_seconds=30.0,
        )
        self.assertEqual(resume_actions, ["resumed:WSearch", "resumed:DoSvc"])
        self.assertFalse(adapter.paused_services)


class _FakeBackgroundLoadAdapter:
    available = True

    def __init__(self, registry_values: dict[str, int | None]) -> None:
        self.registry_values = dict(registry_values)
        self.paused_services: set[str] = set()

    def read_registry_value(self, control) -> int | None:
        return self.registry_values.get(control.id)

    def write_registry_value(self, control, value: int) -> bool:
        self.registry_values[control.id] = value
        return True

    def delete_registry_value(self, control) -> bool:
        self.registry_values.pop(control.id, None)
        return True

    def pause_service(self, service_name: str) -> bool:
        self.paused_services.add(service_name)
        return True

    def resume_service(self, service_name: str) -> bool:
        self.paused_services.discard(service_name)
        return True


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


class AutoOptimizationTriggerTests(unittest.TestCase):
    def _snapshot(self, *, cpu: float = 20.0, memory: float = 90.0) -> MonitorSnapshot:
        return MonitorSnapshot(
            timestamp=0.0,
            cpu_percent=cpu,
            per_core_cpu_percent=[cpu],
            memory=MemoryInfo(total=100, available=10, used=90, percent=memory),
            swap=SwapInfo(total=0, used=0, free=0, percent=0.0),
            disks=[],
            disk_io=DiskIOInfo(),
        )

    def test_ram_threshold_starts_light_auto_clean_when_idle(self) -> None:
        from pc_optimizer_lite.pyside_gui import PCOptimizerQtWindow

        clean_calls: list[tuple[RamCleanMode, bool, str]] = []
        window = SimpleNamespace(
            config=AppConfig(
                observation_only_mode=False,
                ram_auto_clean_enabled=True,
                ram_auto_clean_threshold_percent=80.0,
                auto_cleanup_cooldown_minutes=3.0,
            ),
            _last_auto_ram_clean_at=-10_000.0,
            _auto_cleanup_cooldown_seconds=lambda: 180.0,
            _user_recently_active=lambda snapshot: False,
            clean_ram=lambda mode, automatic, event_title: clean_calls.append((mode, automatic, event_title)),
        )

        PCOptimizerQtWindow._maybe_auto_ram_clean(window, self._snapshot(memory=91.0))  # type: ignore[arg-type]

        self.assertEqual(clean_calls, [(RamCleanMode.LIGHT, True, "RAM threshold auto-clean")])
        self.assertGreater(window._last_auto_ram_clean_at, 0.0)

    def test_periodic_optimization_starts_quiet_eco_cycle_when_idle(self) -> None:
        from pc_optimizer_lite.pyside_gui import PCOptimizerQtWindow

        events: list[tuple[str, str, str, str]] = []
        starts: list[dict[str, bool]] = []
        window = SimpleNamespace(
            config=AppConfig(
                observation_only_mode=False,
                periodic_optimization_enabled=True,
                periodic_optimization_interval_minutes=15.0,
                periodic_optimization_eco_mode=True,
            ),
            _optimization_thread=None,
            _last_periodic_optimization_at=-100_000.0,
            _user_recently_active=lambda snapshot: False,
            _start_optimization=lambda **kwargs: starts.append(kwargs) or True,
            history=SimpleNamespace(add_event=lambda *args: events.append(args)),
        )

        PCOptimizerQtWindow._maybe_periodic_optimization(window, self._snapshot())  # type: ignore[arg-type]

        self.assertEqual(starts, [{"eco_mode": True, "quiet": True}])
        self.assertEqual(events[0][1], "Periodic optimization started")
        self.assertEqual(events[0][2], "Silent eco mode")

    def test_cpu_throttle_actions_mark_graph_and_refresh_activity(self) -> None:
        from pc_optimizer_lite.pyside_gui import PCOptimizerQtWindow

        marks: list[str] = []
        refreshes: list[str] = []
        action = SimpleNamespace(action="throttle", success=True, detail="Limited worker.exe")
        window = SimpleNamespace(
            config=AppConfig(cpu_throttle_enabled=True, observation_only_mode=False),
            cpu_throttler=SimpleNamespace(observe=lambda snapshot, config: [action]),
            graph=SimpleNamespace(mark_intervention=lambda detail: marks.append(detail)),
            refresh_activity=lambda: refreshes.append("refresh"),
        )

        PCOptimizerQtWindow._maybe_cpu_throttle(window, self._snapshot(cpu=95.0))  # type: ignore[arg-type]

        self.assertEqual(marks, ["Limited worker.exe"])
        self.assertEqual(refreshes, ["refresh"])

    def test_background_load_auto_pause_runs_when_high_cpu_and_idle(self) -> None:
        from pc_optimizer_lite.pyside_gui import PCOptimizerQtWindow

        calls: list[dict[str, object]] = []
        events: list[tuple[str, str, str, str]] = []
        window = SimpleNamespace(
            config=AppConfig(
                observation_only_mode=False,
                background_load_control_enabled=True,
                background_load_auto_pause_enabled=True,
                background_load_disabled_ids=list(AUTO_PAUSE_BACKGROUND_LOAD_IDS),
                background_load_pause_cpu_threshold_percent=80.0,
                background_load_pause_idle_seconds=30.0,
                background_load_pause_cooldown_seconds=60.0,
            ),
            _last_background_load_pause_at=-10_000.0,
            background_load=SimpleNamespace(
                update_auto_pause=lambda enabled_ids, **kwargs: calls.append(
                    {"enabled_ids": tuple(enabled_ids), **kwargs}
                )
                or ["paused:WSearch", "paused:DoSvc"]
            ),
            history=SimpleNamespace(add_event=lambda *args: events.append(args)),
            refresh_activity=lambda: None,
        )

        PCOptimizerQtWindow._maybe_background_load_pause(window, self._snapshot(cpu=92.0), idle_seconds=45.0)  # type: ignore[arg-type]

        self.assertEqual(calls[0]["enabled_ids"], AUTO_PAUSE_BACKGROUND_LOAD_IDS)
        self.assertEqual(calls[0]["cpu_percent"], 92.0)
        self.assertEqual(calls[0]["idle_seconds"], 45.0)
        self.assertGreater(window._last_background_load_pause_at, 0.0)
        self.assertEqual(events[0][1], "Фоновая нагрузка Windows снижена")
        self.assertIn("WSearch", events[0][2])

    def test_ultra_lite_runtime_uses_slowest_foreground_intervals(self) -> None:
        from pc_optimizer_lite.pyside_gui import PCOptimizerQtWindow

        graph_modes: list[bool] = []
        activity_intervals: list[int] = []
        window = SimpleNamespace(
            config=AppConfig(
                lite_mode_enabled=True,
                ultra_lite_mode_enabled=True,
                monitor_interval_seconds=3.0,
                process_refresh_seconds=6.0,
            ),
            graph=SimpleNamespace(set_lite_mode=lambda enabled: graph_modes.append(enabled)),
            activity_timer=SimpleNamespace(setInterval=lambda value: activity_intervals.append(value)),
            isVisible=lambda: False,
            isMinimized=lambda: False,
            _apply_visual_effects_mode=lambda: None,
            _apply_background_load_mode=lambda: None,
            _sync_sleep_wake_timer=lambda: None,
        )

        PCOptimizerQtWindow._apply_runtime_performance_mode(window)  # type: ignore[arg-type]

        self.assertEqual(graph_modes, [True])
        self.assertGreaterEqual(window._foreground_monitor_interval, 10.0)
        self.assertGreaterEqual(window._foreground_process_interval, 30.0)
        self.assertEqual(activity_intervals, [45000])

    def test_cpu_threshold_starts_quiet_eco_optimization_when_idle(self) -> None:
        from pc_optimizer_lite.pyside_gui import PCOptimizerQtWindow

        starts: list[dict[str, bool]] = []
        events: list[tuple[str, str, str, str]] = []
        window = SimpleNamespace(
            config=AppConfig(
                observation_only_mode=False,
                cpu_optimizer_enabled=True,
                cpu_throttle_enabled=False,
                auto_lower_priority_enabled=False,
                cpu_threshold_percent=85.0,
                auto_cleanup_cooldown_minutes=3.0,
            ),
            _optimization_thread=None,
            _last_threshold_cpu_optimization_at=-10_000.0,
            _auto_cleanup_cooldown_seconds=lambda: 180.0,
            _user_recently_active=lambda snapshot: False,
            _start_optimization=lambda **kwargs: starts.append(kwargs) or True,
            history=SimpleNamespace(add_event=lambda *args: events.append(args)),
        )

        started = PCOptimizerQtWindow._maybe_threshold_cpu_optimization(window, self._snapshot(cpu=94.0))  # type: ignore[arg-type]

        self.assertTrue(started)
        self.assertEqual(starts, [{"eco_mode": True, "quiet": True}])
        self.assertEqual(events[0][1], "CPU threshold optimization started")
        self.assertIn("CPU 94%", events[0][2])

    def test_quiet_optimization_result_logs_and_notifies_without_process_table_refresh(self) -> None:
        from pc_optimizer_lite.pyside_gui import PCOptimizerQtWindow

        events: list[tuple[str, str, str, str]] = []
        notifications: list[tuple[int, int, str]] = []
        process_refreshes: list[str] = []
        activity_refreshes: list[str] = []
        processes_tab = object()
        window = SimpleNamespace(
            _optimization_quiet=True,
            _last_optimization_result=None,
            refresh_activity=lambda: activity_refreshes.append("refresh"),
            isVisible=lambda: False,
            isMinimized=lambda: True,
            tabs=SimpleNamespace(currentWidget=lambda: object()),
            processes_tab=processes_tab,
            refresh_process_table=lambda: process_refreshes.append("refresh"),
            history=SimpleNamespace(add_event=lambda *args: events.append(args)),
            config=AppConfig(periodic_optimization_notify=True),
            _notify_cleanup_summary=lambda ram, disk, key: notifications.append((ram, disk, key)) or True,
        )
        result = OptimizationResult(
            cpu_before=90.0,
            cpu_after=35.0,
            ram_before_percent=92.0,
            ram_after_percent=72.0,
            ram_freed_bytes=256,
        )

        PCOptimizerQtWindow._on_optimization_result(window, result)  # type: ignore[arg-type]

        self.assertEqual(activity_refreshes, ["refresh"])
        self.assertEqual(process_refreshes, [])
        self.assertEqual(events[0][1], "Автооптимизация завершена")
        self.assertEqual(notifications, [(256, 0, "auto_optimization_done")])


class VisualStyleTests(unittest.TestCase):
    def test_labels_default_to_transparent_background(self) -> None:
        from pc_optimizer_lite.pyside_gui import THEMES, _qss

        qss = _qss(THEMES["dark"])

        self.assertIn("QLabel {", qss)
        self.assertIn("background: transparent;", qss[qss.index("QLabel {") : qss.index("QTabWidget::pane")])


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
