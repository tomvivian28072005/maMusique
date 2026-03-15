[Setup]
AppName=Clom
AppVersion=0.1.6
AppPublisher=Tom Vivian
AppPublisherURL=https://tomvivian28072005.github.io/maMusique/
DefaultDirName={userpf}\Clom
DefaultGroupName=Clom
UninstallDisplayIcon={app}\Clom.exe
OutputDir=dist
OutputBaseFilename=Clom-setup
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
Source: "dist\Clom\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Clom"; Filename: "{app}\Clom.exe"
Name: "{userdesktop}\Clom"; Filename: "{app}\Clom.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Clom.exe"; Description: "Lancer Clom"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if MsgBox('Supprimer aussi tes musiques, playlists et données ?'#13#10#13#10'(fichiers MP3, covers, base de données)', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      DelTree(ExpandConstant('{app}\downloads'), True, True, True);
      DelTree(ExpandConstant('{app}\covers'), True, True, True);
      DeleteFile(ExpandConstant('{app}\music.db'));
      DeleteFile(ExpandConstant('{app}\Clom.log'));
      DelTree(ExpandConstant('{app}'), True, True, True);
    end;
  end;
end;
