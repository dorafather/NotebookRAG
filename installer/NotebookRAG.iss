; NotebookRAG.iss — [티켓 I] 사용자 모드 설치 프로그램 (Inno Setup 6)
;
; 이 파일은 UTF-8(BOM 포함)로 저장돼 있어야 한글이 깨지지 않는다.
; build_installer.ps1이 컴파일 직전에 BOM을 강제로 붙여준다 — 직접
; 텍스트 에디터로 저장할 때도 "UTF-8 with BOM"으로 저장할 것.
;
; 전제: 컴파일 전에 반드시
;   1) sync_bin_assets.ps1 실행 (릴리즈 루트 마스터 → bin/ 동기화)
;   2) installer\staging\config\settings.json.template 생성
;      (MODEL_DOWNLOAD_URL/MODEL_SHA256 값을 채운 배포용 사본 —
;       릴리즈 루트/bin의 마스터 사본은 정책상 계속 빈 값으로 유지)
; build_installer.ps1이 이 두 단계를 자동으로 처리한다. 이 .iss를 직접
; ISCC로 컴파일하지 말고 항상 build_installer.ps1을 통해 실행할 것.

#define MyAppName "NotebookRAG"
; [정보탭_버전관리] 단일 진실 원천은 src/app_paths.py의 NOTEBOOKRAG_VERSION —
; build_installer.ps1이 그 값을 읽어 "ISCC /DMyAppVersion=..."로 넘겨준다.
; 여기 기본값은 build_installer.ps1을 거치지 않고 직접 컴파일했을 때만 쓰이는
; 폴백이니, 값 자체를 신경 쓸 필요는 없다(항상 build_installer.ps1로 빌드할 것).
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-direct-iscc-build"
#endif
#define MyAppPublisher "Point-I"
#define MyAppExeName "bin\tray\tray.exe"

[Setup]
AppId={{6C9F0F2E-6C7B-4B7B-9C3E-2B7F2C6E9F41}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\NotebookRAG
DefaultGroupName=NotebookRAG
DisableProgramGroupPage=yes
; [확정 설계 1] 사용자 모드, 관리자 권한 불필요 — 회사 노트북(관리자 권한
; 없음)에서도 설치돼야 한다는 목표와 직결.
PrivilegesRequired=lowest
OutputDir=output
OutputBaseFilename=NotebookRAG_Setup
Compression=lzma2
SolidCompression=yes
SetupIconFile=..\tray-src\resources\notebookrag.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
; [범위 밖] 코드 서명 인증서 없음 — 서명 없이 배포. SmartScreen 경고가 뜰 수
; 있음(README_RELEASE.md에 기록됨). 티켓 H에서 실측한 대로, 회사 보안
; 에이전트가 서명 안 된 자동시작 항목을 필터링할 가능성도 있어 아래
; [Registry]+[Icons]로 이중 등록한다.
WizardStyle=modern

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
; [확정 설계 3] 기본 체크(ON) — checkedonce: 최초 설치 시엔 체크된 채로
; 시작하되, 이후 재설치/업그레이드 시엔 사용자가 이전에 끈 선택을 존중.
Name: "autostart"; Description: "Windows 시작 시 NotebookRAG 자동 실행"; Flags: checkedonce

[Dirs]
; bge-m3.gguf(605MB)는 패키지에 담지 않는다(아래 [Files] 참고) — 최초
; 실행 시 model_downloader.py가 이 폴더에 내려받으므로 빈 폴더만 미리 만든다.
Name: "{app}\bin\models"

[Files]
; [확정 설계 2] 포함 — notebookrag/mcp-rag/tray의 _internal 전체 포함,
; onboarding 전체, config는 mcp_tools.json만 그대로(아래서 settings.json.
; template은 별도 처리).
Source: "..\bin\notebookrag\*"; DestDir: "{app}\bin\notebookrag"; Flags: recursesubdirs ignoreversion
Source: "..\bin\mcp-rag\*"; DestDir: "{app}\bin\mcp-rag"; Flags: recursesubdirs ignoreversion
Source: "..\bin\tray\*"; DestDir: "{app}\bin\tray"; Flags: recursesubdirs ignoreversion
Source: "..\bin\onboarding\*"; DestDir: "{app}\bin\onboarding"; Flags: recursesubdirs ignoreversion
Source: "..\bin\config\mcp_tools.json"; DestDir: "{app}\bin\config"; Flags: ignoreversion
; ⚠️ 값이 채워진 배포용 사본 — 릴리즈 루트의 마스터(빈 값)가 아니라
; build_installer.ps1이 생성한 staging 사본을 담는다.
Source: "staging\config\settings.json.template"; DestDir: "{app}\bin\config"; Flags: ignoreversion
; models\bge-m3.gguf(605MB)는 의도적으로 제외 — 최초 실행 시 자동 다운로드.

[Registry]
; [확정 설계 3] 자동시작 방식 1 — HKCU Run 키.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "NotebookRAG"; \
  ValueData: """{app}\{#MyAppExeName}"""; \
  Tasks: autostart; Flags: uninsdeletevalue

[Icons]
; [확정 설계 3] 자동시작 방식 2 — 시작프로그램 폴더 바로가기. 티켓 H에서
; 실사용 중 발견한 대로 이 회사 환경에서는 Run 키만으론 보안 에이전트에
; 조용히 필터링될 수 있었고, 이 방식이 실제로 동작했다 — 설치 시점에도
; 같은 이중화를 그대로 적용한다.
Name: "{userstartup}\NotebookRAG"; Filename: "{app}\{#MyAppExeName}"; \
  Tasks: autostart
Name: "{group}\NotebookRAG"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\NotebookRAG 제거"; Filename: "{uninstallexe}"

[Run]
; [폴링고정및중복실행수정 후속 요청] 설치 완료 직후 안내 텍스트를 메모장으로
; 자동으로 띄운다 — 체크박스 없이 무조건 실행(무인 설치 시에만 skipifsilent로
; 건너뜀). CurStepChanged(ssPostInstall)에서 이 파일을 먼저 생성하므로 이
; [Run] 항목이 그 뒤에 실행되는 순서가 보장돼야 한다(Inno는 [Run]을 설치
; 완료 후에만 실행하므로 항상 만족됨).
Filename: "{win}\notepad.exe"; Parameters: """{app}\설치후_안내.txt"""; \
  Flags: nowait skipifsilent

; [확정 설계 4] 설치 완료 후 즉시 실행 옵션.
Filename: "{app}\{#MyAppExeName}"; Description: "NotebookRAG 지금 실행"; \
  Flags: postinstall nowait skipifsilent

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  Guide: TStringList;
  GuidePath: String;
begin
  if CurStep = ssPostInstall then
  begin
    // [확정 설계 4] Claude Code MCP 등록은 자동화하지 않는다 — 실제 설치
    // 경로가 반영된 정확한 명령어를 안내 텍스트 파일로 남긴다.
    Guide := TStringList.Create;
    try
      Guide.Add('NotebookRAG 설치가 완료되었습니다.');
      Guide.Add('');
      Guide.Add('■ 최초 실행 안내');
      Guide.Add('  처음 실행하면 문서 임베딩 모델(약 605MB)을 자동으로');
      Guide.Add('  내려받습니다. 완료 전까지는 검색이 대기 상태로 표시됩니다.');
      Guide.Add('  트레이 아이콘을 좌클릭하면 진행 상황을 볼 수 있습니다.');
      Guide.Add('');
      Guide.Add('■ Claude Code에서 문서 검색 도구로 쓰려면');
      Guide.Add('  터미널에서 아래 명령을 한 번 실행하세요:');
      Guide.Add('');
      Guide.Add('  claude mcp add NotebookRAG -s user -- "' + ExpandConstant('{app}') + '\bin\mcp-rag\mcp-rag.exe"');
      Guide.Add('');
      Guide.Add('■ 색인할 문서 폴더 추가');
      Guide.Add('  트레이 아이콘 → 설정 탭에서 폴더를 추가하세요.');
      GuidePath := ExpandConstant('{app}\설치후_안내.txt');
      Guide.SaveToFile(GuidePath);
    finally
      Guide.Free;
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    // [Files]로 추적되지 않는(설치 후 런타임에 생성된) 파일들 — 재생성/
    // 재다운로드 가능한 것들뿐이라 확인 없이 항상 정리한다. 이걸 안 하면
    // {app} 폴더가 안 비어서 Inno가 폴더 자체를 못 지우고 남긴다.
    DeleteFile(ExpandConstant('{app}\설치후_안내.txt'));
    DeleteFile(ExpandConstant('{app}\bin\models\bge-m3.gguf'));

    // [확정 설계 5] 색인된 문서 데이터(%APPDATA%)는 되돌릴 수 없는 삭제라
    // 사용자 확인이 필요하다. 무인(silent) 제거 시엔 물어볼 사용자가
    // 없으므로 MsgBox가 응답 없이 멈추는 걸 피하기 위해 묻지 않고
    // 건너뛴다 — 확인 없이 삭제하는 것보다 보존이 안전한 기본값이다.
    if UninstallSilent then
      Exit;
    if MsgBox('색인된 문서 데이터(%APPDATA%\NotebookRAG)를 삭제하시겠습니까?' + #13#10 +
             '삭제하지 않으면 나중에 재설치 시 다시 사용할 수 있습니다.',
             mbConfirmation, MB_YESNO) = IDYES then
      DelTree(ExpandConstant('{userappdata}\NotebookRAG'), True, True, True);
  end;
end;
