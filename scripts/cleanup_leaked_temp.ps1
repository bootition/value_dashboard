<#
.SYNOPSIS
清理 S1 测试包装器泄漏在 %TEMP% 下的运行根目录（2026-11-15，reports/125）。

.DESCRIPTION
背景：`scripts/s1-pytest.ps1` 原来只在全部测试通过时删除
`%TEMP%\vd-s1-<guid>`，于是**测试失败 / 被中断 / 走 delta-98-99 分支**时，
整个运行根目录会永久留在 C 盘。每个测试还会在根目录下各复制一份
10–30MB 的 `valuedashboard.duckdb` 快照，单轮可达 20GB 以上；
现场曾累积 40+ 个目录、占用 100GB+。

本脚本用于**一次性清理历史泄漏**（包装器本身已改为无条件清理 + 启动清扫）。

默认 dry-run，只列目录与体积；加 -Execute 才真正删除。
仅删除名字匹配 `vd-s1-*` / `vd-t1-*` / `vd-t2-*` / `tmp.*` 的**目录**，
且默认只删 30 分钟前的（避免删掉正在跑的那一轮）。

.PARAMETER OlderThanMinutes
只清理最后写入时间早于该分钟数的目录（默认 30）。

.PARAMETER Execute
真正删除（默认只报告）。

.EXAMPLE
pwsh -NoProfile -File scripts/cleanup_leaked_temp.ps1
pwsh -NoProfile -File scripts/cleanup_leaked_temp.ps1 -Execute
#>
[CmdletBinding()]
param(
    [int]$OlderThanMinutes = 30,
    [switch]$Execute
)

$ErrorActionPreference = "Stop"
$tempRoot = [System.IO.Path]::GetTempPath()
$cutoff = (Get-Date).AddMinutes(-$OlderThanMinutes)
$patterns = @("vd-s1-*", "vd-t1-*", "vd-t2-*", "tmp.*")

Write-Output "TEMP = $tempRoot"
Write-Output "只处理最后写入时间早于 $($cutoff.ToString('yyyy-MM-dd HH:mm:ss')) 的目录"
Write-Output ""

$targets = @()
foreach ($pattern in $patterns) {
    $targets += Get-ChildItem -LiteralPath $tempRoot -Directory -Filter $pattern -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff }
}
$targets = $targets | Sort-Object FullName -Unique

if (-not $targets) {
    Write-Output "没有需要清理的目录。"
    exit 0
}

$totalBytes = 0
$rows = foreach ($dir in $targets) {
    $bytes = (Get-ChildItem -LiteralPath $dir.FullName -Recurse -File -Force -ErrorAction SilentlyContinue |
        Measure-Object Length -Sum).Sum
    if ($null -eq $bytes) { $bytes = 0 }
    $totalBytes += $bytes
    [PSCustomObject]@{
        GB       = [math]::Round($bytes / 1GB, 2)
        Modified = $dir.LastWriteTime.ToString("yyyy-MM-dd HH:mm")
        Path     = $dir.FullName
    }
}

$rows | Sort-Object GB -Descending | Format-Table -AutoSize | Out-String | Write-Output
Write-Output ("合计 {0} 个目录 / {1:N2} GB" -f $targets.Count, ($totalBytes / 1GB))

if (-not $Execute) {
    Write-Output ""
    Write-Output "[DRY RUN] 未删除任何内容；加 -Execute 执行删除。"
    exit 0
}

$failed = 0
foreach ($dir in $targets) {
    try {
        Remove-Item -LiteralPath $dir.FullName -Recurse -Force -ErrorAction Stop
    }
    catch {
        $failed++
        Write-Warning "删除失败 $($dir.FullName): $($_.Exception.Message)"
    }
}
Write-Output ""
Write-Output ("删除完成：成功 {0} / 失败 {1}；预计回收 {2:N2} GB" -f ($targets.Count - $failed), $failed, ($totalBytes / 1GB))
