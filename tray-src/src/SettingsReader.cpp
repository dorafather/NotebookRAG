#include "SettingsReader.h"
#include <windows.h>
#include <shlobj.h>
#include <fstream>
#include <sstream>
#include <cctype>
#include <cstdlib>

#pragma comment(lib, "shell32.lib")

namespace {

// key(예: RAG_HTTP_PORT) 뒤에 오는 값을 원문 그대로 뽑아낸다. 값이
// "..."로 감싸져 있으면 따옴표를 벗기고, 아니면(정수 등) 다음 구분자
// (콤마/중괄호/공백/개행)까지를 그대로 반환한다. 이 3개 키는 값 안에
// 이스케이프된 따옴표가 나타나지 않는다고 가정한다(호스트명/포트/초
// 단위 정수뿐이므로) — 일반 JSON 문자열 전체를 다루는 파서가 아님.
bool FindJsonRawValue(const std::string& json, const std::string& key, std::string& outValue)
{
    std::string needle = "\"" + key + "\"";
    size_t pos = json.find(needle);
    if (pos == std::string::npos) return false;

    pos += needle.size();
    size_t colon = json.find(':', pos);
    if (colon == std::string::npos) return false;

    size_t valueStart = colon + 1;
    while (valueStart < json.size() && isspace((unsigned char)json[valueStart])) valueStart++;
    if (valueStart >= json.size()) return false;

    if (json[valueStart] == '"')
    {
        size_t strEnd = json.find('"', valueStart + 1);
        if (strEnd == std::string::npos) return false;
        outValue = json.substr(valueStart + 1, strEnd - valueStart - 1);
        return true;
    }

    size_t valueEnd = valueStart;
    while (valueEnd < json.size() &&
           json[valueEnd] != ',' && json[valueEnd] != '}' && json[valueEnd] != '\r' &&
           json[valueEnd] != '\n' && !isspace((unsigned char)json[valueEnd]))
    {
        valueEnd++;
    }
    outValue = json.substr(valueStart, valueEnd - valueStart);
    return true;
}

std::wstring Utf8ToWide(const std::string& s)
{
    if (s.empty()) return L"";
    int len = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
    if (len <= 0) return L"";
    std::wstring w(len, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), &w[0], len);
    return w;
}

std::wstring GetSettingsJsonPath()
{
    wchar_t path[MAX_PATH];
    if (SUCCEEDED(SHGetFolderPathW(nullptr, CSIDL_APPDATA, nullptr, 0, path)))
    {
        return std::wstring(path) + L"\\NotebookRAG\\settings.json";
    }
    return L"";
}

} // namespace

Settings CSettingsReader::Load()
{
    Settings result; // 파일이 없거나 파싱 실패해도 기본값(127.0.0.1/8420/10초)으로 안전하게 동작

    std::wstring path = GetSettingsJsonPath();
    if (path.empty()) return result;

    std::ifstream file(path, std::ios::binary);
    if (!file) return result;

    std::ostringstream ss;
    ss << file.rdbuf();
    std::string json = ss.str();

    std::string value;
    if (FindJsonRawValue(json, "RAG_HTTP_HOST", value) && !value.empty())
        result.host = Utf8ToWide(value);
    if (FindJsonRawValue(json, "RAG_HTTP_PORT", value) && !value.empty())
        result.port = _wtoi(Utf8ToWide(value).c_str());

    return result;
}
