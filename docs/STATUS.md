# 项目当前状态（单一真相源 / Single Source of Truth）

> **本文件是项目当前状态的唯一权威来源。** 任何建议、结论、验收判断必须以此为准；
> `reports/`、`archive/` 中的历史报告只作为追溯证据，不构成当前结论。
> 修改任何代码/数据/文档后，如影响状态，必须同步更新本文件。
- **最后更新**：2026-08-02
- **更新人**：opencode 会话（2026-08-02 第七轮系统红队独立复审）
## 当前裁决（Verdict）
| 层面 | 状态 | 依据 |
|---|---|---|
| 整体启用 | ❌ **BLOCK**（第七轮独立复审：F1/F2 与网页导出已修复，但原生 `vd screening export_csv` 仍静默丢失已持久化的 `truncated` 标记；F4 已隔离实测复现） | `reports/38`（取代 `reports/37` 的"可启用"裁决） |
| 代码层门禁 | ✅ 可自动化门禁全部通过（S1 回归 406、Ruff 零问题、前端 lint/52 node + 10 组件测试；2026-08-02 独立重跑） | `reports/38` §3 |
| 安全控制 | ✅ 无 P0 安全项（注入/穿越/代码执行/空值覆盖/并发写入/Web 写面均有防护与测试） | `reports/34` §3 |
| 数据层 P0-1（股本单位混用） | ✅ 已关闭（5,534 只重建，`circ_shares > total_shares` 1,215 → 0） | `reports/29` |
| 数据层整体（ready） | ✅ ready=TRUE、warning_codes=[]（2026-08-02 正式库只读复验；`snapshot_period_mismatches`=0，筛选 451ms/3,878 只） | `reports/29`、`reports/32`、`docs/evidence/evidence-formal-*20260802.json` |
| 30 股外部真值抽样 | ✅ 已执行（收盘 27/27、总股本 27/27；2 只流通股本为解禁时间差披露项） | `reports/29` |
| 回归/发布验证 | ✅ 前端 + S1 全绿；正式库筛选可用；性能隔离基准就绪（PRD §19.1 目标主机仪式步骤待执行） | `reports/32`、`reports/38` §3 |
## 已知剩余缺口（诚实披露，未消除前不得宣称数据完整）
1. **第七轮 F4（BLOCK 依据，未修复）**：原生 `vd screening export_csv` 忽略已持久化的 `confidence_summary.truncated`，截断结果输出无 `_truncated` 列的静默 CSV（`app/cli/main.py:1392-1403`）。真实隔离 CLI run→save→export 已复现；退出条件见 `reports/38` §4。
2. **次要 P2（见 `reports/34` §5、`reports/36` §5 与 `reports/38` §4）**：存量结果无截断标记迁移、计数查询成本×2、P1-B 溯源 strict 不一致、草稿 409 后自动保存停用、strict-only 客户端过滤、打包恢复提示、写令牌重启后需刷新、Node engines 未声明、runbook 缺口、SQLite 明文个性化数据（PRD §18.3 解释缺口，待所有者明示意图）、`_csv_cell` 首字符防护、杂项。
3. **920305**：极新股，所有免费源无数据（价格/股本/分红缺失，如实记录 missing）。
4. **银行/券商监管字段 92 只**（资本充足率/不良贷款率/拨备覆盖率/风险覆盖率）：免费结构化 API 不可得，保持 NULL，不伪造。
5. **2026-03-31 之前历史期财务**：CSMAR 商业导入值保留，无原始字节 lineage（约 253 万条空 payload 已隔离至 quarantine 表，不删除）。
6. **东财源被封**：已迁移至腾讯/Sina/BaoStock/交易所官方名单；东财适配器保留在回退链末端，网络恢复后自动可用。
## 进行中的工作
- **自动数据更新 + CSRC 行业分类**（最新会话）：PRD 修订已完成（`decisions/01` §7.3/7.4/7.7/15/16.1/20.4/21/22/24），实施计划见 `.planning/2026-07-31-automatic-data-updates/implementation-plan.md`（Phase A–G）。
## 当前有效文档（Current Truth）
| 文档 | 用途 | 注意 |
| `docs/STATUS.md` | 本文件：当前状态唯一权威 | 每次状态变化必须更新 |
| `docs/decisions/01_PRODUCT_REQUIREMENTS_V1.md` | 产品需求规格（验收合同） | **活文档**：随功能演进修订，2026-07-31 修订了自动更新/CSRC 章节 |
| `docs/decisions/02_TECH_CONSTRAINTS.md` | 技术约束清单 | 约束冲突时以 README/实施为准时需人工裁决 |
| `docs/reports/29_DATA_REBUILD_REPORT_2026-07-31.md` | 最新数据重建报告（P0-1 关闭依据） | 数据层当前结论基线 |
| `docs/reports/30_AUDIT_FIX_CLOSURE_2026-08-02.md` | 审计修复闭环报告（`reports/27` 代码级 P1/P2 全部关闭依据） | 代码层审计结论基线 |
| `docs/reports/31_RELEASE_RED_TEAM_FIX_2026-08-02.md` | 发布级红队 P0 修复报告（6 项 P0 关闭依据） | 发布级审查结论基线 |
| `docs/reports/32_RELEASE_P1_FIX_AND_FORMAL_ACCEPTANCE_2026-08-02.md` | 发布级红队 P1 修复 + 正式库只读验收报告 | 发布级 P1 关闭依据 + 正式库当前状态基线 |
| `docs/reports/33_FOURTH_ROUND_REVIEW_FIX_2026-08-02.md` | 第四轮复测修复报告（build-release 清理竞态 P1 + 2 项 P2） | 修复事实保留，结论被 `reports/35` 更新 |
| `docs/reports/34_SYSTEM_RED_TEAM_REVIEW_2026-08-02.md` | 第五轮系统红队独立复核（BLOCK + 4 项 P1 + 退出条件） | 审查发现基线；**BLOCK 裁决已被 `reports/35` 取代** |
| `docs/reports/35_SYSTEM_RED_TEAM_FIX_2026-08-02.md` | 系统红队 4 项 P1 + 3 项发布阻断 P2 修复报告 | 修复事实保留，裁决被 `reports/36`/`reports/37` 更新 |
| `docs/reports/36_SYSTEM_RED_TEAM_REAUDIT_2026-08-02.md` | 第六轮修复后独立复审（BLOCK，F1/F2/F3） | 审查发现基线；BLOCK 已被 `reports/37` 取代 |
| `docs/reports/37_REAUDIT_F1_F2_F3_FIX_2026-08-02.md` | 第六轮复审 F1/F2/F3 修复报告 | F1/F2 与网页导出修复事实保留；"可启用"结论被 `reports/38` 取代 |
| `docs/reports/38_SYSTEM_RED_TEAM_ROUND7_2026-08-02.md` | **当前裁决依据**：第七轮系统红队复审（BLOCK + F4 原生 CLI 导出静默截断） | **当前发布裁决基线** |
| `docs/runbooks/s0-evidence-preservation.md` | 证据保全运行手册 | |
| `docs/contracts/path-isolation-contract.md` | 路径隔离合同（签署版） | |
| `.planning/2026-07-31-automatic-data-updates/` | 当前实施会话计划 | 会话产物，不入 docs/ |
## 已被取代的结论（Superseded，禁止引用为当前结论）
- `reports/25`（7-30 正式验收 "数据层 BLOCK"）→ 数据层结论已被 `reports/29` 取代（P0-1 已修复）；代码层结论仍有效。
- `reports/27`（7-31 综合红队复审 BLOCK，1 P0 + 20 P1）→ **P0-1 已关闭**（见 `reports/29`）；**其余代码级 P1 项已全部关闭**（见 `reports/30`）。
- `reports/28`（7-31 独立红队复审，维持 BLOCK）→ 同 27，数据结论被 29 更新，代码结论被 30 更新。
- `reports/30`–`reports/33`（8-02 审计修复闭环 + 发布级 P0/P1 修复 + 正式库验收 + 第四轮复测）→ **修复事实仍有效**，其"全部关闭"结论被 `reports/34`/`reports/35` 更新。
- `reports/34`（第五轮系统红队 BLOCK，4 项 P1）→ 审查发现基线；其 BLOCK 裁决先被 35 声明修复，再被 `reports/36` 恢复（P1-A/P1-C 残余 F1/F2/F3）。
- `reports/35`（4 项 P1 + 3 项发布阻断 P2 修复，"可启用"）→ **修复事实保留**，整体裁决已被 `reports/36` 取代（P1-A/P1-C 未真正关闭）。
- `reports/36`（第六轮 BLOCK，F1/F2/F3）→ F1/F2 与网页导出 F3 已被 `reports/37` 修复；其裁决又被 `reports/38` 更新（F4 原生 CLI 导出旁路）。
- `reports/37`（F1/F2/F3 修复，"可启用"）→ **修复事实保留**，整体裁决已被 `reports/38` 取代（F4）。
- 更早编号报告（05–24、26）→ 全部 superseded，仅作追溯证据。
## 维护规则（写文档的人必须遵守）
1. **状态变化时**：更新本文件 → 将旧报告 front-matter 的 `status` 改为 `superseded` 并写 `superseded-by` → 新报告/文档必须带 front-matter 且 `status: approved`。
2. **报告类文档**（审计/验收/审查）一律放入 `docs/reports/`，命名 `NN_主题_YYYY-MM-DD.md`。
3. **新功能/修订**：先改 `decisions/01`（PRD）→ 计划放 `.planning/` → 实施 → 验收报告放 `reports/` → 回写本文件。
4. **会话产物**（findings/progress/task_plan）只放 `.planning/`，严禁写入 `docs/`。
5. **机器证据**（JSON/hash）只放 `docs/evidence/`。
6. 给 AI 的建议/结论必须附带依据文档路径与 `last-reviewed` 日期。
