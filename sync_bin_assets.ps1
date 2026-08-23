# sync_bin_assets.ps1
# ------------------------------------------------------------------------
# 목적: config/, models/, onboarding/ (편집용 마스터 사본, 릴리즈 루트)를
#       bin/config/, bin/models/, bin/onboarding/ (실제 exe가 참조하는 사본)로
#       복사한다.
#
# 왜 필요한가: app_paths.get_install_dir()이 얼어붙은(frozen) exe 상태에서
#   실제로 가리키는 위치는 릴리즈 루트가 아니라 "bin/"이다(티켓 A에서 발견,
#   config/·models/에서 먼저 확인됐고, 2026-08-19 실사용 테스트에서
#   onboarding/도 같은 문제였다는 게 추가로 확인됨). 즉 notebookrag.exe/
#   mcp-rag.exe가 실제로 읽는 건 언제나 bin/ 밑의 사본이고, 릴리즈 루트의
#   세 폴더는 "편집하기 편한 마스터 사본"일 뿐이다.
#
# 사용법: config/, models/, onboarding/ 중 무엇이든 고친 뒤, 실제 exe로
#   테스트하기 전에 이 스크립트를 한 번 실행한다.
#     .\sync_bin_assets.ps1
#
# ⚠️ 이 스크립트는 "복사를 자동화"할 뿐, "언제 실행할지"는 아직 사람이
#   기억해야 한다(자동 트리거 없음) — 이후 build.bat들의 사전 단계로
#   엮는 것도 고려할 수 있으나 이번엔 범위 밖으로 남긴다.
#
# InstallShield 패키징 시 참고: 실제 배포판에는 릴리즈 루트의 세 마스터
#   폴더를 따로 담을 필요가 없다 — bin/ 안의 내용(이미 동기화된 상태)만
#   설치 패키지에 포함하면 된다. 마스터 사본은 순수 개발/빌드 시점
#   편의를 위한 것이다.
# ------------------------------------------------------------------------

$root = $PSScriptRoot
$pairs = @(
    @{ Src = Join-Path $root "config";     Dst = Join-Path $root "bin\config" },
    @{ Src = Join-Path $root "models";      Dst = Join-Path $root "bin\models" },
    @{ Src = Join-Path $root "onboarding";  Dst = Join-Path $root "bin\onboarding" }
)

$totalCopied = 0

foreach ($pair in $pairs) {
    $src = $pair.Src
    $dst = $pair.Dst

    if (-not (Test-Path $src)) {
        Write-Host "건너뜀: $src 없음"
        continue
    }

    if (-not (Test-Path $dst)) {
        New-Item -ItemType Directory -Path $dst -Force | Out-Null
        Write-Host "생성: $dst"
    }

    $files = Get-ChildItem -Path $src -File
    if ($files.Count -eq 0) {
        Write-Host "정보: $src 에 파일이 없음 (건너뜀 — 예: models/는 보통 빈 상태)"
        continue
    }

    foreach ($f in $files) {
        $destPath = Join-Path $dst $f.Name
        Copy-Item -Path $f.FullName -Destination $destPath -Force
        Write-Host "복사: $($f.Name) -> $dst"
        $totalCopied++
    }
}

Write-Host ""
Write-Host "완료 — 총 $totalCopied 개 파일 동기화됨"
Write-Host "(config/, models/, onboarding/ -> bin/config/, bin/models/, bin/onboarding/)"
