# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```powershell
# Setup
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Run
python main.py
python main.py --tray   # start minimized to tray

# Tests
python -m unittest discover -s tests

# Run a single test class
python -m unittest tests.test_core.ConfigTests

# Build (requires Inno Setup 6 for the installer)
.\build.bat
```

## Architecture

The app is a passive Windows system monitor with optional one-click optimization. It uses **PySide6** as the primary GUI with a legacy **tkinter** fallback (`pc_optimizer_lite/gui.py`) activated only when PySide6 is missing.

**Entry flow:** `main.py` → loads config → lowers own process priority → calls `pyside_gui.run_app()`.

**Core modules under `pc_optimizer_lite/`:**

| Module | Role |
|---|---|
| `monitor.py` | Background thread collecting psutil snapshots (CPU, RAM, disk, process list). Process list is only gathered when the Processes tab is visible or a threshold is breached. |
| `config.py` | `AppConfig` dataclass persisted to `%APPDATA%\PC Optimizer Lite\config.json` with schema-version migration. `sanitize_config()` clamps all values on load. |
| `optimize_action.py` | One-click optimization pipeline: single `process_iter()` snapshot → classify → RAM clean → CPU relief → sleep/close → cleanup → report. Never calls `process_iter()` more than once per cycle. |
| `optimizer.py` | Temp/cache file cleanup. Only touches a known set of directories and only after user confirmation. |
| `ram_cleaner.py` | `EmptyWorkingSet` for safe inactive processes; deep mode requires admin rights. |
| `cpu_optimizer.py` | ProBalance-style priority/affinity tuning. Fires only after `cpu_sustain_seconds` of sustained threshold breach; restores originals on exit or when the window regains focus. |
| `cpu_throttler.py` | Optional suspend-pulse CPU limiter (off by default). |
| `sleep_manager.py` | Puts inactive background apps into priority-sleep or suspend; windowed/editor processes always get `priority` sleep, never `suspend`. |
| `smart_process_manager.py` | Classifies close candidates; respects foreground window, network-active, and media-playing heuristics. |
| `whitelist.py` | System process names + user-defined name/path exclusions. All optimization subsystems check this before acting. |
| `safety/activity_detector.py` | Decides `priority` vs `suspend` sleep strategy based on process name, window title (unsaved-data markers), and data-risk list. |
| `process_safety.py` | Guards for `get_foreground_pid()` and `is_interactive_process()` used by CPU optimizer and close logic. |
| `updater.py` | Checks GitHub Releases (`kot04ka/pc-optimizer-lite`), downloads `PC-Optimizer-Lite-Setup.exe`, verifies SHA256, launches Inno Setup silently (`/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`), then exits the app immediately. |
| `pyside_gui.py` | All PySide6 UI: tabs (Monitor, Processes, Activity, Settings), tray icon, live graph (`deque(maxlen=60)` + EMA Y-scaling), worker threads for heavy operations. |

**Key design invariants:**
- A single `psutil.process_iter()` snapshot is shared across all subsystems in one optimization cycle—no repeated full scans.
- `observation_only_mode=True` by default; auto-actions are disabled until the user changes `automation_mode`.
- Every action (priority change, RAM clean, sleep, close) goes through `Whitelist` and process-safety guards. `kill()` is never called.
- Config schema version (`CONFIG_SCHEMA_VERSION = 7`) is checked on load; unknown keys are ignored and missing keys get safe defaults via `sanitize_config()`.

**Build artifacts** go to `installer_output/`. GitHub Actions (`.github/workflows/build.yml`) triggers on `v*` tags, runs `build.bat` on `windows-latest`, and publishes a GitHub Release with SHA256 hashes embedded in the release notes.
