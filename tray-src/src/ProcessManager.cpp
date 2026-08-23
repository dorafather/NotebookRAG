#include "ProcessManager.h"
#include <vector>

CProcessManager::CProcessManager()
    : m_hJob(nullptr)
{
    ZeroMemory(&m_pi, sizeof(m_pi));

    m_hJob = CreateJobObjectW(nullptr, nullptr);
    if (m_hJob)
    {
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION limitInfo;
        ZeroMemory(&limitInfo, sizeof(limitInfo));
        limitInfo.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        SetInformationJobObject(m_hJob, JobObjectExtendedLimitInformation,
                                 &limitInfo, sizeof(limitInfo));
    }
}

CProcessManager::~CProcessManager()
{
    // Job 핸들을 닫으면(그리고 이게 그 Job을 참조하는 마지막 핸들이면)
    // KILL_ON_JOB_CLOSE 덕분에 OS가 배정된 자식 프로세스를 자동으로 죽인다.
    if (m_pi.hThread)  CloseHandle(m_pi.hThread);
    if (m_pi.hProcess) CloseHandle(m_pi.hProcess);
    if (m_hJob)        CloseHandle(m_hJob);
}

bool CProcessManager::Launch(const std::wstring& exePath, const std::wstring& workingDir)
{
    if (!m_hJob) return false;

    STARTUPINFOW si;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);

    // CreateProcessW의 두 번째 인자(lpCommandLine)는 쓰기 가능한 버퍼여야
    // 해서 std::wstring을 직접 넘길 수 없다 — vector<wchar_t>로 복사.
    std::wstring cmdLine = L"\"" + exePath + L"\"";
    std::vector<wchar_t> cmdLineBuf(cmdLine.begin(), cmdLine.end());
    cmdLineBuf.push_back(L'\0');

    BOOL ok = CreateProcessW(
        exePath.c_str(),
        cmdLineBuf.data(),
        nullptr, nullptr, FALSE,
        CREATE_SUSPENDED | CREATE_NO_WINDOW,
        nullptr,
        workingDir.empty() ? nullptr : workingDir.c_str(),
        &si, &m_pi);

    if (!ok)
    {
        ZeroMemory(&m_pi, sizeof(m_pi));
        return false;
    }

    // CREATE_SUSPENDED로 띄운 이유: AssignProcessToJobObject()를 부르기
    // 전에 이미 스레드가 돌기 시작하면(특히 CPU를 많이 쓰는 초기화 코드가
    // 있으면) 아주 짧은 순간이지만 Job 보호 없이 실행되는 창이 생긴다 —
    // 그 창을 아예 없애기 위해 정지 상태로 만든 뒤 배정하고 나서 재개한다.
    if (!AssignProcessToJobObject(m_hJob, m_pi.hProcess))
    {
        TerminateProcess(m_pi.hProcess, 1);
        CloseHandle(m_pi.hThread);
        CloseHandle(m_pi.hProcess);
        ZeroMemory(&m_pi, sizeof(m_pi));
        return false;
    }

    ResumeThread(m_pi.hThread);
    return true;
}

void CProcessManager::Terminate()
{
    if (m_pi.hProcess)
    {
        TerminateProcess(m_pi.hProcess, 0);
    }
}

bool CProcessManager::IsRunning() const
{
    if (!m_pi.hProcess) return false;
    DWORD exitCode = 0;
    if (!GetExitCodeProcess(m_pi.hProcess, &exitCode)) return false;
    return exitCode == STILL_ACTIVE;
}
