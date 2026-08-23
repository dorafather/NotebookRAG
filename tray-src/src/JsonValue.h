#pragma once
#include <string>
#include <vector>
#include <utility>

// [티켓 G] 최소 JSON 파서 — notebookrag_main.py의 REST 응답(중첩 객체/배열,
// 문자열/숫자/불/null)을 읽기 위한 범용 구현. 표준 JSON 문법만 참고했고
// 특정 코드베이스를 보거나 베낀 게 없다(클린룸, 티켓 F의 ProcessManager와
// 같은 원칙).
class JsonValue
{
public:
    enum class Type { Null, Bool, Number, String, Object, Array };

    JsonValue() : m_type(Type::Null) {}

    static JsonValue Parse(const std::string& text);

    Type GetType() const { return m_type; }
    bool IsNull() const { return m_type == Type::Null; }

    bool AsBool(bool def = false) const;
    double AsNumber(double def = 0.0) const;
    std::wstring AsString(const std::wstring& def = L"") const;

    // Object 전용 — 키가 없거나 이 값이 Object가 아니면 nullptr.
    const JsonValue* Find(const std::string& key) const;

    // Array 전용.
    size_t ArraySize() const { return m_array.size(); }
    const JsonValue& ArrayAt(size_t i) const { return m_array[i]; }

private:
    Type m_type;
    bool m_boolValue = false;
    double m_numberValue = 0.0;
    std::string m_stringValue; // UTF-8 그대로 보관 — AsString()에서만 wide로 변환
    std::vector<std::pair<std::string, JsonValue>> m_object;
    std::vector<JsonValue> m_array;

    // 재귀 하강 파서 — private static이라 아무 JsonValue 인스턴스의 private
    // 멤버든 자유롭게 접근 가능(같은 클래스이므로). 별도 friend 클래스가
    // 필요 없다.
    static void SkipWhitespace(const std::string& text, size_t& pos);
    static JsonValue ParseValue(const std::string& text, size_t& pos);
    static JsonValue ParseObject(const std::string& text, size_t& pos);
    static JsonValue ParseArray(const std::string& text, size_t& pos);
    static JsonValue ParseStringValue(const std::string& text, size_t& pos);
    static JsonValue ParseBoolValue(const std::string& text, size_t& pos);
    static JsonValue ParseNullValue(const std::string& text, size_t& pos);
    static JsonValue ParseNumberValue(const std::string& text, size_t& pos);
};
