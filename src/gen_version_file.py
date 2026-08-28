#!/usr/bin/env python3
"""
gen_version_file.py — [정보탭_버전관리] PyInstaller `version=` 인자로 쓸
버전 리소스 파일을 app_paths.NOTEBOOKRAG_VERSION(단일 진실 원천)에서 매
빌드마다 새로 생성한다. .spec 파일이 컴파일 시점(=Python 실행 시점)에
이 함수를 호출하므로, 버전 문자열을 .spec에 하드코딩하지 않는다.
"""

from __future__ import annotations

from pathlib import Path

from app_paths import NOTEBOOKRAG_VERSION


def _version_tuple(version: str) -> tuple[int, int, int, int]:
    parts = [int(p) for p in version.split(".")]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def write_version_file(out_path: str, *, file_description: str,
                        original_filename: str) -> None:
    filevers = _version_tuple(NOTEBOOKRAG_VERSION)
    content = f"""# UTF-8
#
# [정보탭_버전관리] 자동 생성 파일 — 직접 수정하지 말 것. app_paths.py의
# NOTEBOOKRAG_VERSION이 바뀌면 .spec 빌드 시점에 다시 생성된다.
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={filevers!r},
    prodvers={filevers!r},
    mask=0x3f,
    flags=0x0,
    OS=0x4,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        u'040904B0',
        [StringStruct(u'CompanyName', u'Point-I'),
        StringStruct(u'FileDescription', u'{file_description}'),
        StringStruct(u'FileVersion', u'{NOTEBOOKRAG_VERSION}'),
        StringStruct(u'InternalName', u'{original_filename}'),
        StringStruct(u'OriginalFilename', u'{original_filename}'),
        StringStruct(u'ProductName', u'NotebookRAG'),
        StringStruct(u'ProductVersion', u'{NOTEBOOKRAG_VERSION}')])
      ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"""
    Path(out_path).write_text(content, encoding="utf-8")
