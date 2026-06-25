# Fix Input Lag Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent PC Optimizer Lite from slowing text input in Telegram, Discord, browsers, editors, or any active window.

**Architecture:** Keep all heavy optimization work out of the input path. Add a central interactive-process guard used by legacy auto-priority, manual priority changes, ProBalance throttling, and one-click CPU relief, and defer first background monitoring work during app startup.

**Tech Stack:** Python 3.12, psutil, PySide6, unittest, Windows foreground-window APIs through pywin32 polling.

---

### Task 1: Prove Foreground Apps Are Protected

**Files:**
- Modify: `tests/test_core.py`
- Modify: `pc_optimizer_lite/optimizer.py`
- Modify: `pc_optimizer_lite/cpu_optimizer.py`
- Modify: `pc_optimizer_lite/cpu_throttler.py`

- [ ] **Step 1: Write failing tests**

Add tests that verify:
- `SystemOptimizer.suggest_heavy_processes()` excludes a foreground-related process when passed a foreground PID.
- `SystemOptimizer.lower_priority_for_process()` refuses a foreground PID.
- `CpuOptimizer._is_candidate()` excludes any visible window app even when `is_foreground_related` is stale.
- `CpuThrottler.select_candidates()` excludes visible/foreground process table rows.

- [ ] **Step 2: Run tests to verify RED**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m unittest tests.test_core.SystemOptimizerInputGuardTests tests.test_core.CpuOptimizerTests tests.test_core.CpuThrottlerTests
```

Expected: at least the new foreground/input guard assertions fail before implementation.

- [ ] **Step 3: Implement minimal guard**

Add foreground and interactive-name checks at the decision boundary, before priority or affinity changes:
- legacy `SystemOptimizer`
- snapshot `CpuOptimizer`
- ProBalance `CpuThrottler`

- [ ] **Step 4: Run tests to verify GREEN**

Run the same unittest command and confirm all listed tests pass.

### Task 2: Prove Startup Is Soft

**Files:**
- Modify: `tests/test_core.py`
- Modify: `pc_optimizer_lite/monitor.py`
- Modify: `pc_optimizer_lite/pyside_gui.py`

- [ ] **Step 1: Write failing tests**

Add tests that verify a new `SystemMonitor.startup_grace_seconds` prevents process-table collection immediately after monitor startup and allows it after the grace window.

- [ ] **Step 2: Run tests to verify RED**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m unittest tests.test_core.SystemMonitorStartupTests
```

Expected: new test fails because the monitor has no startup grace.

- [ ] **Step 3: Implement minimal startup grace**

Add `startup_grace_seconds` to `SystemMonitor`, track `self._started_at`, and gate `include_processes` until the grace period expires. Use a small default that keeps existing behavior reasonable, and pass a stronger value from the PySide app.

- [ ] **Step 4: Run tests to verify GREEN**

Run the same unittest command and confirm it passes.

### Task 3: Version, Build, Release

**Files:**
- Modify: `pc_optimizer_lite/version.py`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `RELEASE.md`
- Modify: `.github/workflows/build.yml`

- [ ] **Step 1: Bump version**

Set app version to `1.3.5`.

- [ ] **Step 2: Update changelog**

Add: "Исправлено: задержка ввода в приложениях при работе оптимизатора."

- [ ] **Step 3: Verify**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m unittest discover -s tests
& '.\.venv\Scripts\python.exe' -m py_compile pc_optimizer_lite\optimizer.py pc_optimizer_lite\cpu_optimizer.py pc_optimizer_lite\cpu_throttler.py pc_optimizer_lite\monitor.py pc_optimizer_lite\pyside_gui.py
.\build.bat
```

Also run app CPU/startup smoke and confirm no input hooks exist by searching for `SetWindowsHookEx`.

- [ ] **Step 4: Publish**

Commit, push branch, open/merge PR, push tag `v1.3.5`, and verify GitHub Release assets.
