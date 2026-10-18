; Real maintained application; owner Desktop client configuration is optional build input.
#ifndef BundleDir
  #error BundleDir must identify the validated PyInstaller onedir output
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif

[Setup]
AppId={{D24539B1-3941-4273-9C44-A62A05A91623}
AppName=CacheCAD
AppVersion=0.1.0
AppVerName=CacheCAD Reconstructed 0.1.0
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
CloseApplications=no
RestartApplications=no
SetupLogging=yes
InfoBeforeFile=APPLICATION-README.txt
VersionInfoDescription=CacheCAD reconstructed application installer

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\CacheCAD"; Filename: "{app}\CacheCAD.exe"; WorkingDir: "{app}"
Name: "{group}\Uninstall CacheCAD"; Filename: "{uninstallexe}"

; No automatic launch, services, autorun, downloads, file associations or profile deletion.
