# 清理 %TEMP% 下"跑测试/探针留下的"临时目录（2026-11-15 reports/125；2026-11-20 reports/149 扩写）
#
# ## 历史
# 最初只治一件事：`scripts/s1-pytest.ps1` 曾在**测试失败/被中断**时不删运行根，累积 40+ 个目录。
#
# ## 现在的真实来源（`reports/149`，2026-11-20）
# `%TEMP%` 一度涨到 **107 GB**，全量回归因此报 50 个 `No space left on device`。
# 那次的主要来源**不是包装器**，而是**人和 AI agent 手搓的运行根**：
#   RUN="$(mktemp -d)/vd-xxx" && pytest --basetemp="$RUN/tmp"     # 跑完不删
# 19 小时累积 455 个 `tmp.*` 目录 / 74 GB —— 名字是随机的，**没有拥有者标记**。
#
# ## 实现已收敛到一处
# 真正的判定/删除逻辑在 `scripts/temp_hygiene.py`（有单元测试 `tests/regression/test_temp_hygiene.py`）。
# 本脚本只是"**激进口径**"的手动入口：连历史手搓的 `vd-*` / `vdbatch-*` / `tmp.*` 一起清。
# 日常请优先用工具托管的方式（不产生垃圾）：
#   uv run --locked python scripts/scoped_pytest.py <目标>     # 建根 + 跑完必删
#
# ## 参数
# -OlderThanMinutes  只清最后写入早于该分钟数的目录（默认 30；活着的会被跳过）
# -Execute           真正删除（默认 dry-run，只报告）
#
# ## 例子
# pwsh -NoProfile -File scripts/cleanup_leaked_temp.ps1
# pwsh -NoProfile -File scripts/cleanup_leaked_temp.ps1 -Execute
[CmdletBinding()]
param(
    [int]$OlderThanMinutes = 30,
    [switch]$Execute
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}
$hygiene = Join-Path $PSScriptRoot "temp_hygiene.py"
if (-not (Test-Path -LiteralPath $hygiene)) {
    throw "找不到 scripts/temp_hygiene.py（本脚本已改为委托它，不再自己实现删除逻辑）"
}

$arguments = @(
    $hygiene,
    "--older-than-minutes", $OlderThanMinutes,
    # 激进口径：连"名字像我们但没标记"的历史残留一起清（默认不碰 tmp.* 之外别人的东西）
    "--include-legacy",
    "--include-tmp"
)
if ($Execute) { $arguments += "--execute" }

& $python @arguments
exit $LASTEXITCODE
