"""GitHub Releases updater for packaged PC Optimizer Lite builds."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import __app_name__
from .config import get_app_data_dir
from .version import APP_VERSION

# Public repository defaults are configured in config.py. Placeholders are kept
# only to recognize very old config files.
GITHUB_OWNER_PLACEHOLDER = "YOUR_GITHUB_OWNER"
GITHUB_REPO_PLACEHOLDER = "YOUR_GITHUB_REPO"
UPDATE_CACHE_FILENAME = "update_cache.json"


class UpdateError(RuntimeError):
    """Raised when an update was found but could not be installed."""


@dataclass(slots=True)
class ReleaseAsset:
    """One downloadable file attached to a GitHub release."""

    name: str
    url: str
    size: int = 0
    sha256: str = ""


@dataclass(slots=True)
class UpdateCheckResult:
    """Result of a non-blocking update check."""

    configured: bool
    update_available: bool = False
    latest_version: str = ""
    release_name: str = ""
    release_url: str = ""
    body: str = ""
    asset: ReleaseAsset | None = None
    skipped: bool = False
    message: str = ""


def check_for_updates(
    *,
    owner: str,
    repo: str,
    current_version: str = APP_VERSION,
    skipped_version: str = "",
    timeout_seconds: float = 6.0,
    force: bool = False,
    cache_ttl_seconds: float = 4 * 60 * 60,
) -> UpdateCheckResult:
    """Query GitHub Releases and return only actionable update information."""

    owner = owner.strip()
    repo = repo.strip()
    if not is_repository_configured(owner, repo):
        return UpdateCheckResult(
            configured=False,
            message="GitHub repository is not configured.",
        )
    if not force:
        cached = _load_cached_update_check()
        if (
            cached
            and cached.get("owner") == owner
            and cached.get("repo") == repo
            and time.time() - float(cached.get("checked_at") or 0.0) < cache_ttl_seconds
        ):
            return _result_from_cache(cached, current_version, skipped_version)

    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    request = urllib.request.Request(
        api_url,
        headers=_github_headers(current_version),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return UpdateCheckResult(configured=True, message=f"Update check failed quietly: {exc}")

    latest_version = _normalize_version(str(payload.get("tag_name") or payload.get("name") or ""))
    if not latest_version:
        result = UpdateCheckResult(configured=True, message="Latest release has no version tag.")
        _save_cached_update_check(owner, repo, result)
        return result
    if latest_version == _normalize_version(skipped_version):
        return UpdateCheckResult(configured=True, latest_version=latest_version, skipped=True)
    body = str(payload.get("body") or "")
    asset = _select_windows_onedir_asset(payload.get("assets", []), body)
    if not is_newer_version(latest_version, current_version):
        result = UpdateCheckResult(
            configured=True,
            latest_version=latest_version,
            release_name=str(payload.get("name") or latest_version),
            release_url=str(payload.get("html_url") or ""),
            message="Already up to date.",
        )
        _save_cached_update_check(owner, repo, result)
        return result

    result = UpdateCheckResult(
        configured=True,
        update_available=asset is not None,
        latest_version=latest_version,
        release_name=str(payload.get("name") or latest_version),
        release_url=str(payload.get("html_url") or ""),
        body=body,
        asset=asset,
        message="Update available." if asset else "Release has no onedir ZIP asset.",
    )
    _save_cached_update_check(owner, repo, result)
    return result


def is_repository_configured(owner: str, repo: str) -> bool:
    """Return False while the code/config still contains placeholders."""

    owner = owner.strip()
    repo = repo.strip()
    return bool(
        owner
        and repo
        and owner != GITHUB_OWNER_PLACEHOLDER
        and repo != GITHUB_REPO_PLACEHOLDER
        and "{" not in owner
        and "}" not in owner
        and "{" not in repo
        and "}" not in repo
    )


def is_newer_version(candidate: str, current: str) -> bool:
    """Compare simple semantic versions without an extra dependency."""

    return _version_tuple(candidate) > _version_tuple(current)


def download_update_asset(
    asset: ReleaseAsset,
    *,
    destination_dir: Path | None = None,
    timeout_seconds: float = 30.0,
    progress_callback: Callable[[int, str], None] | None = None,
) -> Path:
    """Download a release asset and verify size/hash when available."""

    destination = destination_dir or Path(tempfile.gettempdir()) / "pc_optimizer_lite_update"
    destination.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", asset.name).strip() or "pc_optimizer_lite_update.zip"
    target = destination / safe_name
    request = urllib.request.Request(
        asset.url,
        headers=_github_headers(APP_VERSION),
    )
    downloaded = 0
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        with target.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 512)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                if progress_callback and asset.size:
                    progress_callback(min(99, round(downloaded * 100 / asset.size)), f"Скачано {downloaded}/{asset.size} байт")

    actual_size = target.stat().st_size
    if asset.size and actual_size != asset.size:
        target.unlink(missing_ok=True)
        raise UpdateError(f"Downloaded size mismatch: expected {asset.size}, got {actual_size}")
    if asset.sha256:
        actual_hash = _sha256_file(target)
        if actual_hash.lower() != asset.sha256.lower():
            target.unlink(missing_ok=True)
            raise UpdateError("Downloaded SHA256 does not match release notes.")
    if progress_callback:
        progress_callback(100, "Файл скачан")
    return target


def install_downloaded_update(downloaded_archive: Path, *, current_exe: Path | None = None) -> Path:
    """Launch a helper script that swaps the installed onedir application."""

    current = current_exe or Path(sys.executable)
    if current.suffix.lower() != ".exe" or current.name.lower() in {"python.exe", "pythonw.exe"}:
        raise UpdateError("Automatic replacement is available only in the packaged .exe build.")
    if not downloaded_archive.exists():
        raise UpdateError(f"Downloaded update not found: {downloaded_archive}")
    if downloaded_archive.suffix.lower() != ".zip":
        raise UpdateError("Automatic replacement requires an onedir ZIP release asset.")

    script = current.with_name("apply_pc_optimizer_lite_update.ps1")
    script.write_text(_replacement_script(current_exe=current, archive=downloaded_archive, pid=os.getpid()), encoding="utf-8")
    creation_flags = 0
    if os.name == "nt":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            str(script),
        ],
        cwd=str(current.parent),
        creationflags=creation_flags,
        close_fds=True,
    )
    return script


def download_and_install_update(asset: ReleaseAsset, progress_callback: Callable[[int, str], None] | None = None) -> Path:
    """Download an onedir ZIP asset and stage replacement for the app folder."""

    downloaded = download_update_asset(asset, progress_callback=progress_callback)
    return install_downloaded_update(downloaded)


def _select_windows_onedir_asset(raw_assets: Any, release_body: str) -> ReleaseAsset | None:
    if not isinstance(raw_assets, list):
        return None
    hashes = _sha256_hashes_from_text(release_body)
    candidates: list[ReleaseAsset] = []
    for item in raw_assets:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        url = str(item.get("browser_download_url") or "")
        lowered = name.lower()
        if not lowered.endswith(".zip") or not url:
            continue
        if "setup" in lowered or "installer" in lowered:
            continue
        candidates.append(
            ReleaseAsset(
                name=name,
                url=url,
                size=int(item.get("size") or 0),
                sha256=hashes.get(name.lower(), "") or hashes.get("*", ""),
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda asset: _onedir_asset_score(asset.name))
    return candidates[0]


def _onedir_asset_score(name: str) -> tuple[int, int, str]:
    lowered = name.lower()
    return (
        0 if "pc-optimizer-lite" in lowered or "pc optimizer lite" in lowered else 1,
        0 if "windows" in lowered or "win" in lowered or "x64" in lowered else 1,
        lowered,
    )


def _load_cached_update_check() -> dict[str, Any]:
    path = get_app_data_dir() / UPDATE_CACHE_FILENAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _save_cached_update_check(owner: str, repo: str, result: UpdateCheckResult) -> None:
    payload: dict[str, Any] = {
        "checked_at": time.time(),
        "owner": owner,
        "repo": repo,
        "configured": result.configured,
        "update_available": result.update_available,
        "latest_version": result.latest_version,
        "release_name": result.release_name,
        "release_url": result.release_url,
        "body": result.body,
        "asset": (
            {
                "name": result.asset.name,
                "url": result.asset.url,
                "size": result.asset.size,
                "sha256": result.asset.sha256,
            }
            if result.asset
            else None
        ),
        "skipped": result.skipped,
        "message": result.message,
    }
    path = get_app_data_dir() / UPDATE_CACHE_FILENAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _result_from_cache(
    cache: dict[str, Any],
    current_version: str,
    skipped_version: str,
) -> UpdateCheckResult:
    latest_version = str(cache.get("latest_version") or "")
    if latest_version and latest_version == _normalize_version(skipped_version):
        return UpdateCheckResult(configured=True, latest_version=latest_version, skipped=True, message="Skipped version.")
    asset_payload = cache.get("asset") if isinstance(cache.get("asset"), dict) else None
    asset = (
        ReleaseAsset(
            name=str(asset_payload.get("name") or ""),
            url=str(asset_payload.get("url") or ""),
            size=int(asset_payload.get("size") or 0),
            sha256=str(asset_payload.get("sha256") or ""),
        )
        if asset_payload
        else None
    )
    asset_is_zip = asset is not None and asset.name.lower().endswith(".zip")
    update_available = bool(cache.get("update_available")) and asset_is_zip and is_newer_version(
        latest_version,
        current_version,
    )
    return UpdateCheckResult(
        configured=bool(cache.get("configured")),
        update_available=update_available,
        latest_version=latest_version,
        release_name=str(cache.get("release_name") or ""),
        release_url=str(cache.get("release_url") or ""),
        body=str(cache.get("body") or ""),
        asset=asset if update_available else None,
        skipped=bool(cache.get("skipped")),
        message=str(cache.get("message") or "Cached update check."),
    )


def _github_headers(version: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{__app_name__}/{version}",
    }


def _sha256_hashes_from_text(text: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    generic = re.search(r"sha256\s*[:=]\s*([a-fA-F0-9]{64})", text, flags=re.IGNORECASE)
    if generic:
        hashes["*"] = generic.group(1)
    for line in text.splitlines():
        match = re.search(
            r"([A-Za-z0-9._ -]+\.(?:zip|exe))\s+sha256\s*[:=]\s*([a-fA-F0-9]{64})",
            line,
            flags=re.IGNORECASE,
        )
        if match:
            hashes[match.group(1).strip().lower()] = match.group(2)
    return hashes


def _replacement_script(*, current_exe: Path, archive: Path, pid: int) -> str:
    current = _ps_quote(str(current_exe))
    archive_path = _ps_quote(str(archive))
    app_dir = _ps_quote(str(current_exe.parent))
    parent_dir = _ps_quote(str(current_exe.parent.parent))
    app_leaf = _ps_quote(current_exe.parent.name)
    log_path = _ps_quote(str(current_exe.parent.parent / f"pc_optimizer_lite_update_{pid}.log"))
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"$currentExe = {current}\n"
        f"$archive = {archive_path}\n"
        f"$pidToWait = {pid}\n"
        f"$appDir = {app_dir}\n"
        f"$parentDir = {parent_dir}\n"
        f"$appLeaf = {app_leaf}\n"
        "$newRoot = Join-Path $parentDir \"$appLeaf.update.$pidToWait\"\n"
        "$oldDir = Join-Path $parentDir \"$appLeaf.old.$pidToWait\"\n"
        "$oldExe = Join-Path $oldDir 'PC Optimizer Lite.exe'\n"
        f"$logPath = {log_path}\n"
        "function Write-UpdateLog([string]$message) {\n"
        "    try { Add-Content -LiteralPath $logPath -Encoding UTF8 -Value \"$(Get-Date -Format o) $message\" } catch { }\n"
        "}\n"
        "function Get-ProcessesByExecutablePath([string]$path) {\n"
        "    @(Get-Process -ErrorAction SilentlyContinue | Where-Object {\n"
        "        try { $_.Path -and ([string]::Equals($_.Path, $path, [StringComparison]::OrdinalIgnoreCase)) } catch { $false }\n"
        "    })\n"
        "}\n"
        "function Stop-ProcessesByExecutablePath([string]$path) {\n"
        "    foreach ($proc in (Get-ProcessesByExecutablePath $path)) {\n"
        "        try {\n"
        "            Stop-Process -Id $proc.Id -Force -ErrorAction Stop\n"
        "            Write-UpdateLog \"Stopped stale process $($proc.Id) using $path.\"\n"
        "        } catch { }\n"
        "    }\n"
        "}\n"
        "function Remove-DirectoryWithRetry([string]$path, [int]$attempts) {\n"
        "    for ($attempt = 1; $attempt -le $attempts; $attempt++) {\n"
        "        Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue\n"
        "        if (-not (Test-Path -LiteralPath $path)) { return $true }\n"
        "        Start-Sleep -Milliseconds ([Math]::Min(1500, 200 * $attempt))\n"
        "    }\n"
        "    return $false\n"
        "}\n"
        "function Resolve-NewAppDir([string]$root) {\n"
        "    $directExe = Join-Path $root 'PC Optimizer Lite.exe'\n"
        "    if ((Test-Path -LiteralPath $directExe) -and (Test-Path -LiteralPath (Join-Path $root '_internal'))) {\n"
        "        return $root\n"
        "    }\n"
        "    $matches = @(Get-ChildItem -LiteralPath $root -Recurse -Filter 'PC Optimizer Lite.exe' -File -ErrorAction Stop | Where-Object {\n"
        "        Test-Path -LiteralPath (Join-Path $_.DirectoryName '_internal')\n"
        "    })\n"
        "    if ($matches.Count -ne 1) {\n"
        "        throw \"Expected exactly one onedir app in archive, found $($matches.Count).\"\n"
        "    }\n"
        "    return $matches[0].DirectoryName\n"
        "}\n"
        "try {\n"
        "    Write-UpdateLog 'Updater started.'\n"
        "    $deadline = (Get-Date).AddSeconds(60)\n"
        "    while (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue) {\n"
        "        if ((Get-Date) -gt $deadline) {\n"
        f"            throw \"Timed out waiting for process {pid} to exit.\"\n"
        "        }\n"
        "        Start-Sleep -Milliseconds 300\n"
        "    }\n"
        "    Start-Sleep -Milliseconds 700\n"
        "    $pathReleaseDeadline = (Get-Date).AddSeconds(10)\n"
        "    while (Get-ProcessesByExecutablePath $currentExe) {\n"
        "        if ((Get-Date) -gt $pathReleaseDeadline) {\n"
        "            Stop-ProcessesByExecutablePath $currentExe\n"
        "            break\n"
        "        }\n"
        "        Start-Sleep -Milliseconds 300\n"
        "    }\n"
        "    if (-not (Test-Path -LiteralPath $archive)) {\n"
        "        throw \"Downloaded update archive is missing: $archive\"\n"
        "    }\n"
        "    Remove-DirectoryWithRetry $newRoot 5 | Out-Null\n"
        "    New-Item -ItemType Directory -Force -Path $newRoot | Out-Null\n"
        "    Expand-Archive -LiteralPath $archive -DestinationPath $newRoot -Force\n"
        "    $newAppDir = Resolve-NewAppDir $newRoot\n"
        "    if (-not (Test-Path -LiteralPath (Join-Path $newAppDir 'PC Optimizer Lite.exe'))) {\n"
        "        throw \"Updated executable is missing in archive.\"\n"
        "    }\n"
        "    $replaced = $false\n"
        "    $lastError = ''\n"
        "    for ($attempt = 1; $attempt -le 20; $attempt++) {\n"
        "        try {\n"
        "            Remove-DirectoryWithRetry $oldDir 3 | Out-Null\n"
        "            $renamedOld = $false\n"
        "            if (Test-Path -LiteralPath $appDir) {\n"
        "                Rename-Item -LiteralPath $appDir -NewName (Split-Path -Leaf $oldDir) -ErrorAction Stop\n"
        "                $renamedOld = $true\n"
        "            }\n"
        "            try {\n"
        "                Move-Item -LiteralPath $newAppDir -Destination $appDir -ErrorAction Stop\n"
        "            } catch {\n"
        "                $moveError = $_.Exception.Message\n"
        "                if ($renamedOld -and (Test-Path -LiteralPath $oldDir) -and -not (Test-Path -LiteralPath $appDir)) {\n"
        "                    Rename-Item -LiteralPath $oldDir -NewName $appLeaf -ErrorAction SilentlyContinue\n"
        "                }\n"
        "                throw $moveError\n"
        "            }\n"
        "            $replaced = $true\n"
        "            Write-UpdateLog \"Replacement succeeded on attempt $attempt.\"\n"
        "            break\n"
        "        } catch {\n"
        "            $lastError = $_.Exception.Message\n"
        "            Write-UpdateLog \"Replacement attempt $attempt failed: $lastError\"\n"
        "            Start-Sleep -Milliseconds ([Math]::Min(1500, 200 * $attempt))\n"
        "        }\n"
        "    }\n"
        "    if (-not $replaced) {\n"
        "        throw \"Update directory replacement failed: $lastError\"\n"
        "    }\n"
        "    $updatedExe = Join-Path $appDir 'PC Optimizer Lite.exe'\n"
        "    if (-not (Test-Path -LiteralPath $updatedExe)) {\n"
        "        throw \"Updated executable is missing after replacement: $updatedExe\"\n"
        "    }\n"
        "    Remove-DirectoryWithRetry $newRoot 3 | Out-Null\n"
        "    Start-Sleep -Milliseconds 500\n"
        "    Start-Process -FilePath $updatedExe -WorkingDirectory $appDir\n"
        "    $cleanupScript = Join-Path $env:TEMP \"pc_optimizer_lite_update_cleanup_$pidToWait.ps1\"\n"
        "    $cleanupLines = @(\n"
        "        '$ErrorActionPreference = ''SilentlyContinue''',\n"
        "        \"`$oldDir = '$($oldDir.Replace(\"'\", \"''\"))'\",\n"
        "        \"`$oldExe = '$($oldExe.Replace(\"'\", \"''\"))'\",\n"
        "        \"`$newRoot = '$($newRoot.Replace(\"'\", \"''\"))'\",\n"
        "        \"`$archive = '$($archive.Replace(\"'\", \"''\"))'\",\n"
        "        \"`$logPath = '$($logPath.Replace(\"'\", \"''\"))'\",\n"
        "        '$removed = $false',\n"
        "        'for ($attempt = 1; $attempt -le 120; $attempt++) {',\n"
        "        '    foreach ($proc in @(Get-Process -ErrorAction SilentlyContinue | Where-Object { try { $_.Path -and ([string]::Equals($_.Path, $oldExe, [StringComparison]::OrdinalIgnoreCase)) } catch { $false } })) {',\n"
        "        '        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue',\n"
        "        '    }',\n"
        "        '    Remove-Item -LiteralPath $oldDir -Recurse -Force -ErrorAction SilentlyContinue',\n"
        "        '    if (-not (Test-Path -LiteralPath $oldDir)) { $removed = $true; break }',\n"
        "        '    Start-Sleep -Milliseconds 1000',\n"
        "        '}',\n"
        "        'Remove-Item -LiteralPath $newRoot -Recurse -Force -ErrorAction SilentlyContinue',\n"
        "        'Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue',\n"
        "        'if ($removed) {',\n"
        "        '    try { Add-Content -LiteralPath $logPath -Encoding UTF8 -Value \"$(Get-Date -Format o) Old directory cleanup finished.\" } catch { }',\n"
        "        '} else {',\n"
        "        '    try { Add-Content -LiteralPath $logPath -Encoding UTF8 -Value \"$(Get-Date -Format o) Old directory cleanup deferred; files are still locked.\" } catch { }',\n"
        "        '}',\n"
        "        'Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue'\n"
        "    )\n"
        "    Set-Content -LiteralPath $cleanupScript -Encoding UTF8 -Value $cleanupLines\n"
        "    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList \"-NoProfile -ExecutionPolicy Bypass -File `\"$cleanupScript`\"\"\n"
        "    Write-UpdateLog 'Updated executable started.'\n"
        "} catch {\n"
        "    Write-UpdateLog $_.Exception.Message\n"
        "    throw\n"
        "} finally {\n"
        "    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue\n"
        "}\n"
    )


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_version(value: str) -> str:
    return value.strip().lstrip("vV")


def _version_tuple(value: str) -> tuple[int, ...]:
    cleaned = _normalize_version(value)
    parts = re.findall(r"\d+", cleaned)
    if not parts:
        return (0,)
    numbers = tuple(int(part) for part in parts[:4])
    return numbers + (0,) * (4 - len(numbers))
