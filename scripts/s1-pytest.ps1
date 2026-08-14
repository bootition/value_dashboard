[CmdletBinding()]
param(
    [switch]$PolicyOnly,

    [string]$EvidenceDir = "docs/evidence/evidence-s1",

    # Position 0 显式声明: 首个位置参数必须进入 PytestArgs。
    # 此前未声明 Position，PowerShell 会把首个位置参数（如
    # tests/regression/test_x.py）误绑到 $EvidenceDir，导致证据目录
    # 创建在 tests/regression 下并触发 "Cannot create ... already exists"。
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$PytestArgs
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
# 2026-08-14 红队 P2 门禁：S1 依赖 .NET Core API（如
# [System.IO.Path]::IsPathFullyQualified），Windows PowerShell 5.1 会中途
# 报"方法不存在"；入口处显式要求 PowerShell 7+。
if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw "PowerShell 7+ is required to run S1 gates, got $($PSVersionTable.PSVersion)"
}
if ($null -eq $PytestArgs) { $PytestArgs = @() }

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$preflightScript = Join-Path $PSScriptRoot "s1-path-preflight.ps1"
$pwshExe = (Get-Command pwsh -ErrorAction Stop).Source
$runId = [Guid]::NewGuid().ToString("N")
$evidenceRoot = if ([System.IO.Path]::IsPathFullyQualified($EvidenceDir)) {
    [System.IO.Path]::GetFullPath($EvidenceDir)
}
else {
    [System.IO.Path]::GetFullPath((Join-Path $projectRoot $EvidenceDir))
}
$runDir = Join-Path $evidenceRoot $runId
$runRoot = Join-Path ([System.IO.Path]::GetTempPath()) "vd-s1-$runId"
$savedEnvironment = @{}
foreach ($name in @(
    "VD_ENV", "VD_TEST_RUN_ROOT", "VD_DUCKDB_PATH", "VD_SQLITE_PATH", "VD_TEST_EVIDENCE_ROOT",
    "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTHONPATH", "PYTHONHOME"
)) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name)
}

function Test-PathEqual {
    param([string]$Left, [string]$Right)

    return [string]::Equals(
        [System.IO.Path]::TrimEndingDirectorySeparator([System.IO.Path]::GetFullPath($Left)),
        [System.IO.Path]::TrimEndingDirectorySeparator([System.IO.Path]::GetFullPath($Right)),
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Assert-NoReparsePoint {
    param([string]$Path)

    $cursor = [System.IO.Path]::GetFullPath($Path)
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

function Assert-SafeRunTreeForCleanup {
    param([string]$Path, [string]$SentinelValue)

    $expectedParent = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    if (-not (Test-PathEqual -Left ([System.IO.Path]::GetDirectoryName($Path)) -Right $expectedParent)) {
        throw "Refusing cleanup outside the system temp directory: $Path"
    }
    if ([System.IO.Path]::GetFileName($Path) -notmatch "^vd-s1-[0-9a-f]{32}$") {
        throw "Refusing cleanup of an unowned run-root name: $Path"
    }
    Assert-NoReparsePoint -Path $Path
    foreach ($item in (Get-ChildItem -LiteralPath $Path -Force -Recurse -ErrorAction Stop)) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing cleanup because the run tree contains a reparse point: $($item.FullName)"
        }
    }
    $sentinelPath = Join-Path $Path ".vd-s1-owned"
    if (-not (Test-Path -LiteralPath $sentinelPath -PathType Leaf)) {
        throw "Refusing cleanup because the ownership sentinel is missing: $Path"
    }
    if ((Get-Content -LiteralPath $sentinelPath -Raw) -ne $SentinelValue) {
        throw "Refusing cleanup because the ownership sentinel does not match: $Path"
    }
}

function Compare-FormalState {
    param(
        [Parameter(Mandatory = $true)]$Before,
        [Parameter(Mandatory = $true)]$After,
        [Parameter(Mandatory = $true)][string]$OutputDirectory,
        [string]$FilePrefix = ""
    )

    $differences = @()
    $keys = @("formal_duckdb", "formal_sqlite", "duckdb_wal", "sqlite_wal", "sqlite_shm")
    foreach ($key in $keys) {
        $beforeFile = $Before.files.$key
        $afterFile = $After.files.$key
        foreach ($field in @("exists", "length", "sha256")) {
            if ($beforeFile.$field -ne $afterFile.$field) {
                $differences += [ordered]@{
                    file = $key
                    field = $field
                    before = $beforeFile.$field
                    after = $afterFile.$field
                }
            }
        }
    }
    # 2026-08-14 红队 P2 门禁：整棵 data/ 树对比（具名文件之外的
    # CSV/日志/锁等同样不得变化）。
    $beforeTree = $Before.tree
    $afterTree = $After.tree
    $treeRelPaths = @($beforeTree.Keys + $afterTree.Keys) | Sort-Object -Unique
    foreach ($rel in $treeRelPaths) {
        foreach ($field in @("exists", "length", "sha256")) {
            $beforeValue = if ($beforeTree.Contains($rel)) { $beforeTree[$rel][$field] } else { $null }
            $afterValue = if ($afterTree.Contains($rel)) { $afterTree[$rel][$field] } else { $null }
            if ($beforeValue -ne $afterValue) {
                $differences += [ordered]@{
                    file = "data/$rel"
                    field = $field
                    before = $beforeValue
                    after = $afterValue
                }
            }
        }
    }

    $result = [ordered]@{
        schema_version = 1
        compared_at = [DateTimeOffset]::Now.ToString("o")
        delta_detected = ($differences.Count -gt 0)
        before = [ordered]@{}
        after = [ordered]@{}
        files = [ordered]@{}
        differences = $differences
    }
    foreach ($key in $keys) {
        $result.before[$key] = $Before.files.$key
        $result.after[$key] = $After.files.$key
        $result.files[$key] = [ordered]@{
            before = $Before.files.$key
            after = $After.files.$key
            match = -not ($differences | Where-Object { $_.file -eq $key })
        }
    }

    $fileName = if ($differences.Count -gt 0) {
        "${FilePrefix}delta-report.json"
    }
    else {
        "${FilePrefix}hash-evidence.json"
    }
    try {
        Write-NewUtf8File `
            -Path (Join-Path $OutputDirectory $fileName) `
            -Content ($result | ConvertTo-Json -Depth 12)
    }
    catch {
        if ($differences.Count -eq 0) { throw }
        try {
            [Console]::Error.WriteLine(
                "Formal delta detected but evidence write failed: $($_.Exception.Message)"
            )
        }
        catch {}
    }
    return ($differences.Count -gt 0)
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

function Get-PythonProcessIds {
    return @(
        Get-Process -ErrorAction Stop |
            Where-Object { $_.ProcessName -match "^(python|pythonw|py|pytest)([0-9.]*)?$" } |
            ForEach-Object { $_.Id } |
            Sort-Object
    )
}

function Test-IsRegressionTarget {
    param([string]$Argument)

    if ($Argument.Contains("::") -or [System.IO.Path]::IsPathFullyQualified($Argument)) {
        return $false
    }
    $target = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $Argument))
    $regressionRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "tests\regression"))
    Assert-NoReparsePoint -Path $regressionRoot
    Assert-NoReparsePoint -Path $target
    if ($target -eq $regressionRoot) { return $true }
    return $target.StartsWith(
        $regressionRoot + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    ) -and (Test-Path -LiteralPath $target)
}

function Restore-Environment {
    foreach ($entry in $savedEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value)
    }
}

$finalExit = 97
$beforeCaptured = $false
$beforeParsed = $false
$pythonStarted = $false
$afterCaptured = $false
$delta = $true
$pytestExit = 97
$executionError = $null
$cleanupError = $null
$ownershipSentinel = [Guid]::NewGuid().ToString("N")
$evidenceToken = [Guid]::NewGuid().ToString("N")
$nativePreferenceWasDefined = Test-Path Variable:PSNativeCommandUseErrorActionPreference
if ($nativePreferenceWasDefined) {
    $savedNativePreference = $PSNativeCommandUseErrorActionPreference
}

try {
    if (-not (Test-PathEqual -Left (Get-Location).Path -Right $projectRoot)) {
        throw "S1 pytest wrapper must run from repository root: $projectRoot"
    }
    if (Test-Path Env:VD_FORMAL_ACK) {
        throw "VD_FORMAL_ACK is forbidden in the S1 pytest wrapper"
    }
    if (Test-Path -LiteralPath $runRoot) {
        throw "Generated run root unexpectedly exists: $runRoot"
    }
    if ($PolicyOnly -and $PytestArgs.Count -gt 0) {
        throw "PolicyOnly uses a fixed test target and accepts no PytestArgs"
    }
    if (-not $PolicyOnly) {
        $safeOptions = @("--collect-only", "-q", "--no-header", "-v", "-x")
        foreach ($argument in $PytestArgs) {
            if ($argument.StartsWith("-")) {
                if ($argument -notin $safeOptions) {
                    throw "Caller-supplied pytest option is not allowlisted: $argument"
                }
            }
            elseif (-not (Test-IsRegressionTarget -Argument $argument)) {
                throw "Pytest target must be an existing path under tests/regression: $argument"
            }
        }
    }

    $env:VD_ENV = "test"
    $env:VD_TEST_RUN_ROOT = $runRoot
    $env:VD_DUCKDB_PATH = Join-Path $runRoot "valuedashboard.duckdb"
    $env:VD_SQLITE_PATH = Join-Path $runRoot "valuedashboard.sqlite"
    $env:VD_TEST_EVIDENCE_ROOT = $runDir
    Remove-Item Env:PYTEST_ADDOPTS -ErrorAction SilentlyContinue
    Remove-Item Env:PYTEST_PLUGINS -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
    $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
    if ($nativePreferenceWasDefined) {
        $PSNativeCommandUseErrorActionPreference = $false
    }
    & $pwshExe -NoProfile -File $preflightScript `
        -Phase Before `
        -EvidenceDir $runDir `
        -CaptureName "before" `
        -EvidenceToken $evidenceToken | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Preflight Before failed"
    }
    $beforeCaptured = $true

    try {
        try {
            $beforePath = Join-Path $runDir "pre\before-evidence.json"
            $before = Get-Content -LiteralPath $beforePath -Raw | ConvertFrom-Json
            $beforeParsed = $true

            if ($PolicyOnly) {
                $effectiveArgs = @(
                    "--noconftest", "-v", "--tb=short", "tests/regression/test_path_isolation.py"
                )
            }
            else {
                New-Item -ItemType Directory -Path $runRoot -ErrorAction Stop | Out-Null
                Assert-NoReparsePoint -Path $runRoot
                Write-NewUtf8File `
                    -Path (Join-Path $runRoot ".vd-s1-owned") `
                    -Content $ownershipSentinel
                $effectiveArgs = @("--basetemp", (Join-Path $runRoot "pytest-tmp")) + $PytestArgs
            }

            $pythonExe = (Get-Command python -ErrorAction Stop).Source
            # 2026-08-14 红队 P2 门禁：freeze 收窄——允许用户无关 python
            # 进程存在，只追踪本次运行新增且存活的进程。
            $pythonProcessesBefore = @(Get-PythonProcessIds)
            $pythonStarted = $true
            $nativeArgs = @("-m", "pytest") + $effectiveArgs
            if ($nativeArgs | Where-Object { $_ -match "\s" }) {
                throw "Whitespace-bearing pytest arguments are forbidden by the process-tree runner"
            }
            $pythonProcess = Start-Process `
                -FilePath $pythonExe `
                -ArgumentList $nativeArgs `
                -NoNewWindow `
                -Wait `
                -PassThru
            $pytestExit = $pythonProcess.ExitCode
            $pythonProcessesAfter = @(Get-PythonProcessIds)
            $survivors = @(
                $pythonProcessesAfter |
                    Where-Object { $_ -notin $pythonProcessesBefore }
            )
            if ($survivors.Count -gt 0) {
                throw "Python/pytest process(es) survived the process-tree wait: $($survivors -join ',')"
            }
        }
        catch {
            $executionError = $_
            $pytestExit = 97
        }
    }
    finally {
        & $pwshExe -NoProfile -File $preflightScript `
            -Phase After `
            -EvidenceDir $runDir `
            -CaptureName "after" `
            -EvidenceToken $evidenceToken | Out-Null
        if ($LASTEXITCODE -eq 0) {
            try {
                $afterPath = Join-Path $runDir "post\after-evidence.json"
                $after = Get-Content -LiteralPath $afterPath -Raw | ConvertFrom-Json
                if (-not $beforeParsed) {
                    throw "Before evidence could not be parsed"
                }
                try {
                    $delta = Compare-FormalState -Before $before -After $after -OutputDirectory $runDir
                    $afterCaptured = $true
                    $newAfterProcesses = @(
                        $after.python_process_ids |
                            Where-Object { $_ -notin $before.python_process_ids }
                    )
                    if ($newAfterProcesses.Count -gt 0) {
                        $executionError = [System.Management.Automation.ErrorRecord]::new(
                            [System.InvalidOperationException]::new(
                                "Process(es) survived into After capture: $($newAfterProcesses -join ',')"
                            ),
                            "S1SurvivingProcess",
                            [System.Management.Automation.ErrorCategory]::ResourceBusy,
                            $newAfterProcesses
                        )
                        $pytestExit = 97
                    }
                }
                catch {
                    throw "Formal-state comparison failed: $($_.Exception.Message)"
                }
            }
            catch {
                $afterCaptured = $false
                $executionError = $_
            }
        }
    }

    if (-not $afterCaptured) {
        $finalExit = 98
    }
    elseif ($delta) {
        $finalExit = 99
        try {
            [Console]::Error.WriteLine("[FATAL] Formal database state changed; see $runDir\delta-report.json")
        }
        catch {}
    }
    else {
        $finalExit = $pytestExit
        if ($PolicyOnly -and (Test-Path -LiteralPath $runRoot)) {
            $finalExit = 96
            $cleanupError = "PolicyOnly created the forbidden run root: $runRoot"
        }
        if (($pytestExit -eq 0) -and (-not $PolicyOnly)) {
            try {
                Assert-SafeRunTreeForCleanup -Path $runRoot -SentinelValue $ownershipSentinel
                Remove-Item -LiteralPath $runRoot -Recurse -Force -ErrorAction Stop
            }
            catch {
                $cleanupError = $_
                $finalExit = 96
            }

            if ($null -eq $cleanupError) {
                try {
                & $pwshExe -NoProfile -File $preflightScript `
                    -Phase After `
                    -EvidenceDir $runDir `
                    -CaptureName "after-cleanup" `
                    -EvidenceToken $evidenceToken | Out-Null
                if ($LASTEXITCODE -ne 0) {
                        throw "Post-cleanup formal-state capture failed"
                }
                    $postCleanupAfter = Get-Content -LiteralPath (Join-Path $runDir "post\after-cleanup-evidence.json") -Raw |
                        ConvertFrom-Json
                    $postCleanupDelta = Compare-FormalState `
                        -Before $before `
                        -After $postCleanupAfter `
                        -OutputDirectory $runDir `
                        -FilePrefix "post-cleanup-"
                    if ($postCleanupDelta) {
                        $finalExit = 99
                    }
                    $newPostCleanupProcesses = @(
                        $postCleanupAfter.python_process_ids |
                            Where-Object { $_ -notin $before.python_process_ids }
                    )
                    if (($newPostCleanupProcesses.Count -gt 0) -and ($finalExit -ne 99)) {
                        throw "Process(es) survived into post-cleanup capture: $($newPostCleanupProcesses -join ',')"
                    }
                }
                catch {
                    $executionError = $_
                    if ($finalExit -ne 99) {
                        $finalExit = 98
                    }
                }
            }
        }
    }

    if ($null -ne $executionError) {
        [Console]::Error.WriteLine("S1 execution error: $($executionError.Exception.Message)")
    }
    if ($null -ne $cleanupError) {
        $message = if ($cleanupError -is [string]) { $cleanupError } else { $cleanupError.Exception.Message }
        [Console]::Error.WriteLine("S1 cleanup error: $message")
    }
}
catch {
    [Console]::Error.WriteLine("S1 pytest wrapper failed: $($_.Exception.Message)")
    if ($pythonStarted -and $beforeCaptured -and (-not $afterCaptured)) {
        $finalExit = 98
    }
    elseif ($finalExit -eq 97) {
        $finalExit = 97
    }
}
finally {
    try {
        Restore-Environment
        if ($nativePreferenceWasDefined) {
            $PSNativeCommandUseErrorActionPreference = $savedNativePreference
        }
    }
    catch {
        try { [Console]::Error.WriteLine("Environment restoration failed: $($_.Exception.Message)") } catch {}
        if (($finalExit -ne 98) -and ($finalExit -ne 99)) {
            $finalExit = 95
        }
    }
}

exit $finalExit
