#include "TrayApp.h"
#include "DialogSkeleton.h"
#include <shellapi.h>
#include <afxdisp.h> // AfxOleInit()

// ── CTrayWnd ─────────────────────────────────────────────────────────────

CTrayWnd::CTrayWnd()
    : m_pDialog(nullptr)
{
    ZeroMemory(&m_nid, sizeof(m_nid));
}

CTrayWnd::~CTrayWnd()
{
    if (m_pDialog)
    {
        m_pDialog->DestroyWindow();
        delete m_pDialog;
    }
}

BEGIN_MESSAGE_MAP(CTrayWnd, CWnd)
    ON_WM_DESTROY()
    ON_MESSAGE(WM_TRAYICON, &CTrayWnd::OnTrayIcon)
    ON_COMMAND(ID_TRAY_EXIT, &CTrayWnd::OnTrayExit)
END_MESSAGE_MAP()

BOOL CTrayWnd::Init()
{
    // 트레이 메시지만 받으면 되는 숨김 창 — 화면에 안 보이므로
    // CFrameWnd보다 가벼운 CWnd로 충분하다.
    CString className = AfxRegisterWndClass(0);
    if (!CreateEx(0, className, L"NotebookRAGTrayHidden", WS_OVERLAPPED,
                  CRect(0, 0, 0, 0), nullptr, 0))
    {
        return FALSE;
    }

    LaunchChildProcess();

    // 트레이 아이콘 등록
    m_nid.cbSize = sizeof(m_nid);
    m_nid.hWnd = m_hWnd;
    m_nid.uID = 1;
    m_nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP;
    m_nid.uCallbackMessage = WM_TRAYICON;
    m_nid.hIcon = AfxGetApp()->LoadIcon(IDI_TRAY_ICON);
    wcscpy_s(m_nid.szTip, L"NotebookRAG");
    Shell_NotifyIconW(NIM_ADD, &m_nid);

    // [티켓 G] 다이얼로그를 미리(숨김) 만들고 폴링을 바로 시작한다 —
    // 그래야 사용자가 좌클릭했을 때 이미 최신 상태가 반영돼 있다.
    m_pDialog = new CDialogSkeleton(this);
    m_pDialog->Create(CDialogSkeleton::IDD, nullptr);
    m_pDialog->StartPolling();

    return TRUE;
}

void CTrayWnd::LaunchChildProcess()
{
    // notebookrag.exe 경로: tray.exe 자기 위치(bin/tray/tray.exe) 기준
    // 상대경로로 해석 — bin/notebookrag/notebookrag.exe. Init()과
    // RestartChildProcess()(워치독) 양쪽이 공유하는 경로 계산+기동 로직.
    wchar_t exePathBuf[MAX_PATH];
    GetModuleFileNameW(nullptr, exePathBuf, MAX_PATH);
    std::wstring trayExeDir = exePathBuf;
    size_t slash1 = trayExeDir.find_last_of(L'\\');
    trayExeDir = trayExeDir.substr(0, slash1);                    // .../bin/tray
    size_t slash2 = trayExeDir.find_last_of(L'\\');
    std::wstring binDir = trayExeDir.substr(0, slash2);            // .../bin
    std::wstring notebookragDir = binDir + L"\\notebookrag";
    std::wstring notebookragExe = notebookragDir + L"\\notebookrag.exe";

    m_processManager.Launch(notebookragExe, notebookragDir);
}

void CTrayWnd::RestartChildProcess()
{
    // [워치독] Terminate()가 이제 TerminateJobObject를 쓰므로 자식+손자
    // (색인 자식 프로세스)까지 한 번에 죽는다. 다만 TerminateProcess류
    // API는 종료를 "요청"만 하고 비동기이므로, 포트(8420/8421)가 실제로
    // 반납되기 전에 새 프로세스를 바로 띄우면 바인드 실패 race가 생길 수
    // 있다 — 옛 프로세스 핸들이 실제로 신호 상태(종료 완료)가 될 때까지
    // 잠깐 기다린 뒤에 재기동한다(최대 5초, 그 이상 걸리면 포기하고
    // 그냥 진행 — 무한 대기는 안 함).
    HANDLE hOldProcess = m_processManager.GetProcessHandle();
    m_processManager.Terminate();
    if (hOldProcess)
    {
        WaitForSingleObject(hOldProcess, 5000);
    }
    LaunchChildProcess();
}

void CTrayWnd::UpdateTooltip(bool connected)
{
    const wchar_t* text = connected ? L"NotebookRAG (연결됨)" : L"NotebookRAG (연결 안 됨)";
    wcscpy_s(m_nid.szTip, text);
    Shell_NotifyIconW(NIM_MODIFY, &m_nid);
}

LRESULT CTrayWnd::OnTrayIcon(WPARAM /*wParam*/, LPARAM lParam)
{
    switch (lParam)
    {
    case WM_LBUTTONUP:
        OpenDialog();
        break;
    case WM_RBUTTONUP:
        ShowContextMenu();
        break;
    default:
        break;
    }
    return 0;
}

void CTrayWnd::OpenDialog()
{
    // [티켓 G] Init()에서 이미 만들어뒀으므로 여기서는 보여주기만 한다.
    m_pDialog->ShowWindow(SW_SHOW);
    m_pDialog->SetForegroundWindow();
}

void CTrayWnd::ShowContextMenu()
{
    CMenu menu;
    menu.CreatePopupMenu();
    menu.AppendMenuW(MF_STRING, ID_TRAY_EXIT, L"종료");

    CPoint pt;
    GetCursorPos(&pt);
    // 트레이 컨텍스트 메뉴 표준 관용구: SetForegroundWindow를 먼저 불러야
    // 메뉴 바깥을 클릭했을 때 메뉴가 안 닫히는 문제가 안 생긴다.
    SetForegroundWindow();
    menu.TrackPopupMenu(TPM_RIGHTBUTTON, pt.x, pt.y, this);
    PostMessage(WM_NULL);
}

void CTrayWnd::OnTrayExit()
{
    DestroyWindow(); // → OnDestroy에서 정리 후 PostQuitMessage
}

void CTrayWnd::OnDestroy()
{
    Shell_NotifyIconW(NIM_DELETE, &m_nid);
    // 정상 종료 경로에서 명시적으로 한 번 정리 — Job Object 소멸자의
    // KILL_ON_JOB_CLOSE가 어차피 다시 한번 보장해주는 이중 안전장치.
    m_processManager.Terminate();
    CWnd::OnDestroy();
    PostQuitMessage(0);
}

// ── CTrayApp ─────────────────────────────────────────────────────────────

BOOL CTrayApp::InitInstance()
{
    // [폴링고정및중복실행수정] 단일 인스턴스 가드 — 반드시 이 함수의
    // 최상단, CWinApp::InitInstance()보다도 먼저 와야 한다. Run 키와
    // 시작프로그램 바로가기 둘 다로 tray.exe를 기동하도록 이중화해둔
    // 부작용으로(티켓 H/I) 재부팅 시 두 경로가 거의 동시에 tray.exe를
    // 띄워 중복 실행되는 게 실사용 중 확인됨 — 이중 등록 자체는 회사
    // 보안 에이전트의 Run 키 필터링 대응으로 계속 유지해야 하므로, 대신
    // 여기서 두 번째 인스턴스를 창/Job Object/notebookrag.exe 자식
    // 프로세스를 하나도 만들기 전에 걸러낸다.
    m_hSingleInstanceMutex = CreateMutexW(nullptr, TRUE, L"Global\\NotebookRAG_TrayApp_SingleInstance");
    if (GetLastError() == ERROR_ALREADY_EXISTS)
    {
        if (m_hSingleInstanceMutex)
        {
            CloseHandle(m_hSingleInstanceMutex);
            m_hSingleInstanceMutex = nullptr;
        }
        return FALSE;
    }

    CWinApp::InitInstance();

    // [티켓 G] 폴더 선택(IFileOpenDialog)이 COM 기반이라 필요.
    if (!AfxOleInit())
    {
        return FALSE;
    }

    INITCOMMONCONTROLSEX icc;
    icc.dwSize = sizeof(icc);
    // [상태정보확장] 프로그레스바 2개(msctls_progress32) 추가 — 이 클래스를
    // 등록 안 하면 다이얼로그 리소스의 CONTROL "msctls_progress32"가 생성
    // 실패해서 DDX_Control이 NULL HWND를 붙잡고, 이후 다이얼로그 전체
    // 렌더링이 이상해질 수 있음.
    icc.dwICC = ICC_TAB_CLASSES | ICC_PROGRESS_CLASS;
    InitCommonControlsEx(&icc);

    m_pTrayWnd = new CTrayWnd();
    if (!m_pTrayWnd->Init())
    {
        delete m_pTrayWnd;
        m_pTrayWnd = nullptr;
        return FALSE;
    }

    m_pMainWnd = m_pTrayWnd;
    return TRUE;
}

int CTrayApp::ExitInstance()
{
    // [폴링고정및중복실행수정] 정상 종료 시 뮤텍스를 명시적으로 해제 —
    // 안 해도 프로세스 종료 시 OS가 핸들을 정리하지만, 여기서 명시적으로
    // 닫아야 "종료 직후 재실행"이 새 프로세스로 뮤텍스를 즉시 다시 잡을 수
    // 있다는 게 코드만 봐도 분명해진다(타이밍 회귀 방지 목적의 명시적 정리).
    if (m_hSingleInstanceMutex)
    {
        CloseHandle(m_hSingleInstanceMutex);
        m_hSingleInstanceMutex = nullptr;
    }
    return CWinApp::ExitInstance();
}

CTrayApp theApp;
