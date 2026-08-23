#pragma once
#include <string>
#include "JsonValue.h"

// [티켓 G] WinHTTP(Windows 내장) 기반 REST 호출 헬퍼 — GET/POST/PUT/DELETE.
// 폴링 워커 스레드와 UI 버튼 핸들러(일시정지/재개/폴더 추가삭제/규칙 편집)
// 양쪽에서 공용으로 쓴다. 외부 HTTP 라이브러리를 새로 들이지 않는다는
// 티켓 F의 원칙을 그대로 유지.
struct ApiResult
{
    int statusCode = 0; // 0 = 네트워크 오류(연결 실패/타임아웃 등)
    JsonValue body;
};

class CApiClient
{
public:
    CApiClient(const std::wstring& host, int port);

    ApiResult Get(const std::wstring& path) const;
    ApiResult Post(const std::wstring& path, const std::string& jsonBodyUtf8 = "") const;
    ApiResult Put(const std::wstring& path, const std::string& jsonBodyUtf8) const;
    ApiResult Delete(const std::wstring& path, const std::string& jsonBodyUtf8 = "") const;

private:
    ApiResult Request(const std::wstring& method, const std::wstring& path,
                       const std::string& jsonBodyUtf8) const;

    std::wstring m_host;
    int m_port;
};

std::string WideToUtf8(const std::wstring& s);
std::wstring Utf8ToWideStr(const std::string& s);

// 경로/폴더명 등을 JSON 문자열 리터럴 안에 안전하게 넣을 때 사용 —
// 이 프로젝트가 실제로 주고받는 값(파일 경로, 패턴) 범위에 맞춘 최소
// 이스케이프(따옴표/백슬래시/개행류)만 처리한다.
std::string JsonEscapeToUtf8(const std::wstring& s);

// 쿼리스트링 값(예: /indexer/rules?folder=<경로>)에 넣기 위한 percent-encoding.
// UTF-8 바이트 단위로 인코딩하므로 한글 경로도 안전하게 들어간다.
std::wstring UrlEncodeUtf8(const std::wstring& s);
