[CmdletBinding()]
param(
    [string]$OutputDirectory = "dist"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$frontendRoot = Join-Path $projectRoot "frontend"
$specPath = Join-Path $projectRoot "value-dashboard.spec"
$distPath = if ([System.IO.Path]::IsPathFullyQualified($OutputDirectory)) {
    [System.IO.Path]::GetFullPath($OutputDirectory)
}
else {
    [System.IO.Path]::GetFullPath((Join-Path $projectRoot $OutputDirectory))
}

if (-not (Test-Path -LiteralPath (Join-Path $frontendRoot "package-lock.json") -PathType Leaf)) {
    throw "A locked frontend dependency graph is required: frontend/package-lock.json"
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot "uv.lock") -PathType Leaf)) {
    throw "A locked Python dependency graph is required: uv.lock"
}

# Release artifacts are only meaningful after the same isolated gates that
# protect the formal profile have passed. Run this before any build output.
Push-Location $projectRoot
try {
    & (Join-Path $projectRoot "scripts\s1-pytest.ps1") "tests/regression" -q --no-header
    if ($LASTEXITCODE -ne 0) { throw "Isolated Python regression gate failed" }
}
finally {
    Pop-Location
}

Push-Location $frontendRoot
try {
    & npm ci
    if ($LASTEXITCODE -ne 0) { throw "Frontend dependency installation failed" }
    & npm run lint
    if ($LASTEXITCODE -ne 0) { throw "Frontend lint gate failed" }
    & npm run test
    if ($LASTEXITCODE -ne 0) { throw "Frontend contract-test gate failed" }
    & npm run build
    if ($LASTEXITCODE -ne 0) { throw "Frontend build failed" }
}
finally {
    Pop-Location
}

& uv run --locked --extra release python -m PyInstaller --noconfirm --clean --distpath $distPath --workpath (Join-Path $projectRoot "build") $specPath
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

$releaseRoot = Join-Path $distPath "value-dashboard"
foreach ($launcher in @("start.bat", "vd.bat")) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $launcher) -Destination (Join-Path $releaseRoot $launcher) -Force
}
foreach ($required in @("value-dashboard.exe", "_internal\app\web\static\index.html", "_internal\config\default.yaml", "start.bat", "vd.bat")) {
    if (-not (Test-Path -LiteralPath (Join-Path $releaseRoot $required))) {
        throw "Release contract is incomplete: $required"
    }
}
foreach ($forbidden in @("data\valuedashboard.duckdb", "data\valuedashboard.sqlite")) {
    if (Test-Path -LiteralPath (Join-Path $releaseRoot $forbidden)) {
        throw "Release must not package formal data: $forbidden"
    }
}

# Real launch smoke test (P0-6): boot the release through start.bat and require
# a live /api/health endpoint. This exercises CMD parsing of the launcher (not
# just its text) and the actual frozen executable with an empty formal profile.
$healthPort = 8765
if (Get-NetTCPConnection -State Listen -LocalPort $healthPort -ErrorAction SilentlyContinue) {
    throw "Release smoke test cannot bind: port $healthPort is already in use. Stop the dev server and retry."
}
$smokeProc = Start-Process -FilePath "cmd.exe" -ArgumentList "/d", "/c", "start.bat" -WorkingDirectory $releaseRoot -PassThru -WindowStyle Hidden
try {
    $healthy = $false
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
        Start-Sleep -Seconds 1
        if ($smokeProc.HasExited) { break }
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:$healthPort/api/health" -TimeoutSec 2 -UseBasicParsing
            if ($response.StatusCode -eq 200) { $healthy = $true; break }
        }
        catch { }
    }
    if (-not $healthy) {
        $logPath = Join-Path $releaseRoot "data\logs\start.log"
        $logTail = if (Test-Path -LiteralPath $logPath) {
            (Get-Content -LiteralPath $logPath -Tail 30) -join "`n"
        }
        else {
            "(no start.log; launcher did not reach the packaged branch)"
        }
        throw "Release smoke test failed: /api/health unreachable within 120s.`n$logTail"
    }
}
finally {
    if (-not $smokeProc.HasExited) {
        & taskkill /T /F /PID $smokeProc.Id 2>$null | Out-Null
    }
    # taskkill 返回后 EXE 进程可能仍在释放文件句柄（start.log 被占用），
    # 立即删除 data\ 会抛错并使后续 forbidden 检查被跳过（P1 清理竞态）。
    # 等待进程树退出并轮询删除，避免污染发行包/CI 误报。
    $processExited = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        $exeProcesses = @(Get-Process -Name "value-dashboard" -ErrorAction SilentlyContinue)
        if ($exeProcesses.Count -eq 0) {
            $processExited = $true
            break
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $processExited) {
        throw "Release smoke cleanup failed: value-dashboard process did not exit after taskkill"
    }
}
# The smoke boot initializes an empty formal profile; remove it so the release
# artifact stays free of mutable data. 句柄释放可能有延迟，重试删除。
$smokeData = Join-Path $releaseRoot "data"
if (Test-Path -LiteralPath $smokeData) {
    $removed = $false
    for ($attempt = 0; $attempt -lt 20 -and -not $removed; $attempt++) {
        try {
            Remove-Item -LiteralPath $smokeData -Recurse -Force -ErrorAction Stop
            $removed = $true
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $removed) {
        throw "Release smoke cleanup failed: $smokeData is still locked after process exit"
    }
}
foreach ($forbidden in @("data\valuedashboard.duckdb", "data\valuedashboard.sqlite")) {
    if (Test-Path -LiteralPath (Join-Path $releaseRoot $forbidden)) {
        throw "Release must not package formal data: $forbidden"
    }
}
