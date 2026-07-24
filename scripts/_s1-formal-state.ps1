#Requires -Version 7.0
# Private S1 shared helpers — dot-source only; not a public command surface.

# --- path helpers -----------------------------------------------------------
function Get-CanonicalPath ($Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { throw "Get-CanonicalPath: empty path" }
    $resolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    if ($resolved.Length -gt 3 -and $resolved.EndsWith([IO.Path]::DirectorySeparatorChar)) {
        $resolved = $resolved.TrimEnd([IO.Path]::DirectorySeparatorChar)
    }
    return $resolved
}

function Test-IsUnderPath ($Child, $Parent) {
    if ([string]::IsNullOrEmpty($Child) -or [string]::IsNullOrEmpty($Parent)) { return $false }
    $cf = [System.StringComparison]::InvariantCultureIgnoreCase
    if (-not $Child.StartsWith($Parent, $cf)) { return $false }
    if ($Child.Length -eq $Parent.Length) { return $true }
    $nextChar = $Child[$Parent.Length]
    return ($nextChar -eq [IO.Path]::DirectorySeparatorChar) -or ($nextChar -eq '/')
}

function Test-ReparseAncestor ($Path) {
    $current = $Path
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
            if ($item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { return $true }
        }
        $parent = Split-Path -Path $current -Parent
        if ([string]::IsNullOrEmpty($parent) -or $parent -eq $current) { break }
        $current = $parent
    }
    return $false
}

function Get-NearestExistingAncestor ($Path) {
    $current = $Path
    while ($current) {
        if (Test-Path -LiteralPath $current) { return $current }
        $parent = Split-Path -Path $current -Parent
        if ([string]::IsNullOrEmpty($parent) -or $parent -eq $current) { return $null }
        $current = $parent
    }
    return $null
}

function Get-PathLeaf ($Path) {
    $trimmed = $Path.TrimEnd([IO.Path]::DirectorySeparatorChar)
    $idx = $trimmed.LastIndexOf([IO.Path]::DirectorySeparatorChar)
    if ($idx -ge 0) { return $trimmed.Substring($idx + 1) }
    return $trimmed
}

# --- file-state helpers ----------------------------------------------------
function Get-FileState ($FilePath) {
    if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
        return @{ exists = $false; length = $null; sha256 = $null }
    }
    $item = Get-Item -LiteralPath $FilePath -Force
    $hash = (Get-FileHash -LiteralPath $FilePath -Algorithm SHA256).Hash
    return @{ exists = $true; length = $item.Length; sha256 = $hash }
}

$script:S1_EXPECTED_KEYS = @(
    "valuedashboard.duckdb", "valuedashboard.sqlite",
    "valuedashboard.duckdb.wal", "valuedashboard.sqlite-wal",
    "valuedashboard.sqlite-shm"
)

function Get-FormalFileStates ($formalRoot) {
    $result = [ordered]@{}
    foreach ($rel in $script:S1_EXPECTED_KEYS) {
        $result[$rel] = Get-FileState (Join-Path $formalRoot $rel)
    }
    return $result
}

# --- evidence I/O helpers --------------------------------------------------
function Write-EvidenceJson ($jsonObj, $outFile) {
    $parent = Split-Path -Path $outFile -Parent
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $json = $jsonObj | ConvertTo-Json -Depth 10
    Set-Content -LiteralPath $outFile -Value $json -Encoding utf8
    Write-Output $json
}

function Write-HostError { param([string]$Message) Write-Host "[ERROR] $Message" -ForegroundColor Red }

function Write-FailureEvidence ($PhaseName, $Message, $OutDir, $CanonRoot) {
    $failObj = [ordered]@{
        schema_version   = 1
        phase            = $PhaseName
        timestamp        = (Get-Date).ToString("o")
        outcome          = "FAILURE"
        message          = $Message
        formal_data_root = $CanonRoot
    }
    try {
        if (-not (Test-Path -LiteralPath $OutDir)) {
            New-Item -ItemType Directory -Path $OutDir -Force -ErrorAction SilentlyContinue | Out-Null
        }
        $failFile = Join-Path $OutDir "$($PhaseName.ToLowerInvariant())-failure.json"
        $failObj | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $failFile -Encoding utf8 -ErrorAction SilentlyContinue
    } catch { }
}

# --- capture-failure evidence (for After phase failures) ------------------
function Write-CaptureFailureJson ($resolvedFdr, $exitCode, $runDir) {
    $failReport = [ordered]@{
        schema_version   = 1
        phase            = "After"
        outcome          = "FAILURE"
        timestamp        = (Get-Date).ToString("o")
        formal_data_root = $resolvedFdr
        capture_failed   = $true
        preflight_exit   = $exitCode
    }
    try {
        $failFile = Join-Path $runDir "capture-failure.json"
        $parent = Split-Path -Path $failFile -Parent
        if (-not (Test-Path -LiteralPath $parent)) {
            New-Item -ItemType Directory -Path $parent -Force -ErrorAction SilentlyContinue | Out-Null
        }
        $failReport | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $failFile -Encoding utf8 -ErrorAction SilentlyContinue
    } catch { }
}

# --- schema validation -----------------------------------------------------
$script:S1_SUCCESS_PHASES = @("Before", "After")

function Test-EvidenceSchema ($obj, $expectedPhase) {
    $errors = @()
    if (-not $obj) { return @("evidence object is null") }
    if ($obj.PSObject.Properties.Name -notcontains "schema_version" -or $obj.schema_version -ne 1) {
        $errors += "missing or wrong schema_version (expected 1)"
    }
    if ($obj.PSObject.Properties.Name -notcontains "phase") {
        $errors += "missing phase"
    } elseif ($expectedPhase -and $obj.phase -ne $expectedPhase) {
        $errors += "phase mismatch: expected '$expectedPhase', got '$($obj.phase)'"
    } elseif ($obj.phase -notin $script:S1_SUCCESS_PHASES) {
        $errors += "unexpected phase value: '$($obj.phase)'"
    }
    # Reject failure evidence in success path
    if ($obj.PSObject.Properties.Name -contains "outcome" -and $obj.outcome -eq "FAILURE") {
        $errors += "failure evidence passed as success input (outcome=FAILURE)"
    }
    if ($obj.PSObject.Properties.Name -notcontains "formal_data_root" -or [string]::IsNullOrEmpty($obj.formal_data_root)) {
        $errors += "missing or empty formal_data_root"
    }
    if ($obj.PSObject.Properties.Name -notcontains "files") {
        $errors += "missing files object"
    } else {
        $fileKeys = @($obj.files.PSObject.Properties.Name)
        foreach ($k in $script:S1_EXPECTED_KEYS) {
            if ($k -notin $fileKeys) { $errors += "missing expected file key: $k" }
        }
        $unexpected = $fileKeys | Where-Object { $_ -notin $script:S1_EXPECTED_KEYS }
        foreach ($uk in $unexpected) { $errors += "unexpected file key: $uk" }
    }
    return $errors
}

# --- comparator ------------------------------------------------------------
function Compare-FormalState ($beforeObj, $afterObj, $runDir) {
    # Validate schema of both inputs
    $beforeErrors = Test-EvidenceSchema $beforeObj "Before"
    $afterErrors  = Test-EvidenceSchema $afterObj "After"
    $allSchemaErrors = $beforeErrors + $afterErrors | Select-Object -Unique
    if ($allSchemaErrors.Count -gt 0) {
        foreach ($e in $allSchemaErrors) { Write-Host "  [SCHEMA] $e" -ForegroundColor Red }
        Write-Host "[FATAL] Evidence schema validation failed!" -ForegroundColor Red
        $outFile = Join-Path $runDir "delta-report.json"
        $parent = Split-Path -Path $outFile -Parent
        if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        [ordered]@{ compared_at = (Get-Date).ToString("o"); delta_detected = $true; schema_errors = $allSchemaErrors } |
            ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $outFile -Encoding utf8
        return $true
    }

    $beforeFiles = $beforeObj.files; $afterFiles = $afterObj.files
    $beforeKeySet = @($beforeFiles.PSObject.Properties.Name)
    $afterKeySet  = @($afterFiles.PSObject.Properties.Name)
    $deltaDetected = $false

    # Formal root equality
    if ($beforeObj.formal_data_root -ne $afterObj.formal_data_root) {
        $deltaDetected = $true
        Write-Host "  [FORMAL_ROOT] MISMATCH: before='$($beforeObj.formal_data_root)' after='$($afterObj.formal_data_root)'" -ForegroundColor Red
    }

    $comparisons = [ordered]@{}
    foreach ($key in $script:S1_EXPECTED_KEYS) {
        $bExists = $key -in $beforeKeySet; $aExists = $key -in $afterKeySet
        if (-not $bExists -and -not $aExists) {
            $deltaDetected = $true
            $comparisons[$key] = [ordered]@{ before = $null; after = $null; match = $false; note = "missing from both" }
            Write-Host "  $key : DELTA (missing from both captures)" -ForegroundColor Red; continue
        }
        if (-not $bExists) {
            $deltaDetected = $true
            $comparisons[$key] = [ordered]@{ before = $null; after = $afterFiles.$key; match = $false; note = "missing from before" }
            Write-Host "  $key : DELTA (missing from before)" -ForegroundColor Red; continue
        }
        if (-not $aExists) {
            $deltaDetected = $true
            $comparisons[$key] = [ordered]@{ before = $beforeFiles.$key; after = $null; match = $false; note = "missing from after" }
            Write-Host "  $key : DELTA (missing from after)" -ForegroundColor Red; continue
        }
        $b = $beforeFiles.$key; $a = $afterFiles.$key
        $bE = if ($null -ne $b.exists) { [bool]$b.exists } else { $false }
        $aE = if ($null -ne $a.exists) { [bool]$a.exists } else { $false }
        $bL = if ($null -ne $b.length) { [int64]$b.length } else { $null }
        $aL = if ($null -ne $a.length) { [int64]$a.length } else { $null }
        $bH = if ($null -ne $b.sha256) { [string]$b.sha256 } else { $null }
        $aH = if ($null -ne $a.sha256) { [string]$a.sha256 } else { $null }
        $match = ($bE -eq $aE) -and ($bL -eq $aL) -and ($bH -eq $aH)
        if (-not $match) { $deltaDetected = $true }
        $comparisons[$key] = [ordered]@{ before = $b; after = $a; match = $match }
    }

    # Unexpected keys
    $allActual = @($beforeKeySet + $afterKeySet | Select-Object -Unique)
    foreach ($uk in ($allActual | Where-Object { $_ -notin $script:S1_EXPECTED_KEYS })) {
        $deltaDetected = $true
        $bV = if ($uk -in $beforeKeySet) { $beforeFiles.$uk } else { $null }
        $aV = if ($uk -in $afterKeySet) { $afterFiles.$uk } else { $null }
        $comparisons["__unexpected__$uk"] = [ordered]@{ before = $bV; after = $aV; match = $false; note = "unexpected key" }
        Write-Host "  [UNEXPECTED KEY] $uk" -ForegroundColor Red
    }

    $outName = if ($deltaDetected) { "delta-report.json" } else { "hash-evidence.json" }
    $outFile = Join-Path $runDir $outName
    if ($deltaDetected) { Write-Host "[FATAL] Formal-state delta detected!" -ForegroundColor Red }
    else { Write-Host "[OK] Formal-state unchanged." -ForegroundColor Green }

    foreach ($kv in $comparisons.GetEnumerator()) {
        $b = $kv.Value.before; $a = $kv.Value.after
        $s = if ($kv.Value.match) { "MATCH" } else { "DELTA" }
        $bE = if ($b -and $null -ne $b.exists) { $b.exists } else { $false }
        $aE = if ($a -and $null -ne $a.exists) { $a.exists } else { $false }
        $bL = if ($b -and $null -ne $b.length) { $b.length } else { "null" }
        $aL = if ($a -and $null -ne $a.length) { $a.length } else { "null" }
        $bH = if ($b -and $null -ne $b.sha256) { $b.sha256.Substring(0, [Math]::Min(16, $b.sha256.Length)) } else { "null" }
        $aH = if ($a -and $null -ne $a.sha256) { $a.sha256.Substring(0, [Math]::Min(16, $a.sha256.Length)) } else { "null" }
        Write-Host "  $($kv.Key) : $s   before=(exists=$bE len=$bL hash=$bH) after=(exists=$aE len=$aL hash=$aH)"
    }

    $parent = Split-Path -Path $outFile -Parent
    if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    [ordered]@{ compared_at = (Get-Date).ToString("o"); delta_detected = $deltaDetected; files = $comparisons } |
        ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $outFile -Encoding utf8
    return $deltaDetected
}
