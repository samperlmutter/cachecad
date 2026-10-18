; Real maintained application; owner Desktop client configuration is optional build input.
#ifndef BundleDir
  #error BundleDir must identify the validated PyInstaller onedir output
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif

#ifndef ProductVersion
  #error ProductVersion must come from the validated version input
#endif
#ifndef WindowsVersion
  #error WindowsVersion must come from the validated version input
#endif
#ifndef ReleaseNumber
  #error ReleaseNumber must come from the validated version input
#endif
#ifndef PayloadIdentity
  #error PayloadIdentity must identify the exact packaged file set
#endif
#define IdentityKey "Software\CacheCADReconstructed\Installation"
#define RunningMutex "Global\CacheCADReconstructed.D24539B1-3941-4273-9C44-A62A05A91623.Running"
#define InstallMutex "Global\CacheCADReconstructed.D24539B1-3941-4273-9C44-A62A05A91623.Install"

[Setup]
AppId={{D24539B1-3941-4273-9C44-A62A05A91623}
AppName=CacheCAD
AppVersion={#ProductVersion}
AppVerName=CacheCAD Reconstructed {#ProductVersion} (build {#ReleaseNumber})
AppPublisher=Local reconstruction development build
DefaultDirName={localappdata}\Programs\CacheCAD Reconstructed
DefaultGroupName=CacheCAD
DisableProgramGroupPage=no
AllowNoIcons=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=CacheCAD-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=CacheCAD Reconstructed
UninstallDisplayIcon={app}\CacheCAD.exe
Uninstallable=yes
AppMutex={#RunningMutex}
CloseApplications=no
RestartApplications=no
SetupLogging=yes
InfoBeforeFile=APPLICATION-README.txt
VersionInfoVersion={#WindowsVersion}
VersionInfoProductVersion={#ProductVersion}
VersionInfoDescription=CacheCAD reconstructed application installer

[InstallDelete]
; Only the old installer-owned verified source snapshot is replaced. Never profiles/projects.
Type: filesandordirs; Name: "{app}\_internal\app-source"

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\CacheCAD"; Filename: "{app}\CacheCAD.exe"; WorkingDir: "{app}"
Name: "{group}\Uninstall CacheCAD"; Filename: "{uninstallexe}"

; No automatic launch, services, autorun, downloads, file associations or profile deletion.

[Registry]
Root: HKCU64; Subkey: "{#IdentityKey}"; ValueType: string; ValueName: "Version"; ValueData: "{#ProductVersion}"; Flags: uninsdeletekey
Root: HKCU64; Subkey: "{#IdentityKey}"; ValueType: string; ValueName: "WindowsVersion"; ValueData: "{#WindowsVersion}"
Root: HKCU64; Subkey: "{#IdentityKey}"; ValueType: dword; ValueName: "ReleaseNumber"; ValueData: "{#ReleaseNumber}"
Root: HKCU64; Subkey: "{#IdentityKey}"; ValueType: string; ValueName: "PayloadIdentity"; ValueData: "{#PayloadIdentity}"
Root: HKCU64; Subkey: "{#IdentityKey}"; ValueType: string; ValueName: "InstallPath"; ValueData: "{app}"

[Code]
var
  InstallGate: THandle;
  GateOwned: Boolean;

function CreateMutexW(Attributes: LongWord; InitialOwner: LongBool; Name: String): THandle;
  external 'CreateMutexW@kernel32.dll stdcall';
function OpenMutexW(Access: LongWord; InheritHandle: LongBool; Name: String): THandle;
  external 'OpenMutexW@kernel32.dll stdcall';
function WaitForSingleObject(Handle: THandle; Milliseconds: LongWord): LongWord;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function ReleaseMutex(Handle: THandle): LongBool;
  external 'ReleaseMutex@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): LongBool;
  external 'CloseHandle@kernel32.dll stdcall';
function GetLastError: LongWord;
  external 'GetLastError@kernel32.dll stdcall';
function GetFileAttributesW(Name: String): LongWord;
  external 'GetFileAttributesW@kernel32.dll stdcall';

procedure ReleaseInstallGate;
begin
  if InstallGate <> 0 then begin
    if GateOwned then ReleaseMutex(InstallGate);
    CloseHandle(InstallGate);
    InstallGate := 0;
    GateOwned := False;
  end;
end;

function AcquireInstallGate: Boolean;
var
  WaitResult: LongWord;
  RunningHandle: THandle;
begin
  Result := False;
  InstallGate := CreateMutexW(0, False, '{#InstallMutex}');
  if InstallGate = 0 then begin
    Log('CACHECAD_GATE_REFUSED: cannot create install exclusion');
    Exit;
  end;
  { The handoff helper briefly transfers this gate; bounded wait never closes apps. }
  WaitResult := WaitForSingleObject(InstallGate, 10000);
  if WaitResult <> 0 then begin
    { WAIT_ABANDONED grants ownership, but the interrupted state is not safe. }
    GateOwned := WaitResult = $80;
    Log('CACHECAD_GATE_REFUSED: another installer or app is acquiring the gate');
    ReleaseInstallGate;
    Exit;
  end;
  GateOwned := True;
  RunningHandle := OpenMutexW($00100000, False, '{#RunningMutex}');
  if RunningHandle <> 0 then begin
    CloseHandle(RunningHandle);
    Log('CACHECAD_GATE_REFUSED: CacheCAD is running; finish work and close it first');
    ReleaseInstallGate;
    Exit;
  end;
  { ERROR_FILE_NOT_FOUND is the only safe absent-object result. }
  if GetLastError <> 2 then begin
    Log('CACHECAD_GATE_REFUSED: cannot establish application exclusion');
    ReleaseInstallGate;
    Exit;
  end;
  Result := True;
end;

function ReleaseIdentityAllowed: Boolean;
var
  ExistingVersion, ExistingPayload: String;
  ExistingRelease: Cardinal;
  ExistingPacked, ProposedPacked: Int64;
begin
  Result := False;
  if RegKeyExists(HKCU64, '{#IdentityKey}') then begin
    if not RegQueryStringValue(HKCU64, '{#IdentityKey}', 'WindowsVersion', ExistingVersion) or
       not RegQueryDWordValue(HKCU64, '{#IdentityKey}', 'ReleaseNumber', ExistingRelease) or
       not RegQueryStringValue(HKCU64, '{#IdentityKey}', 'PayloadIdentity', ExistingPayload) or
       not StrToVersion(ExistingVersion, ExistingPacked) or
       not StrToVersion('{#WindowsVersion}', ProposedPacked) then begin
      Log('CACHECAD_VERSION_REFUSED: installed identity is incomplete');
      Exit;
    end;
    if (ExistingRelease > {#ReleaseNumber}) or
       (ComparePackedVersion(ExistingPacked, ProposedPacked) > 0) then begin
      Log('CACHECAD_DOWNGRADE_REFUSED: a newer release is installed');
      Exit;
    end;
    if (ExistingRelease = {#ReleaseNumber}) or
       (ComparePackedVersion(ExistingPacked, ProposedPacked) = 0) then begin
      if (ExistingRelease <> {#ReleaseNumber}) or
         (ExistingVersion <> '{#WindowsVersion}') or
         (ExistingPayload <> '{#PayloadIdentity}') then begin
        Log('CACHECAD_REPAIR_REFUSED: repair requires the exact installed release payload');
        Exit;
      end;
    end;
  end else begin
    { A pre-version-identity install is allowed only when strictly older. }
    if RegQueryStringValue(HKCU64,
       'Software\Microsoft\Windows\CurrentVersion\Uninstall\{D24539B1-3941-4273-9C44-A62A05A91623}_is1',
       'DisplayVersion', ExistingVersion) then begin
      if not StrToVersion(ExistingVersion, ExistingPacked) or
         not StrToVersion('{#ProductVersion}', ProposedPacked) or
         (ComparePackedVersion(ExistingPacked, ProposedPacked) >= 0) then begin
        Log('CACHECAD_VERSION_REFUSED: cannot repair or downgrade an unidentified legacy build');
        Exit;
      end;
    end;
  end;
  Result := True;
end;

function InitializeSetup: Boolean;
begin
  Result := AcquireInstallGate;
  if Result then Result := ReleaseIdentityAllowed;
  if not Result then begin
    ReleaseInstallGate;
    if not WizardSilent then
      MsgBox('CacheCAD cannot be installed while it is running, while another install is active, or over an incompatible release. No application was closed. See the setup log.', mbError, MB_OK);
  end;
end;

function NoReparseParents(Path: String): Boolean;
var
  Attributes: LongWord;
  Parent: String;
begin
  Result := False;
  while Path <> '' do begin
    Attributes := GetFileAttributesW(Path);
    if Attributes = $FFFFFFFF then begin
      if (GetLastError <> 2) and (GetLastError <> 3) then Exit;
    end else if (Attributes and $400) <> 0 then Exit;
    Parent := ExtractFileDir(Path);
    if Parent = Path then Break;
    Path := Parent;
  end;
  Result := True;
end;

function NoReparseSourceTree(Path: String): Boolean;
var
  Entry: TFindRec;
  EnumerationError: LongWord;
begin
  Result := False;
  if not NoReparseParents(Path) then Exit;
  if not DirExists(Path) then begin
    Result := not FileExists(Path);
    Exit;
  end;
  if FindFirst(AddBackslash(Path) + '*', Entry) then begin
    try
      repeat
        if (Entry.Name <> '.') and (Entry.Name <> '..') then begin
          { A user may have selected an unusual custom profile/project location. }
          if (CompareText(Entry.Name, 'application-profile.json') = 0) or
             (CompareText(Entry.Name, 'app_data') = 0) or
             (CompareText(Entry.Name, '.cachecad') = 0) then Exit;
          if (Entry.Attributes and $400) <> 0 then Exit;
          if (Entry.Attributes and $10) <> 0 then
            if not NoReparseSourceTree(AddBackslash(Path) + Entry.Name) then Exit;
        end;
      until not FindNext(Entry);
      EnumerationError := GetLastError;
      if EnumerationError <> 18 then Exit; { ERROR_NO_MORE_FILES only }
    finally
      FindClose(Entry);
    end;
  end else begin
    EnumerationError := GetLastError;
    if (EnumerationError <> 2) and (EnumerationError <> 18) then Exit;
  end;
  Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  RegisteredPath, SourcePath: String;
begin
  Result := 'The installation exclusion, release identity, or owned source path check failed. Close CacheCAD and retry the supported installer.';
  if not GateOwned or not ReleaseIdentityAllowed then Exit;
  SourcePath := ExpandConstant('{app}\_internal\app-source');
  if not NoReparseSourceTree(SourcePath) then begin
    Log('CACHECAD_SOURCE_REFUSED: source tree contains linked, unreadable, profile, or project content');
    Exit;
  end;
  if not RegQueryStringValue(HKCU64, '{#IdentityKey}', 'InstallPath', RegisteredPath) then
    RegQueryStringValue(HKCU64,
      'Software\Microsoft\Windows\CurrentVersion\Uninstall\{D24539B1-3941-4273-9C44-A62A05A91623}_is1',
      'Inno Setup: App Path', RegisteredPath);
  if RegisteredPath <> '' then begin
    if CompareText(RemoveBackslashUnlessRoot(RegisteredPath),
                   RemoveBackslashUnlessRoot(ExpandConstant('{app}'))) <> 0 then Exit;
  end else if DirExists(SourcePath) then Exit;
  Result := '';
end;

procedure DeinitializeSetup;
begin
  ReleaseInstallGate;
end;

function InitializeUninstall: Boolean;
begin
  Result := AcquireInstallGate;
end;

procedure DeinitializeUninstall;
begin
  ReleaseInstallGate;
end;
