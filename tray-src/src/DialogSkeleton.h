#pragma once
#include <afxwin.h>
#include <afxcmn.h>
#include <string>
#include <vector>
#include "resource.h"
#include "SettingsReader.h"

// [티켓 G] 폴링 결과를 워커 스레드 → UI 스레드로 넘길 때 쓰는 메시지.
// 워커 스레드가 힙에 PolledStatus를 하나 만들어 lParam으로 PostMessage하고,
// UI 스레드(여기)가 받아서 쓰고 delete한다 — 소유권이 한 번에 넘어가는
// 단발성 전달이라 critical section 없이도 안전하다.
#define WM_STATUS_UPDATE (WM_APP + 1)

// [버그 수정] 감시 폴더 목록 조회(GET /indexer/folders)를 예전엔 "설정" 탭으로
// 전환할 때 UI 스레드에서 직접(동기) 호출해서, notebookrag.exe가 응답이
// 없을 때(임베딩 중 GIL 문제 등) 탭 전환 자체가 최대 8~11초(WinHTTP 타임아웃)
// 동안 다이얼로그 전체를 멈춰버렸다. WM_STATUS_UPDATE와 같은 방식(백그라운드
// 스레드 → PostMessage)으로 옮긴다.
#define WM_FOLDERS_UPDATE (WM_APP + 2)

struct FolderListResult
{
    bool ok = false;
    std::vector<std::wstring> paths;
};

class CTrayWnd;

// [티켓 G] GET /health, /indexer/status, /model/status 세 응답을 UI가
// 쓰기 좋은 형태로 미리 풀어놓은 스냅샷.
struct PolledStatus
{
    bool healthOk = false;
    int chunks = 0;
    bool hasLastSearch = false;
    std::wstring lastSearchAtIso;

    bool indexerOk = false;
    std::wstring phase;
    int totalFiles = 0;
    int doneFiles = 0;
    int chunkCount = 0;
    bool hasProgressFile = false;
    std::wstring progressFileName;
    bool hasFileProgress = false;  // [버그 수정] 파일 내부 진행(청크 단위) — 예전엔
    int fileProgressDone = 0;      // 안 읽어서 큰 파일 처리 중 화면이 몇 분씩
    int fileProgressTotal = 0;     // 안 바뀌어 "멈춘 것처럼" 보였음.
    double activeDbMb = 0.0;
    bool hasReindexDb = false;
    double reindexDbMb = 0.0;
    int warningCount = 0;
    std::vector<std::wstring> recentWarnings;

    bool modelOk = false;
    std::wstring modelPhase;
    double modelDownloadedMb = 0.0;
    double modelTotalMb = 0.0;
    double modelProgressPct = 0.0;
    bool hasModelError = false;
    std::wstring modelError;
    int modelDim = 0;  // [상태정보확장 4단계] GET /model/status의 "차원"

    int searchCountToday = 0;  // [상태정보확장 4단계] GET /health의 "오늘검색횟수"

    // [상태정보확장 4단계] CPU%/메모리/가동시간/PID — API가 아니라 Job Object가
    // 쥔 자식 프로세스 핸들을 폴링 스레드가 직접 조회한 값.
    bool hasCpuSample = false;
    double cpuPercent = 0.0;
    double memMb = 0.0;
    double uptimeSec = 0.0;
    DWORD pid = 0;

    // [상태정보확장 2단계]
    int roundNew = 0, roundChanged = 0, roundReused = 0, roundDupSkip = 0, roundFailed = 0;
    double elapsedSec = 0.0;
    bool hasEta = false;
    double etaSec = 0.0;

    // [DB저장파일수 비교] 감시 폴더 실제 파일 개수 vs DB에 영속 저장된
    // 고유 파일 개수 — 둘이 다르면 "뭔가 색인 안 됐거나 실패했다"는 신호.
    int dirTotalFiles = 0;
    bool hasDbSavedFiles = false;
    int dbSavedFiles = 0;

    // [상태정보확장 3단계] 감시 폴더 — 신규 API 없이 기존 GET /indexer/folders 재사용.
    bool foldersOk = false;
    std::vector<std::wstring> watchedFolders;
};

// [티켓 F 뼈대 → 티켓 G 실제 구현] 탭 2개("상태정보"/"설정")를 실제
// API(GET /health, /indexer/status, /model/status, /folders, /rules,
// POST/DELETE /folders, POST /pause·/resume)와 연동한다.
//
// 폴링(3개 GET)은 전부 별도 워커 스레드에서 수행하고, UI 스레드는
// WM_STATUS_UPDATE로 받은 스냅샷을 컨트롤에 반영만 한다 — 티켓 F가
// /health 하나만 메인 스레드(타이머)에서 동기 호출했던 걸, 이번 티켓에서
// 세 개로 늘어난 것을 계기로 워커 스레드로 옮겼다(티켓 F 스스로 남긴 과제).
class CDialogSkeleton : public CDialog
{
public:
    // pTrayWnd: 폴링 성공/실패에 따라 트레이 아이콘 툴팁도 갱신하기 위해
    // 필요(같은 폴링 결과를 재사용 — /health를 두 번 호출하지 않음).
    explicit CDialogSkeleton(CTrayWnd* pTrayWnd, CWnd* pParent = nullptr);
    virtual ~CDialogSkeleton();

    enum { IDD = IDD_MAIN_DIALOG };

    // CTrayWnd::Init()에서 다이얼로그를 만든 직후(숨김 상태로) 호출 —
    // 폴링은 다이얼로그가 화면에 보이든 아니든 계속 돈다(그래야 탭을 열었을
    // 때 이미 최신 데이터가 반영돼 있음).
    void StartPolling();

protected:
    virtual BOOL OnInitDialog();
    virtual void DoDataExchange(CDataExchange* pDX);
    afx_msg void OnTcnSelchangeTab(NMHDR* pNMHDR, LRESULT* pResult);
    afx_msg void OnClose();
    afx_msg void OnDestroy();
    afx_msg void OnShowWindow(BOOL bShow, UINT nStatus);
    afx_msg LRESULT OnStatusUpdate(WPARAM wParam, LPARAM lParam);
    afx_msg LRESULT OnFoldersUpdate(WPARAM wParam, LPARAM lParam);
    afx_msg void OnBtnPauseResume();
    afx_msg void OnBtnWarnings();
    afx_msg void OnBtnAddFolder();
    afx_msg void OnBtnRemoveFolder();
    afx_msg void OnBtnEditRules();
    afx_msg void OnBtnAutostart();
    DECLARE_MESSAGE_MAP()

private:
    void ShowTab(int index);
    void ApplyStatus(const PolledStatus& s);
    void RefreshFolderList();  // 백그라운드 스레드를 한 번 띄우기만 하고 즉시 반환(비블로킹)

    static unsigned __stdcall PollThreadProc(void* param);
    void PollLoop();
    static unsigned __stdcall RefreshFolderListThreadProc(void* param);

    CTrayWnd* m_pTrayWnd;
    CTabCtrl m_tabCtrl;

    // 탭 1 — 상태정보
    CStatic m_conn;
    CStatic m_modelLabel, m_model;
    CStatic m_mcpLabel, m_mcp;
    CStatic m_indexLabel, m_index;
    CStatic m_diskLabel, m_disk;
    CButton m_btnWarnings;
    CButton m_btnPauseResume;
    CProgressCtrl m_progressOverall, m_progressFile;  // [상태정보확장 1단계]
    CStatic m_foldersLabel, m_folders;                // [상태정보확장 3단계]

    // 탭 2 — 설정
    CListBox m_folderList;
    CButton m_btnAddFolder, m_btnRemoveFolder, m_btnEditRules;
    CButton m_chkAutostart;  // [티켓 H]

    Settings m_settings;
    std::wstring m_currentPhase;      // /indexer/status.phase 최신값(일시정지/재개 버튼 판단용)
    std::vector<std::wstring> m_recentWarnings;
    std::vector<std::wstring> m_folderPaths; // IDC_LIST_FOLDERS와 같은 순서로 유지

    HANDLE m_hPollThread;
    volatile bool m_stopPolling;

    // [버그 수정] 임베딩 중 백엔드가 API 응답을 못 하는 동안(GIL 문제,
    // 큰 파일 하나에 수십 분 걸릴 수 있음) 화면이 "연결 안 됨"으로 깜깜해져서
    // 진행 상황을 전혀 알 수 없다는 사용자 피드백 — 폴링 실패 시 기존 라벨
    // 텍스트를 지우지 않고 그대로 남겨두고(마지막 성공 시점의 값), 연결 줄에만
    // "마지막 확인: N분 전" 표시를 얹는다.
    bool m_haveGoodStatus = false;
    ULONGLONG m_lastGoodTick = 0;

    // [상태정보확장 4단계] CPU% 계산용 이전 샘플(폴링 스레드에서만 읽고 쓰므로
    // 락 불필요) — 100ns 단위(FILETIME 그대로).
    bool m_haveCpuSample = false;
    ULONGLONG m_lastCpuTime100ns = 0;
    ULONGLONG m_lastSampleTime100ns = 0;
    DWORD m_numCores = 1;
};
