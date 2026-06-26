#define MyAppName "PC Optimizer Lite"
#ifndef MyAppVersion
#define MyAppVersion "0.0.0"
#endif
#define MyAppExeName "PC Optimizer Lite.exe"

[Setup]
AppId={{6E9D6384-2E39-420D-A63B-2C195A5C5A09}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=PC Optimizer Lite
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\installer_output
OutputBaseFilename=PC-Optimizer-Lite-Setup
Compression=lzma
SolidCompression=yes
PrivilegesRequired=lowest
SetupIconFile=..\assets\pc_optimizer_lite.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
WizardStyle=modern
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked
Name: "autostart"; Description: "Start PC Optimizer Lite with Windows (tray mode)"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "..\dist\{#MyAppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: filesandordirs; Name: "{app}\*"
Type: files; Name: "{app}\PC Optimizer Lite_new.exe"
Type: files; Name: "{app}\apply_pc_optimizer_lite_update.ps1"
Type: files; Name: "{app}\pc_optimizer_lite_update.log"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"" --tray"; Tasks: autostart

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
function PowerShellQuote(Value: string): string;
begin
  StringChangeEx(Value, '''', '''''', True);
  Result := '''' + Value + '''';
end;

procedure RunHiddenPowerShell(Command: string);
var
  ResultCode: Integer;
begin
  Exec(
    'powershell.exe',
    '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "' + Command + '"',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
end;

procedure StopInstalledApp();
var
  TargetExe: string;
begin
  TargetExe := ExpandConstant('{app}\{#MyAppExeName}');
  RunHiddenPowerShell(
    '$target = ' + PowerShellQuote(TargetExe) + '; ' +
    'Get-Process -ErrorAction SilentlyContinue | ForEach-Object { ' +
    'try { if ($_.Path -and [string]::Equals($_.Path, $target, [StringComparison]::OrdinalIgnoreCase)) { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue } } catch { } }'
  );
end;

procedure CleanupStalePyInstallerTemp();
begin
  RunHiddenPowerShell(
    '$ErrorActionPreference = ''SilentlyContinue''; ' +
    'Get-ChildItem -LiteralPath $env:TEMP -Directory -Filter ''_MEI*'' -ErrorAction SilentlyContinue | ' +
    'ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }'
  );
end;

procedure CleanupOldUpdaterArtifacts();
var
  ProgramsDir: string;
begin
  ProgramsDir := ExtractFileDir(ExpandConstant('{app}'));
  RunHiddenPowerShell(
    '$ErrorActionPreference = ''SilentlyContinue''; ' +
    '$dir = ' + PowerShellQuote(ProgramsDir) + '; ' +
    'Get-ChildItem -LiteralPath $dir -Directory -Filter ''PC Optimizer Lite.update.*'' -ErrorAction SilentlyContinue | ' +
    'ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }; ' +
    'Get-ChildItem -LiteralPath $dir -File -Filter ''pc_optimizer_lite_update_*.log'' -ErrorAction SilentlyContinue | ' +
    'ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }; ' +
    'Remove-Item -LiteralPath (Join-Path $env:TEMP ''pc_optimizer_lite_update'') -Recurse -Force -ErrorAction SilentlyContinue'
  );
end;

procedure CleanupLegacyRegistryEntries();
begin
  RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\PC Optimizer Lite');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then begin
    StopInstalledApp();
    CleanupStalePyInstallerTemp();
    CleanupOldUpdaterArtifacts();
    CleanupLegacyRegistryEntries();
  end;
  if CurStep = ssPostInstall then begin
    CleanupStalePyInstallerTemp();
  end;
end;

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch PC Optimizer Lite"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#MyAppExeName}"; Flags: nowait skipifnotsilent

