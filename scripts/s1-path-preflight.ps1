[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Before", "After")]
    [string]$Phase,

    [string]$EvidenceDir = "docs/evidence/evidence-s1",

    [string]$CaptureName,

    [string]$EvidenceToken
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$script:ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$script:FormalDataRoot = Join-Path $script:ProjectRoot "data"
$script:FormalFiles = [ordered]@{
    formal_duckdb = "valuedashboard.duckdb"
    formal_sqlite = "valuedashboard.sqlite"
    duckdb_wal = "valuedashboard.duckdb.wal"
    sqlite_wal = "valuedashboard.sqlite-wal"
    sqlite_shm = "valuedashboard.sqlite-shm"
}

function Test-PathEqual {
    param([string]$Left, [string]$Right)

    return [string]::Equals(
        [System.IO.Path]::TrimEndingDirectorySeparator([System.IO.Path]::GetFullPath($Left)),
        [System.IO.Path]::TrimEndingDirectorySeparator([System.IO.Path]::GetFullPath($Right)),
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Test-IsDescendantOrEqual {
    param([string]$Candidate, [string]$Root)

    $candidateFull = [System.IO.Path]::TrimEndingDirectorySeparator(
        [System.IO.Path]::GetFullPath($Candidate)
    )
    $rootFull = [System.IO.Path]::TrimEndingDirectorySeparator(
        [System.IO.Path]::GetFullPath($Root)
    )
    if ([string]::Equals($candidateFull, $rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }

    $prefix = $rootFull + [System.IO.Path]::DirectorySeparatorChar
    return $candidateFull.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-SafePathSyntax {
    param([string]$Path, [string]$Name)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "$Name must not be empty"
    }
    if (-not [System.IO.Path]::IsPathFullyQualified($Path)) {
        throw "$Name must be a fully qualified path: $Path"
    }
    if ($Path.StartsWith("\\?\") -or $Path.StartsWith("\\.\") -or
        $Path.StartsWith("\\") -or $Path.StartsWith("//")) {
        throw "$Name must not use a device or UNC path: $Path"
    }
    if ($Path -match '[<>"|?*\x00-\x1F]') {
        throw "$Name contains invalid Windows path characters: $Path"
    }
    if (($Path.Length -gt 2) -and $Path.Substring(2).Contains(":")) {
        throw "$Name must not contain an alternate data stream: $Path"
    }

    $reserved = "^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$"
    foreach ($component in ($Path -split "[\\/]")) {
        if ([string]::IsNullOrEmpty($component) -or $component -match "^[A-Za-z]:$") {
            continue
        }
        if ($component.EndsWith(".") -or $component.EndsWith(" ")) {
            throw "$Name contains a trailing-dot/space component: $component"
        }
        if ($component.Contains("~")) {
            throw "$Name contains a short-name alias marker: $component"
        }
        if ($component -match $reserved) {
            throw "$Name contains a Windows reserved name: $component"
        }
    }
}

function Assert-SafeExistingAncestorChain {
    param([string]$Path)

    $cursor = [System.IO.Path]::GetFullPath($Path)
    while (-not (Test-Path -LiteralPath $cursor)) {
        $parent = [System.IO.Directory]::GetParent($cursor)
        if ($null -eq $parent) {
            throw "No existing ancestor found for path: $Path"
        }
        $cursor = $parent.FullName
    }

    while ($true) {
        $item = Get-Item -LiteralPath $cursor -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Reparse point is forbidden in path ancestry: $cursor"
        }
        $parent = [System.IO.Directory]::GetParent($cursor)
        if ($null -eq $parent) {
            break
        }
        $cursor = $parent.FullName
    }
}

function Get-PythonProcessIds {
    return @(
        Get-Process -ErrorAction Stop |
            Where-Object { $_.ProcessName -match "^(python|pythonw|py|pytest)([0-9.]*)?$" } |
            ForEach-Object { $_.Id } |
            Sort-Object
    )
}

function Get-FormalStateOnce {
    $inventory = [ordered]@{}
    $streams = [ordered]@{}
    try {
        foreach ($entry in $script:FormalFiles.GetEnumerator()) {
            $path = Join-Path $script:FormalDataRoot $entry.Value
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                $inventory[$entry.Key] = $true
                $streams[$entry.Key] = [System.IO.File]::Open(
                    $path,
                    [System.IO.FileMode]::Open,
                    [System.IO.FileAccess]::Read,
                    [System.IO.FileShare]::Read
                )
            }
            elseif (Test-Path -LiteralPath $path) {
                throw "Formal state path exists but is not a file: $path"
            }
            else {
                $inventory[$entry.Key] = $false
            }
        }

        $files = [ordered]@{}
        foreach ($entry in $script:FormalFiles.GetEnumerator()) {
            $path = Join-Path $script:FormalDataRoot $entry.Value
            if ((Test-Path -LiteralPath $path -PathType Leaf) -ne $inventory[$entry.Key]) {
                throw "Formal file existence changed during capture: $path"
            }
            if ($inventory[$entry.Key]) {
                $stream = $streams[$entry.Key]
                $sha = [System.Security.Cryptography.SHA256]::Create()
                try {
                    $files[$entry.Key] = [ordered]@{
                        path = $path
                        exists = $true
                        length = $stream.Length
                        sha256 = [Convert]::ToHexString($sha.ComputeHash($stream))
                    }
                }
                finally {
                    $sha.Dispose()
                }
            }
            else {
                $files[$entry.Key] = [ordered]@{
                    path = $path
                    exists = $false
                    length = $null
                    sha256 = $null
                }
            }
        }

        foreach ($entry in $script:FormalFiles.GetEnumerator()) {
            $path = Join-Path $script:FormalDataRoot $entry.Value
            if ((Test-Path -LiteralPath $path -PathType Leaf) -ne $inventory[$entry.Key]) {
                throw "Formal file existence changed during capture: $path"
            }
        }
        return $files
    }
    finally {
        foreach ($stream in $streams.Values) {
            if ($null -ne $stream) { $stream.Dispose() }
        }
    }
}

function Test-FormalFilesEqual {
    param($Left, $Right)

    foreach ($key in $script:FormalFiles.Keys) {
        foreach ($field in @("exists", "length", "sha256")) {
            if ($Left[$key][$field] -ne $Right[$key][$field]) { return $false }
        }
    }
    return $true
}

function Get-FormalState {
    $processesBefore = @(Get-PythonProcessIds)
    $first = Get-FormalStateOnce
    $second = Get-FormalStateOnce
    $processesAfter = @(Get-PythonProcessIds)
    if (-not (Test-FormalFilesEqual -Left $first -Right $second)) {
        throw "Formal file set was not stable across repeated captures"
    }
    if (($processesBefore -join ",") -ne ($processesAfter -join ",")) {
        throw "Python process state changed during formal capture"
    }
    if (($Phase -eq "Before") -and ($processesAfter.Count -gt 0)) {
        throw "Python process(es) appeared before the S1 test boundary: $($processesAfter -join ',')"
    }

    return [ordered]@{
        schema_version = 1
        phase = $Phase
        timestamp = [DateTimeOffset]::Now.ToString("o")
        formal_data_root = $script:FormalDataRoot
        python_process_ids = $processesAfter
        files = $second
    }
}

function Assert-BeforeEnvironment {
    if (-not (Test-PathEqual -Left (Get-Location).Path -Right $script:ProjectRoot)) {
        throw "S1 preflight must run from repository root: $script:ProjectRoot"
    }

    $pythonProcesses = @(Get-PythonProcessIds)
    if ($pythonProcesses.Count -gt 0) {
        $pids = $pythonProcesses -join ","
        throw "Python process(es) running: $pids - freeze S1"
    }

    if ($env:VD_ENV -ne "test") {
        throw "VD_ENV must be 'test', got '$($env:VD_ENV)'"
    }
    if (Test-Path Env:VD_FORMAL_ACK) {
        throw "VD_FORMAL_ACK is forbidden in the S1 test environment"
    }

    $required = @("VD_DUCKDB_PATH", "VD_SQLITE_PATH", "VD_TEST_RUN_ROOT")
    foreach ($name in $required) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ([string]::IsNullOrWhiteSpace($value)) {
            throw "$name is required"
        }
        Assert-SafePathSyntax -Path $value -Name $name
    }

    $runRoot = [System.IO.Path]::GetFullPath($env:VD_TEST_RUN_ROOT)
    $duckdbPath = [System.IO.Path]::GetFullPath($env:VD_DUCKDB_PATH)
    $sqlitePath = [System.IO.Path]::GetFullPath($env:VD_SQLITE_PATH)

    if (Test-Path -LiteralPath $runRoot) {
        throw "VD_TEST_RUN_ROOT must not exist before execution: $runRoot"
    }
    if (Test-IsDescendantOrEqual -Candidate $runRoot -Root $script:ProjectRoot) {
        throw "VD_TEST_RUN_ROOT must be outside the repository: $runRoot"
    }
    if (-not (Test-PathEqual -Left ([System.IO.Path]::GetDirectoryName($duckdbPath)) -Right $runRoot)) {
        throw "VD_DUCKDB_PATH must be a direct child of VD_TEST_RUN_ROOT"
    }
    if (-not (Test-PathEqual -Left ([System.IO.Path]::GetDirectoryName($sqlitePath)) -Right $runRoot)) {
        throw "VD_SQLITE_PATH must be a direct child of VD_TEST_RUN_ROOT"
    }
    if ([System.IO.Path]::GetFileName($duckdbPath) -ne "valuedashboard.duckdb") {
        throw "VD_DUCKDB_PATH must end with valuedashboard.duckdb"
    }
    if ([System.IO.Path]::GetFileName($sqlitePath) -ne "valuedashboard.sqlite") {
        throw "VD_SQLITE_PATH must end with valuedashboard.sqlite"
    }
    if (Test-PathEqual -Left $duckdbPath -Right $sqlitePath) {
        throw "DuckDB and SQLite paths must be distinct sibling files"
    }

    $denyRoots = @($script:ProjectRoot)
    if (-not [string]::IsNullOrWhiteSpace($env:VD_REBUILD_SOURCE_ROOT)) {
        Assert-SafePathSyntax -Path $env:VD_REBUILD_SOURCE_ROOT -Name "VD_REBUILD_SOURCE_ROOT"
        $denyRoots += [System.IO.Path]::GetFullPath($env:VD_REBUILD_SOURCE_ROOT)
    }
    if (-not [string]::IsNullOrWhiteSpace($env:VD_FORENSIC_ROOTS)) {
        foreach ($root in ($env:VD_FORENSIC_ROOTS -split ";")) {
            if ([string]::IsNullOrWhiteSpace($root)) {
                continue
            }
            Assert-SafePathSyntax -Path $root -Name "VD_FORENSIC_ROOTS entry"
            $denyRoots += [System.IO.Path]::GetFullPath($root)
        }
    }

    foreach ($denyRoot in $denyRoots) {
        if ((Test-IsDescendantOrEqual -Candidate $runRoot -Root $denyRoot) -or
            (Test-IsDescendantOrEqual -Candidate $denyRoot -Root $runRoot)) {
            throw "VD_TEST_RUN_ROOT overlaps denied root '$denyRoot': $runRoot"
        }
    }

    Assert-SafeExistingAncestorChain -Path $runRoot
    $script:PythonExecutable = (Get-Command python -ErrorAction Stop).Source
}

if ([string]::IsNullOrWhiteSpace($CaptureName)) {
    $CaptureName = if ($Phase -eq "Before") { "before" } else { "after" }
}
if ([string]::IsNullOrWhiteSpace($EvidenceToken)) {
    if ($Phase -eq "Before") {
        $EvidenceToken = [Guid]::NewGuid().ToString("N")
    }
    else {
        throw "EvidenceToken is required for After capture"
    }
}
if ($EvidenceToken -notmatch "^[a-f0-9]{32}$") {
    throw "EvidenceToken must be 32 lowercase hexadecimal characters"
}
if ($CaptureName -notmatch "^[a-z0-9-]+$") {
    throw "CaptureName must contain only lowercase letters, digits, and hyphens"
}
$phaseDirectory = if ($Phase -eq "Before") { "pre" } else { "post" }
$resolvedEvidenceDir = if ([System.IO.Path]::IsPathFullyQualified($EvidenceDir)) {
    [System.IO.Path]::GetFullPath($EvidenceDir)
}
else {
    [System.IO.Path]::GetFullPath((Join-Path $script:ProjectRoot $EvidenceDir))
}
$outputDirectory = Join-Path $resolvedEvidenceDir $phaseDirectory
$evidenceDirectorySafe = $false

function Assert-SafeEvidenceDirectory {
    Assert-SafePathSyntax -Path $resolvedEvidenceDir -Name "EvidenceDir"
    Assert-SafeExistingAncestorChain -Path $script:FormalDataRoot
    if (Test-IsDescendantOrEqual -Candidate $resolvedEvidenceDir -Root $script:FormalDataRoot) {
        throw "EvidenceDir must not be under the formal data root: $resolvedEvidenceDir"
    }
    foreach ($variableName in @("VD_REBUILD_SOURCE_ROOT")) {
        $root = [Environment]::GetEnvironmentVariable($variableName)
        if (-not [string]::IsNullOrWhiteSpace($root)) {
            Assert-SafePathSyntax -Path $root -Name $variableName
            Assert-SafeExistingAncestorChain -Path $root
            if (Test-IsDescendantOrEqual -Candidate $resolvedEvidenceDir -Root $root) {
                throw "EvidenceDir must not be under ${variableName}: $resolvedEvidenceDir"
            }
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($env:VD_FORENSIC_ROOTS)) {
        foreach ($root in ($env:VD_FORENSIC_ROOTS -split ";")) {
            if ([string]::IsNullOrWhiteSpace($root)) { continue }
            Assert-SafePathSyntax -Path $root -Name "VD_FORENSIC_ROOTS entry"
            Assert-SafeExistingAncestorChain -Path $root
            if (Test-IsDescendantOrEqual -Candidate $resolvedEvidenceDir -Root $root) {
                throw "EvidenceDir must not be under a forensic root: $resolvedEvidenceDir"
            }
        }
    }
    if ($Phase -eq "Before") {
        if (Test-Path -LiteralPath $resolvedEvidenceDir) {
            throw "Before EvidenceDir must be unique and must not already exist: $resolvedEvidenceDir"
        }
    }
    elseif (-not (Test-Path -LiteralPath $resolvedEvidenceDir -PathType Container)) {
        throw "After EvidenceDir must be the existing wrapper-owned run directory: $resolvedEvidenceDir"
    }
    Assert-SafeExistingAncestorChain -Path $resolvedEvidenceDir
}

function Assert-EvidenceOwnership {
    $markerPath = Join-Path $resolvedEvidenceDir ".vd-s1-evidence-owned"
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        throw "Evidence ownership marker is missing: $markerPath"
    }
    $marker = Get-Item -LiteralPath $markerPath -Force
    if (($marker.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Evidence ownership marker must not be a reparse point: $markerPath"
    }
    if ((Get-Content -LiteralPath $markerPath -Raw) -ne $EvidenceToken) {
        throw "Evidence ownership marker does not match this wrapper run"
    }
}

function Write-NewUtf8File {
    param([string]$Path, [string]$Content)

    $encoding = [System.Text.UTF8Encoding]::new($false)
    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    try {
        $writer = [System.IO.StreamWriter]::new($stream, $encoding)
        try { $writer.Write($Content) } finally { $writer.Dispose() }
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

try {
    Assert-SafeEvidenceDirectory
    if ($Phase -eq "Before") {
        New-Item -ItemType Directory -Path $resolvedEvidenceDir -ErrorAction Stop | Out-Null
        Assert-SafeExistingAncestorChain -Path $resolvedEvidenceDir
        Write-NewUtf8File `
            -Path (Join-Path $resolvedEvidenceDir ".vd-s1-evidence-owned") `
            -Content $EvidenceToken
    }
    Assert-SafeExistingAncestorChain -Path $resolvedEvidenceDir
    Assert-EvidenceOwnership
    $evidenceDirectorySafe = $true
    if (-not (Test-Path -LiteralPath $outputDirectory)) {
        New-Item -ItemType Directory -Path $outputDirectory -ErrorAction Stop | Out-Null
    }
    Assert-SafeExistingAncestorChain -Path $outputDirectory
    if ($Phase -eq "Before") {
        Assert-BeforeEnvironment
    }
    $state = Get-FormalState
    if (($Phase -eq "Before") -and ($null -ne $script:PythonExecutable)) {
        $state.python_executable = $script:PythonExecutable
    }
    $json = $state | ConvertTo-Json -Depth 10
    Write-NewUtf8File -Path (Join-Path $outputDirectory "$CaptureName-evidence.json") -Content $json
    $json
    exit 0
}
catch {
    $failure = [ordered]@{
        schema_version = 1
        phase = $Phase
        timestamp = [DateTimeOffset]::Now.ToString("o")
        outcome = "FAILURE"
        message = $_.Exception.Message
        formal_data_root = $script:FormalDataRoot
    }
    if ($evidenceDirectorySafe) {
        try {
            if (Test-Path -LiteralPath $resolvedEvidenceDir -PathType Container) {
                if (-not (Test-Path -LiteralPath $outputDirectory)) {
                    New-Item -ItemType Directory -Path $outputDirectory -ErrorAction Stop | Out-Null
                }
                Assert-SafeExistingAncestorChain -Path $outputDirectory
                Write-NewUtf8File `
                    -Path (Join-Path $outputDirectory "$CaptureName-failure.json") `
                    -Content ($failure | ConvertTo-Json -Depth 10)
            }
        }
        catch {
            [Console]::Error.WriteLine("Unable to persist preflight failure evidence: $($_.Exception.Message)")
        }
    }
    [Console]::Error.WriteLine("S1 preflight $Phase failed: $($failure.message)")
    exit 1
}
