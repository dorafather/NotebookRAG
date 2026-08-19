#!/usr/bin/env python3
"""
rag_indexing.py — RAG 색인 모듈 [배포 준비 사본 — 원본(rag_demo.py) 기반]

문서를 텍스트로 추출하고 청킹한 뒤, 사전학습된 bge-m3 임베딩 모델로
벡터화해 검색 가능한 상태로 저장하는 파이프라인입니다. ML 모델을
"학습(training)"하는 게 아니라 이미 학습된 모델로 "색인(indexing)"만
수행합니다 — 파일명이 그 점을 정확히 반영합니다.

구조:
  [색인] docs/ 의 문서 → 텍스트 추출 → 청킹 → llama-cpp-python(인프로세스) 임베딩 → FAISS
  [질의] 질문 임베딩 → top-k 검색 → 근거 조각 첨부 → Claude Haiku 답변

v7 변경 (체크포인트 단위를 파일 그룹(100개) → 파일 1개로 축소):
  [CHECKPOINT] v6까지는 RAG_CHECKPOINT_FILES(기본 100)개 파일을 묶어 청크를
      모은 뒤 한 번에 임베딩하고 그룹 단위로 커밋했다. v7부터는 파일 1개를
      추출→청킹→임베딩한 직후 그 파일의 청크만 INSERT하고 즉시 커밋한다 —
      중단/크래시 시 유실 범위가 그룹(최대 100개 파일)에서 파일 1개로
      줄어든다.

v6 변경 (FAISS 전메모리 색인 제거, SQLite+sqlite-vec 디스크 기반 색인 도입):
  [DISKINDEX] 벡터를 FAISS IndexFlatIP(프로세스 메모리에 전체 상주)가 아니라
      SQLite 파일(rag_index.db) + sqlite-vec 확장으로 저장한다.

v5 변경 (Ollama HTTP 제거):
  [EMBED] llama-cpp-python으로 Ollama가 이미 받아둔 bge-m3 GGUF blob을 이
      프로세스 안에서 직접 로드해 임베딩한다.

v4 변경 (노트북 실사용 규모 대응): [FILTER] .ragignore, [CATEGORY] 카테고리
  태깅, [ROBUST] 파일 단위 예외 처리.

이전 버전 기능 유지: [INC-1] 파일별 지문 추적, [INC-2] 임베딩 병렬화,
  [INC-3] 상대경로 source, [DOC-1] 다형식 로더, [DOC-2] 임베딩 견고화.

설정 (.env 또는 settings.json → 환경변수로 주입):
  RAG_DOCS_DIR=<색인할 문서 루트, 절대경로. 콤마(,)로 여러 개 지정 가능>
  RAG_EMBED_WORKERS=4
  ANTHROPIC_API_KEY=... (MCP 전용 모드에서는 불필요 — rag_serve.py 참고)
  RAG_EMBED_MODEL=bge-m3
  RAG_EMBED_MODEL_PATH=<bge-m3 GGUF blob 경로>
  RAG_DATA_DIR=<색인 DB(rag_index.db) 저장 위치. 배포판에서는 %APPDATA%
               쪽을 가리키도록 런처가 설정 — 개발 중엔 미설정 시 이 파일과
               같은 디렉터리를 기본값으로 사용>

.ragignore 예시 (각 RAG_DOCS_DIR 루트 바로 아래에 개별 생성):
  # 주석은 # 으로 시작
  COM_ENG                # 폴더명 통째로 제외
  *임시*                 # 파일/폴더명에 "임시" 포함 시 제외
  *.msi
  *최종*_v[0-9]*         # "최종_v2" 같은 중복본 패턴 (필요시 조정)

사용:
  python3 rag_indexing.py               # 증분 색인 후 대화형 질의
  python3 rag_indexing.py --reindex     # 전체 강제 재색인 (모델 교체 등)
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from llama_cpp import Llama
from dotenv import load_dotenv
load_dotenv()

try:
    import sqlite_vec
except ImportError:
    sys.exit("오류: sqlite-vec 패키지가 없습니다 — 디스크 기반 벡터 색인에 "
             "필요합니다. `pip install sqlite-vec` 실행 후 다시 시도하세요.")

# ── 설정 ─────────────────────────────────────────────────────────────────────

BASE_DIR    = Path(__file__).parent
DATA_DIR    = Path(os.getenv("RAG_DATA_DIR", str(BASE_DIR)))
CACHE_FILE  = DATA_DIR / "rag_index.npz"
CACHE_DB    = DATA_DIR / "rag_index.db"
EMBED_MODEL = os.getenv("RAG_EMBED_MODEL", "bge-m3")
_DEFAULT_EMBED_MODEL_PATH = (
    Path(os.path.expandvars("%USERPROFILE%")) / ".ollama" / "models" / "blobs" /
    "sha256-daec91ffb5dd0c27411bd71f29932917c49cf529a641d0168496c3a501e3062c"
)
EMBED_MODEL_PATH = Path(os.getenv("RAG_EMBED_MODEL_PATH", str(_DEFAULT_EMBED_MODEL_PATH)))
EMBED_WORKERS = int(os.getenv("RAG_EMBED_WORKERS", "4"))
if "nomic" in EMBED_MODEL:
    PREFIX_DOC, PREFIX_QUERY = "search_document: ", "search_query: "
else:
    PREFIX_DOC = PREFIX_QUERY = ""
CHUNK_SIZE, CHUNK_OVERLAP = 500, 100
DEGENERATE_COMPRESSION_RATIO = 0.05
DEGENERATE_SAMPLE_CHARS      = 200_000
ABSOLUTE_MAX_CHARS = 3_000_000
EMBED_MAX_RETRY   = 3
TOP_K       = 4
SUPPORTED   = (".md", ".pdf", ".docx", ".pptx")

# ── [MULTI] 문서 루트 다중 지정 ──────────────────────────────────────────────

def _parse_docs_dirs() -> list[Path]:
    raw = os.getenv("RAG_DOCS_DIR", str(BASE_DIR / "docs"))
    return [Path(p.strip()) for p in raw.split(",") if p.strip()]

def _make_dir_labels(dirs: list[Path]) -> dict[str, Path]:
    labels: dict[str, Path] = {}
    seen: dict[str, int] = {}
    for d in dirs:
        name = d.name or str(d)
        if name in seen:
            seen[name] += 1
            label = f"{name}({seen[name]})"
        else:
            seen[name] = 1
            label = name
        labels[label] = d
    return labels

DOCS_DIRS  = _parse_docs_dirs()
DIR_LABELS = _make_dir_labels(DOCS_DIRS)

def resolve_path(rel: str) -> Path:
    label, _, sub = rel.partition("/")
    return DIR_LABELS[label] / sub

# ── [FILTER] .ragignore ──────────────────────────────────────────────────────

def load_ignore_patterns(root: Path) -> list[str]:
    f = root / ".ragignore"
    if not f.exists():
        return []
    return [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]

def is_ignored(rel: str, patterns: list[str]) -> bool:
    rel_posix = rel.replace("\\", "/")
    parts = rel_posix.split("/")
    for pat in patterns:
        if fnmatch.fnmatch(rel_posix, pat):
            return True
        if any(fnmatch.fnmatch(part, pat) for part in parts):
            return True
    return False

# ── [DOC-1] 형식별 텍스트 추출 ───────────────────────────────────────────────

def extract_text(f: Path) -> str | list[str]:
    suf = f.suffix.lower()
    if suf in (".txt", ".md"):
        return f.read_text(encoding="utf-8", errors="ignore")
    if suf == ".pdf":
        from pypdf import PdfReader
        try:
            return "\n".join(p.extract_text() or "" for p in PdfReader(f).pages)
        except Exception as exc:
            print(f"    경고: {f.name} PDF 추출 실패 — 건너뜀 ({exc})")
            return ""
    if suf == ".docx":
        from docx import Document
        doc = Document(f)
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(parts)
    if suf == ".pptx":
        from pptx import Presentation
        slides = []
        for n, slide in enumerate(Presentation(f).slides, 1):
            texts = [sh.text for sh in slide.shapes
                     if sh.has_text_frame and sh.text.strip()]
            if texts:
                slides.append(f"[슬라이드 {n}]\n" + "\n".join(texts))
        return slides
    return ""

# ── 청킹 ─────────────────────────────────────────────────────────────────────

def chunk_text(text: str, source: str) -> list[dict]:
    chunks = []
    step = CHUNK_SIZE - CHUNK_OVERLAP
    for start in range(0, max(len(text) - CHUNK_OVERLAP, 1), step):
        piece = text[start:start + CHUNK_SIZE].strip()
        if len(piece) > 50:
            chunks.append({"source": source, "text": piece})
    return chunks

def extract_file_chunks(f: Path, rel: str) -> list[dict]:
    rel_posix = rel.replace("\\", "/")
    category = rel_posix.split("/")[0] if "/" in rel_posix else "_root"
    try:
        extracted = extract_text(f)
    except Exception as exc:
        print(f"    경고: {rel} 추출 중 예외 발생 — 건너뜀 ({exc})")
        return []

    if isinstance(extracted, str) and len(extracted) > DEGENERATE_SAMPLE_CHARS:
        import zlib
        sample = extracted[:DEGENERATE_SAMPLE_CHARS].encode("utf-8", errors="ignore")
        ratio = len(zlib.compress(sample, 6)) / max(len(sample), 1)
        if ratio < DEGENERATE_COMPRESSION_RATIO:
            print(f"    ⚠️ 경고: {rel} 반복성 텍스트 의심 (압축률 {ratio:.1%}, 원본 "
                  f"{len(extracted):,}자) — 깨진 추출로 판단, 5,000자만 사용")
            extracted = extracted[:5_000]
        elif len(extracted) > ABSOLUTE_MAX_CHARS:
            print(f"    ⚠️ 경고: {rel} 정상 텍스트이나 매우 큼 (압축률 {ratio:.1%}, "
                  f"{len(extracted):,}자) — 외부 상한 {ABSOLUTE_MAX_CHARS:,}자로 제한")
            extracted = extracted[:ABSOLUTE_MAX_CHARS]

    if isinstance(extracted, list):
        return [{"source": rel, "category": category, "text": s.strip()}
                for s in extracted if len(s.strip()) > 30]
    if extracted.strip():
        chunks = chunk_text(extracted, rel)
        for c in chunks:
            c["category"] = category
        return chunks
    print(f"    경고: {rel} 에서 텍스트를 추출하지 못함 (스캔본 PDF일 수 있음)")
    return []

# ── [INC-1]+[FILTER] 파일별 지문 스캔 ────────────────────────────────────────

def scan_files() -> dict[str, tuple[int, int]]:
    out = {}
    skipped = 0
    any_dir_found = False
    for label, root in DIR_LABELS.items():
        if not root.is_dir():
            print(f"경고: {root}/ 디렉터리가 없습니다 — 건너뜀")
            continue
        any_dir_found = True
        patterns = load_ignore_patterns(root)
        for f in sorted(root.rglob("*")):
            if f.is_file() and f.suffix.lower() in SUPPORTED:
                sub = str(f.relative_to(root))
                if patterns and is_ignored(sub, patterns):
                    skipped += 1
                    continue
                st = f.stat()
                rel = f"{label}/{sub}"
                out[rel] = (st.st_size, st.st_mtime_ns)
    if not any_dir_found:
        sys.exit(f"오류: RAG_DOCS_DIR로 지정된 디렉터리가 하나도 없습니다 "
                 f"({', '.join(str(d) for d in DOCS_DIRS)})")
    if skipped:
        print(f"[색인] .ragignore로 제외된 파일: {skipped}개")
    if not out:
        sys.exit(f"오류: 색인할 문서가 없습니다 (지원 형식: {', '.join(SUPPORTED)})")
    return out

# ── [DEDUP] 콘텐츠 해시 기반 중복 파일 탐지 ──────────────────────────────────

_HASH_CHUNK_BYTES = 1 << 20

def _file_content_hash(f: Path) -> str:
    h = hashlib.sha1()
    with open(f, "rb") as fp:
        for block in iter(lambda: fp.read(_HASH_CHUNK_BYTES), b""):
            h.update(block)
    return h.hexdigest()

def find_content_duplicates(rels: list[str]) -> dict[str, str]:
    dup_of: dict[str, str] = {}
    seen_hashes: dict[str, str] = {}
    for rel in rels:
        try:
            h = _file_content_hash(resolve_path(rel))
        except OSError as exc:
            print(f"    경고: {rel} 해시 계산 실패 — 중복 검사 건너뜀 ({exc})")
            continue
        if h in seen_hashes:
            dup_of[rel] = seen_hashes[h]
        else:
            seen_hashes[h] = rel
    if dup_of:
        print(f"[색인] 콘텐츠 중복 파일 {len(dup_of)}개 발견 (신규/변경 파일 "
              f"{len(rels)}개 중) — 청킹/임베딩 생략:")
        for dup, orig in sorted(dup_of.items()):
            print(f"    중복: {dup}  ==  원본: {orig}")
    return dup_of

# ── [DOC-2]+[INC-2] 임베딩 (견고화 + 병렬, llama-cpp-python 인프로세스) ──────

_MODEL_LOCK = threading.Lock()
_MODEL_LOAD_LOCK = threading.Lock()
_MODEL: Llama | None = None


def _get_model() -> Llama:
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOAD_LOCK:
        if _MODEL is None:
            if not EMBED_MODEL_PATH.exists():
                sys.exit(f"오류: 임베딩 모델 GGUF 파일을 찾을 수 없습니다: "
                         f"{EMBED_MODEL_PATH}\n"
                         f"  .env의 RAG_EMBED_MODEL_PATH를 확인하거나, "
                         f"Ollama가 bge-m3를 받아뒀는지(`ollama list`) 확인하세요.")
            print(f"[임베딩] llama-cpp-python 모델 로드 중: {EMBED_MODEL_PATH}")
            t0 = time.time()
            _MODEL = Llama(
                model_path=str(EMBED_MODEL_PATH),
                embedding=True,
                n_threads=os.cpu_count() or 4,
                verbose=False,
            )
            print(f"[임베딩] 모델 로드 완료 ({time.time() - t0:.1f}초)")
    return _MODEL


def _embed_one(text: str) -> list[float] | None:
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    if not text.strip():
        text = " "
    model = _get_model()
    for attempt in range(1, EMBED_MAX_RETRY + 1):
        try:
            with _MODEL_LOCK:
                vec = model.embed(text)
            if isinstance(vec[0], list):
                vec = np.mean(np.array(vec, dtype=np.float32), axis=0).tolist()
            return vec
        except Exception as exc:
            if attempt == EMBED_MAX_RETRY:
                print(f"    ⚠️ 임베딩 실패(재시도 {EMBED_MAX_RETRY}회 소진): {exc}")
                return None
            time.sleep(0.5)
    return None


def _print_progress(label: str, done: int, total: int, t0: float) -> None:
    elapsed = time.time() - t0
    rate = done / elapsed if elapsed > 0 else 0
    eta_sec = (total - done) / rate if rate > 0 else 0
    print(f"    {label}: {done:,}/{total:,} ({done/total:.1%}) — "
          f"{elapsed/60:.1f}분 경과, 예상 잔여 약 {eta_sec/60:.1f}분")


def _should_report(last_print: list, done: int, total: int, min_interval: float = 3.0) -> bool:
    now = time.time()
    if done == total or now - last_print[0] >= min_interval:
        last_print[0] = now
        return True
    return False


def embed(texts: list[str], label: str = "", workers: int = 1) -> np.ndarray:
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    t0 = time.time()
    n = len(texts)
    vecs: list = [None] * n

    if workers <= 1 or n <= 1:
        last_print = [t0]
        for i, t in enumerate(texts):
            vecs[i] = _embed_one(t)
            if label and _should_report(last_print, i + 1, n):
                _print_progress(label, i + 1, n, t0)
    else:
        last_print = [t0]
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_embed_one, t): i for i, t in enumerate(texts)}
            for fut in as_completed(futures):
                i = futures[fut]
                vecs[i] = fut.result()
                done += 1
                if label and _should_report(last_print, done, n):
                    _print_progress(label, done, n, t0)

    if any(v is None for v in vecs):
        raise RuntimeError("embed(): 일부 텍스트 임베딩 실패(타임아웃) — "
                           "대량 문서처리에는 embed_chunks() 사용")
    m = np.array(vecs, dtype=np.float32)
    return m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-12)


def embed_chunks(chunks: list[dict], workers: int = 1,
                 label: str = "") -> tuple[list[dict], np.ndarray]:
    if not chunks:
        return [], np.zeros((0, 0), dtype=np.float32)
    texts = [PREFIX_DOC + c["text"] for c in chunks]
    t0 = time.time()
    n = len(texts)
    results: list = [None] * n

    if workers <= 1 or n <= 1:
        last_print = [t0]
        for i, t in enumerate(texts):
            results[i] = _embed_one(t)
            if label and _should_report(last_print, i + 1, n):
                _print_progress(label, i + 1, n, t0)
    else:
        last_print = [t0]
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_embed_one, t): i for i, t in enumerate(texts)}
            for fut in as_completed(futures):
                i = futures[fut]
                results[i] = fut.result()
                done += 1
                if label and _should_report(last_print, done, n):
                    _print_progress(label, done, n, t0)

    ok_chunks, ok_vecs = [], []
    for c, v in zip(chunks, results):
        if v is None:
            preview = c["text"][:40].replace("\n", " ")
            print(f"    ⚠️ 경고: {c['source']} 조각 임베딩 실패(타임아웃, 깨진 "
                  f"디코딩 의심) — 건너뜀: {preview!r}...")
        else:
            ok_chunks.append(c)
            ok_vecs.append(v)

    if not ok_vecs:
        return [], np.zeros((0, 0), dtype=np.float32)
    m = np.array(ok_vecs, dtype=np.float32)
    m = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-12)
    return ok_chunks, m

# ── [DISKINDEX] SQLite+sqlite-vec 저장소 ─────────────────────────────────────

_DB_LOCK = threading.Lock()
_SQL_BATCH = 500

def _gen_b_path(primary: Path) -> Path:
    return primary.parent / f"{primary.stem}_b{primary.suffix}"

def _active_pointer_path(primary: Path) -> Path:
    return primary.parent / f"{primary.stem}.active"

def _read_active_gen(primary: Path) -> str:
    p = _active_pointer_path(primary)
    if p.exists():
        v = p.read_text(encoding="utf-8").strip()
        if v in ("a", "b"):
            return v
    return "a"

def _write_active_gen(primary: Path, gen: str) -> None:
    p = _active_pointer_path(primary)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(gen, encoding="utf-8")
    tmp.replace(p)

def _db_path_for_gen(primary: Path, gen: str) -> Path:
    return primary if gen == "a" else _gen_b_path(primary)

def _open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (name,)).fetchone()
    return row is not None

def _ensure_base_schema(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS file_meta "
                 "(rel TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS chunks "
                 "(rowid INTEGER PRIMARY KEY, source TEXT, category TEXT, text TEXT)")
    conn.commit()

def _ensure_vec_table(conn: sqlite3.Connection, dim: int) -> None:
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_items USING vec0(embedding float[{dim}])")
    conn.commit()

def _drop_all_tables(conn: sqlite3.Connection) -> None:
    for t in ("vec_items", "chunks", "file_meta", "meta"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()

def _read_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT key, value FROM meta").fetchall())

def _read_file_meta(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    return {rel: (size, mtime_ns) for rel, size, mtime_ns in
            conn.execute("SELECT rel, size, mtime_ns FROM file_meta").fetchall()}

def _next_rowid(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(rowid), -1) FROM chunks").fetchone()
    return row[0] + 1

def _delete_sources(conn: sqlite3.Connection, sources: list[str]) -> None:
    if not sources:
        return
    has_vec = _table_exists(conn, "vec_items")
    for i in range(0, len(sources), _SQL_BATCH):
        batch = sources[i:i + _SQL_BATCH]
        placeholders = ",".join("?" * len(batch))
        if has_vec:
            rowids = [r[0] for r in conn.execute(
                f"SELECT rowid FROM chunks WHERE source IN ({placeholders})", batch).fetchall()]
            if rowids:
                conn.executemany("DELETE FROM vec_items WHERE rowid=?", [(r,) for r in rowids])
        conn.execute(f"DELETE FROM chunks WHERE source IN ({placeholders})", batch)
        conn.execute(f"DELETE FROM file_meta WHERE rel IN ({placeholders})", batch)
    conn.commit()

def _migrate_npz_if_needed(conn: sqlite3.Connection, db_path: Path) -> None:
    if not CACHE_FILE.exists():
        return
    if _read_meta(conn):
        return
    z = np.load(CACHE_FILE, allow_pickle=True)
    old_chunks = list(z["chunks"])
    old_matrix = z["matrix"]
    old_files = dict(z["file_meta"].item())
    old_model = str(z.get("embed_model", ""))
    if not old_chunks or old_matrix.size == 0:
        return
    dim = old_matrix.shape[1]
    print(f"[마이그레이션] 기존 {CACHE_FILE.name} 발견 → {db_path.name}"
          f"(SQLite+sqlite-vec)로 1회 이전 중... (조각 {len(old_chunks)}개, 차원 {dim})")
    _ensure_vec_table(conn, dim)
    with conn:
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_model', ?)", (old_model,))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('dim', ?)", (str(dim),))
        conn.executemany("INSERT OR REPLACE INTO file_meta VALUES (?,?,?)",
                          [(rel, fp[0], fp[1]) for rel, fp in old_files.items()])
        for i, c in enumerate(old_chunks):
            conn.execute("INSERT INTO chunks (rowid, source, category, text) VALUES (?,?,?,?)",
                         (i, c["source"], c.get("category", "_root"), c["text"]))
            conn.execute("INSERT INTO vec_items (rowid, embedding) VALUES (?,?)",
                         (i, np.asarray(old_matrix[i], dtype=np.float32).tobytes()))
    print(f"[마이그레이션] 완료 — {CACHE_FILE.name}은 삭제하지 않고 그대로 "
          f"보존합니다(더 이상 읽지는 않음)")


class ChunkStore:
    def __init__(self, conn: sqlite3.Connection, count: int):
        self._conn = conn
        self._count = count

    def __len__(self) -> int:
        return self._count

    def __getitem__(self, rowid) -> dict:
        with _DB_LOCK:
            row = self._conn.execute(
                "SELECT source, category, text FROM chunks WHERE rowid=?",
                (int(rowid),)).fetchone()
        if row is None:
            raise KeyError(rowid)
        return {"source": row[0], "category": row[1], "text": row[2]}


def _finalize_index(conn: sqlite3.Connection, total_count: int,
                     db_path: Path, gen: str):
    chunks = ChunkStore(conn, total_count)

    def search(qv, k):
        if total_count == 0:
            return np.array([], dtype=np.int64), np.array([], dtype=np.float32)
        kk = min(k, total_count)
        with _DB_LOCK:
            rows = conn.execute(
                "SELECT rowid, distance FROM vec_items WHERE embedding MATCH ? AND k = ? "
                "ORDER BY distance",
                (np.asarray(qv, dtype=np.float32).tobytes(), kk)).fetchall()
        idx = np.array([r[0] for r in rows], dtype=np.int64)
        dist = np.array([r[1] for r in rows], dtype=np.float32)
        scores = 1.0 - (dist * dist) / 2.0
        return idx, scores

    print(f"[색인] sqlite-vec 적재: 벡터 {total_count}개 "
          f"(임베딩: llama-cpp-python 인프로세스, {EMBED_MODEL_PATH.name}) — "
          f"디스크 기반 저장 ({db_path.name}, 세대 {gen})")

    return chunks, search


def open_existing_index():
    active_gen = _read_active_gen(CACHE_DB)
    db_path = _db_path_for_gen(CACHE_DB, active_gen)

    if not db_path.exists():
        print(f"[색인] 경고: 색인이 없습니다({db_path.name}) — "
              f"rag_indexing.py --reindex를 먼저 실행하거나 RAG.REINDEX 신호를 보내세요")
        return ChunkStore(None, 0), lambda qv, k: (
            np.array([], dtype=np.int64), np.array([], dtype=np.float32))

    with _DB_LOCK:
        conn = _open_db(db_path)
        _ensure_base_schema(conn)
        total_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    return _finalize_index(conn, total_count, db_path, active_gen)


# ── [INC-1] 증분 색인 ────────────────────────────────────────────────────────

def build_index(force: bool = False):
    current = scan_files()

    with _DB_LOCK:
        active_gen = _read_active_gen(CACHE_DB)
        if force:
            target_gen = "b" if active_gen == "a" else "a"
        else:
            target_gen = active_gen
        db_path = _db_path_for_gen(CACHE_DB, target_gen)

        conn = _open_db(db_path)
        _ensure_base_schema(conn)

        if force:
            _drop_all_tables(conn)
            _ensure_base_schema(conn)
        else:
            _migrate_npz_if_needed(conn, db_path)

        meta = _read_meta(conn)
        old_model = meta.get("embed_model", "")
        dim = int(meta["dim"]) if "dim" in meta else 0
        old_files = _read_file_meta(conn)

        model_changed = old_model != EMBED_MODEL
        if model_changed and old_files:
            print(f"[색인] 임베딩 모델 변경 감지({old_model or '없음'} → "
                  f"{EMBED_MODEL}) — 전체 재색인")
        if model_changed:
            _drop_all_tables(conn)
            _ensure_base_schema(conn)
            old_files = {}
            dim = 0

        reuse_files = {} if (force or model_changed) else {
            rel: fp for rel, fp in current.items()
            if old_files.get(rel) == fp
        }
        changed_files = [rel for rel in current if rel not in reuse_files]
        removed = list(set(old_files) - set(current))

        if removed:
            print(f"[색인] 삭제/제외 감지: {len(removed)}개 파일 — 인덱스에서 제외")
            _delete_sources(conn, removed)

        if changed_files:
            dup_of = find_content_duplicates(changed_files)
            files_to_embed = [rel for rel in changed_files if rel not in dup_of]

            dedup_note = f", 콘텐츠 중복 {len(dup_of)}개 제외" if dup_of else ""
            print(f"[색인] 신규/변경 {len(changed_files)}개{dedup_note} "
                  f"→ 실제 처리 {len(files_to_embed)}개, 재사용 {len(reuse_files)}개 "
                  f"(전체 {len(current)}개 문서)")

            _delete_sources(conn, changed_files)

            warmed_up = False
            total_files = len(files_to_embed)
            total_new_chunks = 0
            cats: dict[str, int] = {}

            for i, rel in enumerate(files_to_embed, 1):
                fname = Path(rel).name
                print(f"[색인] [{i:,}/{total_files:,}] {fname} 처리 중")

                file_chunks = extract_file_chunks(resolve_path(rel), rel)
                ok_chunks, file_matrix = [], np.zeros((0, 0), dtype=np.float32)
                if file_chunks:
                    if not warmed_up:
                        print("[색인] 임베딩 모델 워밍업 시작...")
                        embed(["워밍업"], label="워밍업")
                        print("[색인] 워밍업 완료 — 임베딩 모델 응답 확인됨")
                        warmed_up = True
                    ok_chunks, file_matrix = embed_chunks(
                        file_chunks, workers=EMBED_WORKERS, label=f"    {fname}")

                with conn:
                    if ok_chunks:
                        if dim == 0:
                            dim = file_matrix.shape[1]
                            _ensure_vec_table(conn, dim)
                            conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_model', ?)", (EMBED_MODEL,))
                            conn.execute("INSERT OR REPLACE INTO meta VALUES ('dim', ?)", (str(dim),))
                        rid0 = _next_rowid(conn)
                        for j, c in enumerate(ok_chunks):
                            rid = rid0 + j
                            conn.execute(
                                "INSERT INTO chunks (rowid, source, category, text) VALUES (?,?,?,?)",
                                (rid, c["source"], c.get("category", "_root"), c["text"]))
                            conn.execute(
                                "INSERT INTO vec_items (rowid, embedding) VALUES (?,?)",
                                (rid, np.asarray(file_matrix[j], dtype=np.float32).tobytes()))
                        total_new_chunks += len(ok_chunks)
                        for c in ok_chunks:
                            cat = c.get("category", "_root")
                            cats[cat] = cats.get(cat, 0) + 1
                    fp = current[rel]
                    conn.execute("INSERT OR REPLACE INTO file_meta VALUES (?,?,?)",
                                 (rel, fp[0], fp[1]))

            if dup_of:
                with conn:
                    for rel in dup_of:
                        fp = current[rel]
                        conn.execute("INSERT OR REPLACE INTO file_meta VALUES (?,?,?)",
                                     (rel, fp[0], fp[1]))

            if total_new_chunks:
                print(f"[색인] 전체 완료 (신규/갱신 조각 {total_new_chunks:,}개, 차원 {dim})")
                print("[색인] 카테고리별 조각 수(이번 처리분): " +
                      ", ".join(f"{k}={v}" for k, v in sorted(cats.items(), key=lambda x: -x[1])[:10]))
        else:
            print(f"[색인] 변경 없음 — 캐시 그대로 사용")

        with conn:
            if dim:
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_model', ?)", (EMBED_MODEL,))
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('dim', ?)", (str(dim),))

        total_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

        if force:
            _write_active_gen(CACHE_DB, target_gen)

    return _finalize_index(conn, total_count, db_path, target_gen)

# ── 질의 루프 (CLI 단독 실행용) ───────────────────────────────────────────────

RAG_PROMPT = """당신은 문서 기반 질의응답 도우미입니다.
아래 [근거 문서] 조각들만 사용해 질문에 답하세요.

규칙:
- 근거에 없는 내용은 추측하지 말고 "제공된 문서에서 찾을 수 없습니다"라고 답하세요.
- 답변에 사용한 근거의 번호를 문장 끝에 [1] 형식으로 표기하세요.
- 간결하게 답하세요.

[근거 문서]
{context}

[질문]
{question}"""

def main():
    chunks, search = build_index(force="--reindex" in sys.argv)

    from langchain_anthropic import ChatAnthropic
    llm = ChatAnthropic(model="claude-haiku-4-5", temperature=0.0, max_tokens=512)

    print("\nRAG 질의 시작 (종료: 빈 줄 입력)")
    print("─" * 60)
    while True:
        try:
            q = input("\n질문> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            break

        qv = embed([PREFIX_QUERY + q])[0]
        idx, scores = search(qv, TOP_K)

        if len(idx) == 0:
            print("\n검색 결과가 없습니다 — 색인된 문서가 없습니다.")
            continue

        context = "\n\n".join(
            f"[{n + 1}] (출처: {chunks[int(i)]['source']})\n{chunks[int(i)]['text']}"
            for n, i in enumerate(idx))

        answer = llm.invoke(RAG_PROMPT.format(context=context, question=q))
        print("\n" + answer.content)

        print("\n[근거]")
        for n, (i, sc) in enumerate(zip(idx, scores)):
            c = chunks[int(i)]
            preview = c["text"][:60].replace("\n", " ")
            print(f"  [{n + 1}] [{c.get('category','_root')}] {c['source']} "
                  f"(유사도 {sc:.3f}) {preview}...")


if __name__ == "__main__":
    main()
