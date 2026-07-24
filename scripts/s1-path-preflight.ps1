#Requires -Version 7.0
<#
.SYNOPSIS S1 Path Isolation Preflight — formal state capture and validation.
.DESCRIPTION Before: validates formal root, env, run-root constraints then
 captures 5-file state.  After: re-captures without env checks, no Python.
.PARAMETER Phase "Before" or "After"
.PARAMETER FormalDataRoot Defaults to $PSScriptRoot\..\data.
.PARAMETER EvidenceDir Default "docs/evidence-s1".
#>
[CmdletBinding()] param(
    [Parameter(Mandatory)] [ValidateSet("Before","After")] [string]$Phase,
    [string]$FormalDataRoot = "",
    [string]$EvidenceDir = "docs/evidence-s1"
)
$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Path $PSCommandPath -Parent
. (Join-Path $scriptRoot "_s1-formal-state.ps1")

$projectRoot = Get-CanonicalPath (Join-Path $scriptRoot "..")
$rawRoot = if ([string]::IsNullOrWhiteSpace($FormalDataRoot)) { Join-Path $projectRoot "data" } else { $FormalDataRoot }
$canonicalRoot = Get-CanonicalPath $rawRoot
$leaf = Get-PathLeaf $canonicalRoot
if ($leaf -ne "data") { Write-HostError "FormalDataRoot leaf must be 'data', got '$leaf'"; exit 1 }
$mainRepoRoot = Split-Path -Path $canonicalRoot -Parent
if ([string]::IsNullOrEmpty($mainRepoRoot)) { Write-HostError "Cannot derive main repo root"; exit 1 }

$evDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($EvidenceDir)
$phaseDir = if ($Phase -eq "Before") { "pre" } else { "post" }
$outDir = Join-Path $evDir $phaseDir

function Write-FailExit ($msg) { Write-HostError $msg; Write-FailureEvidence $Phase $msg $outDir $canonicalRoot; exit 1 }

# ===== BEFORE =====
if ($Phase -eq "Before") {
    # 1. Python process check
    $py = @(Get-Process -Name "python*" -ErrorAction SilentlyContinue)
    if ($py.Count -gt 0) {
        $diag = ($py | ForEach-Object { "$($_.ProcessName):$($_.Id)" }) -join ", "
        Write-FailExit "Python processes running — refuse to start: $diag"
    }
    # 2. Formal root exists, is directory, no reparse
    if (-not (Test-Path -LiteralPath $canonicalRoot)) { Write-FailExit "FormalDataRoot does not exist: $canonicalRoot" }
    if (-not (Test-Path -LiteralPath $canonicalRoot -PathType Container)) { Write-FailExit "FormalDataRoot not a directory: $canonicalRoot" }
    if (Test-ReparseAncestor $canonicalRoot) { Write-FailExit "FormalDataRoot or ancestor is reparse: $canonicalRoot" }
    # 3. Required files exist as ordinary non-reparse
    $missing = @(); $reparse = @()
    foreach ($rf in @("valuedashboard.duckdb","valuedashboard.sqlite")) {
        $absRf = Join-Path $canonicalRoot $rf
        if (-not (Test-Path -LiteralPath $absRf -PathType Leaf)) { $missing += $rf; continue }
        $item = Get-Item -LiteralPath $absRf -Force
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { $reparse += $rf }
    }
    if ($missing) { Write-FailExit "Required DB files missing: $($missing -join ',')" }
    if ($reparse) { Write-FailExit "Required DB files are reparse: $($reparse -join ',')" }
    # 4. Capture formal state
    try { $beforeFiles = Get-FormalFileStates $canonicalRoot }
    catch { Write-FailExit "Failed to capture file states: $_" }
    # 5. Environment checks
    $envErrors = @()
    $vdEnv = [Environment]::GetEnvironmentVariable("VD_ENV","Process")
    if ([string]::IsNullOrEmpty($vdEnv)) { $envErrors += "VD_ENV not set (must be 'test')" }
    elseif ($vdEnv -ne "test") { $envErrors += "VD_ENV must be 'test', got '$vdEnv'" }
    foreach ($var in @("VD_DUCKDB_PATH","VD_SQLITE_PATH","VD_TEST_RUN_ROOT")) {
        $v = [Environment]::GetEnvironmentVariable($var,"Process")
        if ([string]::IsNullOrEmpty($v)) { $envErrors += "$var is not set" }
        elseif (-not [IO.Path]::IsPathRooted($v)) { $envErrors += "$var not absolute: $v" }
    }
    $vdAck = [Environment]::GetEnvironmentVariable("VD_FORMAL_ACK","Process")
    if (-not [string]::IsNullOrEmpty($vdAck)) { $envErrors += "VD_FORMAL_ACK present (forbidden): '$vdAck'" }
    if ($envErrors) { foreach ($e in $envErrors) { Write-HostError "ENV: $e" }; Write-FailExit "Environment checks failed" }
    # 6. VD_FORMAL_DATA_ROOT env check
    $vdFdr = [Environment]::GetEnvironmentVariable("VD_FORMAL_DATA_ROOT","Process")
    if (-not [string]::IsNullOrEmpty($vdFdr)) {
        $rfdr = Get-CanonicalPath $vdFdr
        if (-not $rfdr.Equals($canonicalRoot, [System.StringComparison]::InvariantCultureIgnoreCase)) {
            Write-FailExit "VD_FORMAL_DATA_ROOT ($rfdr) != FormalDataRoot ($canonicalRoot)"
        }
    }
    # 7. Run-root validation
    $vdRunRoot = [Environment]::GetEnvironmentVariable("VD_TEST_RUN_ROOT","Process")
    $vdDuck = [Environment]::GetEnvironmentVariable("VD_DUCKDB_PATH","Process")
    $vdSql = [Environment]::GetEnvironmentVariable("VD_SQLITE_PATH","Process")
    $canonRunRoot = Get-CanonicalPath $vdRunRoot
    $canonDuck = Get-CanonicalPath $vdDuck; $canonSql = Get-CanonicalPath $vdSql
    $violations = @()
    foreach ($tp in @($canonRunRoot,$canonDuck,$canonSql)) {
        if (Test-IsUnderPath $tp $projectRoot) { $violations += "Under worktree: $tp" }
        if (Test-IsUnderPath $tp $mainRepoRoot) { $violations += "Under main repo: $tp" }
        if (Test-IsUnderPath $tp $canonicalRoot) { $violations += "Under formal root: $tp" }
    }
    if ($violations) { foreach ($v in $violations) { Write-HostError "PATH_VIOLATION: $v" }; Write-FailExit "Path violations" }
    $ep = Split-Path -Path $canonDuck -Parent; $esp = Split-Path -Path $canonSql -Parent
    $ci = [System.StringComparison]::InvariantCultureIgnoreCase
    if (-not $ep.Equals($canonRunRoot,$ci)) { Write-FailExit "DuckDB parent not run root" }
    if (-not $esp.Equals($canonRunRoot,$ci)) { Write-FailExit "SQLite parent not run root" }
    if (-not $ep.Equals($esp,$ci)) { Write-FailExit "DB paths not siblings" }
    if (Test-Path -LiteralPath $canonRunRoot) { Write-FailExit "Run root already exists (stale): $canonRunRoot" }
    $near = Get-NearestExistingAncestor $canonRunRoot
    if ([string]::IsNullOrEmpty($near)) { Write-FailExit "No ancestor for run root" }
    if (Test-ReparseAncestor $near) { Write-FailExit "Run root ancestor is reparse: $near" }
    # --- Success ---
    $evidence = [ordered]@{
        schema_version=1; phase="Before"; timestamp=(Get-Date).ToString("o")
        formal_data_root=$canonicalRoot; project_root=$projectRoot; main_repo_root=$mainRepoRoot
        files=$beforeFiles
        env_checked=@{VD_ENV=$vdEnv;VD_DUCKDB_PATH=$vdDuck;VD_SQLITE_PATH=$vdSql;VD_TEST_RUN_ROOT=$vdRunRoot;VD_FORMAL_ACK=if($vdAck){"present"}else{"absent"}}
    }
    Write-EvidenceJson $evidence (Join-Path $outDir "before-evidence.json")
    exit 0
}

# ===== AFTER =====
if ($Phase -eq "After") {
    if (-not (Test-Path -LiteralPath $canonicalRoot)) { Write-FailExit "FormalDataRoot vanished: $canonicalRoot" }
    try { $afterFiles = Get-FormalFileStates $canonicalRoot }
    catch { Write-FailExit "After capture failed: $_" }
    $evidence = [ordered]@{ schema_version=1; phase="After"; timestamp=(Get-Date).ToString("o"); formal_data_root=$canonicalRoot; files=$afterFiles }
    Write-EvidenceJson $evidence (Join-Path $outDir "after-evidence.json")
    exit 0
}
exit 99
