#!/usr/bin/env python3
"""
indexer_worker.py — [프로세스 분리] 색인(스캔/중복검사/임베딩) 전용 자식
프로세스. API 서빙 프로세스(notebookrag_main.py)와는 별도 GIL을 갖는
완전히 다른 프로세스에서 돈다(자세한 배경은 그 파일 상단 docstring 참고).

[긴급 삭제안전장치 — 2026-08-21] 이 프로세스는 이제 자체 HTTP 서버
(INDEXER_PROC_PORT)도 갖는다. 예전엔 감시 폴더 설정(docs_dirs)을
%APPDATA%\\NotebookRAG\\indexer_config.json 파일로 부모/자식이 주고받았는데,
"한쪽이 쓰는 도중 다른 쪽이 읽으면 불완전한 내용(빈 배열)을 읽는" 경쟁
상태가 있었고, 실제로 이 경로로 기존 색인 데이터(약 670MB, 134,704개
조각)가 통째로 삭제되는 사고가 났다. 원자적 쓰기(임시파일+rename) 패치로도
"쓰기와 읽기가 시간적으로 분리된 두 이벤트"라는 근본 구조는 안 없어지므로,
파일 기반 통신 자체를 없애고 이 프로젝트가 이미 쓰는 패턴(mcp-rag.exe/
tray.exe ↔ notebookrag.exe는 HTTP로 통신)을 색인 프로세스에도 그대로
적용한다 — HTTP 요청/응답은 본질적으로 원자적이라 "반쯤 쓰인 상태를 읽는"
경우 자체가 없다.

`indexer_config.json`에 남는 유일한 역할: 이 프로세스가 부팅 시 1회 초기값
으로 읽는 용도(재시작 시 마지막 폴더 목록 복원). 실시간 변경 전달에는 더
이상 쓰지 않는다 — 폴더 추가/삭제는 곧바로 메모리 상의 docs_dirs를 바꾸고,
그 결과를 파일에도 "저장"만 한다(다음 재시작을 위한 저장 전용, 동시
읽기 경쟁이 없음).

엔드포인트(전부 notebookrag_main.py가 /indexer/* 로 그대로 중계):
  GET    /status
  POST   /pause
  POST   /resume
  GET    /folders
  POST   /folders {"path": ...}
  DELETE /folders {"path": ...}
  GET    /rules?folder=...
  PUT    /rules {"folder":..., "patterns":[...]}
  POST   /force_reindex   (POST /rag/reindex가 이걸 대신 신호로 씀 —
                            티켓 범위 밖이지만 일관성을 위해 같은 채널로 통일)
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from app_paths import load_settings_json, load_indexer_config, save_indexer_config, lower_process_priority

load_settings_json()

from rag_indexing import build_index, load_ignore_patterns, ModelNotReadyError
from indexer_state import IndexerState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-5s] [indexer-worker] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("indexer-worker")

INDEXER_HOST = "127.0.0.1"
INDEXER_PORT = int(os.getenv("INDEXER_PROC_PORT", "8421"))
IDLE_RESCAN_INTERVAL_SEC = int(os.getenv("IDLE_RESCAN_INTERVAL_SEC", "30"))


async def indexer_loop(app: FastAPI) -> None:
    state: IndexerState = app.state.indexer_state
    running: threading.Event = app.state.running
    while True:
        if not running.is_set():
            state.set_phase("paused")
        await asyncio.to_thread(running.wait)
        if state.phase == "paused":
            state.set_phase("idle")

        force = app.state.force_reindex.is_set()
        if force:
            app.state.force_reindex.clear()

        docs_dirs = [Path(p) for p in app.state.docs_dirs]
        try:
            await asyncio.to_thread(build_index, force=force, docs_dirs=docs_dirs,
                                    state=state, running=running)
        except SystemExit as exc:
            log.warning("이번 회차 색인 건너뜀: %s", exc)
            state.add_warning(str(exc))
        except ModelNotReadyError as exc:
            log.warning("모델 준비 안 됨 — 이번 회차 색인 건너뜀: %s", exc)
            state.add_warning(f"모델 준비 중 — 색인 대기: {exc}")
        except Exception as exc:
            log.error("색인 루프 예외: %s", exc, exc_info=True)
            state.add_warning(f"색인 루프 예외: {exc}")
        state.set_phase("idle")
        await asyncio.sleep(IDLE_RESCAN_INTERVAL_SEC)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.indexer_state = IndexerState(persist=True)
    app.state.running = threading.Event()
    app.state.running.set()
    app.state.force_reindex = threading.Event()
    # [긴급 삭제안전장치] indexer_config.json은 부팅 시 1회 초기값으로만
    # 읽는다 — 이후 실행 중에는 이 in-memory 리스트가 유일한 진실 원천이고,
    # 파일은 저장 전용(재시작 복원용)으로만 갱신한다.
    app.state.docs_dirs = list(load_indexer_config().get("docs_dirs", []))
    task = asyncio.create_task(indexer_loop(app))
    log.info("색인 워커 시작: http://%s:%d (PID %d, 초기 감시 폴더 %d개)",
             INDEXER_HOST, INDEXER_PORT, os.getpid(), len(app.state.docs_dirs))
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


def _save_docs_dirs(app: FastAPI) -> None:
    save_indexer_config({"docs_dirs": app.state.docs_dirs})


@app.get("/status")
async def get_status(request: Request):
    return request.app.state.indexer_state.to_dict()


@app.post("/pause")
async def pause(request: Request):
    request.app.state.running.clear()
    return {"status": "ok", "running": False}


@app.post("/resume")
async def resume(request: Request):
    request.app.state.running.set()
    return {"status": "ok", "running": True}


@app.post("/force_reindex")
async def force_reindex(request: Request):
    request.app.state.force_reindex.set()
    return {"status": "requested"}


@app.get("/folders")
async def get_folders(request: Request):
    return {"docs_dirs": request.app.state.docs_dirs}


class FolderRequest(BaseModel):
    path: str


@app.post("/folders")
async def add_folder(req: FolderRequest, request: Request):
    path = req.path.strip()
    if not path or not Path(path).is_dir():
        raise HTTPException(status_code=400, detail=f"존재하지 않는 폴더입니다: {req.path}")
    dirs = request.app.state.docs_dirs
    if path not in dirs:
        dirs.append(path)
        _save_docs_dirs(request.app)
    return {"docs_dirs": dirs}


@app.delete("/folders")
async def remove_folder(req: FolderRequest, request: Request):
    dirs = [d for d in request.app.state.docs_dirs if d != req.path]
    request.app.state.docs_dirs = dirs
    _save_docs_dirs(request.app)
    return {"docs_dirs": dirs}


@app.get("/rules")
async def get_rules(folder: str):
    return {"folder": folder, "patterns": load_ignore_patterns(Path(folder))}


class RulesRequest(BaseModel):
    folder: str
    patterns: list[str]


@app.put("/rules")
async def put_rules(req: RulesRequest):
    root = Path(req.folder)
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"존재하지 않는 폴더입니다: {req.folder}")
    text = "".join(p + "\n" for p in req.patterns)
    (root / ".ragignore").write_text(text, encoding="utf-8")
    return {"folder": req.folder, "patterns": req.patterns}


def run_worker() -> None:
    """multiprocessing.Process(target=run_worker)로 기동된다.

    [실사용 발견 — 2026-08-28] 대량 파일 스캔/콘텐츠해시/텍스트추출을
    실제로 수행하는 게 바로 이 프로세스인데, 예전엔 우선순위 조정이
    전혀 없었다 — 디스크 I/O가 NORMAL 우선순위 그대로라 대량 재색인 중
    시스템 전체(탐색기 포함)가 행 걸리는 문제가 실사용 중 확인됨."""
    lower_process_priority()
    uvicorn.run(app, host=INDEXER_HOST, port=INDEXER_PORT)
