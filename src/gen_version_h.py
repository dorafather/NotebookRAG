#!/usr/bin/env python3
"""
gen_version_h.py — [정보탭_버전관리] tray.exe(.rc VERSIONINFO)가 쓸
버전 헤더(tray-src/src/version.h)를 app_paths.NOTEBOOKRAG_VERSION(단일
진실 원천)에서 매 빌드마다 새로 생성한다. tray-src/build.bat이 rc.exe를
돌리기 전에 이 스크립트를 호출한다 — .rc/.h 어디에도 버전을 직접
하드코딩하지 않는다.

사용법: 이 파일(src/)에서 실행 — tray-src/src/version.h를 만든다.
  python gen_version_h.py
"""

from __future__ import annotations

from pathlib import Path

from app_paths import NOTEBOOKRAG_VERSION

_THIS_DIR = Path(__file__).parent
_OUT_PATH = _THIS_DIR.parent / "tray-src" / "src" / "version.h"


def main() -> None:
    parts = [int(p) for p in NOTEBOOKRAG_VERSION.split(".")]
    while len(parts) < 4:
        parts.append(0)
    csv = ",".join(str(p) for p in parts[:4])

    content = (
        "#pragma once\n"
        "// [정보탭_버전관리] 자동 생성 파일 — 직접 수정하지 말 것.\n"
        "// src/gen_version_h.py가 app_paths.NOTEBOOKRAG_VERSION에서 매 빌드마다\n"
        "// 다시 만든다.\n"
        f'#define NOTEBOOKRAG_VERSION_STR "{NOTEBOOKRAG_VERSION}"\n'
        f"#define NOTEBOOKRAG_VERSION_CSV {csv}\n"
    )
    _OUT_PATH.write_text(content, encoding="utf-8")
    print(f"생성됨: {_OUT_PATH} (버전 {NOTEBOOKRAG_VERSION})")


if __name__ == "__main__":
    main()
