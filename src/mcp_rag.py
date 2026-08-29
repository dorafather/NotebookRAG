#!/usr/bin/env python3
"""
mcp_rag.py — 설정 기반 SLEE ↔ MCP 브릿지 [배포 준비 사본]

mcp_tools.json에서 도구 정의를 로드한다 (tools.json/llm-ra와 동일 철학):
  - 도구 이름/description/파라미터 스키마/라우팅 → 전부 설정(JSON)
  - 새 RA 도구 추가 = mcp_tools.json에 항목 추가, 코드 무변경
  - dsl_key로 MCP 파라미터명(ASCII 필수 — Claude API 스키마 제약)과
    NATS PARAMS 키(한글 허용)를 분리

코드에 남는 단 한 가지: 응답 포맷터(RESPONSE_FORMATTERS).
  RA마다 응답 JSON 모양이 다르므로(배열형 vs 평면 키형 vs raw 청크형),
  "결과를 사람이 읽을 문장으로 조립"하는 규칙만은 이름으로 등록해 재사용한다.

[RELEASE 참고] rag_serve.py가 HTTP(FastAPI)로 전환됨에 따라, mcp_tools.json에
  "transport":"http" 필드를 추가해 nats_request()/http_request() 중 분기하는
  방식으로 전환 완료했다. nats-transport 코드(nats_request(), nats import)는
  하위호환을 위해 그대로 남겨뒀다 — REGISTRY 기반 라우팅 구조 자체는 무수정.

  ⚠️ RAG_HTTP_HOST/RAG_HTTP_PORT는 rag_serve.py와 이름·기본값이 반드시
  일치해야 하는 암묵적 계약이다 — 한쪽만 바꾸면 http 도구 호출이 깨진다.

준비:
  pip install "mcp<2" nats-py httpx --break-system-packages
  # [주의] mcp 2.0.0부터 Server.list_tools()/call_tool() 데코레이터 API가
  # 제거됨(2026-08-18 실제 설치해서 확인) — 반드시 1.x로 고정 설치할 것.
  mcp_tools.json 을 이 스크립트와 같은 디렉터리에 둘 것

Claude Code 등록:
  claude mcp add slee-bridge -s user -e NATS_SERVER=nats://127.0.0.1:4222 \\
      -- python3 /path/to/mcp_rag.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import nats
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

# 배포 레이아웃: bin/mcp-rag/mcp-rag.exe ↔ config/mcp_tools.json (형제 폴더)
# 개발 레이아웃: src/mcp_rag.py ↔ config/mcp_tools.json (형제 폴더)
# PyInstaller로 얼어붙힌(frozen) 실행 시 __file__ 기준 경로 해석이 개발
# 모드와 달라지므로, 두 경우 모두 "실행파일/스크립트가 있는 위치의 부모 밑
# config/"를 보도록 통일한다.
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent
CONFIG_FILE = Path(os.getenv("MCP_TOOLS_FILE", APP_DIR.parent / "config" / "mcp_tools.json"))
NATS_SERVER = os.getenv("NATS_SERVER", "nats://127.0.0.1:4222")
# rag_serve.py와 이름·기본값이 반드시 일치해야 하는 암묵적 계약 (모듈 docstring 참고)
RAG_HTTP_HOST = os.getenv("RAG_HTTP_HOST", "127.0.0.1")
RAG_HTTP_PORT = int(os.getenv("RAG_HTTP_PORT", "8420"))
# [버그 수정 — 2026-08-30] 15초는 콜드 임베딩 모델 로드(부팅/유휴 후 첫
# 요청)가 조금만 느려져도(디스크 경합 등) 바로 넘겨버렸다 — 실제로 재부팅
# 직후 검색이 13초 걸린 사례가 있었고, 예전엔(1.2.2 이전 우선순위 설정)
# 최대 33초까지도 걸렸다(1.2.3에서 완화했지만 0으로 만든 건 아님). 넘기면
# httpx.ReadTimeout이 예외 메시지 없이 잡혀서 "처리 실패: "(빈 문자열)로만
# 보이고, 상위 클라이언트가 재시도하면서 서버에 중복 요청까지 쌓일 수 있다
# (http_request() 참고). 여유를 넉넉히 둔다.
TIMEOUT_SEC = 30

TYPE_MAP = {"string": "string", "integer": "integer", "number": "number",
           "boolean": "boolean"}

# ── 응답 포맷터 레지스트리 (코드에 남는 유일한 부분) ─────────────────────────

def fmt_recommend_list(p: dict, args: dict) -> str:
    items = p.get("추천목록", [])
    lines = [f"{p.get('가입자명', args.get('msisdn',''))}님 추천 상품 ({len(items)}건, "
             f"모델 {p.get('모델버전', '?')}"
             f"{', 콜드스타트' if p.get('콜드스타트') == '1' else ''}):"]
    for i, item in enumerate(items, 1):
        lines.append(f"  {i}. {item.get('상품명','')} "
                     f"[{item.get('스타일','')}/{item.get('카테고리','')}] "
                     f"({item.get('상품코드','')})")
    return "\n".join(lines)

def fmt_flat_answer(p: dict, args: dict) -> str:
    sources = ", ".join(s.get("파일명", "") for s in p.get("출처", []))
    return f"{p.get('답변', '')}\n\n[출처: {sources}] (최고 유사도 {p.get('최고유사도', '?')})"

def fmt_raw_chunks(p: dict, args: dict) -> str:
    """rag-ra raw 응답(MCP 원문 청크 직접 반환):
    {"결과": [{"출처":.., "내용":.., "유사도":0.xx}, ...], "신뢰도충족": bool}
    Haiku 이중 요약을 건너뛴 원문 그대로다 — 문장으로 조합하는 건 이 응답을
    받는 MCP 클라이언트(Claude Code 등) 자신의 몫이다."""
    if not p.get("신뢰도충족", True) and p.get("안내"):
        return p["안내"]
    results = p.get("결과", [])
    lines = [f"검색 결과 {len(results)}건 (원문 청크, 요약 없음):"]
    for i, r in enumerate(results, 1):
        lines.append(f"\n[{i}]")
        lines.append(r.get("내용", ""))
        lines.append(f"출처: {r.get('출처', '')} / 유사도: {r.get('유사도', '?')}")
    return "\n".join(lines)

RESPONSE_FORMATTERS = {
    "recommend_list": fmt_recommend_list,
    "flat_answer": fmt_flat_answer,
    "raw_chunks": fmt_raw_chunks,
}

# ── 설정 로더 (fail-fast — tools.json 로더와 동일 원칙) ──────────────────────

def load_config(path: Path) -> dict[str, dict]:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    registry: dict[str, dict] = {}
    for t in cfg.get("tools", []):
        name = t.get("name", "")
        transport = t.get("transport", "nats")
        if not name or not t.get("description"):
            raise ValueError(f"{path}: 도구 정의 불완전 — {t}")
        if transport == "nats" and not t.get("action_event"):
            raise ValueError(f"{path}: 도구 정의 불완전 — {t}")
        if transport == "http" and not t.get("path"):
            raise ValueError(f"{path}: http 도구 '{name}'에 path가 없습니다")
        fmt = t.get("response_format", "")
        if fmt not in RESPONSE_FORMATTERS:
            raise ValueError(
                f"{path}: 도구 '{name}'의 response_format '{fmt}'가 "
                f"등록되지 않았습니다. 사용 가능: {list(RESPONSE_FORMATTERS)}")
        if name in registry:
            raise ValueError(f"{path}: 도구 이름 '{name}' 중복")
        registry[name] = t
    if not registry:
        raise ValueError(f"{path}: 도구가 하나도 없습니다")
    return registry


def build_input_schema(params: dict) -> dict:
    """MCP inputSchema(JSON Schema) 생성.
    [주의] 여기 키(pname)는 그대로 Claude API 도구 스키마의 property 이름이
    되므로 ASCII만 허용된다(^[a-zA-Z0-9_.-]{1,64}$) — 한글 필드가 필요하면
    mcp_tools.json에서 딱 dsl_key만 한글로 두고 파라미터 키(딕셔너리 키)
    자체는 반드시 ASCII로 작성할 것. (2026-08-18 실제 이 규칙을 어겨 API
    400 에러가 난 사례 있음 — mcp_tools.json 예시 참고.)"""
    properties, required = {}, []
    for pname, p in params.items():
        prop: dict[str, Any] = {
            "type": TYPE_MAP.get(p.get("type", "string"), "string"),
            "description": p.get("description", ""),
        }
        if "enum" in p:
            prop["enum"] = p["enum"]
        if "default" in p:
            prop["default"] = p["default"]
        properties[pname] = prop
        if p.get("required"):
            required.append(pname)
    return {"type": "object", "properties": properties, "required": required}

# ── NATS request-reply ───────────────────────────────────────────────────────

_nc: nats.aio.client.Client | None = None
_lock = asyncio.Lock()

async def get_nats():
    global _nc
    async with _lock:
        if _nc is None or _nc.is_closed:
            _nc = await nats.connect(NATS_SERVER, connect_timeout=5)
    return _nc


async def nats_request(topic: str, action_event: str, params: dict) -> dict:
    nc = await get_nats()
    inbox = nc.new_inbox()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()

    async def on_reply(msg):
        if not fut.done():
            fut.set_result(json.loads(msg.data.decode()))

    sub = await nc.subscribe(inbox, cb=on_reply)
    try:
        payload = {
            "SCE_EVENT": "ACTION", "ACTION_EVENT": action_event,
            "SCE_ID": f"IC-MCP-{uuid.uuid4().hex[:8]}",
            "AS_ID": f"MCP-{uuid.uuid4().hex[:8]}",
            "PARAMS": params,
        }
        await nc.publish(topic, json.dumps(payload, ensure_ascii=False).encode(), reply=inbox)
        response = await asyncio.wait_for(fut, timeout=TIMEOUT_SEC)
        return response.get("PARAMS", {})
    except asyncio.TimeoutError:
        return {"_error": f"{action_event} 응답 시간 초과 ({TIMEOUT_SEC}초)"}
    finally:
        await sub.unsubscribe()

# ── HTTP request (rag_serve.py 등 로컬 FastAPI 도구용) ───────────────────────

async def http_request(path: str, params: dict) -> tuple[int, dict]:
    """반환: (HTTP 상태코드, 파싱된 JSON 바디). 네트워크 오류(연결 실패,
    타임아웃 등) 시 (0, {"_error": ...}) — 예외를 여기서 흡수해야 MCP
    서버 자체가 죽지 않는다."""
    url = f"http://{RAG_HTTP_HOST}:{RAG_HTTP_PORT}{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as client:
            resp = await client.post(url, json=params)
        return resp.status_code, resp.json()
    except Exception as exc:
        return 0, {"_error": str(exc)}

# ── MCP 저수준 서버 — 설정에서 동적으로 list_tools/call_tool 구성 ────────────

REGISTRY = load_config(CONFIG_FILE)
server = Server("slee-bridge")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=name,
            description=t["description"],
            inputSchema=build_input_schema(t.get("params", {})),
        )
        for name, t in REGISTRY.items()
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    t = REGISTRY.get(name)
    if not t:
        return [types.TextContent(type="text", text=f"알 수 없는 도구: {name}")]

    call_params = {}
    for pname, p in t.get("params", {}).items():
        val = arguments.get(pname, p.get("default"))
        if val is None and p.get("required"):
            return [types.TextContent(
                type="text", text=f"필수 파라미터 누락: {pname}")]
        if val is not None:
            call_params[p.get("dsl_key", pname)] = str(val)

    transport = t.get("transport", "nats")
    if transport == "http":
        status, p = await http_request(t["path"], call_params)
        if status != 200:
            return [types.TextContent(
                type="text", text=f"처리 실패: {p.get('_error', status)}")]
        # result_field/reason_field 체크 없음 — "결과 없음"류 판단은 포맷터가
        # 응답 바디의 신뢰도충족/안내 필드로 직접 처리한다 (fmt_raw_chunks 참고).
    else:
        p = await nats_request(t["topic"], t["action_event"], call_params)
        if "_error" in p:
            return [types.TextContent(type="text", text=p["_error"])]
        result_ok = p.get(t["result_field"]) == "0"
        if not result_ok:
            reason = p.get(t["reason_field"], "알 수 없는 오류")
            if reason == "NoRelevantDoc":
                return [types.TextContent(type="text", text="관련 문서를 찾지 못했습니다.")]
            return [types.TextContent(type="text", text=f"처리 실패: {reason}")]

    formatter = RESPONSE_FORMATTERS[t["response_format"]]
    return [types.TextContent(type="text", text=formatter(p, arguments))]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"치명적 오류: {exc}", file=sys.stderr)
        sys.exit(1)
