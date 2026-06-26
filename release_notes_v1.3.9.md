Hot-fix: fixed the PC Optimizer Lite auto-update loop by switching updates to the Inno Setup installer.

Changes:
- Auto-update now downloads `PC-Optimizer-Lite-Setup.exe`, verifies size and SHA256, launches it from `%TEMP%` with `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`, and exits immediately.
- Removed the self-replacement PowerShell script, in-app folder rename/move updater, and blocking update `QMessageBox` before exit.
- Inno Setup now uses `CloseApplications=yes` and `RestartApplications=yes`; silent installs also launch the updated app.
- Main distribution remains PyInstaller onedir, so runtime no longer unpacks into `%TEMP%\_MEI`.
- Windows Defender, antivirus software, and system protection settings are not disabled or changed.

Important for users stuck on 1.3.8:
- The updater in 1.3.8 can be stuck because it tries to replace a folder that Windows still sees as busy.
- Install this release manually once using `PC-Optimizer-Lite-Setup.exe` from GitHub Releases.
- After that, later auto-updates will use the fixed installer-based flow.
- If a PyInstaller `_MEI` error remains, remove `_MEI*` folders from `%TEMP%` manually and reinstall.

Validation:
- `python -m unittest tests.test_core` passed: 61 tests.
- `python -m py_compile main.py pc_optimizer_lite\updater.py pc_optimizer_lite\pyside_gui.py pc_optimizer_lite\version.py installer\installer_app.py` passed.
- `build.ps1` built PyInstaller onedir, ZIP, and Inno Setup installer successfully.
- Search confirmed updater no longer contains `apply_pc_optimizer_lite_update.ps1`, folder `Rename-Item`/`Move-Item` replacement, or `Update directory replacement failed`.
- Defender/antivirus code search found no disabling commands.

Assets:
- PC-Optimizer-Lite-windows-x64.zip size: 55436364 bytes
- PC-Optimizer-Lite-windows-x64.zip sha256: 92547F9C447EE5706E75B5FB0D615B7120B8549D5F7592F7035989AFAA01F22E
- PC-Optimizer-Lite-Setup.exe size: 38553188 bytes
- PC-Optimizer-Lite-Setup.exe sha256: 48A25482FB61E6A03915E93893D4C8FCE738D7BE1CEB17C81BF3B477632F0914
