#!/usr/bin/env python3
"""
rag_serve.py — rag-ra 서빙 프로세스 [배포판: 로컬 HTTP]

역할:
  로컬 HTTP(FastAPI)로 문서검색 요청을 받아 RAG 파이프라인
  (질의 임베딩 → sqlite-vec top-k → 임계값 관문 → Haiku 근거 답변)을 거쳐
  응답한다. POST /reindex 수신 시 docs/를 무중단 재색인한다.

  NotebookRAG는 프로세스가 소수(1~3개)이고 전부 한 컴퓨터 안에서만
  통신하므로 NATS 같은 다대다 브로커가 불필요해 로컬 HTTP로 통신한다.
  실제 RAG 로직(_answer_blocking, _retrieve_raw_blocking)은 통신 계층을
  전혀 모르는 순수 메서드다. 색인/청킹/임베딩 로직은 rag_indexing.py에서
  import(단일 진실 원천).

엔드포인트:
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
    응답 200: {"status": "ok", "chunks": <int>}
    → build_index(force=True)를 동기적으로 실행 후 완료 시 응답.

  GET /health
    응답 200: {"status": "ok", "chunks": <int>}
    → 트레이 앱이 이 프로세스 생존 여부를 확인할 용도.

공통 규칙:
  - 문서를 못 찾은 경우("NoRelevantDoc")는 HTTP 200으로 응답한다 — 검색은
    정상 수행됐지만 결과가 없다는 비즈니스 결과이지 프로토콜 오류가 아니다.
    4xx/5xx는 요청 형식 오류(빈 query 등 → 400)나 서버 내부 예외(→ 500)에만.
  - 서버는 127.0.0.1에만 바인딩한다(0.0.0.0 금지) — 로컬 전용 제품.

사용:
  .env 또는 settings.json → 환경변수: ANTHROPIC_API_KEY(선택, raw 전용
  배포에서는 불필요), RAG_EMBED_MODEL, RAG_DATA_DIR 등 (rag_indexing.py와 공유)
  pm2 start rag_serve.py --name rag-ra --interpreter python3
  # 문서 갱신: curl -X POST http://127.0.0.1:8420/reindex
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from rag_indexing import build_index, open_existing_index, embed, PREFIX_QUERY

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
        log.info("RAG 준비 완료: 조각 %d개, 임계값 %.2f, top-k %d",
                 len(self.chunks), SIM_THRESHOLD, TOP_K)

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
            f"[{n + 1}] (출처: {c['source']})\n{c['text']}"
            for n, (c, _) in enumerate(hits))

        answer = self.llm.invoke(
            ANSWER_PROMPT.format(context=context, question=query)).content

        params = {"답변": answer.strip()}
        seen: list[str] = []
        for c, _ in hits:
            if c["source"] not in seen:
                seen.append(c["source"])
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
            {"출처": self.chunks[int(i)]["source"],
             "내용": self.chunks[int(i)]["text"],
             "유사도": round(float(s), 3)}
            for i, s in zip(idx, scores)
        ]
        log.info("[raw] 검색 성공: %d건 (top-1 유사도 %.3f, 질의: %s)",
                 len(results), float(scores[0]), query)
        return {"결과": results, "신뢰도충족": True}


ra: RagRA


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ra
    ra = RagRA()
    log.info("rag-ra 서빙 시작: http://%s:%d", RAG_HTTP_HOST, RAG_HTTP_PORT)
    yield


app = FastAPI(lifespan=lifespan)


class QueryRequest(BaseModel):
    query: str


@app.post("/search")
async def search(req: QueryRequest):
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query가 비어 있습니다")
    try:
        return await asyncio.to_thread(ra._retrieve_raw_blocking, query)
    except Exception as exc:
        log.error("RAG(raw) 처리 오류: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/search/answer")
async def search_answer(req: QueryRequest):
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query가 비어 있습니다")
    try:
        reason, params = await asyncio.to_thread(ra._answer_blocking, query)
    except Exception as exc:
        log.error("RAG 처리 오류: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    if reason != "Success":
        return {"답변": None, "안내": "문서에서 해당 내용을 찾지 못했습니다"}
    return params


@app.post("/reindex")
async def reindex():
    global ra
    log.info("REINDEX 요청 수신 — docs/ 재색인 시작")
    try:
        chunks, search = await asyncio.to_thread(build_index, True)
        ra.chunks, ra.search = chunks, search
        log.info("REINDEX 완료: 조각 %d개", len(chunks))
        return {"status": "ok", "chunks": len(chunks)}
    except Exception as exc:
        log.error("REINDEX 실패 (기존 색인 유지): %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/health")
async def health():
    return {"status": "ok", "chunks": len(ra.chunks)}


def main():
    uvicorn.run(app, host=RAG_HTTP_HOST, port=RAG_HTTP_PORT)


if __name__ == "__main__":
    main()
