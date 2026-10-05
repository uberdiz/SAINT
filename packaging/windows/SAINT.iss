; Inno Setup script for SAINT-Setup.exe — built by packaging/windows/build.py:
;   ISCC /DAppVersion=2.1.0 /DSourceDir=build\windows\SAINT /DOutputDir=build\windows /DIconFile=...\SAINT.ico SAINT.iss
;
; Installs for the current user (no admin prompt) into %LOCALAPPDATA%\Programs\SAINT, adds a Start menu
; shortcut (and a desktop one if ticked), and a clean uninstaller. Settings, memory and history live in
; %LOCALAPPDATA%\SAINT and are kept on uninstall and upgrade.
;
; SAINT's voice (Kokoro, ~350 MB) isn't inside the installer: it's downloaded on the Ready page into
; %LOCALAPPDATA%\SAINT\tts\kokoro-onnx (checked against its SHA-256), skipped when it's already there.
; If that fails, SAINT downloads it on first start (core/model_assets.py).

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

[InstallDelete]
; An upgrade replaces the program files completely: 2.1 shipped PyTorch (4 GB) in _internal, and a leftover
; torch folder there would still be imported. The user's data in %LOCALAPPDATA%\SAINT isn't in {app}.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\models\hf\hub\models--hexgrad--Kokoro-82M"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{tmp}\kokoro-v1.0.onnx"; DestDir: "{localappdata}\SAINT\tts\kokoro-onnx"; Flags: external skipifsourcedoesntexist ignoreversion uninsneveruninstall
Source: "{tmp}\voices-v1.0.bin"; DestDir: "{localappdata}\SAINT\tts\kokoro-onnx"; Flags: external skipifsourcedoesntexist ignoreversion uninsneveruninstall

[Icons]
Name: "{userprograms}\SAINT"; Filename: "{app}\SAINT.exe"; WorkingDir: "{app}"; Comment: "SAINT desktop assistant"
Name: "{userdesktop}\SAINT"; Filename: "{app}\SAINT.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userstartup}\SAINT"; Filename: "{app}\SAINT.exe"; Parameters: "--background"; WorkingDir: "{app}"; Tasks: startup

[Run]
; SAINT Link (phone / other PCs): allow its port from the local network and Tailscale (100.64.0.0/10) only.
; Without this a dismissed firewall prompt, or a Wi-Fi Windows calls "public", silently blocks pairing.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""SAINT Link"""; Flags: runhidden; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""SAINT Link"" dir=in action=allow protocol=TCP localport=8765 remoteip=localsubnet,100.64.0.0/10 profile=any"; Flags: runhidden; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""SAINT Link"" dir=in action=allow protocol=UDP localport=8766 remoteip=localsubnet profile=any"; Flags: runhidden; Check: IsAdminInstallMode
Filename: "{app}\SAINT.exe"; Description: "Start SAINT now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""SAINT Link"""; Flags: runhidden; RunOnceId: "RemoveSAINTLinkFirewall"; Check: IsAdminInstallMode
Filename: "{cmd}"; Parameters: "/C taskkill /IM SAINT.exe /F"; Flags: runhidden; RunOnceId: "StopSAINT"

[UninstallDelete]
; Models SAINT downloaded next to itself after install (extra voices); the user's data in
; %LOCALAPPDATA%\SAINT is left alone.
Type: filesandordirs; Name: "{app}\models"

[Code]
const
  KokoroBase = 'https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/';

var
  DownloadPage: TDownloadWizardPage;

function KokoroDir: String;
begin
  Result := ExpandConstant('{localappdata}\SAINT\tts\kokoro-onnx');
end;

function KokoroInstalled: Boolean;
begin
  Result := FileExists(KokoroDir + '\kokoro-v1.0.onnx') and FileExists(KokoroDir + '\voices-v1.0.bin');
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage('Downloading SAINT''s voice', 'Kokoro, about 350 MB. This only happens once.', nil);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID <> wpReady) or KokoroInstalled then
    Exit;
  DownloadPage.Clear;
  DownloadPage.Add(KokoroBase + 'kokoro-v1.0.onnx', 'kokoro-v1.0.onnx', '7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5');
  DownloadPage.Add(KokoroBase + 'voices-v1.0.bin', 'voices-v1.0.bin', 'bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d');
  DownloadPage.Show;
  try
    try
      DownloadPage.Download;
    except
      // Install anyway: SAINT downloads the voice itself on first start.
      if not DownloadPage.AbortedByUser then
        SuppressibleMsgBox('SAINT''s voice couldn''t be downloaded now (' + GetExceptionMessage + '). ' +
          'SAINT will download it the first time it starts.', mbInformation, MB_OK, IDOK);
    end;
  finally
    DownloadPage.Hide;
  end;
end;
