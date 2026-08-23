#pragma once
#include <afxwin.h>
#include "ProcessManager.h"
#include "SettingsReader.h"

// Shell_NotifyIcon 콜백 메시지 — 표준 관용구대로 WM_APP 이후 값 사용
// (다이얼로그의 WM_STATUS_UPDATE(WM_APP+1)와는 다른 창으로 가는 다른
// 메시지라 값이 겹쳐도 문제는 없지만, 헷갈리지 않게 +100으로 떨어뜨려둠).
#define WM_TRAYICON (WM_APP + 100)

class CDialogSkeleton;

// [티켓 F 뼈대 → 티켓 G] 트레이 아이콘 자체를 소유하는 숨김 창.
// Shell_NotifyIcon 콜백만 여기서 받는다 — 상태 폴링(/health,
// /indexer/status, /model/status)은 티켓 G부터 CDialogSkeleton이 자체
// 워커 스레드로 수행하고, 그 결과 중 health 성공 여부만 UpdateTooltip()
// 호출로 여기(트레이 툴팁)에 반영한다(같은 폴링 결과 재사용 — /health를
// 두 번 호출하지 않기 위함). 다이얼로그는 Init()에서 미리(숨김 상태로)
// 만들어 두어, 좌클릭했을 때 이미 최신 데이터가 반영돼 있게 한다.
class CTrayWnd : public CWnd
{
public:
    CTrayWnd();
    virtual ~CTrayWnd();

    BOOL Init();

    // CDialogSkeleton이 폴링 결과(health 성공 여부)로 트레이 툴팁을
    // 갱신할 때 호출 — 폴링 워커 스레드가 아니라 그 결과를 처리하는
    // UI 스레드(다이얼로그)에서 호출되므로 스레드 안전성 문제 없음.
    void UpdateTooltip(bool connected);

    // [상태정보확장] CDialogSkeleton의 폴링 스레드가 CPU%/메모리/가동시간을
    // 직접 조회할 때 씀 — CTrayWnd가 CProcessManager를 소유하므로 이걸
    // 거쳐서만 핸들에 접근 가능.
    HANDLE GetChildProcessHandle() const { return m_processManager.GetProcessHandle(); }
    DWORD GetChildProcessId() const { return m_processManager.GetProcessId(); }

protected:
    afx_msg LRESULT OnTrayIcon(WPARAM wParam, LPARAM lParam);
    afx_msg void OnTrayExit();
    afx_msg void OnDestroy();
    DECLARE_MESSAGE_MAP()

private:
    void ShowContextMenu();
    void OpenDialog();

    NOTIFYICONDATAW m_nid;
    CProcessManager m_processManager;
    CDialogSkeleton* m_pDialog;
};

class CTrayApp : public CWinApp
{
public:
    virtual BOOL InitInstance();
    virtual int ExitInstance();

private:
    CTrayWnd* m_pTrayWnd = nullptr;

    // [폴링고정및중복실행수정] Run 키 + 시작프로그램 바로가기 이중 등록
    // 때문에 tray.exe가 두 경로로 동시에 기동될 수 있어서 추가한 단일
    // 인스턴스 가드. InitInstance() 최상단에서 획득, 실패하면 그 즉시
    // 종료(창/Job Object/notebookrag.exe 자식 프로세스 아무것도 안 만듦).
    HANDLE m_hSingleInstanceMutex = nullptr;
};
