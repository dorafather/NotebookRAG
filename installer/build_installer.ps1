# build_installer.ps1 — [티켓 I] NotebookRAG 설치 프로그램 빌드
# ------------------------------------------------------------------------
# 순서:
#   1. sync_bin_assets.ps1 실행 — 릴리즈 루트 마스터(config/models/onboarding)
#      → bin/ 동기화 (bin/이 항상 최신 상태여야 패키징이 정확함).
#   2. bin\config\settings.json.template을 installer\staging\config\ 로
#      복사한 뒤, MODEL_DOWNLOAD_URL/MODEL_SHA256만 실제 값으로 채워
#      넣는다 — model_downloader.py 상단 docstring에 적힌 검증된 값
#      (공개/비gated, MIT 라이선스, ggml-org/bge-m3-Q8_0-GGUF).
#      ⚠️ bin\config\settings.json.template 자기 자신은 절대 건드리지
#      않는다 — 정책상 마스터 사본은 계속 빈 값으로 유지해야 함.
#   3. src\app_paths.py의 NOTEBOOKRAG_VERSION(단일 진실 원천)을 읽어서
#      ISCC "/DMyAppVersion=..."로 넘긴다 — .iss 파일 자체엔 버전을
#      하드코딩하지 않는다([정보탭_버전관리]).
#   4. NotebookRAG.iss에 UTF-8 BOM을 강제(한글 깨짐 방지) 후 ISCC로 컴파일.
#
# 사용법: installer\ 안에서 실행
#   .\build_installer.ps1
# ------------------------------------------------------------------------

$ErrorActionPreference = "Stop"
$installerDir = $PSScriptRoot
$root = Split-Path $installerDir -Parent

Write-Host "[1/4] bin/ 자산 동기화 (sync_bin_assets.ps1)"
& (Join-Path $root "sync_bin_assets.ps1")

Write-Host "[2/4] 배포용 settings.json.template 생성 (모델 다운로드 값 주입)"
$srcTemplate = Join-Path $root "bin\config\settings.json.template"
$stagingConfigDir = Join-Path $installerDir "staging\config"
New-Item -ItemType Directory -Path $stagingConfigDir -Force | Out-Null
$dstTemplate = Join-Path $stagingConfigDir "settings.json.template"

if (-not (Test-Path $srcTemplate)) {
    throw "$srcTemplate 없음 — sync_bin_assets.ps1이 먼저 실행됐는지 확인할 것"
}

# model_downloader.py 상단 docstring 조사 결과(2026-08-20 확인, 교차 검증됨).
# 이 값이 바뀌면 model_downloader.py의 docstring도 같이 갱신할 것.
$modelUrl = "https://huggingface.co/ggml-org/bge-m3-Q8_0-GGUF/resolve/main/bge-m3-q8_0.gguf"
$modelSha256 = "aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173"

$json = Get-Content -Raw -Encoding UTF8 $srcTemplate
$before = $json
$json = $json -replace '"MODEL_DOWNLOAD_URL":\s*""', "`"MODEL_DOWNLOAD_URL`": `"$modelUrl`""
$json = $json -replace '"MODEL_SHA256":\s*""', "`"MODEL_SHA256`": `"$modelSha256`""
if ($json -eq $before) {
    Write-Warning "MODEL_DOWNLOAD_URL/MODEL_SHA256 치환이 실제로 안 일어났음 — 이미 값이 채워져 있거나 템플릿 형식이 바뀐 건 아닌지 확인할 것"
}
[System.IO.File]::WriteAllText($dstTemplate, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "  -> $dstTemplate 생성됨 (MODEL_DOWNLOAD_URL/MODEL_SHA256 주입)"

Write-Host "[3/4] 버전 확인 (src/app_paths.py의 NOTEBOOKRAG_VERSION)"
$srcDir = Join-Path $root "src"
$appVersion = & python -c "import sys; sys.path.insert(0, r'$srcDir'); from app_paths import NOTEBOOKRAG_VERSION; print(NOTEBOOKRAG_VERSION)"
if ($LASTEXITCODE -ne 0 -or -not $appVersion) {
    throw "app_paths.NOTEBOOKRAG_VERSION을 못 읽음 — python 환경/경로 확인할 것"
}
$appVersion = $appVersion.Trim()
Write-Host "  -> 버전: $appVersion"

Write-Host "[4/4] Inno Setup 컴파일"
$issPath = Join-Path $installerDir "NotebookRAG.iss"
# ISCC가 한글을 깨지지 않게 읽으려면 UTF-8 BOM이 필요 — 매 빌드마다 강제.
$issText = Get-Content -Raw -Encoding UTF8 $issPath
[System.IO.File]::WriteAllText($issPath, $issText, (New-Object System.Text.UTF8Encoding($true)))

$iscc = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) {
    $iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
}
if (-not (Test-Path $iscc)) {
    throw "ISCC.exe를 찾을 수 없음 — Inno Setup 6가 설치돼 있는지 확인할 것"
}

& $iscc "/DMyAppVersion=$appVersion" $issPath
if ($LASTEXITCODE -ne 0) {
    throw "ISCC 컴파일 실패 (종료 코드 $LASTEXITCODE)"
}

$outExe = Join-Path $installerDir "output\NotebookRAG_Setup.exe"
if (Test-Path $outExe) {
    $sizeMb = [Math]::Round((Get-Item $outExe).Length / 1MB, 1)
    Write-Host ""
    Write-Host "완료 — $outExe ($sizeMb MB)"
} else {
    throw "컴파일은 성공했다는데 출력 파일이 안 보임: $outExe"
}
