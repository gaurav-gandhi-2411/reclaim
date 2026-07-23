; Reclaim installer script (Inno Setup 6/7).
;
; Per-user install, no admin required — matches reclaim.elevation.assert_not_elevated's
; refusal to ever run elevated (this tool must never need, request, or run under UAC
; elevation, at install time or run time). Installs the Nuitka --standalone build produced by
; packaging/build_installer.ps1 into the user's own AppData, and points every shortcut's
; working directory at the install folder itself so reclaim's relative data/ and config.toml
; paths land somewhere the user already owns.

#define MyAppName "Reclaim"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "Gaurav Gandhi"
#define MyAppExeName "reclaim.exe"
#define MyDistDir "build\entry_point.dist"

[Setup]
AppId={{B6C1B6C7-6B6A-4E3B-9B7B-2B7E1E7C6A21}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
; Per-user default (no admin dialog, no UAC prompt) — matches the "never elevated" invariant.
PrivilegesRequired=lowest
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputBaseFilename=reclaim-setup
OutputDir=dist
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\LICENSE
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "{#MyDistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; "Start in" is deliberately {app} (not {userdocs} or anything else) — reclaim's CLI defaults
; (data/, config.toml, data/mode_log.jsonl) are relative paths, so this is what makes them land
; in the per-user install folder the user already owns, rather than silently landing in
; whatever directory Explorer happened to launch the shortcut from.
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "dashboard"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "dashboard"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "dashboard"; WorkingDir: "{app}"; Description: "Launch {#MyAppName}"; Flags: postinstall nowait skipifsilent unchecked
