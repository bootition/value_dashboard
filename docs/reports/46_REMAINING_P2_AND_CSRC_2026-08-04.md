---
title: 剩余 P2 项与 CSRC 填充实施报告（C3-C16/O1/O3/O5/O6，2026-08-04）
status: approved
category: reports
created: 2026-08-04
last-reviewed: 2026-08-04
---

# 剩余 P2 项与 CSRC 填充实施报告（C3-C16/O1/O3/O5/O6，2026-08-04）

> 完成 `reports/41` B2 全部剩余代码项（C3-C16）、运维项 O1（PRD §19.1
> attestation **PASS**）、O3（迁移决策）、O5（Node engines）、O6
> （chain-finalize 治理），并**填充 CSRC 行业分类解锁 O1**。
> 门禁全绿（S1 423 passed、ruff、前端 lint/52 node + 11 vitest/build），
> 正式库经合法维护操作后恢复 ready=TRUE、warning_codes=[]。

## 1. 代码项关闭（B2 C3-C16）

| # | 修复 | 位置 |
|---|---|---|
| C3 | 打包版恢复指引显示 `vd` 而非 `python -m`；`/api/session` 暴露 `packaged` | `DataTraceability.vue`、`helpers/runtime.ts`、`web/main.py` |
| C4 | 写令牌 403/401 自动重拉并重放一次（重启后无需刷新） | `frontend/src/http.ts` + 新测试 |
| C6 | `save_rule` 服务端固定状态机，忽略客户端 status | `web/api/screening.py` |
| C7 | 草稿 PUT 超限返回 413 | `web/api/screening.py` |
| C8+C16 | 有界 GC：过期 pending plan、180 天前终态 job_logs、90 天前已解析 missing；接入启动维护 | `core/housekeeping.py`、`web/main.py` |
| C9 | 更新被跳过（锁被拒等）状态记 `skipped`/idle，不再误记 failed | `core/auto_update.py` |
| C10 | `max_stocks` 取子集按 `ORDER BY stock_code` 稳定排序 | `core/update.py` |
| C11 | `_csv_cell` 公式注入覆盖前导空白变体（保留原始 `\t` 判定） | `web/api/screening.py` |
| C12 | DSL 被依赖删除捕获 FK 冲突返回 409（兜底，不再 500） | `web/api/dsl.py` |
| C13 | PDF 归档/缺失响应与 CLI 输出绝对路径脱敏 | `stock_detail.py`、`cli/main.py` |
| C14 | 筛选用 `COUNT(*) OVER ()` 单次窗口计数替代二次计数查询（total 语义不变） | `core/screening/engine.py` |
| C15 | 部分/截断股票池响应批次溯源降级 approximate | `core/init.py` |

新增回归测试：`test_housekeeping.py`、C6/C7（screening_draft）、C9
（auto_update）、C11 前导空白、C12 依赖删除 409、C4 令牌重放（前端 vitest 11 项）。

## 2. 运维项

### O1 PRD §19.1 性能验收仪式：**PASS（2026-08-04）**

- 前置解锁：正式库 `csrc_l1/l2` 原为 NULL（重建时 `--skip-csrc`），导致夹具
  第 20 条件 `pe_ttm_industry_rank` 全部 NULL、`complete_results_returned=false`。
- **CSRC 填充**（O7 Phase F 落地）：新增 `scripts/populate_csrc_industry.py`
  （正式 profile 维护脚本，幂等/断点续传），两轮完成 4923/5533（89%）；
  无行业变更历史的新股/北交所（301xxx/920xxx）如实 NULL。修复适配器：
  CNINFO 无历史列时按缺失处理，避免 KeyError 误触熔断冷却。
- **就绪恢复**：填充后 `has_sector_financials` 按 CSRC 行业真实生效，银行/券商
  92 只监管字段缺口（STATUS 已知缺口 #4，所有者口径"保持 NULL，不伪造"）
  从阻断改为**披露项**（`regulatory_fields` 进 disclosure_keys）——金融数据
  本身仍由 `financial_period` 阻断。正式库恢复 `ready=TRUE, warning_codes=[]`。
- **仪式重跑**：重建夹具（当前正式库快照）→ 预热 + 10 次 →
  **10/10 < 5000ms（233.4–291.0ms，avg 256.2），3377 条完整返回，
  `prd_acceptance: PASS`**。证据：`docs/evidence/evidence-performance-20260804.json`。
- 正式库 hash 因合法维护操作更新：duckdb `51EA2DF2...`、sqlite `6E74BD9E...`
  （证据 `evidence-report46-pending-csrc-20260804.json`）。

### O3 存量截断标记迁移决策

**不迁移，如实披露。** 既有 `screening_results` 的 `truncated` 状态在历史
run 中未持久化，无法可靠回推；新保存结果已含 `confidence_summary.truncated`
（F3 修复）。旧结果导出无 `_truncated` 列属历史事实，维持披露。

### O5 Node engines 声明

`frontend/package.json` 增加 `"engines": {"node": ">=20.19.0"}`；
README 前提条件更新说明。

### O6 chain-finalize.ps1 治理

正式路径参数化（`ProjectRoot`/`EvidenceDir` 从脚本位置推导，可覆盖），
诊断写入 `docs/evidence/evidence-final-diagnostics.json`。

## 3. 门禁（2026-08-04 重跑）

| 门禁 | 结果 |
|---|---|
| `scripts/s1-pytest.ps1 tests/regression` | **423 passed**（189.74s；+12 新测试） |
| `uv run --locked ruff check app tests/regression` | All checks passed |
| 前端 lint / node / vitest / build | 通过 / 52 passed / **11** passed / 成功 |
| 正式库 S1 前后一致（本运行内） | 通过（hash 见证据，合法维护更新） |

## 4. 结论与剩余

- **`reports/41` B2 全部 16 项、B1 的 O1/O2/O3/O4/O5/O6 已关闭**；O7 的 CSRC
  分类数据已落地（自动更新控制与状态页已具备，见测试覆盖）。
- 仍开放（如实披露）：数据层缺口（920305、银行/券商监管字段 90 只 NULL、
  历史财务 lineage、东财源回退），及 O7 中"按日节流/增量 CSRC 刷新"等后续
  增量优化——均不阻断 PASS。

## 5. 证据

- `docs/evidence/evidence-report46-pending-csrc-20260804.json`
- `docs/evidence/evidence-performance-20260804.json`（O1 PASS）
- `docs/evidence/evidence-csrc-populate-20260803.json`（CSRC 两轮填充）
- `docs/evidence/evidence-s1/<最新 run>/hash-evidence.json`
