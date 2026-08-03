---
title: 系统红队第七轮复审报告（第六轮 F1/F2/F3 修复后，2026-08-02）
status: superseded
category: reports
created: 2026-08-02
last-reviewed: 2026-08-02
supersedes: reports/37_REAUDIT_F1_F2_F3_FIX_2026-08-02.md
superseded-by: reports/39_SYSTEM_RED_TEAM_ROUND7_F4_FIX_2026-08-03.md
---

# 系统红队第七轮复审报告（第六轮 F1/F2/F3 修复后，2026-08-02）

> 独立复核提交 `3ce4e94` 与 `reports/37` 的「可启用」声明：逐行审查 F1/F2/F3
> 修复与测试、攻击相邻的全部正式导出路径、用隔离合成库执行真实 CLI 链路，
> 并重跑全量门禁。正式数据库全程只读。
>
> **裁决：BLOCK。** F1/F2 和 F3 的网页导出路径已真实关闭；但 F3 在原生
> `vd screening export_csv` 支持路径仍未关闭：CLI 已持久化 `truncated=true`，
> CLI 导出仍静默丢弃该信息，输出无 `_truncated` 列的截断 CSV。

## 1. 已核验关闭项

### F1/F2 季度单季值转换：关闭

- `app/web/api/stock_detail.py:122-146` 先捕获每字段的差分前累计原值 `cumulative`，
  使用 `本期累计 - 上期累计` 计算单季，并将**原累计值**而非已差分单季值写入
  `prior_by_year`。连续 Q1/Q2/Q3/Q4 不再复合错误。
- 同年首行非 Q1 明确置 NULL；Q1/Q3/Q4 缺 Q2 时 Q3 置 NULL，但 Q4 仍用
  Q4累计−Q3累计计算，消除级联过空。
- `test_stock_detail_periods.py:96-149` 实际执行完整四季度、年起 Q2、Q1/Q3/Q4
  三种夹具，非模拟测试。

### F3 CLI 运行 -> 保存 -> 网页导出：关闭

- `app/cli/main.py:645-652` 已在 CLI `screening run` 的 `confidence_summary` 持久化
  `truncated` 与 `total`。
- `test_research_path_integrity.py:287-365` 使用真实 Typer CLI run/save、SQLite
  持久化与 FastAPI web export，断言 `_truncated` 表头和行标记。该网页路径成立。

## 2. BLOCK 依据：F4 原生 CLI 导出仍静默丢失截断信息

- 位置：`app/cli/main.py:1392-1403`。
- 机理：该命令读取 `confidence_summary`，但只使用 `strict_only`；没有读取
  `summary["truncated"]`，也未向 CSV 表头/数据行追加 `_truncated`。因此它绕过了
  `app/web/api/screening.py:277-313` 中网页导出的标记逻辑。
- 对抗复现（隔离临时库，真实 Typer CLI）：10 只匹配股票，临时将
  `MAX_RESULT_ROWS=3`。`vd screening run` 返回 `truncated=true,total=10`；
  `vd screening save_result` 成功；随后真实 `vd screening export_csv` 输出表头：

```text
stock_code,pe_ttm,_data_date,_rule_id,_rule_version,_locked_indicators,_strict_only,_field_provenance,_entry_explanation
```

  不含 `_truncated`。
- 影响：PRD §12.5 的正式 CLI 导出能力对被截断结果静默输出上限内行数，用户无法
  区分「恰好命中上限」与「被截断」，仍违反 P1-C 的保存/导出不得静默丢尾要求。
- 测试缺口：现有 F3 测试只覆盖 CLI run -> save -> **web** export；全仓无任何
  `screening export_csv` 测试。

## 3. 门禁（本会话独立重跑）

| 门禁 | 结果 |
|---|---|
| `scripts/s1-pytest.ps1 tests/regression` | **406 passed**（156.56s） |
| `uv run --locked ruff check app tests/regression` | All checks passed |
| 前端 lint / node / vitest / build | 通过 / 52 passed / 10 passed / 成功 |
| 正式库 SHA-256（前后一致） | DuckDB `741C75BE...`、SQLite `3D41498F...` |
| git | 工作区干净（审查写文档前） |

## 4. 裁决与退出条件

**BLOCK。** 自动化门禁通过、季度计算与网页导出修复有效，不能抵消原生 CLI 导出
这一已实测的静默截断旁路。项目未达到「所有核心写操作与运维操作可由 CLI 完成」
及 CSV 导出完整溯源的 PRD §12.5/§16.1 要求。

退出条件：

1. `app/cli/main.py::screening_export_csv` 对齐网页导出：读取 `summary["truncated"]`，
   为 header 和每条数据行追加 `_truncated=True`；可复用公共 CSV 组装逻辑以防再次漂移。
2. 增加真实 CLI 回归：`screening run`（超上限）-> `save_result` -> `screening export_csv`，
   断言 header 与数据行均含 `_truncated`；另断言恰好上限时不标记。
3. 清理 STATUS.md 中已过时的 F1/F2/F3 "未修复"缺口文字，重跑全量门禁并更新证据。

## 5. 证据

- `docs/evidence/evidence-redteam-round7-gates-20260802.json`
- `docs/evidence/evidence-redteam-round7-repro-20260802.json`
- `.planning/2026-08-02-system-red-team-round7/`
