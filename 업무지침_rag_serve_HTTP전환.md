# 업무지침서 — rag_serve.py: NATS → HTTP(FastAPI) 전환

**작성일**: 2026-08-18
**대상**: dsl-process-developer 서브에이전트
**범위**: `rag_serve.py`의 통신 계층(NATS 봉투)만 HTTP로 교체. 그 외 파일/
로직은 이번 티켓에서 건드리지 않음 (아래 "범위 밖" 참고).

---

## 배경 (왜 필요한가)

NotebookRAG는 배포 시 3개 모듈로 구성될 계획이다:

1. 색인 (`rag_indexing.py` 기반, 백그라운드)
2. 서빙 (`rag_serve.py` 기반, 이번 작업 대상)
3. MCP 브릿지 (`mcp_rag.py`, Claude Code가 직접 spawn하는 독립 프로세스)

2와 3 사이의 통신을 지금은 NATS(브로커 서버 별도 실행 필요)로 하고 있는데,
NotebookRAG는 프로세스가 소수(1~3개)이고 전부 한 컴퓨터 안에서만 통신하므로,
NATS 같은 다대다 브로커가 불필요하다. 사용자가 설치해야 할 것을 하나
줄이기 위해 **로컬 HTTP**로 교체한다.

`_answer_blocking()`, `_retrieve_raw_blocking()` 같은 실제 RAG 로직은
NATS를 전혀 몰랐던 순수 메서드다 — 교체 대상은 `on_request()`/`run()`의
NATS 봉투(`nc.subscribe`, `msg.reply`, `SCE_EVENT`/`ACTION_EVENT` 등)뿐이다.

---

## API 설계 (확정 — 이대로 구현)

기존 "raw/answer 모드를 PARAMS.형식 필드로 구분"하던 방식 대신, REST답게
**엔드포인트를 분리**한다 (모드 플래그보다 명확함):

```
POST /search
  요청: {"query": "<검색어>"}
  응답 200: {"결과": [{"출처":.., "내용":.., "유사도":0.xx}, ...], "신뢰도충족": true}
  응답 200(문서 없음): {"결과": [], "신뢰도충족": false, "안내": "관련 문서를 찾지 못했습니다"}
  → _retrieve_raw_blocking() 그대로 호출. NotebookRAG의 기본/주력 경로
    (MCP 클라이언트가 이미 LLM이므로 API 키 불필요).

POST /search/answer
  요청: {"query": "<검색어>"}
  응답 200: {"답변":"...", "출처":[{"파일형식":..,"파일명":..}, ...], "최고유사도":"0.xx"}
  응답 200(문서 없음): {"답변": null, "안내": "문서에서 해당 내용을 찾지 못했습니다"}
  → _answer_blocking() 그대로 호출. Haiku 필요 — ANTHROPIC_API_KEY 없으면
    이 엔드포인트 호출 시에만 에러(다른 엔드포인트는 영향 없음, 이미
    지연 로딩되어 있으므로 자연히 그렇게 됨 — 확인 필요).

POST /reindex
  요청: 없음 (바디 불필요)
  응답 200: {"status": "ok", "chunks": <int>}
  → build_index(force=True)를 동기적으로 실행 후 완료 시 응답한다
    (기존 on_reindex()와 동일하게 완료까지 기다림 — 진행률 스트리밍 같은
    개선은 이번 범위 밖).

GET /health
  응답 200: {"status": "ok", "chunks": <int>}
  → 트레이 앱이 이 프로세스가 살아있는지 확인할 용도로 신규 추가.
```

**공통 규칙**:
- 문서를 못 찾은 경우("NoRelevantDoc")는 **HTTP 200**으로 응답한다 — 이건
  "검색은 정상 수행됐지만 결과가 없다"는 비즈니스 결과이지 프로토콜
  오류가 아니다. HTTP 4xx/5xx는 요청 형식 오류(빈 query 등 → 400)나
  서버 내부 예외(→ 500)에만 쓴다.
- 서버는 **`127.0.0.1`에만 바인딩**한다(`0.0.0.0` 금지) — 로컬 전용
  제품이라 외부 노출 자체가 보안 리스크다.
- 포트는 환경변수 `RAG_HTTP_PORT`(기본값 `8420`)로 설정 가능하게 한다.

---

## 구현 요구사항

1. `rag_serve.py`를 FastAPI 기반으로 재작성한다. `RagRA` 클래스(`__init__`,
   `llm` 프로퍼티, `_answer_blocking`, `_retrieve_raw_blocking`)는 **한 글자도
   수정하지 않고 그대로 재사용**한다 — `on_request`/`on_reindex`/`run`/
   `main`만 FastAPI 앱 구성으로 교체.
2. `nats` 패키지 의존성을 이 파일에서 제거한다(더 이상 import 불필요).
3. `NATS_SERVER`, `RAG_TOPIC`, `REINDEX_TOPIC` 환경변수는 이 파일에서 더
   이상 읽지 않는다. 대신 `RAG_HTTP_HOST`(기본 `127.0.0.1`),
   `RAG_HTTP_PORT`(기본 `8420`)를 추가한다.
4. `config/settings.json.template`에 `NATS_SERVER` 대신
   `RAG_HTTP_HOST`/`RAG_HTTP_PORT` 항목으로 교체하고, `_NATS_SERVER_설명`에
   있던 "[검토 중]" 문구를 제거한다(전환 완료됐으므로).
5. 필요한 패키지(`fastapi`, `uvicorn`)를 확인하고, `config/` 또는
   `src/`에 `requirements.txt`가 있으면 추가, 없으면 새로 만들지 여부를
   판단해 보고한다.

---

## 범위 밖 (이번 티켓에서 하지 않음 — 다음 티켓들)

- **`mcp_rag.py` 수정 금지.** 지금 `nats_request()`로 rag-ra를 호출하는
  코드는 그대로 둔다. 이 파일이 새 HTTP 엔드포인트를 호출하도록 바꾸는
  건 별도 후속 티켓(`mcp_tools.json`에 `transport: http` 필드 추가하는
  설계가 유력 — README_RELEASE.md 체크리스트 참고)이다. 이번 티켓 완료
  후에도 `mcp_rag.py`는 (아직 존재하지 않는) NATS 서버를 찾다가 실패할
  수 있는데, 이건 예상된 상태이며 문제가 아니다.
- **`rag_indexing.py`는 무수정.** 색인 로직/모듈 병합은 다음 티켓.
- **트레이 앱, PyInstaller, InstallShield 관련 작업 없음.**
- **진행률 스트리밍(`/reindex`의 비동기화, SSE 등) 없음** — 지금은 동기
  호출로 충분. 필요해지면 별도 티켓.

---

## 검증

1. 서버 기동 후 4개 엔드포인트(`/search`, `/search/answer`, `/reindex`,
   `/health`) 각각 수동 호출(curl 또는 `Invoke-RestMethod`)로 정상 응답
   확인.
2. `/search`(raw)는 `ANTHROPIC_API_KEY` 환경변수 없이도 정상 동작하는지
   별도 확인 (NotebookRAG의 핵심 약속 — API 키 없이 MCP 전용 사용 가능).
3. `/search/answer`는 API 키 없을 때 그 호출에서만 에러가 나고, 서버
   자체는 죽지 않는지 확인.
4. 문서 없음(유사도 미달) 케이스에서 두 검색 엔드포인트 모두 HTTP 200 +
   위에서 정의한 응답 형식을 반환하는지 확인.
5. `/reindex` 호출 후 실제로 색인이 갱신되고(`chunks` 개수 변화), 이후
   `/search` 결과에 반영되는지 확인 (A/B 세대교체가 여전히 정상 동작하는지
   — `rag_indexing.py`의 무중단 재색인 로직은 무수정이므로 그대로 동작
   해야 하나, 실제로 재확인 필요).
6. 이 파일에서 `nats`/`SCE_EVENT`/`ACTION_EVENT` 등 NATS 관련 코드가 전혀
   남아있지 않은지 grep으로 확인.

## 산출물 형식 (보고서에 포함)

1. 코드 변경 diff 요약 (`rag_serve.py`, `config/settings.json.template`,
   신규 `requirements.txt` 여부)
2. 검증 1~6 각각의 결과
3. 실제 사용한 포트/호스트 기본값이 문제없었는지, FastAPI/uvicorn 설치
   과정에서 특이사항(Windows 환경 이슈 등) 있었는지
