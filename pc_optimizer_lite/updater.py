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

# Fill these placeholders with the real GitHub repository path before publishing
# releases, or override them through the saved settings UI/config.json.
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
    auth_token: str = ""


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
    auth_token: str = "",
    current_version: str = APP_VERSION,
    skipped_version: str = "",
    timeout_seconds: float = 6.0,
    force: bool = False,
    cache_ttl_seconds: float = 4 * 60 * 60,
) -> UpdateCheckResult:
    """Query GitHub Releases and return only actionable update information."""

    owner = owner.strip()
    repo = repo.strip()
    auth_token = (auth_token or os.environ.get("PC_OPTIMIZER_GITHUB_TOKEN") or "").strip()
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
            return _result_from_cache(cached, current_version, skipped_version, auth_token)

    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    request = urllib.request.Request(
        api_url,
        headers=_github_headers(current_version, auth_token),
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
    asset = _select_windows_exe_asset(payload.get("assets", []), body, auth_token)
    if not is_newer_version(latest_version, current_version):
        if (
            latest_version == _normalize_version(current_version)
            and asset is not None
            and _asset_differs_from_current_exe(asset)
        ):
            result = UpdateCheckResult(
                configured=True,
                update_available=True,
                latest_version=latest_version,
                release_name=str(payload.get("name") or latest_version),
                release_url=str(payload.get("html_url") or ""),
                body=body,
                asset=asset,
                message="Release asset differs from installed exe.",
            )
            _save_cached_update_check(owner, repo, result)
            return result
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
        message="Update available." if asset else "Release has no .exe asset.",
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
    safe_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", asset.name).strip() or "pc_optimizer_lite_update.exe"
    target = destination / safe_name
    request = urllib.request.Request(
        asset.url,
        headers=_github_headers(APP_VERSION, asset.auth_token),
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


def install_downloaded_update(downloaded_exe: Path, *, current_exe: Path | None = None) -> Path:
    """Launch a small bat script that swaps the running packaged executable."""

    current = current_exe or Path(sys.executable)
    if current.suffix.lower() != ".exe" or current.name.lower() in {"python.exe", "pythonw.exe"}:
        raise UpdateError("Automatic replacement is available only in the packaged .exe build.")
    if not downloaded_exe.exists():
        raise UpdateError(f"Downloaded update not found: {downloaded_exe}")

    staged = current.with_name(current.stem + "_new.exe")
    if downloaded_exe.resolve() != staged.resolve():
        staged.write_bytes(downloaded_exe.read_bytes())
    script = current.with_name("apply_pc_optimizer_lite_update.bat")
    script.write_text(_replacement_script(current=current, staged=staged, pid=os.getpid()), encoding="utf-8")
    creation_flags = 0
    if os.name == "nt":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
    subprocess.Popen(
        ["cmd.exe", "/c", str(script)],
        cwd=str(current.parent),
        creationflags=creation_flags,
        close_fds=True,
    )
    return script


def download_and_install_update(asset: ReleaseAsset, progress_callback: Callable[[int, str], None] | None = None) -> Path:
    """Download an asset and stage replacement for the current executable."""

    downloaded = download_update_asset(asset, progress_callback=progress_callback)
    return install_downloaded_update(downloaded)


def _asset_differs_from_current_exe(asset: ReleaseAsset) -> bool:
    """Detect same-version republished exe assets for packaged builds."""

    current = Path(sys.executable)
    if current.suffix.lower() != ".exe" or current.name.lower() in {"python.exe", "pythonw.exe"}:
        return False
    try:
        if asset.sha256:
            return _sha256_file(current).lower() != asset.sha256.lower()
        if asset.size:
            return current.stat().st_size != asset.size
    except OSError:
        return False
    return False


def _select_windows_exe_asset(raw_assets: Any, release_body: str, auth_token: str = "") -> ReleaseAsset | None:
    if not isinstance(raw_assets, list):
        return None
    hashes = _sha256_hashes_from_text(release_body)
    candidates: list[ReleaseAsset] = []
    for item in raw_assets:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        url = str(item.get("browser_download_url") or "")
        if not name.lower().endswith(".exe") or not url:
            continue
        candidates.append(
            ReleaseAsset(
                name=name,
                url=url,
                size=int(item.get("size") or 0),
                sha256=hashes.get(name.lower(), "") or hashes.get("*", ""),
                auth_token=auth_token,
            )
        )
    if not candidates:
        return None
    preferred = [
        asset
        for asset in candidates
        if "setup" not in asset.name.lower() and "installer" not in asset.name.lower()
    ]
    return (preferred or candidates)[0]


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
    auth_token: str = "",
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
            auth_token=auth_token,
        )
        if asset_payload
        else None
    )
    update_available = bool(cache.get("update_available")) and (
        is_newer_version(latest_version, current_version)
        or (asset is not None and latest_version == _normalize_version(current_version) and _asset_differs_from_current_exe(asset))
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


def _github_headers(version: str, auth_token: str = "") -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{__app_name__}/{version}",
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    return headers


def _sha256_hashes_from_text(text: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    generic = re.search(r"sha256\s*[:=]\s*([a-fA-F0-9]{64})", text, flags=re.IGNORECASE)
    if generic:
        hashes["*"] = generic.group(1)
    for match in re.finditer(r"([A-Za-z0-9._ -]+\.exe).*?([a-fA-F0-9]{64})", text, flags=re.IGNORECASE | re.DOTALL):
        hashes[match.group(1).strip().lower()] = match.group(2)
    return hashes


def _replacement_script(*, current: Path, staged: Path, pid: int) -> str:
    return (
        "@echo off\n"
        "setlocal\n"
        f"set \"OLD={current}\"\n"
        f"set \"NEW={staged}\"\n"
        f"set \"PID={pid}\"\n"
        ":wait\n"
        "tasklist /FI \"PID eq %PID%\" | find \"%PID%\" >nul\n"
        "if not errorlevel 1 (\n"
        "  timeout /t 1 /nobreak >nul\n"
        "  goto wait\n"
        ")\n"
        "move /Y \"%NEW%\" \"%OLD%\" >nul\n"
        "start \"\" \"%OLD%\"\n"
        "del \"%~f0\"\n"
    )


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
