# 업무지침서 — mcp_rag.py: HTTP transport 지원 추가 (rag_serve.py 신규 엔드포인트 연결)

**작성일**: 2026-08-18
**대상**: dsl-process-developer 서브에이전트
**전제조건**: `rag_serve.py`의 NATS→HTTP 전환 티켓 완료 상태 (`127.0.0.1:8420`
에서 `/search`, `/search/answer`, `/reindex`, `/health` 라우트 서빙 중).
**범위**: `mcp_rag.py`와 `mcp_tools.json`만. `rag_serve.py`/`rag_indexing.py`
무수정.

---

## 배경 (지금 무엇이 끊겨 있는가)

`rag_serve.py`가 HTTP로 전환되면서, `mcp_rag.py`의 `doc_search` 도구는
지금 **존재하지 않는 NATS 서버**에 요청을 보내려다 실패하는 상태다(예상된
과도기 — 이전 티켓 보고서에 명시됨). 이번 티켓에서 `mcp_rag.py`가 새
HTTP 엔드포인트를 직접 호출하도록 연결한다.

---

## 설계 결정 (확정 — 아래 그대로 구현)

### 1. `mcp_tools.json`에 `transport` 필드 추가, `doc_search`는 http로 전환

```json
{
  "name": "doc_search",
  "transport": "http",
  "path": "/search",
  "description": "...",
  "params": {
    "query": {
      "type": "string",
      "description": "검색할 질문. 자연어 그대로 전달",
      "required": true
    }
  },
  "response_format": "raw_chunks"
}
```

**바뀌는 것들과 이유:**

- **`format` 파라미터 항목을 완전히 삭제한다.** 기존엔 `PARAMS.형식=raw`로
  같은 NATS 토픽 안에서 카카오 경로와 MCP 경로를 나눴는데, 이제 MCP 경로는
  `/search`라는 **전용 엔드포인트**를 쓰므로 이 플래그 자체가 의미가 없다.
  (`/search/answer`는 이번 티켓에서 어떤 도구도 연결하지 않는다 — MCP
  클라이언트는 이미 LLM이므로 raw만 필요하다는 게 원래 설계 의도였다.)
- **`query`의 `dsl_key`를 없앤다** (또는 `"query"`로 동일하게 둔다). `dsl_key`
  분리는 "ASCII 도구 스키마 키 ↔ 한글 NATS PARAMS 필드"를 잇던 장치였는데,
  HTTP 요청 바디는 우리가 직접 설계하므로 애초에 영어 키(`query`)를 쓰면
  되고 그 변환이 불필요해진다. **단, `nats` transport로 남는 도구가 생기면
  그쪽은 여전히 `dsl_key`가 필요하니, `call_tool()`의 `dsl_key` 처리
  로직 자체는 지우지 말고 유지할 것** (nats 경로 하위호환).
- **`topic`/`action_event`는 http 도구에 더 이상 불필요.** 남겨두든 지우든
  상관없지만, 혼동 방지를 위해 지우는 것을 권장.
- `result_field`/`reason_field`도 http 도구에는 적용되지 않는다(아래
  4번 참고). nats 도구를 위해 코드에는 남겨두되, http 도구 항목에서는
  빼도 된다.

### 2. `mcp_rag.py`에 `RAG_HTTP_HOST`/`RAG_HTTP_PORT` 환경변수 추가

`rag_serve.py`와 **정확히 같은 이름·기본값**(`127.0.0.1`/`8420`)을 읽는다.
⚠️ 이 두 파일은 이 환경변수 이름으로 암묵적 계약을 맺는다 — 한쪽만 바꾸면
깨진다는 점을 코드 주석에 명시할 것.

`mcp_tools.json`의 도구 항목에는 전체 URL이 아니라 **경로만**(`"path": "/search"`)
적도록 하고, `mcp_rag.py`가 `f"http://{RAG_HTTP_HOST}:{RAG_HTTP_PORT}{path}"`로
조립한다 (호스트/포트를 JSON에 하드코딩하지 않아야 두 파일의 설정이 어긋날
여지가 줄어든다).

### 3. `http_request()` 함수 신설

```python
async def http_request(path: str, params: dict) -> tuple[int, dict]:
    """반환: (HTTP 상태코드, 파싱된 JSON 바디). 네트워크 오류 시 (0, {"_error": ...})."""
```

- HTTP 클라이언트는 비동기 아키텍처와 맞는 걸 선택할 것(예: `httpx`의
  `AsyncClient`). 기존에 없던 의존성이면 설치하고 보고서에 명시.
- 연결 실패(서버 미기동 등)를 예외로 잡아 사용자에게 "처리 실패: ..."류
  메시지로 보이게 한다 — 예외가 새어나가 MCP 서버 자체가 죽으면 안 됨.

### 4. `call_tool()`에 transport 분기 추가

```python
transport = t.get("transport", "nats")  # 미지정 시 기존 동작(nats) 유지
if transport == "http":
    status, p = await http_request(t["path"], call_params)  # 변수명도 nats_params → call_params로 (아래 참고)
    if status != 200:
        return [types.TextContent(type="text", text=f"처리 실패: {p.get('_error', status)}")]
    # result_field/reason_field 체크 없이 p를 그대로 포맷터에 넘긴다 —
    # "결과 없음"류 판단은 포맷터가 p 안의 신뢰도충족/안내 필드로 직접 처리
else:
    p = await nats_request(t["topic"], t["action_event"], call_params)
    if "_error" in p:
        return [types.TextContent(type="text", text=p["_error"])]
    result_ok = p.get(t["result_field"]) == "0"
    ...  # 기존 로직 그대로
```

**변수명 변경**: 기존 `nats_params`는 이제 두 transport에 공통으로 쓰이므로
`call_params`(또는 다른 transport-중립적 이름)로 리네이밍한다.

**`nats_request()`/`nats` import는 삭제하지 말 것** — 앞으로 nats-transport
도구가 다시 필요해질 가능성에 대비해 하위호환으로 유지한다 (파일 상단
docstring에 이미 "http 필드를 추가해 분기"라고 적혀 있던 원래 계획과 일치).

### 5. `fmt_raw_chunks()` 개선

지금은 `신뢰도충족`이 `false`여도 그냥 "검색 결과 0건"으로만 나온다.
`안내` 필드(예: "관련 문서를 찾지 못했습니다")가 있으면 그걸 우선
보여주도록 개선한다:

```python
def fmt_raw_chunks(p: dict, args: dict) -> str:
    if not p.get("신뢰도충족", True) and p.get("안내"):
        return p["안내"]
    results = p.get("결과", [])
    ...  # 기존 로직
```

---

## 범위 밖 (이번 티켓에서 하지 않음)

- `rag_serve.py`, `rag_indexing.py` 무수정
- `/search/answer` 엔드포인트를 쓰는 새 MCP 도구 추가 없음
- nats-transport 코드 제거 없음 (하위호환 유지)
- 트레이 앱/PyInstaller/InstallShield 관련 작업 없음

---

## 검증

1. `rag_serve.py`가 `127.0.0.1:8420`에서 떠 있는 상태로 (NATS 서버는
   **기동하지 않은 채**) MCP 서버(`mcp_rag.py`)를 등록하고, 실제 Claude
   Code 세션에서 `doc_search`를 호출해 색인된 테스트 문서에서 원문 청크가
   정상 반환되는지 end-to-end 확인. (NATS가 더 이상 이 경로에 필요 없다는
   걸 실측으로 증명하는 것이 이번 검증의 핵심.)
2. 문서 없음(유사도 미달) 케이스를 실제 MCP 호출로 재현해, "검색 결과
   0건" 대신 개선된 안내 메시지가 나오는지 확인.
3. `rag_serve.py`를 **일부러 중단**한 상태에서 `doc_search`를 호출해,
   MCP 서버가 죽지 않고 "처리 실패: ..."류 에러 텍스트를 정상적으로
   반환하는지 확인.
4. `mcp_tools.json`이 여전히 유효한 스키마인지(ASCII 파라미터 키 규칙 등)
   확인 — 이전에 한글 키 실수로 API 400 에러가 난 적이 있었던 부분이니
   재확인할 것.
5. nats-transport 코드 경로(함수 자체)가 삭제되지 않고 그대로 남아있는지
   코드 리뷰로 확인 (실행 검증은 nats 도구가 현재 없으므로 불필요).

## 산출물 형식 (보고서에 포함)

1. 코드 변경 diff 요약 (`mcp_tools.json`, `mcp_rag.py`)
2. 검증 1~5 각각의 결과
3. 신규 의존성(예: `httpx`) 추가 여부와 설치 특이사항
4. `RAG_HTTP_HOST`/`RAG_HTTP_PORT` 환경변수가 `rag_serve.py`와 정확히
   같은 이름·기본값으로 맞춰졌는지 최종 확인
