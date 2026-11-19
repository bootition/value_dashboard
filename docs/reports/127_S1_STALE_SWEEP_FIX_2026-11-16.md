---
status: approved
category: reports
created: 2026-11-16
last-reviewed: 2026-11-16
related: reports/125, reports/126
number-collision-note: 本文件是「S1 陈旧运行根清扫修复」（scripts/s1-pytest.ps1 的 Remove-StaleS1RunRoots 空转）。同编号另有 127_REMAINING_CODE_ITEMS_2026-11-16.md（reports/126 §8 剩余代码项收尾），二者无关；引用时请写全文件名，勿只写 reports/127。
---

# S1 启动清扫从未生效：陈旧运行根仍会无限累积（reports/125 的第二道保险是空转）

## 1. 结论（TL;DR）

1. **缺陷**：`reports/125` 给 `scripts/s1-pytest.ps1` 加了两道保险，其中**第二道**
   `Remove-StaleS1RunRoots`（启动时清扫 24h 前的 `vd-s1-*` 残留）**从未删除过任何一个目录**。
   它拿"本轮新生成的 `$ownershipSentinel`"去校验"上一轮留下的目录"，而
   `Assert-SafeRunTreeForCleanup` 要求哨兵**精确相等** —— 两者必然不等，抛
   `ownership sentinel does not match` 后被 `catch` 吞掉，只留一条 WARN。
2. **影响**：`reports/125` 里"硬杀/断电/蓝屏也不会累积"的结论**不成立**。
   正常失败 / 中断走 `finally`，不受影响；但**硬杀 / 断电 / 蓝屏**这类 `finally`
   不会执行的场景，残留依然会无限累积，直到再次占满 C 盘。
3. **修复**：新增 `Assert-OwnedStaleS1RunRoot`，陈旧目录只要求**哨兵文件存在且非空**，
   其余防误删约束**全部保留**；本轮自己的清理**仍走严格哨兵校验**，一点没放松。
4. **顺带**：修掉 WARN 里 `${($dir.FullName)}` 的插值错误（打出来的路径是空的）。

## 2. 缺陷链

| 环节 | 事实 |
|---|---|
| 哨兵生成 | 第 352 行 `$ownershipSentinel = [Guid]::NewGuid().ToString("N")` —— **每轮重新随机** |
| 哨兵写入 | 运行根创建后，把**本轮** GUID 写进 `<runRoot>\.vd-s1-owned` |
| 清扫调用 | 第 361 行 `Remove-StaleS1RunRoots -MaxAgeHours 24`，遍历 `%TEMP%\vd-s1-*` |
| 校验 | 对陈旧目录也调用 `Assert-SafeRunTreeForCleanup -SentinelValue $ownershipSentinel` |
| 必然失败 | 陈旧目录里的哨兵是**上一轮**的 GUID ≠ 本轮 GUID → 第 122 行抛异常 |
| 被吞 | 第 60–62 行 `catch` 只打印 WARN，后面的 `Remove-Item` 永远到不了 |
| 结果 | 清扫空转；硬杀 / 断电遗留永久累积 |

### 2.1 现场实证（修复前）

造 2 个 25 小时前、各自带哨兵的 `vd-s1-*` 目录，用**本文件里的原函数**执行清扫：

```
--- 清扫前存在 ---  exists=True ×2
--- 调用真实 Remove-StaleS1RunRoots ---
[WARN] 无法清理残留运行根 : Refusing cleanup because the ownership sentinel does not match
--- 清扫后状态 ---  仍存在 ×2
结论: 2 个陈旧运行根中，仍残留 2 个
```

（注意 WARN 里的路径是空的 —— 即 §3.3 的插值 bug。）

## 3. 修复

### 3.1 新增 `Assert-OwnedStaleS1RunRoot`

与 `Assert-SafeRunTreeForCleanup` 的**唯一差别**：不要求哨兵内容等于**本轮**随机 token，
只要求哨兵文件存在且非空。其余防误删约束**全部保留**：

- 必须是 `%TEMP%` 的**直接子项**（`Test-PathEqual` 比对父目录）；
- 名字必须匹配 `^vd-s1-[0-9a-f]{32}$`；
- 整棵目录树**不得含任何重解析点**（`Assert-NoReparsePoint` + 递归扫描）；
- 必须存在 `.vd-s1-owned` 哨兵文件且内容非空白。

### 3.2 `Remove-StaleS1RunRoots` 改用它

```powershell
foreach ($dir in $stale) {
    try {
        Assert-OwnedStaleS1RunRoot -Path $dir.FullName
        Remove-Item -LiteralPath $dir.FullName -Recurse -Force -ErrorAction Stop
    }
    catch { ... WARN ... }
}
```

`finally` 里清理**本轮**运行根仍调用 `Assert-SafeRunTreeForCleanup`（严格哨兵校验），
未做任何放松 —— 契约 `docs/contracts/path-isolation-contract.md` 未约束该哨兵
（它是 reports/125 自加的实现细节），故本改动不违反合约。

### 3.3 修掉 WARN 的路径插值

`"[WARN] ... ${($dir.FullName)}: ..."` → `$($dir.FullName)`。
原写法里 `${...}` 是变量名定界符，`${($dir.FullName)}` 被解析为名为 `($dir.FullName)`
的变量（不存在 → 空），所以 WARN 从未打印出真实路径，排障时无从下手。

## 4. 验证

| 项 | 方法 | 结果 |
|---|---|---|
| 语法 | `[Parser]::ParseFile`（脚本自带 `Set-StrictMode -Version Latest`） | **0 错误** |
| 单元·该删 | 25h 前遗留 + 有哨兵 | **已删除** |
| 单元·无哨兵 | 外来目录（名字合法但无 `.vd-s1-owned`） | **保留**（`sentinel is missing`） |
| 单元·空哨兵 | 哨兵文件为空 | **保留**（`sentinel is empty`） |
| 单元·含重解析点 | 树内含 junction | **保留**（`contains a reparse point`） |
| 端到端 ① | 埋 25h 前 `vd-s1-*`，真实调用本脚本（从非仓库根，只触发清扫） | 目录**已清除**，`%TEMP%\vd-s1-*` = 0 |
| 端到端 ② | 埋 30h 前 `vd-s1-*`，真实调用本脚本（仓库根，进入 preflight） | 目录**已清除**，残留 0 |

单元测试用的是**本文件里的真实函数代码**（按 `function` 边界从源文件抽取后再执行），
不是重写的等价逻辑。

端到端 ② 的最终退出码为 **97**，原因是端口 `8765` 上有 Value Dashboard 服务在监听，
`preflight Before` 按设计拒绝在服务运行时跑 S1（freeze 要求），与本次改动无关；
清扫在 preflight **之前**执行，已实证生效。因此本轮**未跑完整 S1**。

## 5. 遗留观察：`tmp.*` 是另一个泄漏源（与 wrapper 无关）

2026-11-16 10:43–10:58 在 `%TEMP%` 出现 **25 个 `tmp.<10位随机>` 目录，合计 3.28 GB**，
内部结构与 wrapper 的运行根一致（`pytest-tmp\test_*\`），但：

- **0 个**带 `.vd-s1-owned` 哨兵 → **不是** `s1-pytest.ps1` 创建的；
- 说明有调用方**绕过 wrapper**，直接给 `pytest` 指定了自己的 `--basetemp`。

已用 `scripts/cleanup_leaked_temp.ps1 -Execute` 清除（25/25 成功，回收 3.28 GB）。
wrapper 的自动清扫只匹配 `vd-s1-*`，管不到这类目录。

## 6. 运维纪律

1. **跑 S1 一律走 `scripts/s1-pytest.ps1`**，不要绕过 wrapper 自己给 `pytest --basetemp`；
   否则临时目录既不在受控运行根下，也不在自动清扫范围内。
2. 磁盘排查入口不变：`pwsh -NoProfile -File scripts/cleanup_leaked_temp.ps1`（先 dry-run）。
3. 出现"C 盘莫名减少几十 GB"时，先查 `%TEMP%\vd-s1-*` / `%TEMP%\tmp.*` / `%TEMP%\vd-restore-*`。
4. `-KeepRunRoot` 仅排障使用，用完手动删（脚本会打印确切路径）。
