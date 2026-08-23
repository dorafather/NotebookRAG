#!/usr/bin/env python3
"""
indexer_serve.py — 색인기 API 중계(proxy) 라우터
[티켓 C-0 통합 → 프로세스 분리(GIL) → 긴급 삭제안전장치(HTTP화)]

역할 변천사:
  - 티켓 C-0: rag_serve.py와 같은 프로세스에 색인 루프를 얹음.
  - 프로세스 분리(2026-08-21): 파이썬 GIL 때문에 스레드 분리만으론
    임베딩 중 API 전체가 응답불가 상태에 빠지는 문제가 있어, 색인을
    별도 자식 프로세스(indexer_worker.py)로 완전히 떼어냄. 이때는 상태를
    파일(indexer_status.json)로, 폴더 설정을 파일(indexer_config.json)로
    주고받았음.
  - [긴급 삭제안전장치, 이번 개정]: 감시 폴더 설정을 파일로 주고받다가
    "쓰는 도중 읽는" 경쟁 상태로 기존 색인 데이터(약 670MB, 134,704개
    조각)가 통째로 삭제되는 사고가 발생. 원자적 쓰기 패치로는 "쓰기와
    읽기가 시간적으로 분리된 두 이벤트"라는 근본 구조가 안 없어지므로,
    파일 통신을 없애고 색인 프로세스도 자체 HTTP 서버(INDEXER_PROC_PORT,
    indexer_worker.py)를 갖게 했다 — 이 파일은 이제 그 서버로의 순수
    중계(proxy) 라우터다. HTTP 요청/응답은 본질적으로 원자적이라 "반쯤
    쓰인 상태를 읽는" 경쟁 상태 자체가 없다.

  `indexer_config.json`에 남는 유일한 역할은 색인 프로세스가 부팅 시
  1회 초기값으로 읽는 것뿐이다(재시작 복원용, 저장 전용) — 실시간 상태
  전달에는 더 이상 이 파일을 안 쓴다.

  중계 중 색인 프로세스가 응답이 없으면(예: 임베딩 중 GIL을 오래
  붙잡고 있어서) 크래시하지 않고 503으로 우아하게 답한다 — 단
  GET /status만은 마지막으로 색인 프로세스가 직접 써둔
  indexer_status.json(있으면)으로 대신 응답해서, 화면이 완전히
  깜깜해지지 않게 한다(트레이 쪽의 "마지막 성공 상태 유지" 기능과 같은
  철학).

엔드포인트 (통합 실행 시 /indexer 프리픽스 붙음) — 외부 계약(요청/응답
형식)은 이전과 동일하게 유지한다(tray.exe는 무수정으로 계속 동작):
  GET    /status
  POST   /pause
  POST   /resume
  GET    /folders
  POST   /folders {"path":...}
  DELETE /folders {"path":...}
  GET    /rules?folder=<path>
  PUT    /rules {"folder":..., "patterns":[...]}
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app_paths import load_settings_json, load_indexer_status

load_settings_json()

from rag_indexing import CACHE_DB, _read_active_gen, _db_path_for_gen, get_file_meta_count

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-5s] [indexer-proxy] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("indexer-proxy")

INDEXER_PROC_PORT = int(os.getenv("INDEXER_PROC_PORT", "8421"))
_INDEXER_BASE_URL = f"http://127.0.0.1:{INDEXER_PROC_PORT}"
_PROXY_TIMEOUT_SEC = 5.0  # 색인 프로세스가 GIL을 오래 붙잡고 있을 수 있어 짧게 끊고 우아하게 답함


async def _proxy(method: str, path: str, **kwargs) -> httpx.Response:
    """색인 프로세스로 요청을 그대로 중계한다. 응답이 없으면(타임아웃/연결
    거부) 크래시 대신 503으로 답한다 — 검증 요구사항: "색인 프로세스가
    잠깐 응답 안 하면 우아한 오류로 처리되는지"."""
    try:
        async with httpx.AsyncClient(timeout=_PROXY_TIMEOUT_SEC) as client:
            return await client.request(method, _INDEXER_BASE_URL + path, **kwargs)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        log.warning("색인 프로세스 응답 없음(%s %s): %s", method, path, exc)
        raise HTTPException(status_code=503, detail="색인 프로세스가 응답하지 않습니다 — 잠시 후 다시 시도하세요")


def _mb(path: Path) -> float | None:
    return round(path.stat().st_size / (1024 * 1024), 2) if path.exists() else None


def _disk_usage() -> dict:
    active_gen = _read_active_gen(CACHE_DB)
    other_gen = "b" if active_gen == "a" else "a"
    active_mb = _mb(_db_path_for_gen(CACHE_DB, active_gen)) or 0.0
    other_mb = _mb(_db_path_for_gen(CACHE_DB, other_gen))
    return {
        "활성DB_MB": active_mb,
        "재색인중_임시DB_MB": other_mb,
        "전체_MB": round(active_mb + (other_mb or 0.0), 2),
    }


_DEFAULT_STATUS = {
    "phase": "starting", "디렉토리총파일수": 0, "총파일수": 0, "적재된파일수": 0, "청킹수": 0,
    "진행중": {"파일명": None, "단계": None, "파일내_진행": None},
    "이번회차_집계": {"신규": 0, "변경": 0, "재사용": 0, "중복스킵": 0, "처리실패": 0},
    "경고": {"건수": 0, "최근": []},
    "시간": {"경과초": 0.0, "예상잔여초": None},
}


router = APIRouter()


@router.get("/status")
async def get_status(request: Request):
    """[긴급 삭제안전장치] 다른 라우트와 달리 여기서만 색인 프로세스가
    응답 없을 때 503을 던지지 않고, 마지막으로 색인 프로세스가 직접 써둔
    indexer_status.json으로 대신 답한다 — /status는 트레이가 몇 초마다
    계속 폴링하는 경로라, 임베딩 중 잠깐 응답이 없을 때마다 화면이
    깜깜해지면 안 되기 때문이다(오늘 낮에 고친 "마지막 성공 상태 유지"와
    같은 철학, 이번엔 부모 쪽에서 한 번 더).

    [DB저장파일수] 자식의 실시간 상태와 무관하게 이 부모 프로세스가 직접
    조회한다 — 색인 프로세스가 응답을 못 하는 바로 그 상황(한창 임베딩
    중)에서도 "DB에 실제로 몇 개 저장돼 있나"는 계속 보여야, 디렉토리
    파일수와 나란히 비교하는 이 필드의 목적이 유지된다."""
    try:
        r = await _proxy("GET", "/status")
        body = r.json()
    except HTTPException:
        body = load_indexer_status() or dict(_DEFAULT_STATUS)
    body["디스크"] = _disk_usage()
    body["DB저장파일수"] = get_file_meta_count()
    return body


@router.post("/pause")
async def pause():
    r = await _proxy("POST", "/pause")
    return r.json()


@router.post("/resume")
async def resume():
    r = await _proxy("POST", "/resume")
    return r.json()


@router.get("/folders")
async def get_folders():
    r = await _proxy("GET", "/folders")
    return r.json()


class FolderRequest(BaseModel):
    path: str


@router.post("/folders")
async def add_folder(req: FolderRequest):
    r = await _proxy("POST", "/folders", json={"path": req.path})
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.json().get("detail", "알 수 없는 오류"))
    return r.json()


@router.delete("/folders")
async def remove_folder(req: FolderRequest):
    r = await _proxy("DELETE", "/folders", json={"path": req.path})
    return r.json()


@router.get("/rules")
async def get_rules(folder: str):
    r = await _proxy("GET", "/rules", params={"folder": folder})
    return r.json()


class RulesRequest(BaseModel):
    folder: str
    patterns: list[str]


@router.put("/rules")
async def put_rules(req: RulesRequest):
    r = await _proxy("PUT", "/rules", json={"folder": req.folder, "patterns": req.patterns})
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.json().get("detail", "알 수 없는 오류"))
    return r.json()
