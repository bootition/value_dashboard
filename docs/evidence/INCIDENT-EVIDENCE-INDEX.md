---
title: 事故证据索引（事故机器证据在哪儿、怎么取回）
status: approved
category: reference
created: 2026-11-19
last-reviewed: 2026-11-19
---

# 事故证据索引

> **为什么有这份文件**：`AGENTS.md` §Git 纪律 2 明确规定
> `docs/evidence/evidence-s0/` 与 `docs/evidence/evidence-s1/` **永不提交**（体积 + 生成物属性），
> 于是**事故期的机器证据也就一起被挡在 git 之外**。这本是合理取舍，但副作用是：
> 一个新人（或一年后的 AI）根本不知道"出事时留下了什么证据、在哪儿"。
> 本文件是那份"索引"，**只记位置与取回方法，不重复证据内容**。

## 1. 2026-07-22 测试写坏正式库（`incident-2026-07-22`）

唯一一次真正的数据事故。证据有三处，**互相独立**：

| 载体 | 位置 | 在 git 里吗 |
|---|---|---|
| **事故源代码基线** | git tag `incident-2026-07-22`（commit `80b9a87`） | ✅ 是 |
| **正式库前后哈希取证**（10 个文件） | 盘上 `docs/evidence/evidence-s1/baseline-commit/` | ❌ 被 `.gitignore:64` 挡掉；**但历史里有** |
| **历史快照** | commit `f4517d2`（2026-07-24，「capture incident source baseline before S1 remediation」）下的旧路径 `docs/evidence-s1/baseline-commit/` | ✅ 是 |

**从 git 历史取回那 10 个文件**（不需要 force-add、不需要改 ignore 规则）：

```bash
# 看一眼有哪些
git ls-tree --name-only f4517d2 docs/evidence-s1/baseline-commit/

# 取回某一个
git show f4517d2:docs/evidence-s1/baseline-commit/formal-duckdb-hash.txt

# 整个目录导出到临时位置
git archive f4517d2 docs/evidence-s1/baseline-commit | tar -x -C /tmp/
```

里面记的就是事故判据本身：`Formal DuckDB Hash: 5186E660…`，
与盘上 `docs/evidence/evidence-s1/baseline-commit/formal-duckdb-hash.txt` 的
`Incident expected: 5186E660…` 一致 —— **这条一致就是"测试没有污染正式库"的原始证明**。

## 2. `_legacy/docs-evidence-s1-20260901/`（89 个 JSON）—— **不是**事故证据

容易误认，先澄清（2026-11-19 实测鉴定）：

- 内容：S1 路径隔离契约的**逐次运行**取证（每次跑测试前后对正式库取 sha256，
  `delta_detected: false` 证明没污染）
- 时间：**全部是 2026-08-01 09:53–13:17**（事故后 10 天的整改期合规取证）
- `grep -ril "incident" _legacy/` → **0**；`grep -rl "2026-07-22" _legacy/` → **0**
- 与现行 `docs/evidence/evidence-s1/` 的 700+ 目录比对：**同名 0、同内容 0**（所以"重复品"不成立）

**结论**：它有价值（历史时点快照，DB 当时 3.75GB、现已 28GB，不可复现），但**不是事故证据**，
且**不构成"唯一副本"风险**（`_legacy/` 被 gitignore，删了就没了，所以保留）。
`reports/136` §6.6 把 `_legacy/` 描述成"旧代码副本"已不成立 —— 勘误写在 `_legacy/README.md` 末尾。

## 3. 那次事故为什么会发生、现在靠什么防住

| 防线 | 位置 | 说明 |
|---|---|---|
| 路径隔离 | `app/core/storage/path_policy.py` + `scripts/s1-pytest.ps1` | 测试只能写隔离 run root，物理上碰不到 `data/` |
| 前后指纹 | `scripts/s1-pytest.ps1` 的 evidence 捕获 | 跑前跑后对正式库取指纹，`pre == post` 才算通过 |
| 单写者 | `app/core/storage/update_lock.py` | 自动更新子进程 / CLI / Web 三方不能并发写 |
| 回归门禁 | `tests/regression/` + 上面的 wrapper | 见 `AGENTS W6`：禁止裸跑全量 |

**这四项都在 `AGENTS.md` 的「不要删清单」里**，删了就会重演 2026-07-22。

## 4. 如果要让证据真正入库（未做，需人决策）

现状是"历史里能捞回来"，但不直观。三个可选做法，各有代价：

| 做法 | 代价 |
|---|---|
| `git add -f docs/evidence/evidence-s0/s0-20260724-015415`（132KB） | 精确、零风险，但**违反 AGENTS 现行规则**；需同步改规则 |
| 把 `.gitignore` 的 `docs/evidence/evidence-s0/` 改成 `docs/evidence/evidence-s0/*` + 逐条 `!` | git 不下潜被排除的目录，要放开就得一口气放开 700+ 目录 —— **极易重演 `[0-9a-f]*` 吞掉 fixtures 那类事故** |
| 维持现状 + 本索引文件 | 零风险，但依赖人先读到本文件 |

**当前裁决**：维持现状 + 本索引。若哪天认定事故证据必须随仓库分发，再按第 1 行做，
并**同时**修改 `AGENTS.md` §Git 纪律 2 的表述，别让规则和事实打架。

## 5. 维护规则

- 每次**新的**事故取证，在本文件第 1 节加一行（事故编号 / tag / 证据位置 / 是否在 git 里）。
- 本文件**只记位置与取回命令**，不复述证据内容（`AGENTS §0` 单一来源协议）。
- 新增事故 tag 时，tag 名统一用 `incident-YYYY-MM-DD`。
