Hot-fix: onedir stability, settings persistence, auto-optimization triggers, and sleep-mode wake behavior.

Changes:
- Main distribution remains PyInstaller onedir only; the updater downloads the onedir ZIP, verifies size and SHA256 before applying it, and replaces the whole app directory after the old process exits.
- Settings are saved to `%APPDATA%\PC Optimizer Lite\config.json` with a schema version, support export/import/reset, and corrupt or missing configs fall back to the optimal preset.
- RAM threshold cleanup, CPU ProBalance, periodic optimization, and quiet/autopilot notifications are covered by regression tests.
- Sleep mode uses priority sleep for visible/data-risk apps, keeps deep suspend only for headless-safe candidates, and wakes via foreground/cursor polling.
- Label backgrounds are transparent and settings wording/tooltips were clarified.
- Windows Defender, antivirus software, and system protection settings are not disabled or changed.

Recovery for broken installs:
- If the installed app cannot launch with `_PYI_APPLICATION_HOME_DIR environment variable is not defined!` or `Failed to load Python DLL ... _MEI...\python311.dll/python312.dll`, auto-update cannot run from inside that broken app.
- Download `PC-Optimizer-Lite-Setup.exe` manually from this GitHub Release and install it over the old version.
- If the error remains, remove `_MEI*` folders from `%TEMP%` manually and reinstall.

Validation:
- `python -m py_compile main.py pc_optimizer_lite\config.py pc_optimizer_lite\pyside_gui.py pc_optimizer_lite\sleep_manager.py pc_optimizer_lite\updater.py installer\installer_app.py`
- `python -m unittest tests.test_core` passed: 59 tests.
- Real Notepad `/new` sleep/wake smoke passed: priority sleep, no suspend, click/cursor wake.
- Built onedir EXE tray idle CPU smoke: 0.026% CPU over 30 seconds.
- Defender/antivirus code search found no disabling commands.

Assets:
- PC-Optimizer-Lite-windows-x64.zip size: 55439109 bytes
- PC-Optimizer-Lite-windows-x64.zip sha256: 5C6F9F37E7101FCCFA9EB53C0072E33E7D9A498C747EE2A05AC7A1B552BEA5A0
- PC-Optimizer-Lite-Setup.exe size: 38555623 bytes
- PC-Optimizer-Lite-Setup.exe sha256: 1CEEBCD1CFDFC7DA2BC6917E44EBBDF8C4C1F2BA191944FC0F0FCC4F9A8EDF47
