param(
    [string]$VenvPath = "D:\openai-runtime\KnowledgeBrowser-build",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [Console]::OutputEncoding
$ProjectDir = [System.IO.Path]::GetFullPath($PSScriptRoot)
$VenvPath = [System.IO.Path]::GetFullPath($VenvPath)
$Python = Join-Path $VenvPath "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    if ($SkipInstall) { throw "构建环境不存在: $VenvPath" }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $VenvPath) | Out-Null
    python -m venv $VenvPath
}

if (-not $SkipInstall) {
    $env:PIP_CACHE_DIR = "D:\pip-cache"
    & $Python -m pip install --cache-dir $env:PIP_CACHE_DIR -r (Join-Path $ProjectDir "requirements-browser-build.txt")
    & $Python -c "import pywintypes"
    if ($LASTEXITCODE -ne 0) {
        & $Python -m pip install --ignore-installed --no-deps --cache-dir $env:PIP_CACHE_DIR pywin32
    }
}

$BuildTarget = [System.IO.Path]::GetFullPath((Join-Path $ProjectDir "build\KnowledgeBrowser"))
$DistTarget = [System.IO.Path]::GetFullPath((Join-Path $ProjectDir "dist\KnowledgeBrowser"))
foreach ($Target in @($BuildTarget, $DistTarget)) {
    if (-not $Target.StartsWith($ProjectDir, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝清理项目外路径: $Target"
    }
    if (Test-Path -LiteralPath $Target) {
        Remove-Item -LiteralPath $Target -Recurse -Force
    }
}

& $Python -m PyInstaller --noconfirm --clean --distpath (Join-Path $ProjectDir "dist") `
    --workpath (Join-Path $ProjectDir "build\KnowledgeBrowser") `
    (Join-Path $ProjectDir "build\KnowledgeBrowser.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败，退出码 $LASTEXITCODE" }

$Exe = Join-Path $ProjectDir "dist\KnowledgeBrowser\KnowledgeBrowser.exe"
$SmokeDb = Join-Path $env:TEMP ("knowledge-browser-smoke-" + [guid]::NewGuid().ToString() + ".sqlite3")
$SmokeGraph = Join-Path $env:TEMP ("content-graph-smoke-" + [guid]::NewGuid().ToString() + ".sqlite3")
$SmokeLog = $SmokeDb + ".smoke-error.log"
try {
    $Process = Start-Process -FilePath $Exe -ArgumentList @(
        "--smoke-test", "--relations", $SmokeDb, "--content-graph", $SmokeGraph
    ) `
        -Wait -PassThru -WindowStyle Hidden
    if ($Process.ExitCode -ne 0) {
        if (Test-Path -LiteralPath $SmokeLog) { Get-Content -LiteralPath $SmokeLog -Encoding UTF8 }
        throw "EXE smoke test 失败，退出码 $($Process.ExitCode)"
    }
} finally {
    Remove-Item -LiteralPath $SmokeDb -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $SmokeLog -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $SmokeGraph -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath ($SmokeGraph + "-wal") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath ($SmokeGraph + "-shm") -Force -ErrorAction SilentlyContinue
}
Write-Host "构建与 smoke test 通过: $Exe"
