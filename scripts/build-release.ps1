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
