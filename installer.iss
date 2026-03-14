[Setup]
AppName=maMusique
AppVersion=0.1.0
AppPublisher=Tom Vivian
AppPublisherURL=https://tomvivian28072005.github.io/maMusique/
DefaultDirName={userpf}\maMusique
DefaultGroupName=maMusique
UninstallDisplayIcon={app}\maMusique.exe
OutputDir=dist
OutputBaseFilename=maMusique-setup
Compression=lzma2/max
SolidCompression=yes
PrivilegesRequired=lowest
WizardStyle=modern
DisableProgramGroupPage=yes

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "Créer un raccourci sur le Bureau"; GroupDescription: "Raccourcis :"

[Files]
Source: "dist\maMusique\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\maMusique"; Filename: "{app}\maMusique.exe"
Name: "{userdesktop}\maMusique"; Filename: "{app}\maMusique.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\maMusique.exe"; Description: "Lancer maMusique"; Flags: nowait postinstall skipifsilent
