#include "ApiClient.h"
#include <windows.h>
#include <winhttp.h>
#include <vector>
#include <cctype>

#pragma comment(lib, "winhttp.lib")

std::string WideToUtf8(const std::wstring& s)
{
    if (s.empty()) return "";
    int len = WideCharToMultiByte(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0, nullptr, nullptr);
    std::string out(len, '\0');
    WideCharToMultiByte(CP_UTF8, 0, s.c_str(), (int)s.size(), &out[0], len, nullptr, nullptr);
    return out;
}

std::wstring Utf8ToWideStr(const std::string& s)
{
    if (s.empty()) return L"";
    int len = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
    if (len <= 0) return L"";
    std::wstring w(len, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), &w[0], len);
    return w;
}

std::string JsonEscapeToUtf8(const std::wstring& s)
{
    std::string utf8 = WideToUtf8(s);
    std::string out;
    out.reserve(utf8.size() + 8);
    for (char c : utf8)
    {
        switch (c)
        {
        case '"':  out += "\\\""; break;
        case '\\': out += "\\\\"; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        case '\t': out += "\\t"; break;
        default:   out += c; break;
        }
    }
    return out;
}

std::wstring UrlEncodeUtf8(const std::wstring& s)
{
    std::string utf8 = WideToUtf8(s);
    std::wstring out;
    static const wchar_t* hex = L"0123456789ABCDEF";
    for (unsigned char c : utf8)
    {
        if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~')
        {
            out += (wchar_t)c;
        }
        else
        {
            out += L'%';
            out += hex[(c >> 4) & 0xF];
            out += hex[c & 0xF];
        }
    }
    return out;
}

CApiClient::CApiClient(const std::wstring& host, int port)
    : m_host(host), m_port(port)
{
}

ApiResult CApiClient::Get(const std::wstring& path) const
{
    return Request(L"GET", path, "");
}

ApiResult CApiClient::Post(const std::wstring& path, const std::string& jsonBodyUtf8) const
{
    return Request(L"POST", path, jsonBodyUtf8);
}

ApiResult CApiClient::Put(const std::wstring& path, const std::string& jsonBodyUtf8) const
{
    return Request(L"PUT", path, jsonBodyUtf8);
}

ApiResult CApiClient::Delete(const std::wstring& path, const std::string& jsonBodyUtf8) const
{
    return Request(L"DELETE", path, jsonBodyUtf8);
}

ApiResult CApiClient::Request(const std::wstring& method, const std::wstring& path,
                               const std::string& jsonBodyUtf8) const
{
    ApiResult result;

    HINTERNET hSession = WinHttpOpen(L"NotebookRAG-Tray/1.0",
        WINHTTP_ACCESS_TYPE_DEFAULT_PROXY, WINHTTP_NO_PROXY_NAME,
        WINHTTP_NO_PROXY_BYPASS, 0);
    if (!hSession) return result;
    WinHttpSetTimeouts(hSession, 3000, 3000, 3000, 8000);

    HINTERNET hConnect = WinHttpConnect(hSession, m_host.c_str(),
        static_cast<INTERNET_PORT>(m_port), 0);
    if (!hConnect)
    {
        WinHttpCloseHandle(hSession);
        return result;
    }

    HINTERNET hRequest = WinHttpOpenRequest(hConnect, method.c_str(), path.c_str(),
        nullptr, WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, 0);
    if (!hRequest)
    {
        WinHttpCloseHandle(hConnect);
        WinHttpCloseHandle(hSession);
        return result;
    }

    static const wchar_t* kJsonHeader = L"Content-Type: application/json";
    BOOL sent;
    if (!jsonBodyUtf8.empty())
    {
        sent = WinHttpSendRequest(hRequest, kJsonHeader, (DWORD)-1,
            (LPVOID)jsonBodyUtf8.data(), (DWORD)jsonBodyUtf8.size(),
            (DWORD)jsonBodyUtf8.size(), 0);
    }
    else
    {
        sent = WinHttpSendRequest(hRequest, WINHTTP_NO_ADDITIONAL_HEADERS, 0,
            WINHTTP_NO_REQUEST_DATA, 0, 0, 0);
    }

    if (sent && WinHttpReceiveResponse(hRequest, nullptr))
    {
        DWORD statusCode = 0, size = sizeof(statusCode);
        WinHttpQueryHeaders(hRequest, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
            WINHTTP_HEADER_NAME_BY_INDEX, &statusCode, &size, WINHTTP_NO_HEADER_INDEX);
        result.statusCode = (int)statusCode;

        std::string bodyUtf8;
        DWORD available = 0;
        while (WinHttpQueryDataAvailable(hRequest, &available) && available > 0)
        {
            std::vector<char> buf(available);
            DWORD read = 0;
            if (!WinHttpReadData(hRequest, buf.data(), available, &read) || read == 0) break;
            bodyUtf8.append(buf.data(), read);
        }
        if (!bodyUtf8.empty())
        {
            result.body = JsonValue::Parse(bodyUtf8);
        }
    }

    WinHttpCloseHandle(hRequest);
    WinHttpCloseHandle(hConnect);
    WinHttpCloseHandle(hSession);
    return result;
}
