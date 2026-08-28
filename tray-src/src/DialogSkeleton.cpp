#include "DialogSkeleton.h"
#include "TrayApp.h"
#include "ApiClient.h"
#include "RulesEditDialog.h"
#include <algorithm>
#include <process.h>
#include <shobjidl.h>
#include <shlobj.h>
#include <shellapi.h>
#include <psapi.h>
#include <tlhelp32.h>

namespace {

// [상태정보확장 4단계 — 정확도 수정] tray.exe가 직접 띄운 건 API 서빙
// 프로세스(부모, notebookrag.exe)뿐이라 Job Object 핸들도 그 하나뿐이다.
// 그런데 오늘 구조 변경으로 실제 CPU/메모리를 많이 쓰는 색인 작업은 그
// 부모가 multiprocessing으로 띄운 자식(그랜드차일드) 프로세스에서 돈다 —
// 부모만 측정하면 "존재감"의 핵심을 놓친다. Job Object는 그랜드차일드도
// 자동으로 같은 Job에 들어가 있지만(캐스케이드 종료엔 그걸로 충분) 여기서
// 그 핸들을 우리가 직접 갖고 있진 않으므로, 부모 PID를 기준으로 Toolhelp32
// 스냅샷을 떠서 "부모 PID를 부모로 둔 프로세스"를 찾아 그 PID들도 같이
// 측정한다.
std::vector<DWORD> FindChildProcessIds(DWORD parentPid)
{
    std::vector<DWORD> result;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return result;

    PROCESSENTRY32W entry;
    entry.dwSize = sizeof(entry);
    if (Process32FirstW(snap, &entry))
    {
        do
        {
            if (entry.th32ParentProcessID == parentPid && entry.th32ProcessID != parentPid)
                result.push_back(entry.th32ProcessID);
        } while (Process32NextW(snap, &entry));
    }
    CloseHandle(snap);
    return result;
}

// health/model 응답의 ISO 8601 UTC 문자열(예: 2026-08-20T12:34:56.789012+00:00,
// rag_serve.py의 datetime.now(timezone.utc).isoformat())과 지금(UTC)의
// 차이를 분 단위로 계산한다. 파싱 실패 시 -1.
int MinutesAgoFromIso8601(const std::wstring& iso)
{
    int y = 0, mo = 0, d = 0, h = 0, mi = 0, se = 0;
    if (swscanf_s(iso.c_str(), L"%d-%d-%dT%d:%d:%d", &y, &mo, &d, &h, &mi, &se) != 6)
        return -1;

    SYSTEMTIME st = {};
    st.wYear = (WORD)y; st.wMonth = (WORD)mo; st.wDay = (WORD)d;
    st.wHour = (WORD)h; st.wMinute = (WORD)mi; st.wSecond = (WORD)se;

    FILETIME ft;
    if (!SystemTimeToFileTime(&st, &ft)) return -1;
    ULARGE_INTEGER then;
    then.LowPart = ft.dwLowDateTime;
    then.HighPart = ft.dwHighDateTime;

    SYSTEMTIME nowSt;
    GetSystemTime(&nowSt); // 이미 UTC
    FILETIME nowFt;
    if (!SystemTimeToFileTime(&nowSt, &nowFt)) return -1;
    ULARGE_INTEGER now;
    now.LowPart = nowFt.dwLowDateTime;
    now.HighPart = nowFt.dwHighDateTime;

    if (now.QuadPart < then.QuadPart) return 0;
    ULONGLONG diff100ns = now.QuadPart - then.QuadPart;
    return (int)(diff100ns / (10000000ULL * 60ULL));
}

// [티켓 H] 부팅 시 자동시작 — HKCU\Software\Microsoft\Windows\CurrentVersion\Run.
// 관리자 권한 불필요(현재 사용자 전용). 경로는 항상 지금 실행 중인 자기
// 자신(GetModuleFileNameW)에서 구한다 — 하드코딩하면 나중에 다른 위치에
// 설치된 경우 틀어진다.
const wchar_t* kRunKeyPath = L"Software\\Microsoft\\Windows\\CurrentVersion\\Run";
const wchar_t* kRunValueName = L"NotebookRAG";

std::wstring GetSelfExePath()
{
    wchar_t buf[MAX_PATH];
    DWORD len = GetModuleFileNameW(nullptr, buf, MAX_PATH);
    return (len > 0 && len < MAX_PATH) ? std::wstring(buf, len) : std::wstring();
}

bool IsAutostartRegistered()
{
    HKEY hKey;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, kRunKeyPath, 0, KEY_QUERY_VALUE, &hKey) != ERROR_SUCCESS)
        return false;
    DWORD type = 0;
    LONG r = RegQueryValueExW(hKey, kRunValueName, nullptr, &type, nullptr, nullptr);
    RegCloseKey(hKey);
    return (r == ERROR_SUCCESS && type == REG_SZ);
}

// [2026-08-22 실사용 중 발견] 회사 노트북에서 HKCU Run 키에 값이 정상적으로
// 쓰여졌는데도(reg query로 확인됨) 재부팅 후 실제로 실행되지 않았고, 심지어
// 작업관리자 "시작 앱" 탭/WMI Win32_StartupCommand 목록에도 아예 안 보였다
// — 보안 에이전트가 서명 안 된/미승인 exe의 Run 키 항목을 조용히 필터링하는
//것으로 추정됨(로그 한 줄도 안 남음). Run 키만으로는 이 환경에서 신뢰할 수
// 없으므로, Windows "시작프로그램" 폴더(shell:startup)에 바로가기(.lnk)를
// 병행 등록한다 — 같은 보안 정책이라도 두 메커니즘을 다르게 취급할 가능성이
//있고, 최소한 하나라도 뚫리면 실제 자동시작이 동작한다.
std::wstring GetStartupFolderPath()
{
    wchar_t path[MAX_PATH];
    if (SUCCEEDED(SHGetFolderPathW(nullptr, CSIDL_STARTUP, nullptr, 0, path)))
        return std::wstring(path);
    return L"";
}

std::wstring GetStartupShortcutPath()
{
    std::wstring dir = GetStartupFolderPath();
    return dir.empty() ? L"" : (dir + L"\\NotebookRAG.lnk");
}

bool CreateStartupShortcut(const std::wstring& exePath)
{
    std::wstring lnkPath = GetStartupShortcutPath();
    if (lnkPath.empty()) return false;

    IShellLinkW* pLink = nullptr;
    HRESULT hr = CoCreateInstance(CLSID_ShellLink, nullptr, CLSCTX_INPROC_SERVER,
                                   IID_IShellLinkW, reinterpret_cast<void**>(&pLink));
    if (FAILED(hr)) return false;

    pLink->SetPath(exePath.c_str());
    size_t slash = exePath.find_last_of(L'\\');
    if (slash != std::wstring::npos)
        pLink->SetWorkingDirectory(exePath.substr(0, slash).c_str());
    pLink->SetDescription(L"NotebookRAG");

    bool ok = false;
    IPersistFile* pFile = nullptr;
    if (SUCCEEDED(pLink->QueryInterface(IID_IPersistFile, reinterpret_cast<void**>(&pFile))))
    {
        ok = SUCCEEDED(pFile->Save(lnkPath.c_str(), TRUE));
        pFile->Release();
    }
    pLink->Release();
    return ok;
}

bool DeleteStartupShortcut()
{
    std::wstring lnkPath = GetStartupShortcutPath();
    if (lnkPath.empty()) return true;
    if (DeleteFileW(lnkPath.c_str())) return true;
    return GetLastError() == ERROR_FILE_NOT_FOUND;
}

bool SetAutostartRegistered(bool enable)
{
    HKEY hKey;
    bool regOk = false;
    if (RegCreateKeyExW(HKEY_CURRENT_USER, kRunKeyPath, 0, nullptr, 0,
                         KEY_SET_VALUE, nullptr, &hKey, nullptr) == ERROR_SUCCESS)
    {
        LONG r;
        std::wstring exePath = GetSelfExePath();
        if (enable && !exePath.empty())
        {
            std::wstring quoted = L"\"" + exePath + L"\"";
            r = RegSetValueExW(hKey, kRunValueName, 0, REG_SZ,
                                reinterpret_cast<const BYTE*>(quoted.c_str()),
                                (DWORD)((quoted.size() + 1) * sizeof(wchar_t)));
        }
        else if (!enable)
        {
            r = RegDeleteValueW(hKey, kRunValueName);
            if (r == ERROR_FILE_NOT_FOUND) r = ERROR_SUCCESS;  // 이미 없으면 목표 달성
        }
        else
        {
            r = ERROR_INVALID_PARAMETER;  // enable인데 자기 경로를 못 구한 극히 드문 경우
        }
        regOk = (r == ERROR_SUCCESS);
        RegCloseKey(hKey);

        // 시작프로그램 폴더 바로가기는 Run 키 성공 여부와 무관하게 병행
        // 시도한다(둘 중 하나라도 뚫리면 실제 자동시작이 동작하므로) — 다만
        // 체크박스의 성공/실패 판정 자체는 여전히 Run 키(소스 오브 트루스)
        // 기준으로 한다.
        if (enable && !exePath.empty())
            CreateStartupShortcut(exePath);
        else if (!enable)
            DeleteStartupShortcut();
    }
    return regOk;
}

} // namespace

CDialogSkeleton::CDialogSkeleton(CTrayWnd* pTrayWnd, CWnd* pParent)
    : CDialog(IDD, pParent)
    , m_pTrayWnd(pTrayWnd)
    , m_hPollThread(nullptr)
    , m_stopPolling(true)
{
}

CDialogSkeleton::~CDialogSkeleton()
{
    // OnDestroy에서 이미 스레드를 정리하지만, DestroyWindow 없이 곧바로
    // delete되는 경로에 대비한 안전망.
    m_stopPolling = true;
    if (m_hPollThread)
    {
        WaitForSingleObject(m_hPollThread, 5000);
        CloseHandle(m_hPollThread);
    }
}

void CDialogSkeleton::DoDataExchange(CDataExchange* pDX)
{
    CDialog::DoDataExchange(pDX);
    DDX_Control(pDX, IDC_TAB_MAIN, m_tabCtrl);

    DDX_Control(pDX, IDC_STATUS_CONN, m_conn);
    DDX_Control(pDX, IDC_STATUS_MODEL_LABEL, m_modelLabel);
    DDX_Control(pDX, IDC_STATUS_MODEL, m_model);
    DDX_Control(pDX, IDC_STATUS_MCP_LABEL, m_mcpLabel);
    DDX_Control(pDX, IDC_STATUS_MCP, m_mcp);
    DDX_Control(pDX, IDC_STATUS_INDEX_LABEL, m_indexLabel);
    DDX_Control(pDX, IDC_STATUS_INDEX, m_index);
    DDX_Control(pDX, IDC_STATUS_DISK_LABEL, m_diskLabel);
    DDX_Control(pDX, IDC_STATUS_DISK, m_disk);
    DDX_Control(pDX, IDC_BTN_WARNINGS, m_btnWarnings);
    DDX_Control(pDX, IDC_BTN_PAUSE_RESUME, m_btnPauseResume);
    DDX_Control(pDX, IDC_PROGRESS_OVERALL, m_progressOverall);
    DDX_Control(pDX, IDC_PROGRESS_FILE, m_progressFile);
    DDX_Control(pDX, IDC_STATUS_FOLDERS_LABEL, m_foldersLabel);
    DDX_Control(pDX, IDC_STATUS_FOLDERS, m_folders);

    DDX_Control(pDX, IDC_LIST_FOLDERS, m_folderList);
    DDX_Control(pDX, IDC_BTN_ADD_FOLDER, m_btnAddFolder);
    DDX_Control(pDX, IDC_BTN_REMOVE_FOLDER, m_btnRemoveFolder);
    DDX_Control(pDX, IDC_BTN_EDIT_RULES, m_btnEditRules);
    DDX_Control(pDX, IDC_CHECK_AUTOSTART, m_chkAutostart);

    DDX_Control(pDX, IDC_INFO_TITLE, m_infoTitle);
    DDX_Control(pDX, IDC_INFO_VERSION, m_infoVersion);
    DDX_Control(pDX, IDC_INFO_GITHUB_LABEL, m_infoGithubLabel);
    DDX_Control(pDX, IDC_INFO_GITHUB, m_infoGithub);
    DDX_Control(pDX, IDC_INFO_PATH_LABEL, m_infoPathLabel);
    DDX_Control(pDX, IDC_INFO_PATH, m_infoPath);
}

BEGIN_MESSAGE_MAP(CDialogSkeleton, CDialog)
    ON_NOTIFY(TCN_SELCHANGE, IDC_TAB_MAIN, &CDialogSkeleton::OnTcnSelchangeTab)
    ON_WM_CLOSE()
    ON_WM_DESTROY()
    ON_WM_SHOWWINDOW()
    ON_MESSAGE(WM_STATUS_UPDATE, &CDialogSkeleton::OnStatusUpdate)
    ON_MESSAGE(WM_FOLDERS_UPDATE, &CDialogSkeleton::OnFoldersUpdate)
    ON_BN_CLICKED(IDC_BTN_PAUSE_RESUME, &CDialogSkeleton::OnBtnPauseResume)
    ON_BN_CLICKED(IDC_BTN_WARNINGS, &CDialogSkeleton::OnBtnWarnings)
    ON_BN_CLICKED(IDC_BTN_ADD_FOLDER, &CDialogSkeleton::OnBtnAddFolder)
    ON_BN_CLICKED(IDC_BTN_REMOVE_FOLDER, &CDialogSkeleton::OnBtnRemoveFolder)
    ON_BN_CLICKED(IDC_BTN_EDIT_RULES, &CDialogSkeleton::OnBtnEditRules)
    ON_BN_CLICKED(IDC_CHECK_AUTOSTART, &CDialogSkeleton::OnBtnAutostart)
    ON_STN_CLICKED(IDC_INFO_GITHUB, &CDialogSkeleton::OnStnClickedInfoGithub)
END_MESSAGE_MAP()

BOOL CDialogSkeleton::OnInitDialog()
{
    CDialog::OnInitDialog();

    SetWindowTextW(L"NotebookRAG");
    m_settings = CSettingsReader::Load();

    m_tabCtrl.InsertItem(0, L"상태정보");
    m_tabCtrl.InsertItem(1, L"설정");
    m_tabCtrl.InsertItem(2, L"정보");
    m_tabCtrl.SetCurSel(0);

    m_modelLabel.SetWindowTextW(L"모델:");
    m_mcpLabel.SetWindowTextW(L"MCP 연동:");
    m_indexLabel.SetWindowTextW(L"색인:");
    m_diskLabel.SetWindowTextW(L"디스크:");
    m_conn.SetWindowTextW(L"● 연결 안 됨");
    m_model.SetWindowTextW(L"—");
    m_mcp.SetWindowTextW(L"—");
    m_index.SetWindowTextW(L"—");
    m_disk.SetWindowTextW(L"—");
    m_btnWarnings.SetWindowTextW(L"경고 —");
    m_btnPauseResume.SetWindowTextW(L"일시정지");
    m_btnPauseResume.EnableWindow(FALSE);
    m_progressOverall.SetRange(0, 100);
    m_progressFile.SetRange(0, 100);
    m_foldersLabel.SetWindowTextW(L"감시 폴더:");
    m_folders.SetWindowTextW(L"—");

    m_btnAddFolder.SetWindowTextW(L"+ 폴더 추가...");
    m_btnRemoveFolder.SetWindowTextW(L"제거");
    m_btnEditRules.SetWindowTextW(L"규칙");

    // [티켓 H] 체크박스는 "설정값"이 아니라 레지스트리의 실제 현재 상태를
    // 그대로 보여주는 거울이다 — 소스 오브 트루스는 레지스트리 자체.
    m_chkAutostart.SetWindowTextW(L"Windows 시작 시 자동 실행");
    m_chkAutostart.SetCheck(IsAutostartRegistered() ? BST_CHECKED : BST_UNCHECKED);

    // [정보탭_버전관리] 버전/GitHub는 /health 응답이 와야 채워진다(백엔드가
    // 응답 못 하면 "확인 불가"로 우아하게 표시 — ApplyStatus() 참고).
    // 설치 위치는 지금 실행 중인 자기 자신(tray.exe)에서 바로 구할 수
    // 있으므로 백엔드와 무관하게 여기서 한 번만 계산한다.
    m_infoTitle.SetWindowTextW(L"NotebookRAG");
    m_infoVersion.SetWindowTextW(L"버전: 확인 중...");
    m_infoGithubLabel.SetWindowTextW(L"GitHub:");
    m_infoGithub.SetWindowTextW(L"확인 중...");
    m_infoPathLabel.SetWindowTextW(L"설치 위치:");
    {
        // tray.exe 자기 자신 경로 기준({app}\bin\tray\tray.exe → {app}로
        // 세 단계 truncate) — TrayApp.cpp::Init()의 notebookrag.exe 경로
        // 계산과 같은 패턴.
        wchar_t exePathBuf[MAX_PATH];
        GetModuleFileNameW(nullptr, exePathBuf, MAX_PATH);
        std::wstring path = exePathBuf;
        path = path.substr(0, path.find_last_of(L'\\'));   // {app}\bin\tray
        path = path.substr(0, path.find_last_of(L'\\'));   // {app}\bin
        path = path.substr(0, path.find_last_of(L'\\'));   // {app}
        m_infoPath.SetWindowTextW(path.c_str());
    }

    ShowTab(0);

    return TRUE;
}

void CDialogSkeleton::ShowTab(int index)
{
    int showTab1 = (index == 0) ? SW_SHOW : SW_HIDE;
    int showTab2 = (index == 1) ? SW_SHOW : SW_HIDE;
    int showTab3 = (index == 2) ? SW_SHOW : SW_HIDE;

    m_conn.ShowWindow(showTab1);
    m_modelLabel.ShowWindow(showTab1);
    m_model.ShowWindow(showTab1);
    m_mcpLabel.ShowWindow(showTab1);
    m_mcp.ShowWindow(showTab1);
    m_indexLabel.ShowWindow(showTab1);
    m_index.ShowWindow(showTab1);
    m_diskLabel.ShowWindow(showTab1);
    m_disk.ShowWindow(showTab1);
    m_btnWarnings.ShowWindow(showTab1);
    m_btnPauseResume.ShowWindow(showTab1);
    m_progressOverall.ShowWindow(showTab1);
    m_progressFile.ShowWindow(showTab1);
    m_foldersLabel.ShowWindow(showTab1);
    m_folders.ShowWindow(showTab1);

    m_folderList.ShowWindow(showTab2);
    m_btnAddFolder.ShowWindow(showTab2);
    m_btnRemoveFolder.ShowWindow(showTab2);
    m_btnEditRules.ShowWindow(showTab2);
    m_chkAutostart.ShowWindow(showTab2);

    m_infoTitle.ShowWindow(showTab3);
    m_infoVersion.ShowWindow(showTab3);
    m_infoGithubLabel.ShowWindow(showTab3);
    m_infoGithub.ShowWindow(showTab3);
    m_infoPathLabel.ShowWindow(showTab3);
    m_infoPath.ShowWindow(showTab3);

    if (index == 1) RefreshFolderList();
}

void CDialogSkeleton::OnTcnSelchangeTab(NMHDR* /*pNMHDR*/, LRESULT* pResult)
{
    ShowTab(m_tabCtrl.GetCurSel());
    *pResult = 0;
}

void CDialogSkeleton::OnClose()
{
    // 다이얼로그를 닫아도 트레이 앱(및 폴링 스레드, notebookrag.exe)은
    // 계속 실행돼야 하므로 숨기기만 한다.
    ShowWindow(SW_HIDE);
}

void CDialogSkeleton::OnShowWindow(BOOL bShow, UINT nStatus)
{
    CDialog::OnShowWindow(bShow, nStatus);
    // [티켓 H, 검증4] OnInitDialog는 다이얼로그 생애 동안 한 번만 실행되고
    // 트레이 클릭으로 "다시 열 때"는 ShowWindow(SW_SHOW)만 호출된다(창을
    // 파괴하지 않고 숨겼다가 다시 보여주는 방식) — 그 사이 레지스트리가
    // 외부에서 바뀌었을 수 있으므로, 보여질 때마다 실제 상태를 다시 읽는다.
    if (bShow)
        m_chkAutostart.SetCheck(IsAutostartRegistered() ? BST_CHECKED : BST_UNCHECKED);
}

void CDialogSkeleton::OnDestroy()
{
    m_stopPolling = true;
    if (m_hPollThread)
    {
        WaitForSingleObject(m_hPollThread, 5000);
        CloseHandle(m_hPollThread);
        m_hPollThread = nullptr;
    }
    CDialog::OnDestroy();
}

// ── 폴링 워커 스레드 ─────────────────────────────────────────────────────

void CDialogSkeleton::StartPolling()
{
    m_stopPolling = false;
    m_hPollThread = (HANDLE)_beginthreadex(nullptr, 0, &CDialogSkeleton::PollThreadProc, this, 0, nullptr);
}

unsigned __stdcall CDialogSkeleton::PollThreadProc(void* param)
{
    CDialogSkeleton* self = static_cast<CDialogSkeleton*>(param);
    self->PollLoop();
    return 0;
}

void CDialogSkeleton::PollLoop()
{
    // m_settings.host/port는 다이얼로그 생애 동안 안 바뀌므로(포트 편집
    // UI는 이번 티켓 범위 밖) 스레드 시작 시점에 한 번만 읽어도 안전하다.
    CApiClient api(m_settings.host, m_settings.port);

    SYSTEM_INFO sysInfo;
    GetSystemInfo(&sysInfo);
    m_numCores = std::max<DWORD>(1, sysInfo.dwNumberOfProcessors);

    while (!m_stopPolling)
    {
        PolledStatus* status = new PolledStatus();

        ApiResult health = api.Get(L"/health");
        status->healthOk = (health.statusCode == 200);
        if (status->healthOk)
        {
            if (const JsonValue* v = health.body.Find("chunks"))
                status->chunks = (int)v->AsNumber();
            if (const JsonValue* v = health.body.Find("마지막검색"))
            {
                if (!v->IsNull())
                {
                    status->hasLastSearch = true;
                    status->lastSearchAtIso = v->AsString();
                }
            }
            if (const JsonValue* v = health.body.Find("오늘검색횟수"))
                status->searchCountToday = (int)v->AsNumber();
            // [정보탭_버전관리] 하드코딩 금지 — 백엔드가 응답 못 하면
            // status->version/github는 빈 문자열로 남고, ApplyStatus()가
            // "확인 불가"로 표시한다.
            if (const JsonValue* v = health.body.Find("버전"))
                status->version = v->AsString();
            if (const JsonValue* v = health.body.Find("github"))
                status->github = v->AsString();
        }

        // [워치독] notebookrag.exe(부모 데몬)가 프로세스로는 살아있어도
        // HTTP가 완전히 응답 없는 상태(리스닝 소켓 자체가 사라지는 경우가
        // 실사용 중 확인됨 — OneDrive 등과의 디스크 I/O 경합이 시작 단계의
        // 동기 DB/인덱스 로딩을 막아버린 것으로 추정)를 감시한다.
        //
        // 타임아웃을 5분으로 넉넉하게 잡은 이유: 정상적인 시작 단계(특히
        // 디스크 경합이 겹칠 때)도 꽤 오래 걸릴 수 있어서, 너무 짧으면
        // 멀쩡히 느리게 뜨는 중인 프로세스를 오작동으로 오인해 죽이게
        // 된다. 연속 재시작을 3회로 제한한 이유: 진짜 문제(DB 손상,
        // 근본적인 디스크 병목 등)라면 재시작 자체가 디스크에 부하를 또
        // 얹어서 상황을 악화시킬 수 있으므로, 일정 횟수 이상은 포기하고
        // 조용히 "연결 안 됨"으로 둔다(사용자가 상태정보 탭에서 확인하고
        // 수동으로 "다시 시도"할 수 있음 — ApplyStatus/OnBtnPauseResume 참고).
        // 한 번이라도 정상 응답이 오면 전부 리셋된다.
        if (status->healthOk)
        {
            m_watchdogWindowStartTick = GetTickCount64();
            m_watchdogRestartCount = 0;
            m_watchdogGaveUp = false;
        }
        else if (!m_watchdogGaveUp)
        {
            if (m_watchdogWindowStartTick == 0)
                m_watchdogWindowStartTick = GetTickCount64();

            const ULONGLONG kWatchdogTimeoutMs = 5ULL * 60 * 1000;  // 5분
            const int kWatchdogMaxRestarts = 3;
            ULONGLONG elapsed = GetTickCount64() - m_watchdogWindowStartTick;

            if (elapsed >= kWatchdogTimeoutMs)
            {
                if (m_watchdogRestartCount < kWatchdogMaxRestarts)
                {
                    m_watchdogRestartCount++;
                    m_pTrayWnd->RestartChildProcess();
                    m_watchdogWindowStartTick = GetTickCount64();  // 재시작 직후부터 다시 5분 카운트
                }
                else
                {
                    m_watchdogGaveUp = true;
                }
            }
        }

        // [상태정보확장 4단계 — 부모+자식(색인 워커) 합산] CPU%/메모리/
        // 가동시간/PID — API가 아니라 Job Object가 쥔 프로세스들을 직접
        // 조회한다. 부모(API 서빙)만 보면 실제 CPU/메모리를 많이 쓰는
        // 색인 그랜드차일드가 안 잡히므로, 부모 PID의 자식들도 찾아서
        // 합산한다(사용자 피드백: "이 정보가 중요함").
        if (HANDLE hParent = m_pTrayWnd->GetChildProcessHandle())
        {
            status->pid = m_pTrayWnd->GetChildProcessId();

            std::vector<HANDLE> handlesToClose;
            std::vector<HANDLE> allHandles = { hParent };  // hParent는 여기서 안 닫음(소유자가 따로 있음)
            for (DWORD childPid : FindChildProcessIds(status->pid))
            {
                HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, childPid);
                if (h)
                {
                    allHandles.push_back(h);
                    handlesToClose.push_back(h);
                }
            }

            FILETIME ftNow; GetSystemTimeAsFileTime(&ftNow);
            ULARGE_INTEGER nowU;
            nowU.LowPart = ftNow.dwLowDateTime; nowU.HighPart = ftNow.dwHighDateTime;

            ULONGLONG totalCpuTime100ns = 0;
            double totalMemMb = 0.0;
            double earliestUptimeSec = 0.0;
            for (HANDLE h : allHandles)
            {
                FILETIME ftCreate, ftExit, ftKernel, ftUser;
                if (GetProcessTimes(h, &ftCreate, &ftExit, &ftKernel, &ftUser))
                {
                    ULARGE_INTEGER kernel, user, create;
                    kernel.LowPart = ftKernel.dwLowDateTime; kernel.HighPart = ftKernel.dwHighDateTime;
                    user.LowPart = ftUser.dwLowDateTime;     user.HighPart = ftUser.dwHighDateTime;
                    create.LowPart = ftCreate.dwLowDateTime; create.HighPart = ftCreate.dwHighDateTime;
                    totalCpuTime100ns += kernel.QuadPart + user.QuadPart;
                    double up = (double)(nowU.QuadPart - create.QuadPart) / 10000000.0;
                    if (up > earliestUptimeSec) earliestUptimeSec = up;  // 부모(가장 먼저 뜬 것) 기준 가동시간
                }
                PROCESS_MEMORY_COUNTERS pmc;
                ZeroMemory(&pmc, sizeof(pmc));
                if (GetProcessMemoryInfo(h, &pmc, sizeof(pmc)))
                    totalMemMb += (double)pmc.WorkingSetSize / 1024.0 / 1024.0;
            }
            for (HANDLE h : handlesToClose) CloseHandle(h);

            status->uptimeSec = earliestUptimeSec;
            status->memMb = totalMemMb;
            if (m_haveCpuSample)
            {
                ULONGLONG deltaCpu = (totalCpuTime100ns > m_lastCpuTime100ns)
                    ? (totalCpuTime100ns - m_lastCpuTime100ns) : 0;
                ULONGLONG deltaWall = (nowU.QuadPart > m_lastSampleTime100ns)
                    ? (nowU.QuadPart - m_lastSampleTime100ns) : 0;
                if (deltaWall > 0)
                {
                    status->hasCpuSample = true;
                    status->cpuPercent = 100.0 * (double)deltaCpu / (double)deltaWall / (double)m_numCores;
                }
            }
            m_lastCpuTime100ns = totalCpuTime100ns;
            m_lastSampleTime100ns = nowU.QuadPart;
            m_haveCpuSample = true;
        }

        ApiResult idx = api.Get(L"/indexer/status");
        status->indexerOk = (idx.statusCode == 200);
        if (status->indexerOk)
        {
            if (const JsonValue* v = idx.body.Find("phase")) status->phase = v->AsString();
            if (const JsonValue* v = idx.body.Find("총파일수")) status->totalFiles = (int)v->AsNumber();
            if (const JsonValue* v = idx.body.Find("적재된파일수")) status->doneFiles = (int)v->AsNumber();
            if (const JsonValue* v = idx.body.Find("청킹수")) status->chunkCount = (int)v->AsNumber();
            if (const JsonValue* v = idx.body.Find("디렉토리총파일수")) status->dirTotalFiles = (int)v->AsNumber();
            if (const JsonValue* v = idx.body.Find("DB저장파일수"))
            {
                status->hasDbSavedFiles = true;
                status->dbSavedFiles = (int)v->AsNumber();
            }
            if (const JsonValue* v = idx.body.Find("진행중"))
            {
                if (const JsonValue* fname = v->Find("파일명"))
                {
                    if (!fname->IsNull())
                    {
                        status->hasProgressFile = true;
                        status->progressFileName = fname->AsString();
                    }
                }
                if (const JsonValue* fp = v->Find("파일내_진행"))
                {
                    if (!fp->IsNull() && fp->ArraySize() >= 2)
                    {
                        status->hasFileProgress = true;
                        status->fileProgressDone = (int)fp->ArrayAt(0).AsNumber();
                        status->fileProgressTotal = (int)fp->ArrayAt(1).AsNumber();
                    }
                }
            }
            if (const JsonValue* v = idx.body.Find("디스크"))
            {
                if (const JsonValue* a = v->Find("활성DB_MB")) status->activeDbMb = a->AsNumber();
                if (const JsonValue* r = v->Find("재색인중_임시DB_MB"))
                {
                    if (!r->IsNull())
                    {
                        status->hasReindexDb = true;
                        status->reindexDbMb = r->AsNumber();
                    }
                }
            }
            if (const JsonValue* v = idx.body.Find("경고"))
            {
                if (const JsonValue* cnt = v->Find("건수")) status->warningCount = (int)cnt->AsNumber();
                if (const JsonValue* recent = v->Find("최근"))
                {
                    for (size_t i = 0; i < recent->ArraySize(); i++)
                    {
                        const JsonValue& item = recent->ArrayAt(i);
                        if (const JsonValue* msg = item.Find("메시지"))
                            status->recentWarnings.push_back(msg->AsString());
                    }
                }
            }
            // [상태정보확장 2단계] 이번 회차 집계 + 경과/예상잔여 시간.
            if (const JsonValue* v = idx.body.Find("이번회차_집계"))
            {
                if (const JsonValue* n = v->Find("신규"))     status->roundNew     = (int)n->AsNumber();
                if (const JsonValue* n = v->Find("변경"))     status->roundChanged = (int)n->AsNumber();
                if (const JsonValue* n = v->Find("재사용"))   status->roundReused  = (int)n->AsNumber();
                if (const JsonValue* n = v->Find("중복스킵")) status->roundDupSkip = (int)n->AsNumber();
                if (const JsonValue* n = v->Find("처리실패")) status->roundFailed  = (int)n->AsNumber();
            }
            if (const JsonValue* v = idx.body.Find("시간"))
            {
                if (const JsonValue* e = v->Find("경과초")) status->elapsedSec = e->AsNumber();
                if (const JsonValue* e = v->Find("예상잔여초"))
                {
                    if (!e->IsNull())
                    {
                        status->hasEta = true;
                        status->etaSec = e->AsNumber();
                    }
                }
            }
        }

        ApiResult model = api.Get(L"/model/status");
        status->modelOk = (model.statusCode == 200);
        if (status->modelOk)
        {
            if (const JsonValue* v = model.body.Find("phase")) status->modelPhase = v->AsString();
            if (const JsonValue* v = model.body.Find("다운로드_MB")) status->modelDownloadedMb = v->AsNumber();
            if (const JsonValue* v = model.body.Find("전체_MB")) status->modelTotalMb = v->AsNumber();
            if (const JsonValue* v = model.body.Find("진행률")) status->modelProgressPct = v->AsNumber();
            if (const JsonValue* v = model.body.Find("오류"))
            {
                if (!v->IsNull())
                {
                    status->hasModelError = true;
                    status->modelError = v->AsString();
                }
            }
            if (const JsonValue* v = model.body.Find("차원"))
                status->modelDim = (int)v->AsNumber();
        }

        // [상태정보확장 3단계] 감시 폴더 — 신규 API 없이 기존 GET /indexer/folders
        // 재사용, 상태 탭 폴링에 같이 얹음.
        ApiResult folders = api.Get(L"/indexer/folders");
        status->foldersOk = (folders.statusCode == 200);
        if (status->foldersOk)
        {
            if (const JsonValue* v = folders.body.Find("docs_dirs"))
            {
                for (size_t i = 0; i < v->ArraySize(); i++)
                    status->watchedFolders.push_back(v->ArrayAt(i).AsString());
            }
        }

        if (!m_stopPolling && m_hWnd && ::IsWindow(m_hWnd))
        {
            ::PostMessage(m_hWnd, WM_STATUS_UPDATE, 0, (LPARAM)status);
        }
        else
        {
            delete status;
        }

        // [폴링고정및중복실행수정] 일반 사용자가 결정할 선택지가 아닌 기술
        // 튜닝값이라 판단해 설정에서 제거하고 코드 상수로 고정했다.
        const int kPollIntervalMs = 2000;
        int slept = 0;
        while (slept < kPollIntervalMs && !m_stopPolling)
        {
            Sleep(100);
            slept += 100;
        }
    }
}

LRESULT CDialogSkeleton::OnStatusUpdate(WPARAM /*wParam*/, LPARAM lParam)
{
    PolledStatus* status = reinterpret_cast<PolledStatus*>(lParam);
    if (status)
    {
        ApplyStatus(*status);
        delete status;
    }
    return 0;
}

void CDialogSkeleton::ApplyStatus(const PolledStatus& s)
{
    m_pTrayWnd->UpdateTooltip(s.healthOk);

    if (!s.healthOk)
    {
        // [버그 수정] 예전엔 여기서 m_model/m_mcp/m_index/m_disk를 전부
        // "—"/"연결 안 됨"으로 덮어써서, 임베딩 중 응답이 없을 때(큰 파일
        // 하나에 몇십 분 걸리는 경우도 정상 — 그 자체는 문제가 아님) 화면이
        // 완전히 깜깜해져 "진행 중인 파일/조각 수"가 안 보이는 게 실제
        // 문제였다. 이제 그 라벨들은 그대로 두고(마지막 성공 폴링 값이 계속
        // 보임) 연결 줄에만 "마지막 확인: N분 전"을 표시한다.
        if (m_haveGoodStatus)
        {
            ULONGLONG minutesAgo = (GetTickCount64() - m_lastGoodTick) / 60000ULL;
            wchar_t buf[64];
            if (minutesAgo == 0)
                swprintf_s(buf, L"● 응답 없음 (방금까지는 정상)");
            else
                swprintf_s(buf, L"● 응답 없음 (마지막 확인: %llu분 전)", minutesAgo);
            m_conn.SetWindowTextW(buf);
        }
        else
        {
            m_conn.SetWindowTextW(L"● 연결 안 됨");
            m_model.SetWindowTextW(L"—");
            m_mcp.SetWindowTextW(L"—");
            m_index.SetWindowTextW(L"연결 안 됨");
            m_disk.SetWindowTextW(L"—");
            m_btnWarnings.SetWindowTextW(L"경고 —");
        }
        // [워치독] 자동 재시작을 포기한 상태면, 원래 일시정지/재개 버튼
        // 자리를 수동 "다시 시도"로 바꿔서 사용자가 직접 재시도할 길을
        // 남겨둔다(디스크 부하 재발 방지를 위해 자동 재시작은 3회로
        // 제한했지만, 완전히 손 놓지는 않는다).
        if (m_watchdogGaveUp)
        {
            m_btnPauseResume.SetWindowTextW(L"다시 시도");
            m_btnPauseResume.EnableWindow(TRUE);
        }
        else
        {
            m_btnPauseResume.EnableWindow(FALSE);
        }
        return;
    }

    m_haveGoodStatus = true;
    m_lastGoodTick = GetTickCount64();

    // [정보탭_버전관리] /health의 "버전"/"github"를 그대로 표시 — 하드코딩
    // 금지. 응답이 끊기면(위 !s.healthOk 분기) 여기 안 오므로 마지막으로
    // 성공했던 값이 그대로 남는다(다른 필드들과 같은 기존 관례).
    if (!s.version.empty())
    {
        std::wstring versionText = L"버전: v" + s.version;
        m_infoVersion.SetWindowTextW(versionText.c_str());
    }
    if (!s.github.empty())
    {
        m_githubUrl = s.github;
        m_infoGithub.SetWindowTextW(s.github.c_str());
    }
    m_btnPauseResume.EnableWindow(TRUE);

    // [상태정보확장 4단계] 가동시간/PID/host:port + CPU%/메모리.
    wchar_t connBuf[192];
    if (s.uptimeSec > 0.0)
    {
        int upMin = (int)(s.uptimeSec / 60.0);
        swprintf_s(connBuf, L"● 실행 중 (가동 %d분, PID %u, %s:%d)",
                   upMin, s.pid, m_settings.host.c_str(), m_settings.port);
    }
    else
    {
        swprintf_s(connBuf, L"● 실행 중 (PID %u, %s:%d)", s.pid, m_settings.host.c_str(), m_settings.port);
    }
    std::wstring connText = connBuf;
    if (s.hasCpuSample)
    {
        wchar_t cpuBuf[64];
        swprintf_s(cpuBuf, L"\r\nCPU %.0f%% · 메모리 %.0fMB (색인 프로세스 포함 합산)", s.cpuPercent, s.memMb);
        connText += cpuBuf;
    }
    else if (s.memMb > 0.0)
    {
        wchar_t memBuf[64];
        swprintf_s(memBuf, L"\r\n메모리 %.0fMB", s.memMb);
        connText += memBuf;
    }
    m_conn.SetWindowTextW(connText.c_str());

    // [모델]
    std::wstring modelText;
    if (s.modelPhase == L"ready")
    {
        modelText = L"준비됨";
    }
    else if (s.modelPhase == L"error")
    {
        modelText = L"오류: " + (s.hasModelError ? s.modelError : L"알 수 없음");
    }
    else if (!s.modelPhase.empty())
    {
        wchar_t buf[128];
        swprintf_s(buf, L"다운로드 중 %.1f%% (%.1f/%.1fMB)",
                   s.modelProgressPct, s.modelDownloadedMb, s.modelTotalMb);
        modelText = buf;
    }
    else
    {
        modelText = L"—";
    }
    if (s.modelDim > 0)
    {
        wchar_t dimBuf[32];
        swprintf_s(dimBuf, L" (%d차원)", s.modelDim);
        modelText += dimBuf;
    }
    m_model.SetWindowTextW(modelText.c_str());

    // [MCP 연동]
    std::wstring mcpText;
    if (!s.hasLastSearch)
    {
        mcpText = L"아직 없음";
    }
    else
    {
        int minutesAgo = MinutesAgoFromIso8601(s.lastSearchAtIso);
        if (minutesAgo <= 0)
        {
            mcpText = L"마지막 검색: 방금";
        }
        else
        {
            wchar_t buf[64];
            swprintf_s(buf, L"마지막 검색: %d분 전", minutesAgo);
            mcpText = buf;
        }
    }
    {
        wchar_t cntBuf[32];
        swprintf_s(cntBuf, L" (오늘 %d회)", s.searchCountToday);
        mcpText += cntBuf;
    }
    m_mcp.SetWindowTextW(mcpText.c_str());

    // [색인]
    m_currentPhase = s.phase;
    std::wstring indexText;
    if (!s.indexerOk)
    {
        indexText = L"연결 안 됨";
    }
    else
    {
        // [DB저장파일수 비교] phase와 무관하게 항상 보이는 상시 정보 —
        // 감시 폴더 실제 파일 수 vs DB에 영속 저장된 고유 파일 수. 다르면
        // "몇 개가 아직 색인 안 됐거나 실패했다"는 신호.
        wchar_t dirDbBuf[128];
        if (s.hasDbSavedFiles)
        {
            swprintf_s(dirDbBuf, L"디렉토리: %d개 · DB 저장: %d개", s.dirTotalFiles, s.dbSavedFiles);
        }
        else
        {
            swprintf_s(dirDbBuf, L"디렉토리: %d개", s.dirTotalFiles);
        }
        indexText = dirDbBuf;
        if (s.hasDbSavedFiles && s.dirTotalFiles != s.dbSavedFiles)
        {
            wchar_t warnBuf[64];
            swprintf_s(warnBuf, L"\r\n⚠ 차이 %d개 (색인 대기/실패 가능성)",
                       s.dirTotalFiles - s.dbSavedFiles);
            indexText += warnBuf;
        }

        wchar_t buf[256];
        swprintf_s(buf, L"\r\n이번 회차: %s — %d/%d 파일, %d 조각",
                   s.phase.c_str(), s.doneFiles, s.totalFiles, s.chunkCount);
        indexText += buf;
        if (s.hasProgressFile)
        {
            indexText += L"\r\n";
            indexText += s.progressFileName;
            indexText += L" 처리 중";
            if (s.hasFileProgress && s.fileProgressTotal > 0)
            {
                wchar_t fpBuf[64];
                swprintf_s(fpBuf, L" (%d/%d 조각)", s.fileProgressDone, s.fileProgressTotal);
                indexText += fpBuf;
            }
        }
        // [상태정보확장 2단계]
        wchar_t roundBuf[192];
        swprintf_s(roundBuf, L"\r\n이번 회차: 신규 %d · 변경 %d · 재사용 %d · 중복스킵 %d · 실패 %d",
                   s.roundNew, s.roundChanged, s.roundReused, s.roundDupSkip, s.roundFailed);
        indexText += roundBuf;
        wchar_t timeBuf[96];
        if (s.hasEta)
            swprintf_s(timeBuf, L"\r\n경과 %.1f분, 예상 잔여 약 %.1f분", s.elapsedSec / 60.0, s.etaSec / 60.0);
        else
            swprintf_s(timeBuf, L"\r\n경과 %.1f분", s.elapsedSec / 60.0);
        indexText += timeBuf;
    }
    m_index.SetWindowTextW(indexText.c_str());

    // [상태정보확장 1단계] 프로그레스바 2개 — 우선 기존 필드(누적 doneFiles/
    // totalFiles, 파일 내부 진행)만으로 채운다. "이번 회차" 기준 분자/분모는
    // 다음 단계에서 회차 집계 필드를 다시 들여올 때 정확히 맞춘다.
    int overallPct = (s.totalFiles > 0)
        ? std::max(0, std::min(100, (int)(100.0 * s.doneFiles / s.totalFiles + 0.5)))
        : 100;
    m_progressOverall.SetPos(overallPct);

    int filePct = (s.hasFileProgress && s.fileProgressTotal > 0)
        ? std::max(0, std::min(100, (int)(100.0 * s.fileProgressDone / s.fileProgressTotal + 0.5)))
        : 0;
    m_progressFile.SetPos(filePct);

    // [디스크]
    wchar_t diskBuf[128];
    if (s.hasReindexDb)
        swprintf_s(diskBuf, L"활성 %.1fMB (재색인 중 %.1fMB)", s.activeDbMb, s.reindexDbMb);
    else
        swprintf_s(diskBuf, L"활성 %.1fMB", s.activeDbMb);
    m_disk.SetWindowTextW(diskBuf);

    // [상태정보확장 3단계] 감시 폴더 — 최대 3개까지 이름 나열 + "외 N개".
    std::wstring foldersText;
    if (!s.foldersOk)
    {
        foldersText = L"연결 안 됨";
    }
    else if (s.watchedFolders.empty())
    {
        foldersText = L"없음 (설정 탭에서 추가하세요)";
    }
    else
    {
        wchar_t cntBuf[16];
        swprintf_s(cntBuf, L"%zu개: ", s.watchedFolders.size());
        foldersText = cntBuf;
        size_t shown = std::min<size_t>(3, s.watchedFolders.size());
        for (size_t i = 0; i < shown; i++)
        {
            if (i > 0) foldersText += L", ";
            const std::wstring& p = s.watchedFolders[i];
            size_t slash = p.find_last_of(L"/\\");
            foldersText += (slash == std::wstring::npos) ? p : p.substr(slash + 1);
        }
        if (s.watchedFolders.size() > 3)
        {
            wchar_t moreBuf[16];
            swprintf_s(moreBuf, L" 외 %zu개", s.watchedFolders.size() - 3);
            foldersText += moreBuf;
        }
    }
    m_folders.SetWindowTextW(foldersText.c_str());

    // [경고]
    wchar_t warnBuf[64];
    swprintf_s(warnBuf, L"경고 %d건 (클릭해서 보기)", s.warningCount);
    m_btnWarnings.SetWindowTextW(warnBuf);
    m_recentWarnings = s.recentWarnings;

    // [일시정지]/[재개]
    bool isPaused = (s.phase == L"paused");
    m_btnPauseResume.SetWindowTextW(isPaused ? L"재개" : L"일시정지");
}

// ── 버튼 핸들러 (개별 클릭 액션 — 짧고 1회성이라 UI 스레드에서 직접 호출.
//    반복적으로 도는 폴링만 워커 스레드로 옮기라는 게 이번 티켓의 요구였음) ──

void CDialogSkeleton::OnBtnPauseResume()
{
    // [워치독] 자동 재시작을 포기한 뒤엔 이 버튼이 "다시 시도"로 바뀌어
    // 있다(ApplyStatus 참고) — 그 상태에서 클릭되면 일시정지/재개가 아니라
    // 수동 재시작 요청으로 취급한다. 워치독 카운터도 리셋해서, 이번에도
    // 5분 안에 안 되면 다시 자동으로 3회까지 재시도할 기회를 준다.
    if (m_watchdogGaveUp)
    {
        m_watchdogGaveUp = false;
        m_watchdogRestartCount = 0;
        m_watchdogWindowStartTick = GetTickCount64();
        m_pTrayWnd->RestartChildProcess();
        return;
    }

    CApiClient api(m_settings.host, m_settings.port);
    bool isPaused = (m_currentPhase == L"paused");
    api.Post(isPaused ? L"/indexer/resume" : L"/indexer/pause");
    // 버튼 캡션/phase는 다음 폴링 회차에 실제 응답 기준으로 갱신된다 —
    // 여기서 미리 낙관적으로 바꾸지 않는다(실제 상태와 어긋날 수 있어서).
}

void CDialogSkeleton::OnBtnWarnings()
{
    if (m_recentWarnings.empty())
    {
        MessageBoxW(L"경고가 없습니다.", L"NotebookRAG", MB_ICONINFORMATION);
        return;
    }
    std::wstring text;
    for (const auto& w : m_recentWarnings)
    {
        text += w;
        text += L"\r\n\r\n";
    }
    MessageBoxW(text.c_str(), L"NotebookRAG — 최근 경고", MB_ICONWARNING);
}

void CDialogSkeleton::RefreshFolderList()
{
    // [버그 수정] 여기서 직접 API를 부르지 않는다 — 백그라운드 스레드를 하나
    // 띄우고 즉시 반환한다(UI 스레드를 절대 안 막음). 스레드 자체 수명은
    // OS가 관리하므로 핸들은 바로 닫아도 안전하다(스레드는 계속 실행됨).
    HANDLE h = (HANDLE)_beginthreadex(nullptr, 0, &CDialogSkeleton::RefreshFolderListThreadProc,
                                       this, 0, nullptr);
    if (h) CloseHandle(h);
}

unsigned __stdcall CDialogSkeleton::RefreshFolderListThreadProc(void* param)
{
    CDialogSkeleton* self = static_cast<CDialogSkeleton*>(param);
    FolderListResult* result = new FolderListResult();

    CApiClient api(self->m_settings.host, self->m_settings.port);
    ApiResult r = api.Get(L"/indexer/folders");
    if (r.statusCode == 200)
    {
        result->ok = true;
        if (const JsonValue* arr = r.body.Find("docs_dirs"))
        {
            for (size_t i = 0; i < arr->ArraySize(); i++)
                result->paths.push_back(arr->ArrayAt(i).AsString());
        }
    }

    if (self->m_hWnd && ::IsWindow(self->m_hWnd))
        ::PostMessage(self->m_hWnd, WM_FOLDERS_UPDATE, 0, (LPARAM)result);
    else
        delete result;
    return 0;
}

LRESULT CDialogSkeleton::OnFoldersUpdate(WPARAM /*wParam*/, LPARAM lParam)
{
    FolderListResult* result = reinterpret_cast<FolderListResult*>(lParam);
    if (result)
    {
        // 실패 시엔 목록을 지우지 않고 그대로 둔다(마지막으로 알려진 목록이
        // 나은 정보임 — WM_STATUS_UPDATE 쪽과 같은 원칙).
        if (result->ok)
        {
            m_folderList.ResetContent();
            m_folderPaths = result->paths;
            for (const auto& p : m_folderPaths)
                m_folderList.AddString(p.c_str());
        }
        delete result;
    }
    return 0;
}

void CDialogSkeleton::OnBtnAddFolder()
{
    IFileOpenDialog* pDlg = nullptr;
    HRESULT hr = CoCreateInstance(CLSID_FileOpenDialog, nullptr, CLSCTX_INPROC_SERVER,
                                    IID_PPV_ARGS(&pDlg));
    if (FAILED(hr)) return;

    DWORD opts = 0;
    pDlg->GetOptions(&opts);
    pDlg->SetOptions(opts | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM);

    hr = pDlg->Show(m_hWnd);
    if (SUCCEEDED(hr))
    {
        IShellItem* pItem = nullptr;
        if (SUCCEEDED(pDlg->GetResult(&pItem)))
        {
            PWSTR pszPath = nullptr;
            if (SUCCEEDED(pItem->GetDisplayName(SIGDN_FILESYSPATH, &pszPath)))
            {
                std::wstring normalized = pszPath;
                CoTaskMemFree(pszPath);
                // 백슬래시 -> 슬래시: indexer_config.json/Python 쪽과 표기를 통일.
                for (auto& ch : normalized) if (ch == L'\\') ch = L'/';

                std::string body = "{\"path\":\"" + JsonEscapeToUtf8(normalized) + "\"}";
                CApiClient api(m_settings.host, m_settings.port);
                ApiResult r = api.Post(L"/indexer/folders", body);
                if (r.statusCode != 200)
                {
                    std::wstring detail = L"알 수 없는 오류";
                    if (const JsonValue* d = r.body.Find("detail")) detail = d->AsString(detail);
                    MessageBoxW((L"폴더 추가 실패: " + detail).c_str(), L"NotebookRAG", MB_ICONWARNING);
                }
                else
                {
                    RefreshFolderList();
                }
            }
            pItem->Release();
        }
    }
    pDlg->Release();
}

void CDialogSkeleton::OnBtnRemoveFolder()
{
    int sel = m_folderList.GetCurSel();
    if (sel == LB_ERR)
    {
        MessageBoxW(L"제거할 폴더를 먼저 선택하세요.", L"NotebookRAG", MB_ICONINFORMATION);
        return;
    }
    std::wstring path = m_folderPaths[sel];
    std::string body = "{\"path\":\"" + JsonEscapeToUtf8(path) + "\"}";
    CApiClient api(m_settings.host, m_settings.port);
    api.Delete(L"/indexer/folders", body);
    RefreshFolderList();
}

void CDialogSkeleton::OnBtnEditRules()
{
    int sel = m_folderList.GetCurSel();
    if (sel == LB_ERR)
    {
        MessageBoxW(L"규칙을 편집할 폴더를 먼저 선택하세요.", L"NotebookRAG", MB_ICONINFORMATION);
        return;
    }
    std::wstring folder = m_folderPaths[sel];

    CApiClient api(m_settings.host, m_settings.port);
    ApiResult r = api.Get(L"/indexer/rules?folder=" + UrlEncodeUtf8(folder));

    std::vector<std::wstring> patterns;
    if (r.statusCode == 200)
    {
        if (const JsonValue* arr = r.body.Find("patterns"))
        {
            for (size_t i = 0; i < arr->ArraySize(); i++)
                patterns.push_back(arr->ArrayAt(i).AsString());
        }
    }

    CRulesEditDialog dlg(folder, patterns, this);
    if (dlg.DoModal() == IDOK)
    {
        const auto& newPatterns = dlg.GetPatterns();
        std::string arrJson = "[";
        for (size_t i = 0; i < newPatterns.size(); i++)
        {
            if (i) arrJson += ",";
            arrJson += "\"" + JsonEscapeToUtf8(newPatterns[i]) + "\"";
        }
        arrJson += "]";
        std::string body = "{\"folder\":\"" + JsonEscapeToUtf8(folder) + "\",\"patterns\":" + arrJson + "}";
        api.Put(L"/indexer/rules", body);
    }
}

void CDialogSkeleton::OnBtnAutostart()
{
    // [티켓 H] 체크박스 클릭 즉시 반영 — 별도 "저장" 버튼 없음(이진 토글).
    bool wantEnable = (m_chkAutostart.GetCheck() == BST_CHECKED);
    bool ok = SetAutostartRegistered(wantEnable);
    if (!ok)
    {
        MessageBoxW(wantEnable ? L"자동 시작 등록에 실패했습니다." : L"자동 시작 해제에 실패했습니다.",
                    L"NotebookRAG", MB_ICONWARNING);
    }
    // 낙관적 업데이트 금지 — 실제 반영된 레지스트리 상태를 다시 조회해서
    // 체크박스에 반영한다(실패 시 원래 상태로 되돌아감).
    m_chkAutostart.SetCheck(IsAutostartRegistered() ? BST_CHECKED : BST_UNCHECKED);
}

void CDialogSkeleton::OnStnClickedInfoGithub()
{
    // [정보탭_버전관리] Windows 표준 방식(ShellExecute)으로 기본 브라우저를
    // 연다 — 새 의존성 불필요. github URL은 /health에서 받아온 값을 그대로
    // 쓴다(하드코딩 금지). 아직 한 번도 못 받았으면(healthOk 없었던 경우)
    // 조용히 무시한다.
    if (m_githubUrl.empty()) return;
    ShellExecuteW(m_hWnd, L"open", m_githubUrl.c_str(), nullptr, nullptr, SW_SHOWNORMAL);
}
