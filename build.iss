; build.iss — Inno Setup script for PZ Server Manager
; Usage: iscc /DMyAppVersion=0.1.0 build.iss
; Output: Output\PZServerManager-Setup.exe
;
; Requires PyInstaller to have already built dist\PZServerManager.exe

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "PZ Server Manager"
#define MyAppPublisher "PZ Server Manager Contributors"
#define MyAppURL "https://github.com/mirokune/Zomboid-Server-Manager"
#define MyAppExeName "PZServerManager.exe"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\PZServerManager
DefaultGroupName={#MyAppName}
OutputBaseFilename=PZServerManager-Setup
OutputDir={#SourcePath}\Output
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Allow user to choose install dir; do not require admin (avoids C:\Program Files write issues)
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
