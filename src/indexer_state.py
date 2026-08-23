#!/usr/bin/env python3
"""
indexer_state.py — 색인기 진행 상태를 스레드 안전하게 보관하는 객체.

rag_indexing.py의 build_index()가 색인을 진행하며 이 객체를 갱신하고,
indexer_serve.py의 GET /status가 to_dict()를 읽어 그대로 응답한다.
락으로 감싸는 이유: build_index()는 백그라운드 색인 루프(스레드풀,
asyncio.to_thread)에서 갱신하고 /status는 FastAPI(다른 스레드/이벤트루프)가
동시에 읽기 때문.

예상잔여초 계산은 rag_indexing._eta_seconds()를 그대로 재사용한다(새 ETA
로직을 따로 만들지 않음 — 콘솔 진행률 출력(_print_progress)과 동일 공식).
"""

from __future__ import annotations

import threading
import time

_COUNT_KEYS = ("신규", "변경", "재사용", "중복스킵", "처리실패")


class IndexerState:
    def __init__(self, max_warnings: int = 20, persist: bool = False):
        """persist=True([프로세스 분리]): 상태가 바뀔 때마다
        app_paths.save_indexer_status()로 디스크에도 즉시 반영한다 — 색인이
        별도 프로세스(자식)에서 돌 때, API를 서빙하는 부모 프로세스가 이
        객체를 직접 참조할 수 없으므로 파일을 거쳐 상태를 넘긴다. 같은
        프로세스 안에서만 쓰는 기존 용법(단독 실행 등)은 기본값 False로 두면
        디스크 I/O 없이 예전과 동일하게 동작한다."""
        self._lock = threading.Lock()
        self._max_warnings = max_warnings
        self._persist = persist
        self._경고: list[dict] = []  # 라운드 경계와 무관하게 누적 — _reset_locked()가 건드리지 않음
        # [DB저장파일수 비교용] 감시 폴더의 실제 파일 개수 — phase(idle 포함)와
        # 무관하게 항상 최신 값을 유지해야 해서 _reset_locked()가 안 건드림.
        self.디렉토리총파일수 = 0
        self._reset_locked()

    def _reset_locked(self) -> None:
        self.phase = "idle"  # idle | scanning | processing | paused
        self.총파일수 = 0
        self.적재된파일수 = 0
        self.청킹수 = 0
        self._진행중_파일명: str | None = None
        self._진행중_단계: str | None = None
        self._진행중_파일내_진행: tuple[int, int] | None = None
        self._집계 = {k: 0 for k in _COUNT_KEYS}
        self._시작시각 = time.time()

    # ── 갱신 (rag_indexing.build_index()가 호출) ─────────────────────────────

    def start_round(self, total_files: int, reuse_count: int, dup_count: int = 0) -> None:
        """이번 색인 회차 시작 — 처리 대상 파일 수, 재사용(변경 없음)/콘텐츠
        중복 건수를 기록(둘 다 파일별 루프 밖에서 한 번에 집계되는 값)."""
        with self._lock:
            self._reset_locked()
            self.phase = "processing" if total_files else "idle"
            self.총파일수 = total_files
            self._집계["재사용"] = reuse_count
            self._집계["중복스킵"] = dup_count
            self._persist_locked()

    def set_dir_total(self, count: int) -> None:
        """scan_files() 결과 개수 — build_index()가 스캔 직후 매 회차 호출."""
        with self._lock:
            self.디렉토리총파일수 = count
            self._persist_locked()

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self.phase = phase
            if phase == "idle":
                self._진행중_파일명 = None
                self._진행중_단계 = None
                self._진행중_파일내_진행 = None
            self._persist_locked()

    def set_progress(self, filename: str, stage: str,
                      file_progress: tuple[int, int] | None = None) -> None:
        """stage 예: "extract"(텍스트 추출), "embed"(임베딩)."""
        with self._lock:
            self._진행중_파일명 = filename
            self._진행중_단계 = stage
            self._진행중_파일내_진행 = file_progress
            self._persist_locked()

    def file_done(self, chunks_added: int, kind: str) -> None:
        """kind: "신규" | "변경" | "중복스킵" | "처리실패" (재사용은 start_round에서 집계)."""
        with self._lock:
            self.적재된파일수 += 1
            self.청킹수 += chunks_added
            if kind in self._집계:
                self._집계[kind] += 1
            self._persist_locked()

    def add_warning(self, message: str) -> None:
        with self._lock:
            self._경고.append({"시각": time.time(), "메시지": message})
            if len(self._경고) > self._max_warnings:
                self._경고 = self._경고[-self._max_warnings:]
            self._persist_locked()

    # ── 조회 (indexer_serve.py GET /status가 호출, 또는 [프로세스 분리] 이후
    #    자식 프로세스 자신의 파일 기록용) ─────────────────────────────────────

    def _to_dict_locked(self) -> dict:
        """호출자가 이미 self._lock을 쥐고 있다고 가정 — 락을 다시 안 잡는다
        (재진입 불가능한 threading.Lock이라 여기서 또 잡으면 데드락)."""
        from rag_indexing import _eta_seconds  # 무거운 임포트를 실제 조회 시점까지 지연

        elapsed = time.time() - self._시작시각
        eta = _eta_seconds(self.적재된파일수, self.총파일수, elapsed)
        return {
            "phase": self.phase,
            "디렉토리총파일수": self.디렉토리총파일수,
            "총파일수": self.총파일수,
            "적재된파일수": self.적재된파일수,
            "청킹수": self.청킹수,
            "진행중": {
                "파일명": self._진행중_파일명,
                "단계": self._진행중_단계,
                "파일내_진행": list(self._진행중_파일내_진행)
                             if self._진행중_파일내_진행 else None,
            },
            "이번회차_집계": dict(self._집계),
            "경고": {"건수": len(self._경고), "최근": list(self._경고[-5:])},
            "시간": {
                "경과초": round(elapsed, 1),
                "예상잔여초": round(eta, 1) if eta else None,
            },
        }

    def _persist_locked(self) -> None:
        """[프로세스 분리] self._persist=True일 때만 디스크에 씀 — 매 상태
        변경마다 불리므로 비용이 큰 예외는 삼키고 경고만 남긴다(색인 자체를
        막으면 안 됨)."""
        if not self._persist:
            return
        try:
            from app_paths import save_indexer_status
            save_indexer_status(self._to_dict_locked())
        except Exception:
            pass  # 상태 파일 쓰기 실패는 색인 진행을 막을 이유가 아님

    def to_dict(self) -> dict:
        with self._lock:
            return self._to_dict_locked()
