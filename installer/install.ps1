$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Windows.Forms

$AppName = 'PC Optimizer Lite'
$AppVersion = '__APP_VERSION__'
$SourceExe = Join-Path $PSScriptRoot 'PC Optimizer Lite.exe'
if (-not (Test-Path -LiteralPath $SourceExe)) {
    $SourceExe = Join-Path $PSScriptRoot 'PCOptimizerLite.exe'
}
$InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\PC Optimizer Lite'
$TargetExe = Join-Path $InstallDir 'PC Optimizer Lite.exe'
$StartMenuDir = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs\PC Optimizer Lite'
$DesktopShortcut = Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'PC Optimizer Lite.lnk'
$StartMenuShortcut = Join-Path $StartMenuDir 'PC Optimizer Lite.lnk'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$UninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\PC Optimizer Lite'

function Show-InstallOptions {
    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'PC Optimizer Lite Setup'
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.Width = 430
    $form.Height = 220

    $label = New-Object System.Windows.Forms.Label
    $label.Text = "Install PC Optimizer Lite to:`r`n$InstallDir"
    $label.AutoSize = $true
    $label.Left = 18
    $label.Top = 18
    $form.Controls.Add($label)

    $desktop = New-Object System.Windows.Forms.CheckBox
    $desktop.Text = 'Create desktop shortcut'
    $desktop.Left = 20
    $desktop.Top = 72
    $desktop.Width = 330
    $desktop.Checked = $true
    $form.Controls.Add($desktop)

    $autostart = New-Object System.Windows.Forms.CheckBox
    $autostart.Text = 'Start with Windows (tray mode)'
    $autostart.Left = 20
    $autostart.Top = 102
    $autostart.Width = 330
    $form.Controls.Add($autostart)

    $install = New-Object System.Windows.Forms.Button
    $install.Text = 'Install'
    $install.Left = 220
    $install.Top = 145
    $install.Width = 85
    $install.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $form.AcceptButton = $install
    $form.Controls.Add($install)

    $cancel = New-Object System.Windows.Forms.Button
    $cancel.Text = 'Cancel'
    $cancel.Left = 315
    $cancel.Top = 145
    $cancel.Width = 85
    $cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $form.CancelButton = $cancel
    $form.Controls.Add($cancel)

    $result = $form.ShowDialog()
    if ($result -ne [System.Windows.Forms.DialogResult]::OK) {
        exit 1602
    }

    return [pscustomobject]@{
        DesktopShortcut = $desktop.Checked
        Autostart = $autostart.Checked
    }
}

function New-Shortcut {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Target,
        [string]$Arguments = '',
        [string]$WorkingDirectory = ''
    )
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($Path)
    $shortcut.TargetPath = $Target
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = $WorkingDirectory
    $shortcut.IconLocation = "$Target,0"
    $shortcut.Save()
}

function Stop-InstalledApp {
    if (-not (Test-Path -LiteralPath $TargetExe)) {
        return
    }
    Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            if ($_.Path -eq $TargetExe) {
                Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
            }
        } catch {
        }
    }
}

try {
    if (-not (Test-Path -LiteralPath $SourceExe)) {
        throw "Missing payload file: $SourceExe"
    }

    $options = Show-InstallOptions
    Stop-InstalledApp

    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Copy-Item -LiteralPath $SourceExe -Destination $TargetExe -Force

    New-Item -ItemType Directory -Force -Path $StartMenuDir | Out-Null
    New-Shortcut -Path $StartMenuShortcut -Target $TargetExe -WorkingDirectory $InstallDir
    if ($options.DesktopShortcut) {
        New-Shortcut -Path $DesktopShortcut -Target $TargetExe -WorkingDirectory $InstallDir
    } elseif (Test-Path -LiteralPath $DesktopShortcut) {
        Remove-Item -LiteralPath $DesktopShortcut -Force
    }

    New-Item -Path $RunKey -Force | Out-Null
    if ($options.Autostart) {
        New-ItemProperty -Path $RunKey -Name $AppName -Value "`"$TargetExe`" --tray" -PropertyType String -Force | Out-Null
    } else {
        Remove-ItemProperty -Path $RunKey -Name $AppName -ErrorAction SilentlyContinue
    }

    $uninstallScript = Join-Path $InstallDir 'Uninstall.ps1'
    @'
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
'@ | Set-Content -LiteralPath $uninstallScript -Encoding UTF8

    New-Item -Path $UninstallKey -Force | Out-Null
    $estimatedSize = [int]([math]::Ceiling((Get-Item -LiteralPath $TargetExe).Length / 1KB))
    New-ItemProperty -Path $UninstallKey -Name 'DisplayName' -Value $AppName -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $UninstallKey -Name 'DisplayVersion' -Value $AppVersion -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $UninstallKey -Name 'Publisher' -Value 'PC Optimizer Lite' -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $UninstallKey -Name 'InstallLocation' -Value $InstallDir -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $UninstallKey -Name 'DisplayIcon' -Value $TargetExe -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $UninstallKey -Name 'UninstallString' -Value "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$uninstallScript`"" -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $UninstallKey -Name 'QuietUninstallString' -Value "powershell.exe -WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -File `"$uninstallScript`"" -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $UninstallKey -Name 'NoModify' -Value 1 -PropertyType DWord -Force | Out-Null
    New-ItemProperty -Path $UninstallKey -Name 'NoRepair' -Value 1 -PropertyType DWord -Force | Out-Null
    New-ItemProperty -Path $UninstallKey -Name 'EstimatedSize' -Value $estimatedSize -PropertyType DWord -Force | Out-Null
    New-ItemProperty -Path $UninstallKey -Name 'InstallDate' -Value (Get-Date -Format 'yyyyMMdd') -PropertyType String -Force | Out-Null

    [System.Windows.Forms.MessageBox]::Show(
        "PC Optimizer Lite installed successfully.`r`n`r`n$TargetExe",
        'PC Optimizer Lite Setup',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Information
    ) | Out-Null
} catch {
    [System.Windows.Forms.MessageBox]::Show(
        "Installation failed:`r`n$($_.Exception.Message)",
        'PC Optimizer Lite Setup',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
    exit 1
}

