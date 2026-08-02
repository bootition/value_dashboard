# 项目当前状态（单一真相源 / Single Source of Truth）

> **本文件是项目当前状态的唯一权威来源。** 任何建议、结论、验收判断必须以此为准；
> `reports/`、`archive/` 中的历史报告只作为追溯证据，不构成当前结论。
> 修改任何代码/数据/文档后，如影响状态，必须同步更新本文件。

- **最后更新**：2026-08-02
- **更新人**：opencode 会话（2026-08-02 第五轮全项目系统性红队独立复核）

## 当前裁决（Verdict）

| 层面 | 状态 | 依据 |
|---|---|---|
| 整体启用 | ❌ **BLOCK**（第五轮独立复核发现 4 个未缓解 P1：季度单季值造假、股票池静默退市、筛选 LIMIT 5000 静默截断、CSRC/申万排名口径错标；均无测试覆盖、不在此前关闭清单内） | `reports/34`（本裁决依据，取代 30–33 的"全部关闭"声明） |
| 代码层门禁 | ✅ 可自动化门禁全部通过（S1 回归 392、Ruff 零问题、前端 lint/52 node + 9 组件测试、build；2026-08-02 独立重跑） | `reports/34` §2 |
| 安全控制 | ✅ 无 P0 安全项（注入/穿越/代码执行/空值覆盖/并发写入/Web 写面均有防护与测试） | `reports/34` §3 |
| 数据层 P0-1（股本单位混用） | ✅ 已关闭（5,534 只重建，`circ_shares > total_shares` 1,215 → 0） | `reports/29` |
| 数据层整体（ready） | ✅ ready=TRUE、warning_codes=[]（2026-08-02 正式库只读复验；`snapshot_period_mismatches`=0，筛选 451ms/3,878 只） | `reports/29`、`reports/32`、`docs/evidence/evidence-formal-*20260802.json` |
| 30 股外部真值抽样 | ✅ 已执行（收盘 27/27、总股本 27/27；2 只流通股本为解禁时间差披露项） | `reports/29` |
| 回归/发布验证 | ✅ 前端 + S1 全绿；正式库筛选可用；性能隔离基准就绪（PRD §19.1 目标主机仪式步骤待执行） | `reports/32`、`reports/34` §2 |

## 已知剩余缺口（诚实披露，未消除前不得宣称数据完整）

1. **第五轮红队 4 项 P1（BLOCK 依据，未修复）**：季度单季值造假（`app/web/api/stock_detail.py:94-112`）、股票池静默退市（`app/core/init.py:227-255`）、筛选 LIMIT 5000 静默截断（`app/core/screening/engine.py:416`）、CSRC 计算却以申万命名暴露（`engine.py:366-385` + `screening.py:428-429`）。修复与回归测试要求见 `reports/34` §7。
2. **920305**：极新股，所有免费源无数据（价格/股本/分红缺失，如实记录 missing）。
3. **银行/券商监管字段 92 只**（资本充足率/不良贷款率/拨备覆盖率/风险覆盖率）：免费结构化 API 不可得，保持 NULL，不伪造。
4. **2026-03-31 之前历史期财务**：CSMAR 商业导入值保留，无原始字节 lineage（约 253 万条空 payload 已隔离至 quarantine 表，不删除）。
5. **东财源被封**：已迁移至腾讯/Sina/BaoStock/交易所官方名单；东财适配器保留在回退链末端，网络恢复后自动可用。

## 进行中的工作

- **自动数据更新 + CSRC 行业分类**（最新会话）：PRD 修订已完成（`decisions/01` §7.3/7.4/7.7/15/16.1/20.4/21/22/24），实施计划见 `.planning/2026-07-31-automatic-data-updates/implementation-plan.md`（Phase A–G）。

## 当前有效文档（Current Truth）

| 文档 | 用途 | 注意 |
|---|---|---|
| `docs/STATUS.md` | 本文件：当前状态唯一权威 | 每次状态变化必须更新 |
| `docs/decisions/01_PRODUCT_REQUIREMENTS_V1.md` | 产品需求规格（验收合同） | **活文档**：随功能演进修订，2026-07-31 修订了自动更新/CSRC 章节 |
| `docs/decisions/02_TECH_CONSTRAINTS.md` | 技术约束清单 | 约束冲突时以 README/实施为准时需人工裁决 |
| `docs/reports/29_DATA_REBUILD_REPORT_2026-07-31.md` | 最新数据重建报告（P0-1 关闭依据） | 数据层当前结论基线 |
| `docs/reports/30_AUDIT_FIX_CLOSURE_2026-08-02.md` | 审计修复闭环报告（`reports/27` 代码级 P1/P2 全部关闭依据） | 代码层审计结论基线 |
| `docs/reports/31_RELEASE_RED_TEAM_FIX_2026-08-02.md` | 发布级红队 P0 修复报告（6 项 P0 关闭依据） | 发布级审查结论基线 |
| `docs/reports/32_RELEASE_P1_FIX_AND_FORMAL_ACCEPTANCE_2026-08-02.md` | 发布级红队 P1 修复 + 正式库只读验收报告 | 发布级 P1 关闭依据 + 正式库当前状态基线 |
| `docs/reports/33_FOURTH_ROUND_REVIEW_FIX_2026-08-02.md` | 第四轮复测修复报告（build-release 清理竞态 P1 + 2 项 P2） | 修复事实保留，**"全部关闭"结论已被 `reports/34` 取代** |
| `docs/reports/34_SYSTEM_RED_TEAM_REVIEW_2026-08-02.md` | **当前裁决依据**：第五轮全项目系统性红队独立复核（BLOCK + 4 项 P1 + 退出条件） | **当前发布裁决基线** |
| `docs/runbooks/s0-evidence-preservation.md` | 证据保全运行手册 | |
| `docs/contracts/path-isolation-contract.md` | 路径隔离合同（签署版） | |
| `.planning/2026-07-31-automatic-data-updates/` | 当前实施会话计划 | 会话产物，不入 docs/ |

## 已被取代的结论（Superseded，禁止引用为当前结论）

- `reports/25`（7-30 正式验收 "数据层 BLOCK"）→ 数据层结论已被 `reports/29` 取代（P0-1 已修复）；代码层结论仍有效。
- `reports/27`（7-31 综合红队复审 BLOCK，1 P0 + 20 P1）→ **P0-1 已关闭**（见 `reports/29`）；**其余代码级 P1 项已全部关闭**（见 `reports/30`）。
- `reports/28`（7-31 独立红队复审，维持 BLOCK）→ 同 27，数据结论被 29 更新，代码结论被 30 更新。
- `reports/30`–`reports/33`（8-02 审计修复闭环 + 发布级 P0/P1 修复 + 正式库验收 + 第四轮复测）→ **修复事实仍有效**，但其"代码级/发布级 P0/P1/P2 全部关闭"的整体结论已被 `reports/34`（第五轮独立复核，BLOCK）取代。
- 更早编号报告（05–24、26）→ 全部 superseded，仅作追溯证据。

## 维护规则（写文档的人必须遵守）

1. **状态变化时**：更新本文件 → 将旧报告 front-matter 的 `status` 改为 `superseded` 并写 `superseded-by` → 新报告/文档必须带 front-matter 且 `status: approved`。
2. **报告类文档**（审计/验收/审查）一律放入 `docs/reports/`，命名 `NN_主题_YYYY-MM-DD.md`。
3. **新功能/修订**：先改 `decisions/01`（PRD）→ 计划放 `.planning/` → 实施 → 验收报告放 `reports/` → 回写本文件。
4. **会话产物**（findings/progress/task_plan）只放 `.planning/`，严禁写入 `docs/`。
5. **机器证据**（JSON/hash）只放 `docs/evidence/`。
6. 给 AI 的建议/结论必须附带依据文档路径与 `last-reviewed` 日期。
