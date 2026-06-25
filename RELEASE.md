# Release Process

## 1. Bump Version

Edit the single runtime version source:

```text
pc_optimizer_lite/version.py
```

Set:

```python
APP_VERSION = "1.3.5"
```

Keep `pyproject.toml` in sync for package metadata.

## 2. Build Locally

From the repository root:

```powershell
.\build.bat
```

Expected outputs:

```text
dist\PC Optimizer Lite.exe
installer_output\PC-Optimizer-Lite-Setup.exe
```

## 3. Create GitHub Release

Create a tag matching the version:

```powershell
git tag v1.3.5
git push origin v1.3.5
```

Then create a GitHub Release for that tag and attach these assets:

```text
dist\PC Optimizer Lite.exe
installer_output\PC-Optimizer-Lite-Setup.exe
```

Recommended release notes:

```text
Hot-fix: Исправлена задержка ввода в Telegram, Discord и других интерактивных приложениях при работе оптимизатора.

PC-Optimizer-Lite.exe sha256: <SHA256 of dist\PC Optimizer Lite.exe>
PC-Optimizer-Lite-Setup.exe sha256: <SHA256 of installer_output\PC-Optimizer-Lite-Setup.exe>
```

The updater prefers the portable exe asset and uses the release tag for semver comparison. If the exe is replaced without changing the tag, users will still see the update button when the release sha256 or asset size differs from their installed exe.

## 4. Automatic Build From Tags

The workflow in `.github/workflows/build.yml` builds and uploads release assets automatically when a tag like `v1.3.5` is pushed.

The app checks:

```text
https://api.github.com/repos/kot04ka/pc-optimizer-lite/releases/latest
```

Public repositories work without credentials. `kot04ka/pc-optimizer-lite` is public, so the app reads GitHub Releases without a token. Do not hardcode tokens into the source, commits, releases, or built exe.

After the Release is published, users with "Проверять обновления при запуске" enabled will see the "Обновить" banner in the app.
