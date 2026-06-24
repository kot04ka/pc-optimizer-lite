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

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked
Name: "autostart"; Description: "Start PC Optimizer Lite with Windows (tray mode)"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"" --tray"; Tasks: autostart

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch PC Optimizer Lite"; Flags: nowait postinstall skipifsilent

