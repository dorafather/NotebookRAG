#!/usr/bin/env python3
"""
notebookrag_main.py — NotebookRAG 통합 진입점 [티켓 C-0]

역할:
  rag_serve.py(검색: /search, /search/answer, /reindex)와
  indexer_serve.py(색인 상태/제어: /status, /pause, /resume, /folders,
  /rules)를 "파일은 나누고, 실행은 합친다" 원칙에 따라 한 프로세스·한
  포트(RAG_HTTP_PORT, 기본 8420)로 통합해 띄우는 진입점이다.

  왜 통합하는가: 두 파일이 참조하는 라이브러리(특히 llama-cpp-python 등
  네이티브 확장)가 같은데, 이를 별도 PyInstaller --onedir 폴더로 각각
  빌드하면 같은 네이티브 라이브러리 뭉치가 중복 배포된다. "관심사 분리"는
  이미 라우트/파일 단위(rag_serve.py ↔ indexer_serve.py)로 달성돼 있으므로,
  "프로세스를 몇 개로 쪼개느냐"는 독립적인 결정이다.

  부수 효과: rag_indexing.py의 _MODEL(bge-m3 임베딩)은 이미 모듈 전역
  싱글턴(락 보호)이라, 같은 프로세스 안에서 rag_serve(질의 임베딩)와
  indexer_serve(문서 임베딩)가 import될 때 파이썬 모듈 캐시 덕분에 자동으로
  하나만 로드된다 — 프로세스가 둘이었을 때는 각자 로드해서 메모리가
  중복됐었다.

라우트 구성:
  /health                → 프리픽스 없이 루트 고정 (rag_serve.health 재사용)
  /rag/*                 → rag_serve.router (기존 /search, /search/answer, /reindex)
  /indexer/*             → indexer_serve.router (기존 /status, /pause, /resume, /folders, /rules)

기동 순서(lifespan):
  ① RagRA()로 검색용 색인을 연다 — open_existing_index() 기반이라 빠름,
     색인 루프를 기다리지 않고 바로 서빙 가능.
  ② indexer_loop()를 백그라운드 asyncio 태스크로 기동 — 서빙 요청 처리를
     막지 않는다.
  ③ [티켓 D] bge-m3 모델 파일이 없으면 model_downloader.download_model()을
     백그라운드 태스크로 기동 — 역시 서빙을 막지 않는다. 모델이 없는 동안
     /rag/search 등은 rag_indexing.ModelNotReadyError를 잡아 503으로 답하고,
     색인 루프도 그 회차를 건너뛰고 다음 재스캔 때 재시도한다(선결 과제).
     진행률은 GET /model/status로 노출.

성능/우선순위 관련(2026-08-20 사용자 피드백 반영): 이 프로세스는 트레이 앱이
관리하는 상시 백그라운드 서비스라 "존재감을 드러내지 않아야" 한다는 요구가
있었다. 두 가지로 대응:
  1. 프로세스 우선순위를 BELOW_NORMAL로 낮춤(아래 _lower_process_priority()) —
     사용자가 포그라운드에서 뭘 하든 OS 스케줄러가 이 프로세스를 항상 양보하게
     만든다. 검색 API도 같은 프로세스라 같이 낮아지지만, 로컬호스트 응답
     속도에는 체감상 영향이 없는 수준이라고 판단.
  2. rag_indexing.EMBED_THREADS(llama.cpp 내부 스레드 수)를 기존
     os.cpu_count() 고정값에서 코어수의 절반(기본값)으로 낮춤 — 예전엔
     RAG_EMBED_WORKERS를 1로 낮춰도 임베딩 한 번마다 전체 코어를 썼었다
     (RAG_EMBED_WORKERS는 embed() 호출 큐잉 동시성만 조절할 뿐 CPU 코어
     점유와는 무관했음 — rag_demo.py 때부터 있던 설계, 회귀 아님).

인스턴스 공유: RagRA/IndexerState/일시정지 플래그(threading.Event)는 전부
`app.state`에 보관한다 — rag_serve.py/indexer_serve.py의 라우트 함수들이
이미 `request.app.state.*`로 접근하도록 리팩터링돼 있어서(단독 실행 시엔
각 파일 자신의 lifespan이 같은 이름으로 app.state를 채움), 이 파일은
그 두 lifespan의 내용을 합쳐서 실행하기만 하면 된다.

사용:
  python notebookrag_main.py
  pm2 start notebookrag_main.py --name notebookrag --interpreter python3
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import multiprocessing
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request

from app_paths import load_settings_json, get_install_dir

load_settings_json()  # load_dotenv()보다 먼저 호출 (우선순위: 환경변수 > .env > settings.json)
load_dotenv()


def _lower_process_priority() -> None:
    """[성능] 프로세스 전체를 BELOW_NORMAL_PRIORITY_CLASS로 낮춘다 — 상시
    백그라운드 서비스라 사용자의 포그라운드 작업에 항상 CPU를 양보하게
    만드는 게 목적. ctypes+kernel32만 쓰고 새 의존성(예: psutil)을 들이지
    않는다. 실패해도(권한 문제 등) 치명적이지 않으므로 조용히 무시하고
    NORMAL로 계속 실행한다 — 이 프로세스의 핵심 기능과 무관한 최적화이므로."""
    if sys.platform != "win32":
        return
    try:
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        kernel32 = ctypes.windll.kernel32
        # [주의] GetCurrentProcess()는 포인터 크기 HANDLE을 반환하는데,
        # ctypes는 선언 없는 함수를 기본 c_int(32비트)로 취급해서 64비트에서
        # 핸들이 잘린다(실제로 GetLastError=6 ERROR_INVALID_HANDLE로 재현됨) —
        # restype을 명시해야 한다.
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        handle = kernel32.GetCurrentProcess()
        ok = kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS)
        if ok:
            log.info("프로세스 우선순위를 BELOW_NORMAL로 낮췄습니다")
        else:
            log.warning("SetPriorityClass가 실패를 반환함(GetLastError=%d) — NORMAL로 계속",
                       ctypes.windll.kernel32.GetLastError())
    except Exception as exc:
        log.warning("프로세스 우선순위 조정 실패(무시하고 계속): %s", exc)


import rag_serve
import indexer_serve
import indexer_worker
from rag_serve import RagRA, health
from rag_indexing import EMBED_MODEL_PATH, get_embed_dim, open_existing_index, _make_dir_labels
from model_downloader import ModelDownloadState, download_model

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-5s] [notebookrag] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("notebookrag")

_lower_process_priority()

RAG_HTTP_HOST = rag_serve.RAG_HTTP_HOST
RAG_HTTP_PORT = rag_serve.RAG_HTTP_PORT

# [티켓 D] 정책상 URL/체크섬은 하드코딩하지 않는다 — settings.json.template/
# .env/실제 환경변수로만 채운다(model_downloader.py 상단 docstring에 실제
# 조사해서 확인한 값이 적혀있으니 운영자가 그 값을 settings.json에 넣을 것).
MODEL_DOWNLOAD_URL = os.getenv("MODEL_DOWNLOAD_URL", "")
MODEL_SHA256 = os.getenv("MODEL_SHA256", "")


def _spawn_indexer_process():
    """[긴급 삭제안전장치] 색인 워커는 이제 자기 상태(일시정지 플래그 등)를
    전부 자기 프로세스 안에서 관리하고 자체 HTTP 서버(INDEXER_PROC_PORT)로
    노출한다 — 부모가 넘겨줄 공유 객체(예전의 multiprocessing.Event)가
    더 이상 필요 없다."""
    ctx = multiprocessing.get_context("spawn")  # Windows에서는 spawn이 유일한 선택지라 명시
    p = ctx.Process(target=indexer_worker.run_worker, daemon=True)
    p.start()
    log.info("색인 워커 프로세스 기동: PID %d", p.pid)
    return p


async def _supervise_indexer_process(app: FastAPI) -> None:
    """[프로세스 분리] 색인 자식 프로세스가 죽으면(예: 예외 하나가 정말로
    프로세스 전체를 끝장내는 예상 밖 상황) 감시해서 다시 띄운다 — API
    서버는 색인 프로세스 생사와 무관하게 계속 서빙돼야 하므로, 여기서
    예외가 나도 서버를 죽이지 않는다."""
    while True:
        await asyncio.sleep(10)
        try:
            proc = app.state.indexer_process
            if not proc.is_alive():
                log.error("색인 워커 프로세스(PID %d)가 죽어있음 — 재기동", proc.pid)
                app.state.indexer_process = _spawn_indexer_process()
        except Exception as exc:
            log.error("색인 워커 감시 루프 예외(무시하고 계속): %s", exc, exc_info=True)


REFRESH_INTERVAL_SEC = int(os.getenv("IDLE_RESCAN_INTERVAL_SEC", "30"))


async def _refresh_search_index_periodically(app: FastAPI) -> None:
    """[프로세스 분리] 색인 자식 프로세스가 DB에 새로 써넣은 내용을 검색이
    실제로 반영하도록, 검색용 RagRA.chunks/search를 주기적으로 다시 연다.
    open_existing_index()는 "이미 있는 DB를 그냥 여는" 가벼운 동작이라
    (재색인이 아님) 자주 불러도 부담이 적다.

    [버그 수정 — 2026-08-21] 예전엔 이 상수를 indexer_serve.IDLE_RESCAN_
    INTERVAL_SEC로 참조했는데, 긴급 삭제안전장치 개정으로 indexer_serve.py를
    순수 프록시로 다시 쓰면서 그 상수가 없어졌다 — 이 태스크가 첫 반복에서
    AttributeError로 죽어서 검색 인덱스가 다시는 갱신 안 됐을 가능성이 큼
    (조용히 죽는 백그라운드 태스크라 눈치채기 어려웠음). 이 파일 자신의
    상수로 독립시켜 다른 파일 리팩터링에 안 흔들리게 한다.

    [폴링및출처표시개선] 같은 주기에 검색 결과 출처 표시용 라벨→절대경로
    매핑(RagRA.source_label_map)도 같이 새로고침한다 — 색인 자식 프로세스의
    GET /folders(오늘 신설된 HTTP API)를 불러서 재구성. 이 프로세스는 색인
    자식과 분리돼 있어 rag_indexing.DIR_LABELS(자식만 최신으로 유지)를
    직접 쓸 수 없기 때문."""
    indexer_port = int(os.getenv("INDEXER_PROC_PORT", "8421"))
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_SEC)
        try:
            chunks, search = await asyncio.to_thread(open_existing_index)
            app.state.ra.chunks, app.state.ra.search = chunks, search
        except Exception as exc:
            log.warning("검색 인덱스 갱신 실패(다음 주기에 재시도): %s", exc)

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"http://127.0.0.1:{indexer_port}/folders")
            dirs = [Path(p) for p in r.json().get("docs_dirs", [])]
            onboarding = get_install_dir() / "onboarding"
            if onboarding.is_dir() and onboarding not in dirs:
                dirs = dirs + [onboarding]
            app.state.ra.source_label_map = _make_dir_labels(dirs)
        except Exception as exc:
            log.warning("출처 표시용 폴더 매핑 갱신 실패(다음 주기에 재시도): %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ① 검색용 색인 열기 — 이미 만들어진 DB를 여는 것뿐이라 빠름, 즉시 서빙 가능
    app.state.ra = RagRA()
    app.state.last_search_at = None  # [티켓 G] GET /health의 "마지막검색" 필드용
    app.state.started_at = datetime.now(timezone.utc).isoformat()  # [상태정보확장]

    # ② [프로세스 분리 — 2026-08-21] 색인(스캔/중복검사/임베딩)을 별도
    # 자식 프로세스로 완전히 떼어낸다. 이유: 파이썬 GIL 때문에 스레드로만
    # 나눠서는(예전 방식) 색인 스레드가 native 호출 하나를 오래 붙잡을 때
    # API를 서빙하는 이벤트 루프 스레드가 완전히 굶는 문제가 실측됐음
    # (TCP는 붙는데 accept()가 하염없이 안 불려서 결국 connection refused까지
    # 감). 프로세스는 각자 GIL을 가지므로 이 문제가 원천적으로 안 생긴다.
    # [긴급 삭제안전장치] 색인 프로세스와의 모든 통신(상태/일시정지/폴더
    # 설정/재색인 신호)은 이제 그 프로세스 자신의 HTTP 서버(indexer_serve.py
    # 가 중계)를 거친다 — multiprocessing.Event 공유가 더 이상 필요 없다.
    app.state.indexer_process = _spawn_indexer_process()
    supervisor_task = asyncio.create_task(_supervise_indexer_process(app))
    refresh_task = asyncio.create_task(_refresh_search_index_periodically(app))

    # ③ [티켓 D] 모델이 없으면 백그라운드로 다운로드 — 서빙/색인 루프는 안 막힘
    app.state.model_download_state = ModelDownloadState()
    download_task = None
    if EMBED_MODEL_PATH.exists():
        app.state.model_download_state.phase = "ready"
    elif MODEL_DOWNLOAD_URL and MODEL_SHA256:
        download_task = asyncio.create_task(download_model(
            MODEL_DOWNLOAD_URL, EMBED_MODEL_PATH, MODEL_SHA256,
            app.state.model_download_state))
    else:
        app.state.model_download_state.phase = "error"
        app.state.model_download_state.error = (
            "모델 파일이 없고 MODEL_DOWNLOAD_URL/MODEL_SHA256도 설정되지 않았습니다 "
            "— settings.json에 두 값을 채워주세요")
        log.error(app.state.model_download_state.error)

    log.info("notebookrag_main 시작: http://%s:%d", RAG_HTTP_HOST, RAG_HTTP_PORT)
    yield
    supervisor_task.cancel()
    refresh_task.cancel()
    if download_task:
        download_task.cancel()
    proc = app.state.indexer_process
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=5)


app = FastAPI(lifespan=lifespan)
app.include_router(rag_serve.router, prefix="/rag")
app.include_router(indexer_serve.router, prefix="/indexer")
app.add_api_route("/health", health, methods=["GET"])


@app.get("/model/status")
async def model_status(request: Request):
    d = request.app.state.model_download_state.to_dict()
    d["차원"] = get_embed_dim()  # [상태정보확장] DB meta 테이블에서 조회
    return d


def main():
    uvicorn.run(app, host=RAG_HTTP_HOST, port=RAG_HTTP_PORT)


if __name__ == "__main__":
    # [프로세스 분리] PyInstaller로 얼린(frozen) exe에서 multiprocessing이
    # 자식을 spawn하면 "같은 exe를 특수 인자로 재실행"하는 방식을 쓴다 —
    # freeze_support()가 그 재실행을 감지해서 자식 부트스트랩만 돌리고
    # 끝내야, 자식이 실수로 이 main()(uvicorn 서버 재기동)까지 실행해
    # 포트 충돌을 내는 걸 막는다. 반드시 다른 어떤 코드보다도 먼저 불러야
    # 한다(공식 문서 권장 패턴).
    multiprocessing.freeze_support()
    main()
