#pragma once
#include <afxwin.h>
#include <string>
#include <vector>
#include "resource.h"

// [티켓 G] .ragignore 패턴을 여러 줄 텍스트로 편집하는 작은 모달 다이얼로그.
// 리스트/그리드가 아니라 텍스트박스 하나를 고른 이유: .ragignore 자체가
// "한 줄에 패턴 하나, #으로 주석" 형식의 평문 파일이라(rag_indexing.py의
// load_ignore_patterns()), 그 파일 내용을 거의 그대로 보여주고 그대로
// 저장하면 되는 구조라 리스트 컨트롤로 줄 단위 추가/삭제 UI를 새로 만드는
// 것보다 훨씬 적은 코드로 같은 결과를 낸다. 사용자도 이미 .ragignore를
// 텍스트 파일로 다뤄본 전제(지침서 예시)와도 자연스럽게 맞는다.
class CRulesEditDialog : public CDialog
{
public:
    explicit CRulesEditDialog(const std::wstring& folder,
                              const std::vector<std::wstring>& patterns,
                              CWnd* pParent = nullptr);

    enum { IDD = IDD_RULES_DIALOG };

    // OK를 눌러 닫혔으면 편집된 패턴 목록(빈 줄 제거)을 반환한다.
    const std::vector<std::wstring>& GetPatterns() const { return m_resultPatterns; }

protected:
    virtual BOOL OnInitDialog();
    virtual void DoDataExchange(CDataExchange* pDX);
    virtual void OnOK();
    DECLARE_MESSAGE_MAP()

private:
    std::wstring m_folder;
    std::wstring m_initialText;
    std::vector<std::wstring> m_resultPatterns;
    CEdit m_editText;
};
