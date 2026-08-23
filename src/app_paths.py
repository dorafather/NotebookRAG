#!/usr/bin/env python3
"""
app_paths.py — 설치 폴더(읽기 전용) ↔ 사용자 데이터 폴더(%APPDATA%\\NotebookRAG)
경로 해석을 한 곳에 모은 공용 모듈. rag_indexing.py/rag_serve.py 양쪽에서
같은 로직을 중복 구현하지 않도록 여기서 공유한다.

경로 분리 원칙:
  설치 폴더 (읽기 전용)
    config/mcp_tools.json         — 그대로 유지, 사용자가 안 건드림
    config/settings.json.template — 최초 실행 시 복사 원본
  %APPDATA%\\NotebookRAG\\ (쓰기 가능, 사용자별)
    settings.json                 — 실제 설정값(API 키 등)
    rag_index.db (+_b, .active)   — 색인 DB

⚠️ get_install_dir()의 frozen 분기(`.parent.parent`)는 exe 빌드 티켓에서
확정된 `bin/<name>/<name>.exe` 배치를 가정한 것이다 — 실제 exe 기준으로는
검증 완료(mcp-rag.exe)했지만, notebookrag.exe(색인+서빙)는 별도 빌드
티켓에서 동일 배치인지 재확인 필요.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def get_install_dir() -> Path:
    """설치 폴더(읽기 전용) 루트. exe면 실행파일 위치, 아니면 개발 소스 위치."""
    if is_frozen():
        return Path(sys.executable).parent.parent  # bin/<name>/<exe> → 설치 루트
    return Path(__file__).parent.parent  # src/ → 개발 루트


def get_app_data_dir() -> Path:
    """사용자 데이터 루트(%APPDATA%\\NotebookRAG). 없으면 생성.
    [DEV] 개발 모드에서는 기존 워크플로우(스크립트와 같은 폴더에 DB 두기)를
    깨지 않기 위해 RAG_DATA_DIR 환경변수가 명시적으로 설정돼 있으면 그 값을
    최우선으로 그대로 쓴다(하위호환)."""
    override = os.getenv("RAG_DATA_DIR")
    if override:
        p = Path(override)
    elif is_frozen():
        p = Path(os.environ["APPDATA"]) / "NotebookRAG"
    else:
        p = Path(__file__).parent  # 개발 모드 기본값: 지금까지와 동일(src/)
    p.mkdir(parents=True, exist_ok=True)
    return p


def ensure_settings_json() -> Path:
    """%APPDATA%\\NotebookRAG\\settings.json이 없으면 템플릿을 복사해 생성.
    이미 있으면 절대 덮어쓰지 않는다(사용자가 입력한 값 보존)."""
    dest = get_app_data_dir() / "settings.json"
    if not dest.exists():
        template = get_install_dir() / "config" / "settings.json.template"
        if template.exists():
            shutil.copy(template, dest)
    return dest


def load_settings_json() -> None:
    """settings.json 값을 os.environ에 주입한다. 이미 설정된 환경변수는
    덮어쓰지 않는다 — load_dotenv()보다 먼저 호출해서 우선순위를
    "실제 환경변수 > .env(dev 편의) > settings.json > 코드 기본값" 순으로
    맞춘다 (load_dotenv() 역시 이미 설정된 환경변수는 덮어쓰지 않으므로
    호출 순서만 지키면 됨)."""
    path = ensure_settings_json()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return
    for k, v in data.items():
        if k.startswith("_"):  # "_설명" 류 주석 필드는 건너뜀
            continue
        if v and k not in os.environ:  # 빈 값은 무시, 이미 있으면 안 덮어씀
            os.environ[k] = str(v)


# ── indexer_config.json (색인 대상 폴더 목록 — 트레이 앱이 실시간으로 편집) ──

def get_indexer_config_path() -> Path:
    return get_app_data_dir() / "indexer_config.json"


def load_indexer_config() -> dict:
    """{"docs_dirs": [...]}. 파일이 없거나 손상됐으면 빈 목록으로 취급한다
    (트레이 앱에서 폴더를 아직 하나도 안 넣은 최초 상태와 동일하게 처리)."""
    path = get_indexer_config_path()
    if not path.exists():
        return {"docs_dirs": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"docs_dirs": []}
    data.setdefault("docs_dirs", [])
    return data


def save_indexer_config(config: dict) -> None:
    path = get_indexer_config_path()
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


# ── indexer_status.json ([프로세스 분리] 색인 자식 프로세스 → API 부모
#    프로세스로 진행 상황을 넘기는 채널. IndexerState.to_dict()가 바뀔 때마다
#    이 파일에 그대로 덮어써지고, GET /indexer/status는 이 파일을 그때그때
#    읽어서 응답한다 — indexer_config.json과 동일한 "디스크로 통신" 패턴이라
#    공유메모리/멀티프로세싱 IPC를 새로 안 만들어도 된다. ──────────────────

def get_indexer_status_path() -> Path:
    return get_app_data_dir() / "indexer_status.json"


def save_indexer_status(status: dict) -> None:
    """임시 파일에 쓰고 교체(os.replace)해서, 부모 프로세스가 읽는 도중에
    쓰기가 겹쳐도 항상 완전한 JSON만 보이게 한다(원자적 교체 — 두 프로세스가
    같은 파일을 동시에 열 때의 반쪽짜리 읽기 방지)."""
    path = get_indexer_status_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_indexer_status() -> dict | None:
    """파일이 없거나(색인 자식이 아직 한 번도 안 씀) 손상됐으면 None —
    호출부가 "아직 상태 없음"으로 처리하게 한다."""
    path = get_indexer_status_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
