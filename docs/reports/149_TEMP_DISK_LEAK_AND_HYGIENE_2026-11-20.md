---
title: 临时目录泄漏把 C 盘撑满（107 GB）与根治方案
status: approved
category: reports
created: 2026-11-20
last-reviewed: 2026-11-20
---

# 临时目录泄漏把 C 盘撑满（107 GB）与根治方案

## 1. 事故

`%TEMP%` 一度涨到 **107.12 GB**，直接后果是**全量回归出现 50 个
`OSError: [Errno 28] No space left on device`**（`pytest` 里表现为
`could not create numbered dir with prefix … after 10 tries`、`unable to open database file`）。

**这个症状极具误导性**：它长得像"刚才那次重构把代码搞坏了"。
本轮我自己就先怀疑合并/重构出了问题，白花了一轮排查才想到去看 `df`。
**教训：pytest 里出现几十个 ERROR + 打不开数据库，先看磁盘，再怀疑代码。**

## 2. 来源：不是项目代码，是"跑测试的人"手搓的运行根

项目代码自己只会造三类临时目录，且都有人清：
`vd-s1-*`（S1 包装器，`finally` 兜底 + 启动清扫）、`vd-restore-*`（备份）、
`vd-cold-measure-*`（冷库）。

真正的泄漏源是**靶向测试/探针的标准写法**：
```bash
RUN="$(mktemp -d)/vd-xxx" && mkdir -p "$RUN/tmp"
VD_ENV=test VD_TEST_RUN_ROOT="$RUN" … pytest <files> -q --basetemp="$RUN/tmp"
# ↑ 没有任何一行负责删除
```
实测 19 小时累积 **455 个 `tmp.<随机>` 目录 / 74.11 GB**，另有
`vd-s1-*` 12 个 / 12.14 GB、`vd-r4-*`/`vd-L*b`/`vd-final*`/`t4cost`/`fxsplit` 等 15 个 / 5.4 GB。
**最近 2 小时仍在以约 4 GB/小时 增长**（清理过程中 5 分钟又涨 2.3 GB）。

为什么此前那道修复挡不住：`cleanup_leaked_temp.ps1` 原本只删
**带 `.vd-s1-owned` 哨兵**且 **>24 小时**的目录 —— 手搓的目录**没有哨兵**、且大多只有几小时。

## 3. 为什么"记得清"不可行

手写 `try/finally` 也救不了：那段命令是**复制粘贴到 shell 里**执行的，
一旦 agent 中途被打断、超时被杀、或单纯忘了写 `rm -rf`，就漏一次。
**能保证的做法只有一个：把"创建"和"删除"放进同一个进程的 `finally`。**

## 4. 根治方案（三层，缺一不可）

### 4.1 入口层：`scripts/scoped_pytest.py` —— 靶向测试的唯一入口
- 建根（带拥有者标记 `.vd-scratch-owner` + 活性标记 `.vd-active`）→ 设环境变量 →
  起 pytest 子进程 → **`finally` 里无条件删根**（正常返回/非零退出/抛异常/Ctrl-C 都删）。
- **自愈**：每次开跑前先回收"旧的、没在跑的" scratch 根（默认空闲 >120 分钟）——
  即使上一次被硬杀，下一次运行会自己收拾干净。
- `--keep` 是显式调试逃生口（默认永远删）。

### 4.2 实现层：`scripts/temp_hygiene.py` —— 判定与删除的唯一实现
- **有主才回收**：只认"带 `.vd-scratch-owner`"或固定前缀（`vd-scoped-`/`vd-run-`）；
  历史手搓的（`vd-r*`/`vd-L*`/`vd-M*`/`vdbatch-*`/`t4cost`/`fxsplit`…）与 `tmp.*`
  **必须显式开 `--include-legacy` / `--include-tmp`** 才碰 —— 不做"`%TEMP%` 里所有旧目录"的乱删。
- **活着的不动**：带 `.vd-active`、或 mtime 在 10 分钟内的跳过。
- 默认 **dry-run**，加 `--execute` 才真删。
- `scripts/cleanup_leaked_temp.ps1` 已改为**委托**本模块（激进口径：`--include-legacy --include-tmp`），
  避免出现两份删除逻辑。

### 4.3 门禁层：`session_preflight.py` 的临时目录预算
- 我们造的运行根 **>5 GB → WARN**、**>15 GB → BLOCK**，并给出**一条**可执行清理命令。
- 目的：**在 10 GB 就拦住，而不是等 107 GB 靠"测试跑不动了"才发现**。
- 探测本身失败 → 降级放行（fail-open，与既有 preflight 原则一致）。

### 4.4 流程层：写进 `AGENTS W6`
- 靶向测试**只准**走 `scripts/scoped_pytest.py`；**禁止** `mktemp -d` + `--basetemp` 手搓。
- `s1-pytest.ps1` 每次全量开跑前自动执行一次 `temp_hygiene --older-than-minutes 120 --execute`。

## 5. 验证

| 项 | 证据 |
|---|---|
| 跑完必删 | `test_scoped_pytest_deletes_run_root_even_when_runner_raises` / `…on_nonzero_exit`；**变异**（把 `rmtree` 去掉）→ 两条同时红 |
| 自愈回收 | `test_scoped_pytest_reclaims_stale_roots_before_running` |
| 清扫不越界 | `test_prune_only_touches_owned_scratch_dirs`（别的程序目录、只像我们的目录都不碰） |
| 不动活着的 | `test_prune_skips_active_and_recent_roots` |
| 预算门禁 | `test_preflight_temp_check_passes_when_clean` / `…warns_then_blocks` / `…fails_open` |
| 实测 | 跑完一个真测试后 `%TEMP%` 条目数 **94 → 94**（无残留）；清理历史 75 个目录 **16 GB → 579 MB** |

## 6. 残留与后续
- 历史 `tmp.*` 目录**无法从名字判断归属**（Python `tempfile` 默认就是这个前缀），
  所以只能靠"时间 + 显式开关"清理；今后一律用 `scoped_pytest` 就不会再产生无主目录。
- 若将来引入并行测试（xdist）或别的语言栈，需把同样的"根生命周期由进程保证"模式复制过去，
  **不要**回到"文档里写一句记得清"。
