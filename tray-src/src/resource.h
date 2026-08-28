#pragma once

// [티켓 F/G] 리소스 ID 목록.

#define IDD_MAIN_DIALOG          101
#define IDI_TRAY_ICON            102
#define IDD_RULES_DIALOG         103

#define IDC_TAB_MAIN             1001
#define IDC_STATIC_PLACEHOLDER  1002   // [티켓 G] 더 이상 안 씀 — 실 컨트롤로 교체됨. 번호만 남겨둠(재사용 금지).

// [티켓 G] 탭 1 — 상태정보
#define IDC_STATUS_CONN         1010
#define IDC_STATUS_MODEL_LABEL  1011
#define IDC_STATUS_MODEL        1012
#define IDC_STATUS_MCP_LABEL    1013
#define IDC_STATUS_MCP          1014
#define IDC_STATUS_INDEX_LABEL  1015
#define IDC_STATUS_INDEX        1016
#define IDC_STATUS_DISK_LABEL   1017
#define IDC_STATUS_DISK         1018
#define IDC_BTN_WARNINGS        1019
#define IDC_BTN_PAUSE_RESUME    1020

// [상태정보확장] 감시 폴더 + 프로그레스바 2개
#define IDC_STATUS_FOLDERS_LABEL 1021
#define IDC_STATUS_FOLDERS      1022
#define IDC_STATUS_OVERALL_LABEL 1023
#define IDC_PROGRESS_OVERALL    1024
#define IDC_STATUS_OVERALL_PCT  1025
#define IDC_STATUS_FILE_LABEL   1026
#define IDC_PROGRESS_FILE       1027
#define IDC_STATUS_FILE_PCT     1028

// [티켓 G] 탭 2 — 설정
#define IDC_LIST_FOLDERS        1030
#define IDC_BTN_ADD_FOLDER      1031
#define IDC_BTN_REMOVE_FOLDER   1032
#define IDC_BTN_EDIT_RULES      1033
// [폴링고정및중복실행수정] 1034~1036(폴링 주기 라벨/입력칸/저장 버튼)은
// 설정에서 완전히 제거됨 — 번호만 남겨둠(재사용 금지).

// [티켓 H] 부팅 시 자동시작 (HKCU\...\Run)
#define IDC_CHECK_AUTOSTART     1037

// [티켓 G] 규칙 편집 다이얼로그(IDD_RULES_DIALOG)
#define IDC_EDIT_RULES_TEXT     1040

// [정보탭_버전관리] 탭 3 — 정보. 라이선스 필드는 아직 프로젝트 라이선스
// 자체가 미정(README.md TODO 참고)이라 이번엔 뺐다 — 1054/1055는 나중에
// 확정되면 쓸 수 있게 번호만 남겨둠(재사용 금지).
#define IDC_INFO_TITLE          1050
#define IDC_INFO_VERSION        1051
#define IDC_INFO_GITHUB_LABEL   1052
#define IDC_INFO_GITHUB         1053
#define IDC_INFO_PATH_LABEL     1056
#define IDC_INFO_PATH           1057

#define ID_TRAY_EXIT            40001
