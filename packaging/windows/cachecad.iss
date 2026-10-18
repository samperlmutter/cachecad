; Personal/noncommercial offline development tryout; no original branding/assets.
#ifndef BundleDir
  #error BundleDir must identify the validated PyInstaller onedir output
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif

[Setup]
AppId={{4A03D312-83C5-4EC3-9C46-8CE43DA792EA}
AppName=CacheCAD Offline Development
AppVersion=0.1.0
AppVerName=CacheCAD Offline Development 0.1.0
AppPublisher=Local reconstruction development build
DefaultDirName={localappdata}\Programs\CacheCAD Offline Development
DefaultGroupName=CacheCAD Offline Development
DisableProgramGroupPage=no
AllowNoIcons=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=CacheCAD-Offline-Development-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=CacheCAD Offline Development
UninstallDisplayIcon={app}\CacheCAD-Offline.exe
Uninstallable=yes
CloseApplications=no
RestartApplications=no
SetupLogging=yes
InfoBeforeFile=OFFLINE-README.txt
VersionInfoDescription=CacheCAD offline development installer (disposable data)

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\CacheCAD Offline Development"; Filename: "{app}\CacheCAD-Offline.exe"; WorkingDir: "{app}"
Name: "{group}\Uninstall CacheCAD Offline Development"; Filename: "{uninstallexe}"

; Intentionally no [Run], [UninstallRun], services, autorun, downloads or file associations.
