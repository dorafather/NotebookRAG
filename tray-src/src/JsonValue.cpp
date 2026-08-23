#include "JsonValue.h"
#include <windows.h>
#include <cctype>
#include <cstdlib>

namespace {

std::wstring Utf8ToWide(const std::string& s)
{
    if (s.empty()) return L"";
    int len = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
    if (len <= 0) return L"";
    std::wstring w(len, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), &w[0], len);
    return w;
}

} // namespace

JsonValue JsonValue::Parse(const std::string& text)
{
    size_t pos = 0;
    return ParseValue(text, pos);
}

void JsonValue::SkipWhitespace(const std::string& text, size_t& pos)
{
    while (pos < text.size() && isspace((unsigned char)text[pos])) pos++;
}

JsonValue JsonValue::ParseValue(const std::string& text, size_t& pos)
{
    SkipWhitespace(text, pos);
    if (pos >= text.size()) return JsonValue();

    char c = text[pos];
    if (c == '{') return ParseObject(text, pos);
    if (c == '[') return ParseArray(text, pos);
    if (c == '"') return ParseStringValue(text, pos);
    if (c == 't' || c == 'f') return ParseBoolValue(text, pos);
    if (c == 'n') return ParseNullValue(text, pos);
    return ParseNumberValue(text, pos);
}

JsonValue JsonValue::ParseObject(const std::string& text, size_t& pos)
{
    JsonValue v;
    v.m_type = Type::Object;
    pos++; // '{'
    SkipWhitespace(text, pos);
    if (pos < text.size() && text[pos] == '}') { pos++; return v; }

    while (pos < text.size())
    {
        SkipWhitespace(text, pos);
        JsonValue key = ParseStringValue(text, pos);
        SkipWhitespace(text, pos);
        if (pos < text.size() && text[pos] == ':') pos++;
        JsonValue val = ParseValue(text, pos);
        v.m_object.emplace_back(key.m_stringValue, val);
        SkipWhitespace(text, pos);
        if (pos < text.size() && text[pos] == ',') { pos++; continue; }
        if (pos < text.size() && text[pos] == '}') { pos++; break; }
        break; // 형식이 깨졌으면 더 진행하지 않고 지금까지 파싱한 것만 반환
    }
    return v;
}

JsonValue JsonValue::ParseArray(const std::string& text, size_t& pos)
{
    JsonValue v;
    v.m_type = Type::Array;
    pos++; // '['
    SkipWhitespace(text, pos);
    if (pos < text.size() && text[pos] == ']') { pos++; return v; }

    while (pos < text.size())
    {
        JsonValue val = ParseValue(text, pos);
        v.m_array.push_back(val);
        SkipWhitespace(text, pos);
        if (pos < text.size() && text[pos] == ',') { pos++; continue; }
        if (pos < text.size() && text[pos] == ']') { pos++; break; }
        break;
    }
    return v;
}

JsonValue JsonValue::ParseStringValue(const std::string& text, size_t& pos)
{
    JsonValue v;
    v.m_type = Type::String;
    if (pos >= text.size() || text[pos] != '"') return v;
    pos++; // 여는 "

    std::string out;
    while (pos < text.size() && text[pos] != '"')
    {
        char c = text[pos];
        if (c == '\\' && pos + 1 < text.size())
        {
            char next = text[pos + 1];
            switch (next)
            {
            case '"':  out += '"';  pos += 2; break;
            case '\\': out += '\\'; pos += 2; break;
            case '/':  out += '/';  pos += 2; break;
            case 'n':  out += '\n'; pos += 2; break;
            case 't':  out += '\t'; pos += 2; break;
            case 'r':  out += '\r'; pos += 2; break;
            case 'b':  out += '\b'; pos += 2; break;
            case 'f':  out += '\f'; pos += 2; break;
            case 'u':
                // \uXXXX — 이 프로젝트 응답은 대부분 원문 한글이 그대로 UTF-8
                // 바이트로 오므로 흔치 않지만, 나오면 코드포인트를 UTF-8로
                // 인코딩해 넣는다(서로게이트 쌍은 처리 안 함 — 이모지 등
                // 필요한 응답이 없음).
                if (pos + 6 <= text.size())
                {
                    std::string hex = text.substr(pos + 2, 4);
                    unsigned int cp = (unsigned int)strtoul(hex.c_str(), nullptr, 16);
                    if (cp < 0x80)
                    {
                        out += (char)cp;
                    }
                    else if (cp < 0x800)
                    {
                        out += (char)(0xC0 | (cp >> 6));
                        out += (char)(0x80 | (cp & 0x3F));
                    }
                    else
                    {
                        out += (char)(0xE0 | (cp >> 12));
                        out += (char)(0x80 | ((cp >> 6) & 0x3F));
                        out += (char)(0x80 | (cp & 0x3F));
                    }
                    pos += 6;
                }
                else
                {
                    pos += 2;
                }
                break;
            default:
                out += next;
                pos += 2;
                break;
            }
        }
        else
        {
            out += c;
            pos++;
        }
    }
    if (pos < text.size()) pos++; // 닫는 "
    v.m_stringValue = out;
    return v;
}

JsonValue JsonValue::ParseBoolValue(const std::string& text, size_t& pos)
{
    JsonValue v;
    v.m_type = Type::Bool;
    if (text.compare(pos, 4, "true") == 0) { v.m_boolValue = true; pos += 4; }
    else if (text.compare(pos, 5, "false") == 0) { v.m_boolValue = false; pos += 5; }
    return v;
}

JsonValue JsonValue::ParseNullValue(const std::string& text, size_t& pos)
{
    JsonValue v;
    v.m_type = Type::Null;
    if (text.compare(pos, 4, "null") == 0) pos += 4;
    return v;
}

JsonValue JsonValue::ParseNumberValue(const std::string& text, size_t& pos)
{
    JsonValue v;
    v.m_type = Type::Number;
    size_t start = pos;
    while (pos < text.size() &&
           (isdigit((unsigned char)text[pos]) || text[pos] == '-' || text[pos] == '+' ||
            text[pos] == '.' || text[pos] == 'e' || text[pos] == 'E'))
    {
        pos++;
    }
    std::string numStr = text.substr(start, pos - start);
    v.m_numberValue = numStr.empty() ? 0.0 : atof(numStr.c_str());
    return v;
}

bool JsonValue::AsBool(bool def) const
{
    return m_type == Type::Bool ? m_boolValue : def;
}

double JsonValue::AsNumber(double def) const
{
    return m_type == Type::Number ? m_numberValue : def;
}

std::wstring JsonValue::AsString(const std::wstring& def) const
{
    return m_type == Type::String ? Utf8ToWide(m_stringValue) : def;
}

const JsonValue* JsonValue::Find(const std::string& key) const
{
    if (m_type != Type::Object) return nullptr;
    for (const auto& kv : m_object)
    {
        if (kv.first == key) return &kv.second;
    }
    return nullptr;
}
