# run_notebookrag.ps1 — notebookrag_main (NotebookRAG 통합 HTTP: 검색 /rag/*, 색인기 /indexer/*, /health)
# [티켓 C-0] run_rag_serve.ps1을 대체 — rag_serve.py 단독 대신 검색+색인기를
# 한 프로세스(RAG_HTTP_PORT 하나)로 통합한 notebookrag_main.py를 띄운다.

$Host.UI.RawUI.WindowTitle = "notebookrag (NotebookRAG HTTP)"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$env:PYTHONIOENCODING = "utf-8"
$env:RAG_HTTP_HOST    = "127.0.0.1"
$env:RAG_HTTP_PORT    = "8420"

# DEV-ONLY: 배포판 자체 색인 DB가 아직 없어 개발 원본 DB를 임시로 참조.
# rag_index.db는 개인 문서 실데이터라 배포 폴더에는 의도적으로 안 포함
# (README_RELEASE.md 참고). 실제 배포 시 이 줄을 지우거나
# %APPDATA%\NotebookRAG 쪽 경로로 바꿀 것.
$env:RAG_DATA_DIR = "C:\changwoon\개인자료\DSL\pm2-list\6. rag-ra"

$srcDir = Join-Path (Split-Path $PSScriptRoot -Parent) "src"
Push-Location $srcDir
try {
    & "C:\Python314\python.exe" notebookrag_main.py
}
finally {
    Pop-Location
}

Read-Host "Press Enter to exit"
