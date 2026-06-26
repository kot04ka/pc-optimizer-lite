# Release Process

## 1. Bump Version

Edit the single runtime version source:

```text
pc_optimizer_lite/version.py
```

Set:

```python
APP_VERSION = "1.3.9"
```

Keep `pyproject.toml` in sync for package metadata.

## 2. Build Locally

From the repository root:

```powershell
.\build.bat
```

Local setup builds require Inno Setup 6 (`iscc.exe`). The GitHub Actions release workflow installs Inno Setup before running `build.bat`.

Expected outputs:

```text
dist\PC Optimizer Lite\
installer_output\PC-Optimizer-Lite-windows-x64.zip
installer_output\PC-Optimizer-Lite-Setup.exe
```

The app is built as PyInstaller onedir only. The installer places that folder under `%LOCALAPPDATA%\Programs\PC Optimizer Lite`; auto-update downloads and runs the installer, not the ZIP.

## 3. Create GitHub Release

Create a tag matching the version:

```powershell
git tag v1.3.9
git push origin v1.3.9
```

Then create a GitHub Release for that tag and attach these assets:

```text
installer_output\PC-Optimizer-Lite-windows-x64.zip
installer_output\PC-Optimizer-Lite-Setup.exe
```

Recommended release notes:

```text
Hot-fix: fixed the auto-update loop by switching updates to the Inno Setup installer.

Changes:
- Main distribution is now an onedir folder installed by Inno Setup.
- Auto-update downloads `PC-Optimizer-Lite-Setup.exe`, verifies size and SHA256, launches it from `%TEMP%` with `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`, and exits immediately.
- The old self-replacement PowerShell script and folder rename/move logic were removed from the updater.
- Inno Setup now has `CloseApplications=yes` and `RestartApplications=yes`; silent installs also launch the updated app.
- Users stuck on 1.3.8 must install this release manually once with `PC-Optimizer-Lite-Setup.exe`; later auto-updates use the fixed installer flow.
- Windows Defender and other system protection settings are not changed.

PC-Optimizer-Lite-windows-x64.zip sha256: <SHA256 of installer_output\PC-Optimizer-Lite-windows-x64.zip>
PC-Optimizer-Lite-Setup.exe sha256: <SHA256 of installer_output\PC-Optimizer-Lite-Setup.exe>
```

The updater selects the `PC-Optimizer-Lite-Setup.exe` asset and uses the release tag for semver comparison. Always bump the tag for a new public update.

## 4. Automatic Build From Tags

The workflow in `.github/workflows/build.yml` builds and uploads release assets automatically when a tag like `v1.3.9` is pushed.

The app checks:

```text
https://api.github.com/repos/kot04ka/pc-optimizer-lite/releases/latest
```

Public repositories work without credentials. `kot04ka/pc-optimizer-lite` is public, so the app reads GitHub Releases without a token. Do not hardcode tokens into the source, commits, releases, or built app.

## Recovery Note For Broken Installs

If the installed app cannot launch with `_PYI_APPLICATION_HOME_DIR environment variable is not defined!` or `Failed to load Python DLL ... _MEI...\python311.dll/python312.dll`, auto-update cannot run from inside the broken app. Download `PC-Optimizer-Lite-Setup.exe` manually from GitHub Releases and install it over the old version. If the error remains, remove `_MEI*` folders from `%TEMP%` manually and reinstall.
