#!/usr/bin/env python3
"""
rag_serve.py — rag-ra 검색 라우터 [티켓 C-0: notebookrag_main.py로 통합]

역할:
  로컬 HTTP(FastAPI)로 문서검색 요청을 받아 RAG 파이프라인
  (질의 임베딩 → sqlite-vec top-k → 임계값 관문 → Haiku 근거 답변)을 거쳐
  응답한다. POST /reindex 수신 시 docs/를 무중단 재색인한다.

  [티켓 C-0] 이 파일은 이제 `app = FastAPI()`를 직접 만들지 않고 `router =
  APIRouter()`만 노출한다 — 실제 서버 기동은 notebookrag_main.py가
  `app.include_router(router, prefix="/rag")`로 가져다 쓴다(indexer_serve.py의
  라우터와 같은 프로세스·같은 포트에 합치기 위함 — llama-cpp-python 등
  네이티브 의존성이 겹쳐서 exe로 각각 --onedir 빌드하면 중복 배포되는 걸
  피하려는 목적). `RagRA` 인스턴스는 `app.state.ra`에 보관하고 각 라우트가
  `request.app.state.ra`로 접근한다(app.state 경유 공유 — 통합/단독 실행
  양쪽에서 lifespan이 똑같이 채워주면 되므로).

  `python rag_serve.py`로 이 파일만 단독 실행하는 것도 계속 가능하다 —
  하단 `if __name__ == "__main__":`이 자체 FastAPI 앱을 만들어 router를
  얹는 방식(개발 중 한쪽만 빠르게 테스트할 때 씀).

  NotebookRAG는 프로세스가 소수(1~3개)이고 전부 한 컴퓨터 안에서만
  통신하므로 NATS 같은 다대다 브로커가 불필요해 로컬 HTTP로 통신한다.
  실제 RAG 로직(_answer_blocking, _retrieve_raw_blocking)은 통신 계층을
  전혀 모르는 순수 메서드다. 색인/청킹/임베딩 로직은 rag_indexing.py에서
  import(단일 진실 원천).

엔드포인트 (통합 실행 시 /rag 프리픽스 붙음, 단독 실행 시 프리픽스 없음):
  POST /search
    요청: {"query": "<검색어>"}
    응답 200: {"결과": [{"출처":..,"내용":..,"유사도":0.xx}, ...], "신뢰도충족": true}
    응답 200(문서 없음): {"결과": [], "신뢰도충족": false, "안내": "관련 문서를 찾지 못했습니다"}
    → _retrieve_raw_blocking() 그대로 호출. NotebookRAG의 기본/주력 경로
      (MCP 클라이언트가 이미 LLM이므로 API 키 불필요).

  POST /search/answer
    요청: {"query": "<검색어>"}
    응답 200: {"답변":"...", "출처":[{"파일형식":..,"파일명":..}, ...], "최고유사도":"0.xx"}
    응답 200(문서 없음): {"답변": null, "안내": "문서에서 해당 내용을 찾지 못했습니다"}
    → _answer_blocking() 그대로 호출. Haiku 필요 — ANTHROPIC_API_KEY 없으면
      이 엔드포인트 호출 시에만 에러(지연 로딩이라 다른 엔드포인트는 영향 없음).

  POST /reindex
    요청: 없음
    응답 200: {"status": "requested"}
    → [프로세스 분리] 색인 자식 프로세스에 강제 재색인 신호만 보내고 즉시
      응답한다(오래 걸릴 수 있어 동기 대기 안 함). 진행 상황은 /indexer/status.

  GET /health (프리픽스 없이 항상 루트 — notebookrag_main.py가 별도 등록)
    응답 200: {"status": "ok", "chunks": <int>}
    → 트레이 앱이 이 프로세스 생존 여부를 확인할 용도.

공통 규칙:
  - 문서를 못 찾은 경우("NoRelevantDoc")는 HTTP 200으로 응답한다 — 검색은
    정상 수행됐지만 결과가 없다는 비즈니스 결과이지 프로토콜 오류가 아니다.
    4xx/5xx는 요청 형식 오류(빈 query 등 → 400)나 서버 내부 예외(→ 500)에만.
  - [티켓 D] 임베딩 모델이 아직 준비 안 됨(최초 실행 시 자동 다운로드 중)은
    HTTP 503으로 응답한다 — 서버 크래시가 아니라 "잠시 후 다시 시도"류의
    정상적인 일시 상태다. GET /model/status(model_downloader.py)로 진행률
    확인 가능.
  - 서버는 127.0.0.1에만 바인딩한다(0.0.0.0 금지) — 로컬 전용 제품.

사용:
  .env 또는 settings.json → 환경변수: ANTHROPIC_API_KEY(선택, raw 전용
  배포에서는 불필요), RAG_EMBED_MODEL, RAG_DATA_DIR 등 (rag_indexing.py와 공유)
  통합 실행(권장): python notebookrag_main.py
  단독 실행(개발용): python rag_serve.py
  # 문서 갱신: curl -X POST http://127.0.0.1:8420/rag/reindex (통합) 또는
  #            curl -X POST http://127.0.0.1:8420/reindex (단독)
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel

from app_paths import load_settings_json, NOTEBOOKRAG_VERSION, GITHUB_URL

load_settings_json()  # load_dotenv()보다 먼저 호출 (우선순위: 환경변수 > .env > settings.json)
load_dotenv()

from rag_indexing import open_existing_index, embed, PREFIX_QUERY, ModelNotReadyError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-5s] [rag-ra] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("rag-ra")

RAG_HTTP_HOST = os.getenv("RAG_HTTP_HOST", "127.0.0.1")
RAG_HTTP_PORT = int(os.getenv("RAG_HTTP_PORT", "8420"))
TOP_K         = int(os.getenv("RAG_TOP_K", "4"))
SIM_THRESHOLD = float(os.getenv("RAG_SIM_THRESHOLD", "0.45"))
MAX_SOURCES   = 3

ANSWER_PROMPT = """당신은 사내 문서 질의응답 챗봇입니다.
아래 [근거 문서] 조각들만 사용해 질문에 답하세요.

규칙:
- 카카오톡 메시지용이므로 3문장 이내로 간결하게 답하세요.
- 근거에 없는 내용은 추측하지 말고 "문서에서 해당 내용을 찾지 못했습니다"라고 답하세요.
- 마크다운/인용번호 없이 평문으로 답하세요.

[근거 문서]
{context}

[질문]
{question}"""


class RagRA:
    def __init__(self):
        # [FIX] 서빙 기동은 "이미 만들어진 DB에서 검색만" 하면 되므로,
        # build_index()가 아니라 활성 DB를 그대로 여는 open_existing_index()를
        # 쓴다. 색인 갱신은 REINDEX 신호(on_reindex → build_index(True))로만.
        self.chunks, self.search = open_existing_index()
        # [FIX] ChatAnthropic을 __init__에서 즉시 생성하지 않고 지연 로딩한다.
        # MCP 전용 배포(NotebookRAG 등)는 ANTHROPIC_API_KEY 없이
        # retrieve_raw() 경로만으로 동작해야 한다.
        self._llm = None
        # [폴링및출처표시개선 — 2026-08-19] 검색 결과 출처를 라벨/상대경로
        # 대신 전체 절대경로로 보여주기 위한 매핑(label -> 절대경로 Path).
        # notebookrag_main.py의 주기적 새로고침 태스크가 색인 자식 프로세스의
        # GET /folders를 불러서 채운다 — 이 프로세스는 오늘 구조 개편으로
        # 색인 자식과 분리돼 rag_indexing.DIR_LABELS(자식이 스캔할 때만
        # 갱신)를 직접 쓸 수 없다. 갱신 전(막 기동 직후)에는 빈 채로 시작해서
        # _resolve_source()가 원본 라벨/상대경로를 그대로 반환한다(안전한
        # 성능 저하 — 매핑 실패해도 검색 자체는 깨지지 않음).
        self.source_label_map: dict[str, Path] = {}
        log.info("RAG 준비 완료: 조각 %d개, 임계값 %.2f, top-k %d",
                 len(self.chunks), SIM_THRESHOLD, TOP_K)

    def _resolve_source(self, source: str) -> str:
        """저장된 source(예: "지능망산출문서/파일.pdf")의 앞부분(라벨)을
        source_label_map으로 실제 폴더 절대경로로 바꿔치기한다. 매핑에 없는
        라벨(주기적 갱신 사이의 짧은 지연, 또는 아직 한 번도 갱신 안 됨)이면
        원본 문자열을 그대로 반환 — 이 표시 계층 변경 하나 때문에 검색
        자체가 실패해선 안 된다."""
        label, sep, sub = source.partition("/")
        root = self.source_label_map.get(label)
        if root is None:
            return source
        return str(root / sub) if sub else str(root)

    @property
    def llm(self):
        if self._llm is None:
            from langchain_anthropic import ChatAnthropic
            self._llm = ChatAnthropic(model="claude-haiku-4-5",
                                      temperature=0.0, max_tokens=300)
        return self._llm

    def _answer_blocking(self, query: str) -> tuple[str, dict]:
        qv = embed([PREFIX_QUERY + query])[0]
        idx, scores = self.search(qv, TOP_K)

        if len(scores) == 0 or float(scores[0]) < SIM_THRESHOLD:
            top = float(scores[0]) if len(scores) else 0.0
            log.info("검색 실패: top-1 유사도 %.3f < %.2f (질의: %s)",
                     top, SIM_THRESHOLD, query)
            return "NoRelevantDoc", {}

        hits = [(self.chunks[int(i)], float(s)) for i, s in zip(idx, scores)]
        context = "\n\n".join(
            f"[{n + 1}] (출처: {self._resolve_source(c['source'])})\n{c['text']}"
            for n, (c, _) in enumerate(hits))

        answer = self.llm.invoke(
            ANSWER_PROMPT.format(context=context, question=query)).content

        params = {"답변": answer.strip()}
        seen: list[str] = []
        for c, _ in hits:
            resolved = self._resolve_source(c["source"])
            if resolved not in seen:
                seen.append(resolved)
            if len(seen) == MAX_SOURCES:
                break
        params["출처"] = [
            {"파일형식": src.rsplit(".", 1)[-1] if "." in src else "",
             "파일명": src}
            for src in seen
        ]
        params["최고유사도"] = f"{float(scores[0]):.3f}"
        return "Success", params

    def _retrieve_raw_blocking(self, query: str, k: int = TOP_K) -> dict:
        """[MCP 원문 청크 반환] Haiku를 호출하지 않고 검색된 원문 청크
        (출처+텍스트+유사도)를 그대로 구조화해 반환한다. MCP 클라이언트
        (Claude Code 등)는 이미 LLM이므로 문장 조합은 호출자 몫으로 넘긴다."""
        qv = embed([PREFIX_QUERY + query])[0]
        idx, scores = self.search(qv, k)

        if len(scores) == 0 or float(scores[0]) < SIM_THRESHOLD:
            top = float(scores[0]) if len(scores) else 0.0
            log.info("[raw] 검색 실패: top-1 유사도 %.3f < %.2f (질의: %s)",
                     top, SIM_THRESHOLD, query)
            return {"결과": [], "신뢰도충족": False,
                    "안내": "관련 문서를 찾지 못했습니다"}

        results = [
            {"출처": self._resolve_source(self.chunks[int(i)]["source"]),
             "내용": self.chunks[int(i)]["text"],
             "유사도": round(float(s), 3)}
            for i, s in zip(idx, scores)
        ]
        log.info("[raw] 검색 성공: %d건 (top-1 유사도 %.3f, 질의: %s)",
                 len(results), float(scores[0]), query)
        return {"결과": results, "신뢰도충족": True}


router = APIRouter()


class QueryRequest(BaseModel):
    query: str


def _record_search(request: Request) -> None:
    """[상태정보확장] 마지막 검색 시각 + "오늘" 검색 횟수를 같이 기록한다.
    "오늘"은 이 프로세스가 도는 로컬 PC의 벽시계 날짜 기준(단일 사용자
    데스크톱 제품이라 UTC로 하면 KST 자정 근처에서 날짜가 어긋나 보임) —
    날짜가 바뀌면 카운터를 0부터 다시 센다."""
    request.app.state.last_search_at = datetime.now(timezone.utc).isoformat()
    today = datetime.now().date()
    if getattr(request.app.state, "search_count_date", None) != today:
        request.app.state.search_count_date = today
        request.app.state.search_count_today = 0
    request.app.state.search_count_today = getattr(request.app.state, "search_count_today", 0) + 1


@router.post("/search")
async def search(req: QueryRequest, request: Request):
    ra: RagRA = request.app.state.ra
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query가 비어 있습니다")
    # [티켓 G] 결과가 있든 없든(NoRelevantDoc 포함) 이 호출 자체가 MCP
    # 클라이언트 연동의 증거이므로, 실제 파이프라인 호출 직전에 기록한다.
    _record_search(request)
    try:
        return await asyncio.to_thread(ra._retrieve_raw_blocking, query)
    except ModelNotReadyError:
        raise HTTPException(status_code=503, detail="임베딩 모델 준비 중입니다 — 잠시 후 다시 시도하세요")
    except Exception as exc:
        log.error("RAG(raw) 처리 오류: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/search/answer")
async def search_answer(req: QueryRequest, request: Request):
    ra: RagRA = request.app.state.ra
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query가 비어 있습니다")
    _record_search(request)
    try:
        reason, params = await asyncio.to_thread(ra._answer_blocking, query)
    except ModelNotReadyError:
        raise HTTPException(status_code=503, detail="임베딩 모델 준비 중입니다 — 잠시 후 다시 시도하세요")
    except Exception as exc:
        log.error("RAG 처리 오류: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    if reason != "Success":
        return {"답변": None, "안내": "문서에서 해당 내용을 찾지 못했습니다"}
    return params


@router.post("/reindex")
async def reindex(request: Request):
    """[프로세스 분리 → 긴급 삭제안전장치] 색인이 별도 자식 프로세스에서
    돌아서, 이 API 프로세스가 직접 build_index()를 부를 수 없다 — 그
    프로세스의 자체 HTTP 서버(INDEXER_PROC_PORT)에 POST /force_reindex로
    신호만 보내고 즉시 반환한다(indexer_config.json 같은 파일 경쟁 상태를
    피하려고 폴더 설정과 같은 HTTP 채널로 통일). 큰 폴더는 강제 전체
    재색인이 몇십 분 걸릴 수 있어(실측) 동기 대기는 어차피 의미가 없었다
    — 진행 상황은 GET /indexer/status로 확인."""
    port = int(os.getenv("INDEXER_PROC_PORT", "8421"))
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"http://127.0.0.1:{port}/force_reindex")
    except (httpx.ConnectError, httpx.TimeoutException):
        raise HTTPException(status_code=503, detail="색인 워커가 응답하지 않습니다 — 잠시 후 다시 시도하세요")
    log.info("REINDEX 요청 수신 — 색인 워커에 강제 재색인 신호를 보냄")
    return {"status": "requested"}


async def health(request: Request):
    """[티켓 C-0] router에는 안 얹는다 — /health는 프리픽스 없이 항상 루트에
    있어야 하므로, 이 함수를 app.add_api_route("/health", health, ...)로
    직접 등록한다(아래 단독 실행용 main()과 notebookrag_main.py 양쪽에서
    똑같이 재사용)."""
    ra: RagRA = request.app.state.ra
    # [티켓 G] getattr 기본값으로 방어 — lifespan에서 초기화를 안 했어도
    # (예: 아직 이 필드를 모르는 다른 lifespan) 예외 없이 null로 답한다.
    last_search_at = getattr(request.app.state, "last_search_at", None)
    return {
        "status": "ok",
        "chunks": len(ra.chunks),
        "마지막검색": last_search_at,
        # [상태정보확장]
        "오늘검색횟수": getattr(request.app.state, "search_count_today", 0),
        "가동시작시각": getattr(request.app.state, "started_at", None),
        # [정보탭_버전관리] 하드코딩 금지 — app_paths.NOTEBOOKRAG_VERSION이
        # 유일한 진실 원천, 여기서는 그대로 참조만 한다.
        "버전": NOTEBOOKRAG_VERSION,
        "github": GITHUB_URL,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    """단독 실행(`python rag_serve.py`) 전용 lifespan — 통합 실행 때는
    notebookrag_main.py의 lifespan이 이 자리를 대신한다(같은 방식으로
    app.state.ra를 채움)."""
    app.state.ra = RagRA()
    app.state.last_search_at = None
    app.state.started_at = datetime.now(timezone.utc).isoformat()
    log.info("rag-ra 서빙 시작(단독 실행): http://%s:%d", RAG_HTTP_HOST, RAG_HTTP_PORT)
    yield


def main():
    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    app.add_api_route("/health", health, methods=["GET"])
    uvicorn.run(app, host=RAG_HTTP_HOST, port=RAG_HTTP_PORT)


if __name__ == "__main__":
    main()
