#pragma once
#include <windows.h>
#include <string>

// [티켓 F] Job Object 기반 자식 프로세스 관리 — 클린룸 재구현.
// SLEE.exe(00. daemon)가 쓰던 "Job Object로 자식을 묶어서 부모가 어떻게
// 죽든 같이 정리되게 한다"는 아이디어만 재사용하고 코드는 새로 짰다.
//
// 핵심 원리: CreateJobObject()로 만든 Job에
// JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE를 걸어두면, 이 Job에 배정된
// 프로세스들은 "Job을 참조하는 마지막 핸들이 닫히는 순간" OS가 자동으로
// 전부 종료시킨다. tray.exe 프로세스가 정상 종료/강제 종료(작업
// 관리자)/크래시 중 무엇으로 끝나든 관계없이, 프로세스가 죽으면 OS가
// 그 프로세스가 들고 있던 핸들(Job 핸들 포함)을 전부 회수하므로 이
// 매커니즘이 항상 성립한다 — tray.exe 쪽에서 "종료 처리를 반드시
// 실행해야 한다"는 보장이 필요 없다는 게 핵심.
class CProcessManager
{
public:
    CProcessManager();
    ~CProcessManager();

    // exePath를 workingDir에서 기동해 Job Object에 배정한다.
    bool Launch(const std::wstring& exePath, const std::wstring& workingDir);

    // 명시적 종료(우클릭 메뉴 "종료" 등) — Job Object의 KILL_ON_JOB_CLOSE와는
    // 별개의 즉시 종료 경로. 둘 다 있어도 문제없음(TerminateProcess를 이미
    // 죽은 프로세스에 불러도 실패만 하고 안전함).
    void Terminate();

    bool IsRunning() const;

    // [상태정보확장] CPU%/메모리/가동시간을 API 없이 Job Object가 쥐고 있는
    // 자식 프로세스 핸들로 직접 조회하기 위한 접근자. 소유권은 그대로
    // CProcessManager에 있다 — 호출자는 CloseHandle하면 안 된다.
    HANDLE GetProcessHandle() const { return m_pi.hProcess; }
    DWORD GetProcessId() const { return m_pi.dwProcessId; }

private:
    CProcessManager(const CProcessManager&) = delete;
    CProcessManager& operator=(const CProcessManager&) = delete;

    HANDLE m_hJob;
    PROCESS_INFORMATION m_pi;
};
