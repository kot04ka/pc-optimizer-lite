$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$VersionFile = Join-Path $Root 'pc_optimizer_lite\version.py'
$VersionMatch = Select-String -Path $VersionFile -Pattern 'APP_VERSION\s*=\s*"([^"]+)"'
if (-not $VersionMatch) {
    throw "APP_VERSION was not found in $VersionFile"
}
$Version = $VersionMatch.Matches[0].Groups[1].Value
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
$LegacyPortableExe = Join-Path $Root 'dist\PC Optimizer Lite.exe'
$OnedirSpecPath = Join-Path $Root 'PC Optimizer Lite Onedir.spec'
$OnedirAppDir = Join-Path $Root 'dist\PC Optimizer Lite'
$OnedirExe = Join-Path $OnedirAppDir 'PC Optimizer Lite.exe'
$InstallerOutput = Join-Path $Root 'installer_output'
$InstallerExe = Join-Path $InstallerOutput 'PC-Optimizer-Lite-Setup.exe'
$OnedirZip = Join-Path $InstallerOutput 'PC-Optimizer-Lite-windows-x64.zip'
$IconPath = Join-Path $Root 'assets\pc_optimizer_lite.ico'
$InnoScript = Join-Path $Root 'installer\PC Optimizer Lite.iss'

function Get-Python {
    if (Test-Path -LiteralPath $VenvPython) {
        return $VenvPython
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $python) {
        throw 'python.exe was not found. Install Python or create .venv first.'
    }
    & $python.Source -m venv (Join-Path $Root '.venv')
    return $VenvPython
}

function Get-InnoCompiler {
    $command = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    $candidates = @(
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe',
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    return $null
}

function Build-IExpressInstaller {
    $iexpress = Get-Command iexpress.exe -ErrorAction SilentlyContinue
    if (-not $iexpress) {
        return $false
    }

    $payloadDir = Join-Path $InstallerOutput 'iexpress_payload'
    $sedPath = Join-Path $InstallerOutput 'pc_optimizer_lite_iexpress.sed'
    if (Test-Path -LiteralPath $payloadDir) {
        Remove-Item -LiteralPath $payloadDir -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $payloadDir | Out-Null
    Copy-Item -LiteralPath $OnedirZip -Destination (Join-Path $payloadDir 'PC-Optimizer-Lite-windows-x64.zip') -Force
    $installScript = (Get-Content -LiteralPath (Join-Path $Root 'installer\install.ps1') -Raw).Replace('__APP_VERSION__', $Version)
    Set-Content -LiteralPath (Join-Path $payloadDir 'install.ps1') -Value $installScript -Encoding UTF8

    $payloadDirForSed = $payloadDir.TrimEnd('\') + '\'
    Remove-Item -LiteralPath $InstallerExe -Force -ErrorAction SilentlyContinue
    $sed = @"
[Version]
Class=IEXPRESS
SEDVersion=3

[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=0
HideExtractAnimation=1
UseLongFileName=1
InsideCompressed=1
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=
DisplayLicense=
FinishMessage=PC Optimizer Lite setup finished.
TargetName=$InstallerExe
FriendlyName=PC Optimizer Lite Setup
AppLaunched=powershell.exe -NoProfile -ExecutionPolicy Bypass -File install.ps1
PostInstallCmd=<None>
AdminQuietInstCmd=
UserQuietInstCmd=
SourceFiles=SourceFiles

[Strings]
FILE0="PC-Optimizer-Lite-windows-x64.zip"
FILE1="install.ps1"

[SourceFiles]
SourceFiles0=$payloadDirForSed

[SourceFiles0]
%FILE0%=
%FILE1%=
"@
    Set-Content -LiteralPath $sedPath -Value $sed -Encoding ASCII
    & $iexpress.Source /N /Q $sedPath
    if (-not (Test-Path -LiteralPath $InstallerExe)) {
        Write-Warning "IExpress did not create installer: $InstallerExe"
        return $false
    }
    return $true
}

Set-Location $Root
New-Item -ItemType Directory -Force -Path $InstallerOutput | Out-Null
Remove-Item -LiteralPath (Join-Path $InstallerOutput 'iexpress_payload') -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $InstallerOutput 'pc_optimizer_lite_iexpress.sed') -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $InstallerOutput 'pc_optimizer_lite_iexpress_test.sed') -Force -ErrorAction SilentlyContinue
Get-ChildItem -LiteralPath $InstallerOutput -Filter '~PCOptimizerLiteSetup*' -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue
Get-ChildItem -LiteralPath $InstallerOutput -Filter '~PC Optimizer Lite Setup*' -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue
Get-ChildItem -LiteralPath $InstallerOutput -Filter '~PC-Optimizer-Lite-Setup*' -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue

$Python = Get-Python
& $Python -m pip install -r (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE -ne 0) {
    throw "pip install failed with exit code $LASTEXITCODE"
}
& $Python (Join-Path $Root 'tools\create_icon.py') | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Icon generation failed with exit code $LASTEXITCODE"
}

if (-not (Test-Path -LiteralPath $IconPath)) {
    throw "Icon was not generated: $IconPath"
}

Remove-Item -LiteralPath $LegacyPortableExe -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $OnedirAppDir -Recurse -Force -ErrorAction SilentlyContinue
& $Python -m PyInstaller --noconfirm --clean $OnedirSpecPath
if ($LASTEXITCODE -ne 0) {
    throw "Onedir PyInstaller failed with exit code $LASTEXITCODE"
}
if (-not (Test-Path -LiteralPath $OnedirExe)) {
    throw "Onedir EXE was not built: $OnedirExe"
}

Remove-Item -LiteralPath $OnedirZip -Force -ErrorAction SilentlyContinue
Compress-Archive -LiteralPath $OnedirAppDir -DestinationPath $OnedirZip -Force
if (-not (Test-Path -LiteralPath $OnedirZip)) {
    throw "Onedir ZIP was not built: $OnedirZip"
}

$iscc = Get-InnoCompiler
if ($iscc) {
    & $iscc "/DMyAppVersion=$Version" $InnoScript
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup failed with exit code $LASTEXITCODE"
    }
    if (-not (Test-Path -LiteralPath $InstallerExe)) {
        throw "Inno Setup did not create installer: $InstallerExe"
    }
} else {
    Write-Warning 'Inno Setup 6 (iscc.exe) was not found. Building a local IExpress setup wrapper around the onedir ZIP instead.'
    if (-not (Build-IExpressInstaller)) {
        Write-Warning 'Installer was not built locally. Install Inno Setup 6 or use the GitHub Actions release workflow.'
    }
}

Write-Host ''
Write-Host 'Build completed.'
Write-Host "Onedir app: $OnedirAppDir"
Write-Host "Onedir ZIP: $OnedirZip"
if (Test-Path -LiteralPath $InstallerExe) {
    Write-Host "Installer: $InstallerExe"
} else {
    Write-Host 'Installer: not built'
}
Write-Host 'Rebuild command: .\build.bat'

