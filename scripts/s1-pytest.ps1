#Requires -Version 7.0
<#
.SYNOPSIS S1 pytest wrapper — preflight gate, pytest, post-flight capture.
.DESCRIPTION PolicyOnly: fixed path-policy tests (--noconftest), never creates
 run root, no caller args.  Normal: unique external run root, --basetemp owned
 by wrapper, cleanup on success only.  PreflightOnly: Before/After/Compare QA
 seam, no Python.  CompareOnly: direct before/after evidence comparison by path.
Every invocation (except CompareOnly) sets VD_FORMAL_DATA_ROOT then restores
original env/caller-CWD on exit.
#>
[CmdletBinding()] param(
    [switch]$PolicyOnly, [string]$FormalDataRoot="", [string]$EvidenceDir="docs/evidence-s1",
    [switch]$PreflightOnly, [switch]$CompareOnly,
    [string]$BeforeEvidence="", [string]$AfterEvidence="",
    [Parameter(ValueFromRemainingArguments)][string[]]$PytestArgs
)
$ErrorActionPreference="Stop"; $InformationPreference="Continue"
$scriptRoot = Split-Path -Path $PSCommandPath -Parent
. (Join-Path $scriptRoot "_s1-formal-state.ps1")

# --- Mode validation (no env mutation) ------------------------------------
$incompat = ($PolicyOnly -and $PreflightOnly) -or ($PolicyOnly -and $CompareOnly) -or ($PreflightOnly -and $CompareOnly)
if ($incompat) { Write-HostError "Incompatible modes: use only one of -PolicyOnly, -PreflightOnly, -CompareOnly"; exit 1 }

# --- CompareOnly (no env mutation) ----------------------------------------
if ($CompareOnly) {
    if ([string]::IsNullOrEmpty($BeforeEvidence) -or [string]::IsNullOrEmpty($AfterEvidence)) { Write-HostError "CompareOnly requires -BeforeEvidence and -AfterEvidence"; exit 1 }
    if (-not (Test-Path $BeforeEvidence -PathType Leaf)) { Write-HostError "BeforeEvidence not found: $BeforeEvidence"; exit 1 }
    if (-not (Test-Path $AfterEvidence -PathType Leaf)) { Write-HostError "AfterEvidence not found: $AfterEvidence"; exit 1 }
    $b = Get-Content $BeforeEvidence -Raw -ErrorAction Stop | ConvertFrom-Json
    $a = Get-Content $AfterEvidence -Raw -ErrorAction Stop | ConvertFrom-Json
    $d = Compare-FormalState $b $a (Split-Path $BeforeEvidence -Parent)
    if ($d) { Write-Host "[CompareOnly] DELTA — exit 99" -ForegroundColor Red; exit 99 }
    Write-Host "[CompareOnly] No delta — exit 0" -ForegroundColor Green; exit 0
}

# --- Resolve paths --------------------------------------------------------
$scriptRepoRoot = Get-CanonicalPath (Join-Path $scriptRoot "..")
$repoOk = $true
$gitPath = Join-Path $scriptRepoRoot ".git"
if (-not (Test-Path $gitPath)) { Write-HostError "Repo root missing .git: $scriptRepoRoot"; $repoOk=$false }
else { $gi = Get-Item $gitPath -Force; if (-not ($gi.PSIsContainer -or (-not $gi.PSIsContainer -and $gi.Length -gt 0))) { Write-HostError ".git is invalid"; $repoOk=$false } }
if (-not (Test-Path (Join-Path $scriptRepoRoot "pyproject.toml") -PathType Leaf)) { Write-HostError "Missing pyproject.toml"; $repoOk=$false }
if (-not $repoOk) { exit 1 }
$policyTestPath = Join-Path $scriptRepoRoot "tests/regression/test_path_isolation.py"
if (-not (Test-Path $policyTestPath -PathType Leaf)) { Write-Host "[Wrapper] Warning: policy test not found: $policyTestPath" -ForegroundColor Yellow }
$runId = [Guid]::NewGuid().ToString("N").Substring(0,16)
$runDir = Join-Path (Get-CanonicalPath $EvidenceDir) $runId
$resolvedFdr = if ([string]::IsNullOrWhiteSpace($FormalDataRoot)) { Get-CanonicalPath (Join-Path $scriptRepoRoot "data") } else { Get-CanonicalPath $FormalDataRoot }

# ---- Env-mutating logic as a function so return unwinds to outer scope ----
function Invoke-S1PytestWrapper ($PolicyOnly, $PreflightOnly, $PytestArgs, $resolvedFdr, $runDir, $scriptRepoRoot, $policyTestPath, $PSScriptRootVal) {
    $env:VD_FORMAL_DATA_ROOT = $resolvedFdr

    # --- PreflightOnly ----------------------------------------------------
    if ($PreflightOnly) {
        Write-Host "[PreflightOnly] Before/After/Compare cycle — FormalDataRoot=$resolvedFdr"
        New-Item -ItemType Directory -Path $runDir -Force -ErrorAction Stop | Out-Null
        $beforeOut = & "$PSScriptRootVal/s1-path-preflight.ps1" -Phase Before -FormalDataRoot $resolvedFdr -EvidenceDir $runDir
        if ($LASTEXITCODE -ne 0) { Write-HostError "PreflightOnly Before failed (exit $LASTEXITCODE)"; return $LASTEXITCODE }
        $bo = $beforeOut | ConvertFrom-Json
        $afterOut = & "$PSScriptRootVal/s1-path-preflight.ps1" -Phase After -FormalDataRoot $resolvedFdr -EvidenceDir $runDir
        if ($LASTEXITCODE -ne 0) { Write-HostError "PreflightOnly After failed (exit $LASTEXITCODE)"; return $LASTEXITCODE }
        $ao = $afterOut | ConvertFrom-Json
            $delta = Compare-FormalState $bo $ao $runDir
            if ($delta) { return 99 } else { return 0 }
    }

    # --- Normal / PolicyOnly ---
    $ack = [Environment]::GetEnvironmentVariable("VD_FORMAL_ACK","Process")
    if (-not [string]::IsNullOrEmpty($ack)) { Write-HostError "VD_FORMAL_ACK present: '$ack'"; return 1 }

    $tmpBase = [System.IO.Path]::GetTempPath()
    $runRoot = Join-Path $tmpBase "vd-s1-$runId"
    if (Test-Path $runRoot) { Write-HostError "Run root exists: $runRoot"; return 1 }

    $env:VD_ENV="test"; $env:VD_TEST_RUN_ROOT=$runRoot
    $env:VD_DUCKDB_PATH=Join-Path $runRoot "valuedashboard.duckdb"
    $env:VD_SQLITE_PATH=Join-Path $runRoot "valuedashboard.sqlite"
    $env:VD_TEST_EVIDENCE_ROOT=$runDir
    New-Item -ItemType Directory -Path $runDir -Force -ErrorAction Stop | Out-Null

    if ($PolicyOnly) {
        if ($PytestArgs.Count -gt 0) { Write-HostError "PolicyOnly accepts no caller args"; return 1 }
        $effectiveArgs = @("--noconftest","-v","--tb=short",$policyTestPath)
    } else {
        $i=0; $bsRejected=$false
        while ($i -lt $PytestArgs.Count) { $a = $PytestArgs[$i]; if ($a -eq "--basetemp" -or $a -like "--basetemp=*") { $bsRejected=$true; break }; $i++ }
        if ($bsRejected) { Write-HostError "--basetemp forbidden"; return 1 }
        $effectiveArgs = @("--basetemp",(Join-Path $runRoot "pytest-tmp")) + $PytestArgs
    }

    # Preflight Before
    Write-Host "[Preflight] Before..."
    $beforeOut = & "$PSScriptRootVal/s1-path-preflight.ps1" -Phase Before -FormalDataRoot $resolvedFdr -EvidenceDir $runDir
    if ($LASTEXITCODE -ne 0) { Write-HostError "Before failed (exit $LASTEXITCODE)"; return $LASTEXITCODE }
    $bo = $beforeOut | ConvertFrom-Json

    # Inner lifecycle: run-root creation, pytest, After capture
    $pytestExit=98; $afterCap=$false; $delta=$true; $origInner = $PWD.Path
    try {
        if (-not $PolicyOnly) {
            New-Item -ItemType Directory -Path $runRoot -ErrorAction Stop | Out-Null
            $cr=$runRoot
            while ($cr) {
                if (Test-Path $cr) { $it=Get-Item $cr -Force -ErrorAction SilentlyContinue
                    if ($it -and ($it.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { throw "Post-creation reparse: $runRoot" } }
                $p=Split-Path $cr -Parent; if ([string]::IsNullOrEmpty($p)-or$p-eq$cr){break}; $cr=$p
            }
        }
        $pythonExe = (Get-Command python -ErrorAction Stop).Source
        Set-Location $scriptRepoRoot
        Write-Host "[Wrapper] Running: & $pythonExe -m pytest @effectiveArgs (cwd=$scriptRepoRoot)"
        & $pythonExe -m pytest @effectiveArgs *>&1 | ForEach-Object { Write-Host $_ }
        $pytestExit = $LASTEXITCODE
    } catch { Write-HostError "Step failed: $_"; $pytestExit=98 }
    finally {
        if ($PWD.Path -ne $origInner) { Set-Location $origInner -ErrorAction SilentlyContinue }
        Write-Host "[Preflight] After..."
        $afterOut = & "$PSScriptRootVal/s1-path-preflight.ps1" -Phase After -FormalDataRoot $resolvedFdr -EvidenceDir $runDir
        if ($LASTEXITCODE -eq 0) { $ao=$afterOut|ConvertFrom-Json; $delta=Compare-FormalState $bo $ao $runDir; $afterCap=$true }
        else { Write-HostError "After capture failed (exit $LASTEXITCODE)"; Write-CaptureFailureJson $resolvedFdr $LASTEXITCODE $runDir }
    }

    # Exit resolution: delta 99 > capture fail 98 > pytest exit
    if (-not $afterCap) { return 98 }
    if ($delta) { return 99 }
    if ($pytestExit -eq 0 -and -not $PolicyOnly) { Remove-Item $runRoot -Recurse -ErrorAction Stop; Write-Host "[Wrapper] Run root removed" }
    return $pytestExit
}

# --- Outer lifecycle with single env/cwd restoration ----------------------
$envKeys = @("VD_FORMAL_DATA_ROOT","VD_ENV","VD_TEST_RUN_ROOT","VD_DUCKDB_PATH","VD_SQLITE_PATH","VD_TEST_EVIDENCE_ROOT")
$origEnv = @{}
foreach ($k in $envKeys) {
    $exists = Test-Path "Env:\$k" 2>$null
    $val = if ($exists) { [Environment]::GetEnvironmentVariable($k,"Process") } else { $null }
    $origEnv[$k] = @{ exists = $exists; value = $val }
}
$origCwd = $PWD.Path
$exitCode = 0

try {
    $exitCode = Invoke-S1PytestWrapper -PolicyOnly:$PolicyOnly -PreflightOnly:$PreflightOnly -PytestArgs $PytestArgs `
        -resolvedFdr $resolvedFdr -runDir $runDir -scriptRepoRoot $scriptRepoRoot -policyTestPath $policyTestPath -PSScriptRootVal $scriptRoot
}
finally {
    foreach ($k in $envKeys) {
        $info = $origEnv[$k]
        if ($info -and $info.exists) { [Environment]::SetEnvironmentVariable($k, $info.value, "Process") }
        else { Remove-Item "Env:\$k" -Force -ErrorAction SilentlyContinue }
    }
    if ($PWD.Path -ne $origCwd) { Set-Location $origCwd -ErrorAction SilentlyContinue }
}
exit $exitCode
