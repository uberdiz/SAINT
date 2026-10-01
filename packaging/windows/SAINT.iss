; Inno Setup script for SAINT-Setup.exe — built by packaging/windows/build.py:
;   ISCC /DAppVersion=2.1.0 /DSourceDir=build\windows\SAINT /DOutputDir=build\windows /DIconFile=...\SAINT.ico SAINT.iss
;
; Installs for the current user (no admin prompt) into %LOCALAPPDATA%\Programs\SAINT, adds a Start menu
; shortcut (and a desktop one if ticked), and a clean uninstaller. Settings, memory and history live in
; %LOCALAPPDATA%\SAINT and are kept on uninstall and upgrade.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6B1C8E3A-5A1D-4C55-9C2E-5A1E7D2B9F11}
AppName=SAINT
AppVersion={#AppVersion}
AppVerName=SAINT {#AppVersion}
AppPublisher=SAINT
AppPublisherURL=https://github.com/uberdiz/SAINT
DefaultDirName={localappdata}\Programs\SAINT
DefaultGroupName=SAINT
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=SAINT-Setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\SAINT.exe
UninstallDisplayName=SAINT
Compression=lzma2/fast
SolidCompression=no
DiskSpanning=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start SAINT when I sign in to Windows"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\SAINT"; Filename: "{app}\SAINT.exe"; WorkingDir: "{app}"; Comment: "SAINT desktop assistant"
Name: "{userdesktop}\SAINT"; Filename: "{app}\SAINT.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userstartup}\SAINT"; Filename: "{app}\SAINT.exe"; Parameters: "--background"; WorkingDir: "{app}"; Tasks: startup

[Run]
Filename: "{app}\SAINT.exe"; Description: "Start SAINT now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM SAINT.exe /F"; Flags: runhidden; RunOnceId: "StopSAINT"

[UninstallDelete]
; Models SAINT downloaded next to itself after install (extra voices); the user's data in
; %LOCALAPPDATA%\SAINT is left alone.
Type: filesandordirs; Name: "{app}\models"
