"""Configuration and logging helpers for PC Optimizer Lite."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psutil

from . import __app_name__
from .background_load import AUTO_PAUSE_BACKGROUND_LOAD_IDS, BACKGROUND_LOAD_CONTROLS, SAFE_BACKGROUND_LOAD_PRESET

MIN_MONITOR_INTERVAL_SECONDS = 2.0
DEFAULT_CONFIG_FILENAME = "config.json"
DEFAULT_LOG_FILENAME = "pc_optimizer_lite.log"
DEFAULT_GITHUB_OWNER = "kot04ka"
DEFAULT_GITHUB_REPO = "pc-optimizer-lite"
DEFAULT_UPDATE_CHECK_INTERVAL_HOURS = 4.0
CONFIG_SCHEMA_VERSION = 10
BACKGROUND_LOAD_CONTROL_IDS = {control.id for control in BACKGROUND_LOAD_CONTROLS}


@dataclass(frozen=True, slots=True)
class HardwareProfile:
    """Small hardware summary used to choose conservative first-run defaults."""

    cpu_cores: int
    ram_bytes: int
    disk_kind: str = "unknown"


@dataclass(slots=True)
class AppConfig:
    """Runtime settings persisted between application launches."""

    config_version: int = CONFIG_SCHEMA_VERSION
    optimal_preset_tier: str = "balanced"
    optimal_preset_applied: bool = False
    monitor_interval_seconds: float = 3.0
    process_refresh_seconds: float = 6.0
    lite_mode_enabled: bool = False
    lite_mode_prompted: bool = False
    visual_effects_low_power_enabled: bool = False
    visual_effects_restore_on_exit: bool = True
    visual_effects_preset: str = "custom"
    visual_effects_disabled_ids: list[str] = field(default_factory=list)
    visual_effects_auto_on_load: bool = False
    background_load_control_enabled: bool = False
    background_load_restore_on_exit: bool = True
    background_load_auto_on_load: bool = False
    background_load_disabled_ids: list[str] = field(default_factory=list)
    background_load_auto_pause_enabled: bool = False
    background_load_pause_cpu_threshold_percent: float = 85.0
    background_load_pause_idle_seconds: float = 30.0
    background_load_pause_cooldown_seconds: float = 180.0
    ultra_lite_mode_enabled: bool = False
    cpu_threshold_percent: float = 85.0
    cpu_sustain_seconds: float = 2.8
    ram_threshold_percent: float = 85.0
    notification_cooldown_seconds: float = 180.0
    auto_lower_priority_enabled: bool = False
    max_auto_priority_changes: int = 3
    user_whitelist_names: list[str] = field(default_factory=list)
    user_whitelist_paths: list[str] = field(default_factory=list)
    window_starts_hidden: bool = False
    theme: str = "dark"
    graph_collapsed: bool = False
    core_table_collapsed: bool = False
    automation_mode: str = "observation"
    observation_only_mode: bool = True
    auto_close_mode: str = "ask"
    auto_close_min_background_minutes: float = 12.0
    auto_close_cpu_threshold_percent: float = 35.0
    auto_close_memory_threshold_percent: float = 12.0
    auto_close_duplicate_count: int = 3
    sleep_enabled: bool = False
    sleep_after_minutes: float = 15.0
    sleep_check_seconds: float = 15.0
    max_sleep_actions_per_cycle: int = 2
    optimize_consent_accepted: bool = False
    ram_auto_clean_enabled: bool = False
    ram_auto_clean_threshold_percent: float = 85.0
    ram_clean_warning_seen: bool = False
    cpu_optimizer_enabled: bool = True
    cpu_optimizer_priority_mode: str = "below_normal"
    cpu_optimizer_min_process_cpu_percent: float = 20.0
    cpu_optimizer_max_processes: int = 3
    cpu_optimizer_affinity_ratio: float = 0.5
    cpu_optimizer_affinity_min_cores: int = 1
    cpu_optimizer_restore_after_seconds: float = 120.0
    cpu_throttle_enabled: bool = False
    cpu_throttle_affinity_enabled: bool = True
    cpu_limiter_enabled: bool = False
    cpu_limiter_suspend_milliseconds: int = 50
    cpu_limiter_cooldown_seconds: float = 2.0
    autopilot_consent_accepted: bool = False
    periodic_optimization_enabled: bool = False
    periodic_optimization_interval_minutes: float = 30.0
    periodic_optimization_eco_mode: bool = True
    periodic_optimization_notify: bool = False
    scheduled_cleanup_enabled: bool = False
    scheduled_cleanup_interval_minutes: float = 10.0
    scheduled_cleanup_notify: bool = False
    auto_cleanup_cooldown_minutes: float = 5.0
    cleanup_temp_enabled: bool = True
    cleanup_windows_temp_enabled: bool = True
    cleanup_browser_cache_enabled: bool = True
    cleanup_prefetch_enabled: bool = False
    cleanup_logs_enabled: bool = True
    cleanup_logs_older_than_days: int = 7
    cleanup_recycle_bin_enabled: bool = False
    optimize_step_snapshot_enabled: bool = True
    optimize_step_classify_enabled: bool = True
    optimize_step_ram_enabled: bool = True
    optimize_step_standby_enabled: bool = True
    optimize_step_cpu_enabled: bool = True
    optimize_step_sleep_enabled: bool = True
    optimize_step_close_enabled: bool = True
    optimize_step_cleanup_enabled: bool = True
    check_updates_on_startup: bool = True
    update_notify_enabled: bool = True
    update_check_interval_hours: float = DEFAULT_UPDATE_CHECK_INTERVAL_HOURS
    auto_install_updates: bool = False
    skipped_update_version: str = ""
    github_owner: str = DEFAULT_GITHUB_OWNER
    github_repo: str = DEFAULT_GITHUB_REPO


def get_app_data_dir() -> Path:
    """Return the per-user application data directory."""

    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / __app_name__
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pc-optimizer-lite"


def get_config_path() -> Path:
    """Return the default JSON configuration path."""

    return get_app_data_dir() / DEFAULT_CONFIG_FILENAME


def get_log_path() -> Path:
    """Return the default log file path."""

    return get_app_data_dir() / DEFAULT_LOG_FILENAME


def sanitize_config(config: AppConfig) -> AppConfig:
    """Clamp settings that could make the app too noisy or too CPU-heavy."""

    config.config_version = CONFIG_SCHEMA_VERSION
    config.monitor_interval_seconds = max(
        MIN_MONITOR_INTERVAL_SECONDS, float(config.monitor_interval_seconds)
    )
    config.process_refresh_seconds = max(
        config.monitor_interval_seconds, float(config.process_refresh_seconds)
    )
    config.lite_mode_enabled = bool(config.lite_mode_enabled)
    config.lite_mode_prompted = bool(config.lite_mode_prompted)
    config.visual_effects_low_power_enabled = bool(config.visual_effects_low_power_enabled)
    config.visual_effects_restore_on_exit = bool(config.visual_effects_restore_on_exit)
    config.visual_effects_preset = (
        config.visual_effects_preset
        if config.visual_effects_preset in {"performance", "balanced", "appearance", "custom"}
        else "custom"
    )
    if not isinstance(config.visual_effects_disabled_ids, list):
        config.visual_effects_disabled_ids = []
    config.visual_effects_disabled_ids = [
        s for s in config.visual_effects_disabled_ids if isinstance(s, str)
    ]
    config.visual_effects_auto_on_load = bool(config.visual_effects_auto_on_load)
    config.background_load_control_enabled = bool(config.background_load_control_enabled)
    config.background_load_restore_on_exit = bool(config.background_load_restore_on_exit)
    config.background_load_auto_on_load = bool(config.background_load_auto_on_load)
    if not isinstance(config.background_load_disabled_ids, list):
        config.background_load_disabled_ids = []
    config.background_load_disabled_ids = [
        s
        for s in config.background_load_disabled_ids
        if isinstance(s, str) and s in BACKGROUND_LOAD_CONTROL_IDS
    ]
    config.background_load_auto_pause_enabled = bool(config.background_load_auto_pause_enabled)
    config.background_load_pause_cpu_threshold_percent = min(
        max(float(config.background_load_pause_cpu_threshold_percent), 50.0), 100.0
    )
    config.background_load_pause_idle_seconds = max(
        10.0, float(config.background_load_pause_idle_seconds)
    )
    config.background_load_pause_cooldown_seconds = max(
        60.0, float(config.background_load_pause_cooldown_seconds)
    )
    config.ultra_lite_mode_enabled = bool(config.ultra_lite_mode_enabled)
    config.optimal_preset_tier = (
        config.optimal_preset_tier
        if config.optimal_preset_tier in {"low", "balanced", "high"}
        else "balanced"
    )
    config.optimal_preset_applied = bool(config.optimal_preset_applied)
    if config.lite_mode_enabled:
        config.monitor_interval_seconds = max(config.monitor_interval_seconds, 3.5)
        config.process_refresh_seconds = max(config.process_refresh_seconds, 12.0)
    if config.ultra_lite_mode_enabled:
        config.lite_mode_enabled = True
        config.monitor_interval_seconds = max(config.monitor_interval_seconds, 10.0)
        config.process_refresh_seconds = max(config.process_refresh_seconds, 30.0)
    config.cpu_threshold_percent = min(max(float(config.cpu_threshold_percent), 1.0), 100.0)
    config.cpu_sustain_seconds = max(0.5, float(config.cpu_sustain_seconds))
    config.ram_threshold_percent = min(max(float(config.ram_threshold_percent), 1.0), 100.0)
    config.notification_cooldown_seconds = max(float(config.notification_cooldown_seconds), 30.0)
    config.max_auto_priority_changes = max(1, int(config.max_auto_priority_changes))
    config.user_whitelist_names = sorted({name.strip().lower() for name in config.user_whitelist_names if name.strip()})
    config.user_whitelist_paths = sorted({str(Path(path).expanduser()) for path in config.user_whitelist_paths if path.strip()})
    config.theme = config.theme if config.theme in {"dark", "light"} else "dark"
    config.graph_collapsed = bool(config.graph_collapsed)
    config.core_table_collapsed = bool(config.core_table_collapsed)
    if config.automation_mode not in {"observation", "manual", "autopilot"}:
        config.automation_mode = "observation" if config.observation_only_mode else "manual"
    config.auto_close_mode = config.auto_close_mode if config.auto_close_mode in {"off", "ask", "auto"} else "ask"
    config.auto_close_min_background_minutes = max(1.0, float(config.auto_close_min_background_minutes))
    config.auto_close_cpu_threshold_percent = min(
        max(float(config.auto_close_cpu_threshold_percent), 1.0), 100.0
    )
    config.auto_close_memory_threshold_percent = min(
        max(float(config.auto_close_memory_threshold_percent), 1.0), 100.0
    )
    config.auto_close_duplicate_count = max(2, int(config.auto_close_duplicate_count))
    config.sleep_after_minutes = max(1.0, float(config.sleep_after_minutes))
    config.sleep_check_seconds = max(5.0, float(config.sleep_check_seconds))
    config.max_sleep_actions_per_cycle = max(1, int(config.max_sleep_actions_per_cycle))
    config.ram_auto_clean_threshold_percent = min(
        max(float(config.ram_auto_clean_threshold_percent), 50.0), 99.0
    )
    config.cpu_optimizer_enabled = bool(config.cpu_optimizer_enabled)
    config.cpu_optimizer_priority_mode = (
        config.cpu_optimizer_priority_mode
        if config.cpu_optimizer_priority_mode in {"below_normal", "idle"}
        else "below_normal"
    )
    config.cpu_optimizer_min_process_cpu_percent = min(
        max(float(config.cpu_optimizer_min_process_cpu_percent), 1.0), 100.0
    )
    config.cpu_optimizer_max_processes = min(max(1, int(config.cpu_optimizer_max_processes)), 8)
    if config.lite_mode_enabled:
        config.cpu_optimizer_max_processes = min(config.cpu_optimizer_max_processes, 2)
    if config.ultra_lite_mode_enabled:
        config.cpu_optimizer_max_processes = min(config.cpu_optimizer_max_processes, 1)
    config.cpu_optimizer_affinity_ratio = min(max(float(config.cpu_optimizer_affinity_ratio), 0.25), 0.75)
    config.cpu_optimizer_affinity_min_cores = min(max(1, int(config.cpu_optimizer_affinity_min_cores)), 8)
    config.cpu_optimizer_restore_after_seconds = max(15.0, float(config.cpu_optimizer_restore_after_seconds))
    config.cpu_throttle_enabled = bool(config.cpu_throttle_enabled)
    config.cpu_throttle_affinity_enabled = bool(config.cpu_throttle_affinity_enabled)
    config.cpu_limiter_enabled = bool(config.cpu_limiter_enabled)
    config.cpu_limiter_suspend_milliseconds = min(
        max(10, int(config.cpu_limiter_suspend_milliseconds)), 250
    )
    config.cpu_limiter_cooldown_seconds = max(1.0, float(config.cpu_limiter_cooldown_seconds))
    config.periodic_optimization_enabled = bool(config.periodic_optimization_enabled)
    config.periodic_optimization_interval_minutes = max(
        15.0, float(config.periodic_optimization_interval_minutes)
    )
    config.periodic_optimization_eco_mode = bool(config.periodic_optimization_eco_mode)
    config.periodic_optimization_notify = bool(config.periodic_optimization_notify)
    config.scheduled_cleanup_enabled = bool(config.scheduled_cleanup_enabled)
    config.scheduled_cleanup_interval_minutes = max(10.0, float(config.scheduled_cleanup_interval_minutes))
    config.scheduled_cleanup_notify = bool(config.scheduled_cleanup_notify)
    config.auto_cleanup_cooldown_minutes = max(3.0, float(config.auto_cleanup_cooldown_minutes))
    config.cleanup_temp_enabled = bool(config.cleanup_temp_enabled)
    config.cleanup_windows_temp_enabled = bool(config.cleanup_windows_temp_enabled)
    config.cleanup_browser_cache_enabled = bool(config.cleanup_browser_cache_enabled)
    config.cleanup_prefetch_enabled = bool(config.cleanup_prefetch_enabled)
    config.cleanup_logs_enabled = bool(config.cleanup_logs_enabled)
    config.cleanup_logs_older_than_days = max(1, int(config.cleanup_logs_older_than_days))
    config.cleanup_recycle_bin_enabled = bool(config.cleanup_recycle_bin_enabled)
    for key in (
        "optimize_step_snapshot_enabled",
        "optimize_step_classify_enabled",
        "optimize_step_ram_enabled",
        "optimize_step_standby_enabled",
        "optimize_step_cpu_enabled",
        "optimize_step_sleep_enabled",
        "optimize_step_close_enabled",
        "optimize_step_cleanup_enabled",
        "check_updates_on_startup",
        "update_notify_enabled",
        "auto_install_updates",
    ):
        setattr(config, key, bool(getattr(config, key)))
    config.update_check_interval_hours = DEFAULT_UPDATE_CHECK_INTERVAL_HOURS
    config.skipped_update_version = str(config.skipped_update_version).strip()
    config.github_owner = str(config.github_owner).strip() or DEFAULT_GITHUB_OWNER
    config.github_repo = str(config.github_repo).strip() or DEFAULT_GITHUB_REPO
    if config.github_owner == "YOUR_GITHUB_OWNER":
        config.github_owner = DEFAULT_GITHUB_OWNER
    if config.github_repo == "YOUR_GITHUB_REPO":
        config.github_repo = DEFAULT_GITHUB_REPO
    return config


def apply_automation_mode(config: AppConfig) -> AppConfig:
    """Make the global mode authoritative over individual automatic actions."""

    if config.automation_mode == "observation":
        config.observation_only_mode = True
        return config

    config.observation_only_mode = False
    if config.automation_mode == "manual":
        config.auto_lower_priority_enabled = False
        config.sleep_enabled = False
        config.ram_auto_clean_enabled = False
        config.cpu_throttle_enabled = False
        config.auto_close_mode = "off"
    elif config.automation_mode == "autopilot":
        config.cpu_optimizer_enabled = True
        config.auto_lower_priority_enabled = True
        config.sleep_enabled = True
        config.ram_auto_clean_enabled = True
        config.cpu_throttle_enabled = True
        config.periodic_optimization_enabled = True
        config.periodic_optimization_eco_mode = True
        config.periodic_optimization_notify = True
        config.scheduled_cleanup_enabled = True
        config.scheduled_cleanup_notify = True
        config.auto_close_mode = "off"
    return config


def detect_hardware_profile() -> HardwareProfile:
    """Detect enough hardware to pick a safe default preset without slow probes."""

    cpu_cores = os.cpu_count() or 2
    try:
        ram_bytes = int(psutil.virtual_memory().total)
    except Exception:
        ram_bytes = 8 * 1024**3
    return HardwareProfile(cpu_cores=cpu_cores, ram_bytes=ram_bytes)


def choose_optimal_preset_tier(profile: HardwareProfile) -> str:
    """Classify the machine for defaults that avoid high background CPU."""

    ram_gb = profile.ram_bytes / 1024**3
    if profile.cpu_cores <= 4 or ram_gb <= 8:
        return "low"
    if profile.cpu_cores >= 8 and ram_gb >= 24:
        return "high"
    return "balanced"


def apply_optimal_preset(config: AppConfig, profile: HardwareProfile | None = None) -> AppConfig:
    """Apply a conservative out-of-box preset while keeping destructive actions off."""

    profile = profile or detect_hardware_profile()
    tier = choose_optimal_preset_tier(profile)
    config.optimal_preset_tier = tier
    config.optimal_preset_applied = True
    config.automation_mode = "autopilot"
    config.observation_only_mode = False
    config.auto_close_mode = "off"
    config.auto_lower_priority_enabled = True
    config.cpu_optimizer_enabled = True
    config.cpu_throttle_enabled = True
    config.cpu_throttle_affinity_enabled = True
    config.cpu_limiter_enabled = False
    config.ram_auto_clean_enabled = True
    config.sleep_enabled = True
    config.scheduled_cleanup_enabled = True
    config.scheduled_cleanup_notify = True
    config.periodic_optimization_enabled = True
    config.periodic_optimization_eco_mode = True
    config.periodic_optimization_notify = True
    config.cleanup_recycle_bin_enabled = False
    config.cleanup_prefetch_enabled = False
    config.visual_effects_restore_on_exit = True
    config.background_load_restore_on_exit = True

    config.cpu_threshold_percent = 85.0
    config.cpu_sustain_seconds = 3.0
    config.ram_threshold_percent = 80.0
    config.ram_auto_clean_threshold_percent = 80.0
    config.sleep_after_minutes = 15.0
    config.scheduled_cleanup_interval_minutes = 15.0
    config.periodic_optimization_interval_minutes = 15.0
    config.auto_cleanup_cooldown_minutes = 5.0

    if tier == "low":
        config.lite_mode_enabled = True
        config.ultra_lite_mode_enabled = True
        config.visual_effects_low_power_enabled = True
        config.background_load_control_enabled = True
        config.background_load_auto_on_load = True
        config.background_load_auto_pause_enabled = True
        config.background_load_disabled_ids = list(SAFE_BACKGROUND_LOAD_PRESET) + list(AUTO_PAUSE_BACKGROUND_LOAD_IDS)
        config.monitor_interval_seconds = 3.5
        config.process_refresh_seconds = 12.0
        config.cpu_optimizer_max_processes = 2
        config.cpu_threshold_percent = 90.0
        config.ram_auto_clean_threshold_percent = 85.0
        config.scheduled_cleanup_interval_minutes = 20.0
        config.periodic_optimization_interval_minutes = 20.0
    elif tier == "high":
        config.lite_mode_enabled = False
        config.ultra_lite_mode_enabled = False
        config.visual_effects_low_power_enabled = False
        config.background_load_control_enabled = False
        config.background_load_auto_pause_enabled = False
        config.background_load_disabled_ids = []
        config.monitor_interval_seconds = 2.5
        config.process_refresh_seconds = 6.0
        config.cpu_optimizer_max_processes = 4
        config.sleep_after_minutes = 12.0
    else:
        config.lite_mode_enabled = False
        config.ultra_lite_mode_enabled = False
        config.visual_effects_low_power_enabled = False
        config.background_load_control_enabled = False
        config.background_load_auto_pause_enabled = False
        config.background_load_disabled_ids = []
        config.monitor_interval_seconds = 3.0
        config.process_refresh_seconds = 6.0
        config.cpu_optimizer_max_processes = 3

    return apply_automation_mode(sanitize_config(config))


def load_config(path: Path | None = None) -> AppConfig:
    """Load settings from JSON, falling back to safe defaults."""

    config_path = path or get_config_path()
    if not config_path.exists():
        return apply_optimal_preset(AppConfig())

    try:
        data: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logging.getLogger(__name__).warning("Failed to load config %s: %s", config_path, exc)
        return apply_optimal_preset(AppConfig())

    return _config_from_data(data, config_path)


def import_config(path: Path) -> AppConfig:
    """Load a user-selected config export and validate it before applying."""

    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot import config {path}: {exc}") from exc
    return _config_from_data(data, path)


def export_config(config: AppConfig, path: Path) -> Path:
    """Write a portable copy of the current validated configuration."""

    return save_config(config, path)


def _config_from_data(data: dict[str, Any], source: Path) -> AppConfig:
    defaults = asdict(AppConfig())
    try:
        data = _migrate_config_data(data)
        defaults.update({key: value for key, value in data.items() if key in defaults})
        return apply_automation_mode(sanitize_config(AppConfig(**defaults)))
    except (TypeError, ValueError) as exc:
        logging.getLogger(__name__).warning("Invalid config values in %s: %s", source, exc)
        return apply_optimal_preset(AppConfig())


def _migrate_config_data(data: dict[str, Any]) -> dict[str, Any]:
    """Apply conservative migrations for older persisted defaults."""

    migrated = dict(data)
    raw_version = migrated.get("config_version", migrated.get("version", 0))
    try:
        version = int(raw_version or 0)
    except (TypeError, ValueError):
        version = 0
    if version < 2:
        if float(migrated.get("cpu_threshold_percent", 90.0)) == 90.0:
            migrated["cpu_threshold_percent"] = 85.0
        if float(migrated.get("cpu_sustain_seconds", 10.0)) == 10.0:
            migrated["cpu_sustain_seconds"] = 2.8
        if float(migrated.get("cpu_optimizer_min_process_cpu_percent", 10.0)) == 10.0:
            migrated["cpu_optimizer_min_process_cpu_percent"] = 20.0
        migrated["config_version"] = 2
    if version < 3:
        migrated.setdefault("lite_mode_enabled", False)
        migrated.setdefault("lite_mode_prompted", False)
        migrated.setdefault("optimize_step_snapshot_enabled", True)
        migrated.setdefault("optimize_step_classify_enabled", True)
        migrated.setdefault("optimize_step_ram_enabled", True)
        migrated.setdefault("optimize_step_standby_enabled", True)
        migrated.setdefault("optimize_step_cpu_enabled", True)
        migrated.setdefault("optimize_step_sleep_enabled", True)
        migrated.setdefault("optimize_step_close_enabled", True)
        migrated.setdefault("optimize_step_cleanup_enabled", True)
        migrated.setdefault("check_updates_on_startup", True)
        migrated.setdefault("update_notify_enabled", True)
        migrated.setdefault("update_check_interval_hours", DEFAULT_UPDATE_CHECK_INTERVAL_HOURS)
        migrated.setdefault("auto_install_updates", False)
        migrated.setdefault("skipped_update_version", "")
        migrated.setdefault("github_owner", DEFAULT_GITHUB_OWNER)
        migrated.setdefault("github_repo", DEFAULT_GITHUB_REPO)
        migrated["config_version"] = 3
    if version < 4:
        migrated["config_version"] = 4
    if version < 5:
        migrated["update_check_interval_hours"] = DEFAULT_UPDATE_CHECK_INTERVAL_HOURS
        migrated["config_version"] = 5
    if version < 6:
        migrated.setdefault("graph_collapsed", False)
        migrated.setdefault("core_table_collapsed", False)
        migrated.setdefault("auto_cleanup_cooldown_minutes", 5.0)
        migrated.setdefault("cleanup_temp_enabled", True)
        migrated.setdefault("cleanup_windows_temp_enabled", True)
        migrated.setdefault("cleanup_browser_cache_enabled", True)
        migrated.setdefault("cleanup_prefetch_enabled", False)
        migrated.setdefault("cleanup_logs_enabled", True)
        migrated.setdefault("cleanup_logs_older_than_days", 7)
        migrated.setdefault("cleanup_recycle_bin_enabled", False)
        migrated["config_version"] = 6
    if version < 7:
        migrated.setdefault("optimal_preset_tier", "balanced")
        migrated.setdefault("optimal_preset_applied", False)
        migrated["config_version"] = 7
    if version < 8:
        migrated.setdefault("visual_effects_low_power_enabled", False)
        migrated.setdefault("visual_effects_restore_on_exit", True)
        migrated["config_version"] = 8
    if version < 9:
        migrated.setdefault("visual_effects_preset", "custom")
        migrated.setdefault("visual_effects_disabled_ids", [])
        migrated.setdefault("visual_effects_auto_on_load", False)
        migrated["config_version"] = 9
    if version < 10:
        migrated.setdefault("background_load_control_enabled", False)
        migrated.setdefault("background_load_restore_on_exit", True)
        migrated.setdefault("background_load_auto_on_load", False)
        migrated.setdefault("background_load_disabled_ids", [])
        migrated.setdefault("background_load_auto_pause_enabled", False)
        migrated.setdefault("background_load_pause_cpu_threshold_percent", 85.0)
        migrated.setdefault("background_load_pause_idle_seconds", 30.0)
        migrated.setdefault("background_load_pause_cooldown_seconds", 180.0)
        migrated.setdefault("ultra_lite_mode_enabled", False)
        migrated["config_version"] = 10
    return migrated


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    """Persist settings to JSON using an atomic replace."""

    config = sanitize_config(config)
    config_path = path or get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    payload_data = asdict(config)
    payload_data["version"] = config.config_version
    payload = json.dumps(payload_data, indent=2, ensure_ascii=False)
    tmp_path.write_text(payload + "\n", encoding="utf-8")
    tmp_path.replace(config_path)
    return config_path


def setup_logging(log_path: Path | None = None) -> Path:
    """Configure application logging to a persistent file."""

    path = log_path or get_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        encoding="utf-8",
    )
    logging.getLogger(__name__).info("Logging initialized at %s", path)
    return path
