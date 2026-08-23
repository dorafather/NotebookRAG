#include "RulesEditDialog.h"

CRulesEditDialog::CRulesEditDialog(const std::wstring& folder,
                                   const std::vector<std::wstring>& patterns,
                                   CWnd* pParent)
    : CDialog(IDD, pParent)
    , m_folder(folder)
{
    std::wstring text;
    for (const auto& p : patterns)
    {
        text += p;
        text += L"\r\n";
    }
    m_initialText = text;
}

void CRulesEditDialog::DoDataExchange(CDataExchange* pDX)
{
    CDialog::DoDataExchange(pDX);
    DDX_Control(pDX, IDC_EDIT_RULES_TEXT, m_editText);
}

BEGIN_MESSAGE_MAP(CRulesEditDialog, CDialog)
END_MESSAGE_MAP()

BOOL CRulesEditDialog::OnInitDialog()
{
    CDialog::OnInitDialog();

    std::wstring title = L"규칙 편집 — " + m_folder;
    SetWindowTextW(title.c_str());
    m_editText.SetWindowTextW(m_initialText.c_str());
    GetDlgItem(IDOK)->SetWindowTextW(L"저장");
    GetDlgItem(IDCANCEL)->SetWindowTextW(L"취소");

    return TRUE;
}

void CRulesEditDialog::OnOK()
{
    CString text;
    m_editText.GetWindowTextW(text);

    m_resultPatterns.clear();
    int start = 0;
    while (start <= text.GetLength())
    {
        int nl = text.Find(L'\n', start);
        CString line = (nl == -1) ? text.Mid(start) : text.Mid(start, nl - start);
        line.TrimLeft(L" \t\r");
        line.TrimRight(L" \t\r");
        if (!line.IsEmpty())
        {
            m_resultPatterns.push_back(std::wstring(line));
        }
        if (nl == -1) break;
        start = nl + 1;
    }

    CDialog::OnOK();
}
