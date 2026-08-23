#!/usr/bin/env python3
"""
model_downloader.py — bge-m3 GGUF 모델 자동 다운로드 [티켓 D]

httpx로 직접 구현한다(huggingface_hub 패키지나 외부 프로그램 호출 없음 —
notebookrag.exe 번들에 새 위험 요소를 늘리지 않기 위함). Range 헤더로
이어받기를 지원하고, 다운로드 완료 후 SHA256 체크섬을 검증한다.

⚠️ 검증 전까지는 반드시 `.part` 임시 파일로만 존재하고, 체크섬이 맞아야만
최종 파일명으로 원자적 rename한다 — `rag_indexing._default_embed_model_path()`는
파일 "존재 여부"만으로 "모델 준비됨"을 판단하므로, 중간에 끊겼거나 손상된
파일이 완성품으로 오인되면 절대 안 된다.

조사 결과(2026-08-20 확인 — 공개/비gated, MIT 라이선스로 상업적 재배포 가능):
  리포지토리: https://huggingface.co/ggml-org/bge-m3-Q8_0-GGUF
    (BAAI/bge-m3 원본을 llama.cpp 공식 조직 ggml-org가 GGUF로 변환해 배포한 것.
    로그인/토큰 불필요, gated 아님. 원본 BAAI/bge-m3도 MIT, 이 변환 리포도 MIT.)
  파일: bge-m3-q8_0.gguf (634,553,760 bytes, 약 605MiB/635MB)
  URL: https://huggingface.co/ggml-org/bge-m3-Q8_0-GGUF/resolve/main/bge-m3-q8_0.gguf
  SHA256: aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173
    (Git LFS pointer 파일과 HF API(?blobs=true) 두 경로로 교차 확인함)
  ※ settings.json.template에는 정책상(지침서 명시) 이 값을 하드코딩하지 않고
    빈 값으로 남겨둔다 — 운영자가 이 문서의 값을 확인 후 직접 채워 넣을 것.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

import httpx

CHUNK_SIZE = 1 << 20  # 1MB


@dataclass
class ModelDownloadState:
    """asyncio 이벤트 루프 위에서만 갱신되는 단일 코루틴(download_model)의
    상태라 락이 필요 없다 — IndexerState와 달리 실제 다운로드 루프는 별도
    OS 스레드(asyncio.to_thread)로 안 넘어가고 이벤트 루프에 그대로 있다
    (SHA256 계산만 잠깐 스레드로 넘어가는데, 그동안은 이 객체를 안 건드림)."""
    phase: str = "checking"  # checking | downloading | verifying | ready | error
    downloaded_bytes: int = 0
    total_bytes: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        total_mb = round(self.total_bytes / (1024 * 1024), 1) if self.total_bytes else 0.0
        down_mb = round(self.downloaded_bytes / (1024 * 1024), 1)
        pct = (round(self.downloaded_bytes / self.total_bytes * 100, 1)
               if self.total_bytes else (100.0 if self.phase == "ready" else 0.0))
        return {
            "phase": self.phase,
            "다운로드_MB": down_mb,
            "전체_MB": total_mb,
            "진행률": pct,
            "오류": self.error,
        }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(block)
    return h.hexdigest()


async def download_model(url: str, dest: Path, expected_sha256: str,
                          state: ModelDownloadState) -> bool:
    """이어받기 가능한 다운로드. 완료 후 체크섬 검증, 불일치 시 파일 삭제 후
    False 반환(호출부가 재시도 여부 결정). 성공 시 임시파일(.part)을 최종
    경로로 원자적 rename."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    state.phase = "checking"
    resume_from = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code == 416:
                    # 서버가 "그 범위 없음"이라고 답함 — 이미 끝까지 받아둔 상태로 보고 검증으로 진행
                    resume_from = part.stat().st_size if part.exists() else 0
                elif resp.status_code == 206:
                    # 부분 응답 정상 — 이어받기 성공
                    content_length = int(resp.headers.get("Content-Length", "0"))
                    state.total_bytes = resume_from + content_length
                    state.downloaded_bytes = resume_from
                    state.phase = "downloading"
                    with open(part, "ab") as f:
                        async for chunk in resp.aiter_bytes(CHUNK_SIZE):
                            f.write(chunk)
                            state.downloaded_bytes += len(chunk)
                elif resp.status_code == 200:
                    # 서버가 Range를 무시(또는 애초에 이어받을 게 없음) — 처음부터 새로 받음
                    state.total_bytes = int(resp.headers.get("Content-Length", "0"))
                    state.downloaded_bytes = 0
                    state.phase = "downloading"
                    with open(part, "wb") as f:
                        async for chunk in resp.aiter_bytes(CHUNK_SIZE):
                            f.write(chunk)
                            state.downloaded_bytes += len(chunk)
                else:
                    state.phase = "error"
                    state.error = f"다운로드 실패: HTTP {resp.status_code}"
                    return False
    except httpx.HTTPError as exc:
        state.phase = "error"
        state.error = f"네트워크 오류: {exc}"
        return False

    state.phase = "verifying"
    actual = await asyncio.to_thread(_sha256_file, part)
    if actual.lower() != expected_sha256.lower():
        part.unlink(missing_ok=True)
        state.phase = "error"
        state.error = (f"체크섬 불일치(기대 {expected_sha256[:12]}…, "
                       f"실제 {actual[:12]}…) — 손상 파일 삭제됨")
        return False

    part.replace(dest)  # 원자적 rename — 검증 통과한 파일만 최종 이름을 갖는다
    state.phase = "ready"
    return True
