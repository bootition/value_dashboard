---
title: 修复后系统红队复审报告（第六轮，独立复核，2026-08-02）
status: approved
category: reports
created: 2026-08-02
last-reviewed: 2026-08-02
supersedes: reports/35_SYSTEM_RED_TEAM_FIX_2026-08-02.md
---

# 修复后系统红队复审报告（第六轮，独立复核，2026-08-02）

> 对 `reports/35`（d9358db）宣称的「4 项 P1 全部关闭、可启用」进行独立复审：
> 不信任修复结论，逐文件审计修复提交 + 调用方 + 新增测试，以**真实 API
> 调用序**对抗复现 P1-A/P1-C 路径，并重跑全部门禁。正式数据库全程只读。
>
> **裁决：BLOCK。** P1-A 与 P1-C 未真正关闭：季度单季值转换在**常态连续
> 场景**下静默产出错误数值（F1/F2），CLI→保存→导出链路的截断标记丢失
> （F3）；P1-B 与 P1-D 修复经独立审计成立。全部自动化门禁通过，但项目
> 「结论永不静默降级」的核心合同仍被主要研究路径违反。

## 1. 复核方法

- 基线：`reports/35` 关闭声明 + `d9358db` 提交全量 diff（74 文件，+613/−15890）。
- 源码审计：`_to_single_quarter`、筛选引擎计数查询、CLI `screening run/save_result`、web 导出、适配器/股票池门禁、前端截断警示及其全部调用方与测试。
- 对抗复现：以真实调用方输入序（`ORDER BY report_date DESC` + 函数内 reverse）构造合成财务行，直接调用 `_to_single_quarter`。
- 门禁重跑：S1 隔离回归、ruff、前端 lint/test；正式库哈希前后比对。

## 2. 门禁实测（本次会话独立重跑）

| 门禁 | 结果 |
|---|---|
| `scripts/s1-pytest.ps1 tests/regression` | **402 passed**（191.5s，含新增 `test_research_path_integrity.py` 8 项） |
| `uv run --locked ruff check app tests/regression` | All checks passed |
| 前端 lint / node / vitest | 通过 / 52 passed / 10 passed |
| 正式库 SHA-256（前后一致） | DuckDB `741C75BE…`、SQLite `3D41498F…` |
| git | 工作区干净 |

## 3. 经核验成立的修复（P1-B、P1-D、P1-C 的 web 路径）

- **P1-B**：按交易所的退市门禁（本次抓取数 < 当前上市数 90% → 跳过退市标记 + `delist_guarded_exchanges` 披露）；适配器部分响应 → `approximate` + 错误；`update.py` 仅 success 时刷新节流标记；CLI/init/update 均如实传播 partial。首跑（listed=0）与交易所缺席响应行为正确。
- **P1-D**：API 指标列表已暴露 `{field}_industry_rank/_industry_percentile`（CSRC，真实标签），无「申万」字样；存量规则引用 `_sw1_rank` 仍兼容；测试断言标签契约。
- **P1-C（web 路径）**：计数查询与结果查询共用同一 WITH 骨架、参数对齐、strict_clause 合并正确；`total`=真实匹配数、`truncated = total > len(results)`；`MAX_RESULT_ROWS` 为唯一结果 LIMIT；前端截断警示 + 组件测试存在。

## 4. BLOCK 依据：3 个残余 P1（全部实测复现/源码确认，无测试覆盖）

### F1 — P1-A 未关闭：每年首行不是 Q1 时，累计值被当作单季值直通输出

- 位置：`app/web/api/stock_detail.py:118-135`。`prior is None`（同一年首行）时行内容原样保留，`_quarter_index` 只防护差分分支——与修复文档串「绝不把累计值……当作单季值输出」自相矛盾。
- 实测（真实调用序，年初即 Q2：累计 230/400/580）：Q2 输出 **230.0**（应为 NULL，单季值不可推导）、Q3=170.0（碰巧正确）、Q4=**410.0**（真值 180）。静默错误。
- 测试缺口：新增两条夹具均以 Q1 起年（`test_stock_detail_periods.py:53-93`），无「年起于非 Q1」夹具。

### F2 — P1-A 未关闭（更严重）：连续季度差分复合错误，常态路径 Q3/Q4 系统性失真

- 位置：`app/web/api/stock_detail.py:126-135`。`prior_by_year[year]` 在行被原地差分**之后**取值，下一季度减去的"上一季度累计"实为上一季度的**单季值**。
- 实测（真实调用序，完全连续年度，累计 100/230/400/580）：推导单季 100/130/**270**/**310**；真值 100/130/**170**/**180**。**任何一年内有 ≥3 个报告期的股票，季度视图 Q3/Q4 全部静默错误**——这是常态，不是缺口场景。
- 测试缺口：既有连续用例（`test_stock_detail_periods.py:12-35`）同一年内最多 Q1→Q2；全仓无「同一年 ≥3 个连续季度」夹具。

### F3 — P1-C 未关闭：CLI 运行→保存→web 导出的截断标记丢失

- 位置：`app/cli/main.py:645`（`confidence_summary = {"strict_only", "locked_indicators"}`，无 `truncated`/`total`）；`main.py:684-685`（save_result 原样复制该 summary）；`app/web/api/screening.py:277-278`（导出从 summary 读 `truncated` → 恒 False）→ `_truncated` 表头/行省略（297-313）。
- 触发序列：池 > 5000（正式池 5,534）→ `vd screening run`（引擎返回 truncated=True，但未持久化）→ `vd screening save_result` → 对已保存结果 web 导出 CSV → **5,000 行、无 `_truncated` 列、total 未知的静默截断导出**。正是 `reports/35` 声称关闭的「保存结果与 CSV 导出同病」条款，CLI 支持路径仍然开口。
- 测试缺口：`test_research_path_integrity.py:177-209` 名为「API 透传 truncated」实只断言引擎键；全仓无任何测试断言 `_truncated` CSV 列或 CLI 持久化路径。

## 5. 次要发现（P2，复审新增，无新 P0）

1. P1-A 级联过空：Q1/Q3/Q4（仅缺 Q2）时 Q3=NULL（正确）但 Q4 也被置 NULL（Q4−Q3 实际可推导）。保守、仅可用性损失。
2. P1-C 存量结果：修复前保存的运行无 `truncated` 键，真正被截断的存量结果导出仍无标记；无迁移/回填。
3. P1-C 性能：计数查询每次全量重跑 WITH 链（含窗口排名），SQL 成本约 ×2（~451ms→~900ms），仍在 PRD §19.1 5 秒 SLA 内，不阻断。
4. P1-B 溯源矛盾（minor）：适配器**静默**截断（无错误）时 `init.py:284` 仍以 `confidence="strict"` 记录该截断批次，而步骤级报 `partial`。
5. P1-D 命名残留：`_sw1_rank/_sw2_rank` 列仍产出并被 API 列出（标签已真实化），仅命名遗留，`reports/35` 已披露。

## 6. 裁决

**BLOCK（不可正式启用）。**

`reports/35` 的「4 项 P1 全部关闭」声明不成立：P1-A 在**常态**下（而非缺口下）静默产出错误的季度单季值（F2 为系统性、F1 为年起非 Q1 场景），P1-C 在 CLI→保存→导出支持路径上静默丢失截断标记。两者均无测试覆盖且已对抗实测。P1-B/P1-D 修复成立、门禁全绿、无 P0——但季度研究视图（PRD §14）与 CSV 导出（PRD §12.5）仍静默违反可信度合同，故维持 BLOCK。

## 7. 退出条件（小而精确）

1. **F2**：`prior_by_year` 存入差分**前**的累计原值（单季 = 本期累计 − 上期累计，再以原累计值供下期）；补「同一年 4 个连续季度」回归测试断言 100/130/170/180。
2. **F1**：年内首行非 Q1 时按不可推导处理 → NULL（或要求紧邻上一季度，绝不直通累计值）；补「年起于 Q2」夹具测试。
3. **F3**：CLI `screening.run` 的 `confidence_summary` 持久化 `truncated` 与 `total`（与 web 路径对齐）；补「CLI run → save_result → web export 含 `_truncated` 列」回归测试；评估存量结果回填。
4. 重跑全部门禁，更新 STATUS.md、文档地图、证据与 README 裁决行。

## 8. 证据索引

- 复现结果：`docs/evidence/evidence-redteam-reaudit-repro-20260802.json`
- 门禁结果：`docs/evidence/evidence-redteam-reaudit-gates-20260802.json`
- 会话产物：`.planning/2026-08-02-system-red-team-reaudit/`（task_plan/findings/progress）
