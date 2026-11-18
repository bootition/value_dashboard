<#
.SYNOPSIS
    安装 value-dashboard 的 git hooks（Windows / PowerShell 用；幂等，可重复跑）。

.DESCRIPTION
    做的事只有三件：
      1. git config --local core.hooksPath .githooks
      2. 把 .githooks/* 的 CRLF 刷成 LF（core.autocrlf=true 时 checkout 会把钩子写成 CRLF，
         而 "#!/bin/sh`r" 会让钩子直接起不来 —— Windows 上最常见的"装了却没生效"）
      3. 语法自检 sh -n，尽早暴露钩子本身写坏了
    Windows 下不设可执行位（git 在 Windows 上不看 exec bit）；Linux/macOS 请用
    scripts/install-git-hooks.sh。不碰工作区其它文件，不改 .gitignore，不提交任何东西。

.PARAMETER Uninstall
    移除 core.hooksPath，恢复 git 默认的 .git/hooks 目录。

.EXAMPLE
    pwsh -File scripts/install-git-hooks.ps1
    pwsh -File scripts/install-git-hooks.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# 输出编码：被管道/重定向时强制 UTF-8，否则 GBK 控制台会把中文打成 mojibake
# （本仓库 `reports/138` §3.1 就是这么把中文写坏过的）。附着在真实控制台时不动，
# 由 PowerShell 自己按 Unicode 渲染。
try {
    if ([Console]::IsOutputRedirected) {
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
        $OutputEncoding = [Console]::OutputEncoding
    }
}
catch {
    # 无控制台句柄（某些 CI/服务环境）时忽略即可，不影响安装逻辑
}

function Write-Info { param([string]$Message) Write-Host $Message }
function Write-WarnLine { param([string]$Message) Write-Warning $Message }
function Stop-WithError {
    param([string]$Message)
    Write-Host "[error] $Message" -ForegroundColor Red
    exit 1
}

$HookDirName = ".githooks"
$HookFile = "pre-commit"

# ---- 定位仓库根（脚本在哪都能跑，不依赖当前目录） --------------------------
$repoRoot = $null
try {
    $candidate = (& git rev-parse --show-toplevel 2>$null)
    if ($LASTEXITCODE -eq 0 -and $candidate) { $repoRoot = $candidate.Trim() }
}
catch { $repoRoot = $null }
if (-not $repoRoot) {
    $repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
}
Set-Location -LiteralPath $repoRoot
Write-Info "仓库根：$repoRoot"

# ---- 卸载 ----------------------------------------------------------------
if ($Uninstall) {
    $current = (& git config --local --get core.hooksPath 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $current) {
        Write-Info "core.hooksPath 本来就没设置，无需卸载（幂等）。"
    }
    else {
        & git config --local --unset core.hooksPath
        if ($LASTEXITCODE -ne 0) { Stop-WithError "移除 core.hooksPath 失败" }
        Write-Info "已移除 core.hooksPath（原值：$current）。git 回到默认 .git/hooks 目录。"
    }
    Write-Info ""
    Write-Info "验证：git config --local --get core.hooksPath   # 应无输出"
    exit 0
}

# ---- 安装 ----------------------------------------------------------------
$hookPath = Join-Path $repoRoot (Join-Path $HookDirName $HookFile)
if (-not (Test-Path -LiteralPath $hookPath)) {
    Stop-WithError "缺少 $HookDirName/$HookFile，无法安装（仓库不完整？）"
}

# 1) 幂等设置 core.hooksPath
$current = (& git config --local --get core.hooksPath 2>$null)
if ($LASTEXITCODE -eq 0 -and $current -eq $HookDirName) {
    Write-Info "core.hooksPath 已是 $HookDirName，跳过设置（幂等）。"
}
else {
    & git config --local core.hooksPath $HookDirName
    if ($LASTEXITCODE -ne 0) { Stop-WithError "git config core.hooksPath 失败" }
    if ($current) { Write-Info "core.hooksPath：$current -> $HookDirName" }
    else { Write-Info "core.hooksPath：未设置 -> $HookDirName" }
}

# 2) CRLF -> LF。Windows + core.autocrlf=true 下钩子会被 checkout 成 CRLF，
#    "#!/bin/sh`r" 会让 sh 报 bad interpreter，钩子等于没装。
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$fixed = 0
foreach ($file in Get-ChildItem -LiteralPath (Join-Path $repoRoot $HookDirName) -File) {
    $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
    $text = $utf8NoBom.GetString($bytes)
    if ($text.Contains("`r`n")) {
        [System.IO.File]::WriteAllText($file.FullName, $text.Replace("`r`n", "`n"), $utf8NoBom)
        $fixed++
    }
}
if ($fixed -gt 0) {
    Write-WarnLine "已把 $fixed 个钩子文件的 CRLF 刷成 LF（否则 ""#!/bin/sh`r"" 起不来）。"
}

# 3) 语法自检（有 sh 才做；没有就跳过，不阻塞安装）
$shExe = $null
foreach ($name in @("sh", "bash")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { $shExe = $cmd.Source; break }
}
if ($shExe) {
    & $shExe -n $hookPath
    if ($LASTEXITCODE -ne 0) { Stop-WithError "钩子语法检查失败，安装中止：$HookDirName/$HookFile" }
    Write-Info "语法自检通过：sh -n $HookDirName/$HookFile"
}
else {
    Write-WarnLine "PATH 里没有 sh/bash，跳过语法自检（Git Bash 里跑 scripts/install-git-hooks.sh 可补做）。"
}

Write-Info ""
Write-Info "安装完成（钩子会在 git commit 时自动运行，可用 --no-verify 绕过）"
Write-Info ""
Write-Info "验证方式："
Write-Info "  1) git config --local --get core.hooksPath          # 应输出 .githooks"
Write-Info "  2) bash .githooks/pre-commit; echo exit=`$?          # 无暂存内容时快速 exit=0"
Write-Info "  3) 真实触发：随便改一个文件 git add 后 git commit，看是否出现 [pre-commit] 输出"
Write-Info ""
Write-Info "钩子做了什么 / 怎么绕过：见 .githooks/README.md"
Write-Info "卸载：pwsh -File scripts/install-git-hooks.ps1 -Uninstall"
