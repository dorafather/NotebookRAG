# NotebookRAG — 배포 준비 폴더

![NotebookRAG](assets/NotebookRAG.jpg)

**작성일**: 2026-08-18
**목적**: 개발 원본(`C:\changwoon\개인자료\DSL\pm2-list\...`)은 그대로 두고,
InstallShield 등으로 패키징할 "배포용 레이아웃"을 별도로 구성한 폴더입니다.
개발 중인 원본 파일은 이 작업으로 전혀 수정되지 않았습니다.

## 폴더 구조

```
NotebookRAG-release/
  ├─ assets/                 로고/아이콘 원본
  │   ├─ NotebookRAG.jpg      워드마크 포함 — README/문서/발표자료용
  │   └─ NotebookRAG.png      아이콘만(원형 배지) — exe/트레이 아이콘 원본
  │                           (추후 .ico 변환 시 이 파일 기준)
  ├─ src/                    파이썬 소스 (정리된 사본, 개발 원본과 파일명 다름 — 아래 참고)
  │   ├─ rag_indexing.py       색인 로직 (원본: rag_demo.py) + RAG_DATA_DIR 추가
  │   ├─ rag_serve.py          서빙 로직 (원본과 동일 파일명, import만 rag_indexing으로 수정)
  │   └─ mcp_rag.py            MCP 브릿지 (원본: mcp_bridge_config.py) + 주의사항 보강
  ├─ config/                 설정 템플릿 (비밀값 없음)
  │   ├─ mcp_tools.json        doc_search만 포함 (recommend_for_me 등 SLEE 고유
  │   │                        비즈니스 로직은 의도적으로 제외 — 아래 참고)
  │   └─ settings.json.template   .env 대체 템플릿, 모든 값 비어있음
  ├─ python/                 (빈 폴더) 파이썬 런타임 동봉 위치
  ├─ bin/                    (빈 폴더) 실행파일/런처 위치
  ├─ models/                 (빈 폴더) bge-m3 GGUF 파일 위치
  └─ README_RELEASE.md       이 파일
```

## 원본과 다른 점 (의도적 변경)

1. **파일명 변경** (개발 원본 → 배포판)
   - `rag_demo.py` → **`rag_indexing.py`** — "demo"는 프로토타입 시절 이름.
     "색인(indexing)" 작업을 수행하는 모듈이라는 걸 명확히 하기 위해
     `rag_train.py`(오해 소지 있음 — ML 학습이 아님) → `rag_index.py`(명사형)
     검토를 거쳐 최종적으로 동명사형인 `rag_indexing.py`로 확정.
   - `mcp_bridge_config.py` → **`mcp_rag.py`** — 이 배포판은 RAG 전용
     브릿지이므로(recommend_for_me 등 다른 RA 도구 없음) 실제 역할과 정확히
     일치하는 이름으로 변경.
   - `rag_serve.py` — 이름 유지, 다만 `rag_indexing`을 import하도록 내부 수정.

2. **`rag_indexing.py`에 `RAG_DATA_DIR` 환경변수 추가**
   원본은 `rag_index.db`/`rag_index.npz`를 소스 파일과 같은 폴더에 저장했습니다.
   배포판은 "설치 폴더"(코드/설정)와 "사용자 데이터"(색인 DB)를 분리해야
   하므로(Windows 관례 — `%APPDATA%` 등), 이 경로를 환경변수로 뺐습니다.
   미설정 시 원본과 동일하게 동작(하위 호환).

3. **`mcp_tools.json`에서 `recommend_for_me` 도구 제외**
   SLEE의 통신사 상품 추천 엔진(MSISDN 기반)은 NotebookRAG(개인 문서 검색
   제품)와 무관한 회사 고유 비즈니스 로직입니다. `doc_search`만 남겼습니다.

4. **`settings.json.template` 신설** — `.env`를 대체합니다. 모든 값이
   비어 있고, 실제 값은 사용자가 최초 실행 시(트레이 앱 설정 화면 등을 통해)
   입력해 `%APPDATA%\NotebookRAG\settings.json`에 저장하는 방식을 가정합니다.

5. **`assets/` 로고 추가** — 마스코트(얼룩말) 로고. `.jpg`(워드마크 포함)는
   문서/발표용, `.png`(아이콘만)는 추후 `.ico` 변환의 원본으로 사용.

## 의도적으로 포함하지 않은 것 (원본 폴더에는 있지만 여기 없음)

- **`.env`(실제 API 키, 개발자 개인 값)** — 대신 빈 값의 템플릿만 포함
- **`rag_index.db`, `rag_index.npz`** — 개발자 개인 문서로 색인된 실데이터.
  사용자는 자기 문서로 처음부터 새로 색인해야 합니다.
- **`docs/`** — 원본 문서 사본이 들어있을 수 있어 제외
- **`test_*.py`, `*.bak-*`, `__pycache__`, `_e2e_scratch*`** — 개발용 산물

## 아직 안 된 것 (다음 단계)

- [ ] `rag_serve.py`의 NATS 계층 → HTTP(FastAPI)로 교체
- [ ] `mcp_rag.py`에 `transport: http` 분기 추가
- [ ] 색인(백그라운드)과 서빙(HTTP)을 하나의 프로세스로 합치기 (`notebookrag_main.py` 신설)
- [ ] 트레이 아이콘 앱(`tray.exe`) 설계 — SLEE.exe(00. daemon)의 Job Object
      패턴 참고. 트레이 아이콘은 `assets/NotebookRAG.png` 기준으로 제작
- [ ] `assets/NotebookRAG.png` → `.ico`(다중 해상도) 변환, 트레이/exe 아이콘 적용
- [ ] PyInstaller로 `llama-cpp-python` 단일 exe화 가능한지 시험
      (실패 시 `python/` 폴더에 런타임 동봉하는 현재 계획대로 진행)
- [ ] bge-m3 GGUF 파일의 배포 라이선스 조건 확인
- [ ] InstallShield 스크립트 작성 (이 폴더를 그대로 소스로 사용 가능하도록
      레이아웃이 이미 설계되어 있음)
