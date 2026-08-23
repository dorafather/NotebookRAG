#pragma once
#include <string>

// [티켓 F] %APPDATA%\NotebookRAG\settings.json에서 읽어오는 값들.
// 기본값은 Python 쪽(rag_serve.py 등)과 반드시 일치시킬 것 — 이 구조체
// 필드명 자체는 아니지만, JSON 키 이름(RAG_HTTP_HOST/RAG_HTTP_PORT)은
// Python·C++ 양쪽이 공유하는 계약이다.
// [폴링고정및중복실행수정] TRAY_POLL_INTERVAL_SEC은 일반 사용자가 결정할
// 선택지가 아닌 기술 튜닝값이라 판단해 설정에서 완전히 제거하고
// DialogSkeleton.cpp의 kPollIntervalSec 코드 상수로 고정했다.
struct Settings
{
    std::wstring host = L"127.0.0.1";
    int port = 8420;
};

// 완전한 JSON 파서가 아니다 — 이 프로젝트가 스스로 생성하는 flat(중첩
// 없는) 최상위 JSON 객체 형식에 맞춘 간이 스캐너다. settings.json.template
// 의 키(RAG_HTTP_HOST/RAG_HTTP_PORT)만 뽑아낸다.
class CSettingsReader
{
public:
    // 파일이 없거나 파싱 실패해도 예외 없이 기본값을 담은 Settings를 반환한다.
    static Settings Load();
};
