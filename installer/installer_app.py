"""Graphical per-user installer for PC Optimizer Lite."""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import sys
import winreg
from pathlib import Path

import psutil
import win32com.client
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QLabel,
    QMessageBox,
    QPushButton,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from pc_optimizer_lite.version import APP_VERSION

APP_NAME = "PC Optimizer Lite"


def resource_path(relative: str) -> Path:
    """Return a payload path both from source and PyInstaller onefile mode."""

    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def install_dir() -> Path:
    """Return the per-user install directory."""

    return Path(os.environ["LOCALAPPDATA"]) / "Programs" / APP_NAME


def start_menu_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP_NAME


def desktop_shortcut() -> Path:
    return Path.home() / "Desktop" / f"{APP_NAME}.lnk"


def stop_installed_copy(target_exe: Path) -> None:
    """Stop the installed app before replacing its executable."""

    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            if Path(proc.info.get("exe") or "") == target_exe:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except (psutil.TimeoutExpired, psutil.NoSuchProcess):
                    proc.kill()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue


def create_shortcut(path: Path, target: Path, args: str = "") -> None:
    """Create a Windows .lnk shortcut."""

    path.parent.mkdir(parents=True, exist_ok=True)
    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortcut(str(path))
    shortcut.TargetPath = str(target)
    shortcut.Arguments = args
    shortcut.WorkingDirectory = str(target.parent)
    shortcut.IconLocation = f"{target},0"
    shortcut.Save()


def set_run_value(target_exe: Path, enabled: bool) -> None:
    """Enable or disable HKCU Run autostart."""

    run_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, run_path) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{target_exe}" --tray')
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def write_uninstaller(target_dir: Path) -> Path:
    """Write a PowerShell uninstaller that removes shortcuts and registry entries."""

    script_path = target_dir / "Uninstall.ps1"
    script_path.write_text(
        r"""
$ErrorActionPreference = 'SilentlyContinue'
$AppName = 'PC Optimizer Lite'
$InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TargetExe = Join-Path $InstallDir 'PC Optimizer Lite.exe'
$StartMenuDir = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs\PC Optimizer Lite'
$DesktopShortcut = Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'PC Optimizer Lite.lnk'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$UninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\PC Optimizer Lite'

Get-Process | ForEach-Object {
    try {
        if ($_.Path -eq $TargetExe) {
            Stop-Process -Id $_.Id -Force
        }
    } catch {
    }
}

Remove-ItemProperty -Path $RunKey -Name $AppName -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $DesktopShortcut -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $StartMenuDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path $UninstallKey -Recurse -Force -ErrorAction SilentlyContinue

$cleanup = Join-Path $env:TEMP 'pc_optimizer_lite_cleanup.ps1'
$escapedInstallDir = $InstallDir.Replace("'", "''")
Set-Content -LiteralPath $cleanup -Encoding UTF8 -Value "Start-Sleep -Seconds 2; Remove-Item -LiteralPath '$escapedInstallDir' -Recurse -Force -ErrorAction SilentlyContinue; Remove-Item -LiteralPath `$MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue"
Start-Process powershell.exe -WindowStyle Hidden -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$cleanup`""
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return script_path


def register_uninstall(target_dir: Path, target_exe: Path, uninstall_script: Path) -> None:
    """Register the app in the current user's Add/Remove Programs list."""

    subkey = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\PC Optimizer Lite"
    estimated_size = max(1, int(sum(path.stat().st_size for path in target_dir.rglob("*") if path.is_file()) / 1024))
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, subkey) as key:
        values = {
            "DisplayName": APP_NAME,
            "DisplayVersion": APP_VERSION,
            "Publisher": APP_NAME,
            "InstallLocation": str(target_dir),
            "DisplayIcon": str(target_exe),
            "UninstallString": f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{uninstall_script}"',
            "QuietUninstallString": f'powershell.exe -WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -File "{uninstall_script}"',
            "InstallDate": _dt.datetime.now().strftime("%Y%m%d"),
        }
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, estimated_size)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)


def perform_install(create_desktop: bool, autostart: bool, *, remove_desktop_when_unchecked: bool = True) -> Path:
    """Install the application for the current user."""

    payload_root = resource_path("payload")
    source_exe = payload_root / "PC Optimizer Lite.exe"
    if not source_exe.exists():
        nested_payload = payload_root / "PC Optimizer Lite"
        source_exe = nested_payload / "PC Optimizer Lite.exe"
        if source_exe.exists():
            payload_root = nested_payload
    if not source_exe.exists():
        raise FileNotFoundError(source_exe)

    target_dir = install_dir()
    target_exe = target_dir / "PC Optimizer Lite.exe"
    stop_installed_copy(target_exe)
    target_dir.mkdir(parents=True, exist_ok=True)
    for stale in (
        target_dir / "PC Optimizer Lite_new.exe",
        target_dir / "apply_pc_optimizer_lite_update.ps1",
        target_dir / "pc_optimizer_lite_update.log",
    ):
        stale.unlink(missing_ok=True)
    for item in payload_root.iterdir():
        destination = target_dir / item.name
        if item.is_dir():
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(item, destination)
        else:
            shutil.copy2(item, destination)

    create_shortcut(start_menu_dir() / "PC Optimizer Lite.lnk", target_exe)
    if create_desktop:
        create_shortcut(desktop_shortcut(), target_exe)
    elif remove_desktop_when_unchecked and desktop_shortcut().exists():
        desktop_shortcut().unlink()

    set_run_value(target_exe, autostart)
    uninstall_script = write_uninstaller(target_dir)
    register_uninstall(target_dir, target_exe, uninstall_script)
    return target_exe


class InstallerWindow(QWidget):
    """Small installer UI with the requested checkboxes."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PC Optimizer Lite Setup")
        self.setFixedSize(500, 240)
        self.create_desktop = QCheckBox("Create desktop shortcut")
        self.create_desktop.setChecked(True)
        self.autostart = QCheckBox("Start with Windows (tray mode)")

        install_path = install_dir()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        title = QLabel("Install PC Optimizer Lite")
        title.setStyleSheet("font-size: 16px; font-weight: 700;")
        destination = QLabel(f"Destination: {install_path}")
        destination.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(destination)
        layout.addSpacing(8)
        layout.addWidget(self.create_desktop)
        layout.addWidget(self.autostart)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.close)
        install = QPushButton("Install")
        install.clicked.connect(self.on_install)
        buttons.addWidget(cancel)
        buttons.addWidget(install)
        layout.addLayout(buttons)

    def on_install(self) -> None:
        try:
            target = perform_install(self.create_desktop.isChecked(), self.autostart.isChecked())
        except Exception as exc:  # noqa: BLE001 - installer must surface any failure
            QMessageBox.critical(self, "PC Optimizer Lite Setup", f"Installation failed:\n{exc}")
            return
        QMessageBox.information(self, "PC Optimizer Lite Setup", f"Installed successfully:\n{target}")
        self.close()


def main() -> int:
    if "--silent" in sys.argv:
        try:
            perform_install(
                create_desktop="--desktop" in sys.argv,
                autostart="--autostart" in sys.argv,
                remove_desktop_when_unchecked=False,
            )
        except Exception as exc:  # noqa: BLE001 - installer entry point must return a setup error
            print(f"Installation failed: {exc}", file=sys.stderr)
            return 1
        return 0

    app = QApplication.instance() or QApplication(sys.argv)
    window = InstallerWindow()
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())

