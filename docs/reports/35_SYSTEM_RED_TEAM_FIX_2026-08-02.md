---
title: 系统红队 4 项 P1 修复报告（2026-08-02）
status: approved
category: reports
created: 2026-08-02
last-reviewed: 2026-08-02
supersedes: reports/34_SYSTEM_RED_TEAM_REVIEW_2026-08-02.md
---

# 系统红队 4 项 P1 修复报告（2026-08-02）

> 关闭 `docs/reports/34_SYSTEM_RED_TEAM_REVIEW_2026-08-02.md`（第五轮系统
> 红队，BLOCK）的全部 4 项 P1 与 3 项发布阻断类 P2。全部修复均附带回归
> 测试；门禁全绿：S1 **402 passed**（+10）、Ruff 零错误、前端 lint +
> 52 node + 10 vitest、build、`uv lock --locked`。

## P1 关闭清单

| 编号 | 问题 | 修复 | 回归测试 |
|---|---|---|---|
| P1-A | `_to_single_quarter` 在年中报告期缺失时伪造单季值（Q3−Q1 吞入 Q2；Q1 可为负） | `app/web/api/stock_detail.py:94-127`：按季度索引（3/6/9/12 → 1/2/3/4）校验"紧邻上一季度"，缺失/不紧邻时流字段置 NULL（fail-closed，与 `calculate_ttm_trend` 一致），不输出伪单季值 | `test_stock_detail_periods.py` +2（Q1+Q3 无 Q2 → Q3=NULL；Q1+Q4 缺中间 → Q4=NULL） |
| P1-B | 部分/截断股票池响应静默将有效股票置为退市；适配器把部分列表伪装成 strict 成功 | ① `akshare_adapter._fetch_stock_list`：任一板块失败 → `confidence="approximate"` + `error="partial stock list: …"`（调用方 fail-closed）；② `init._fetch_stock_universe` 退市门禁：按交易所比对"本次抓取数 vs 当前上市数"，<90% 即拒绝该交易所退市标记并披露 `delist_guarded_exchanges`（status=partial） | `test_research_path_integrity.py` +3（10/100 部分 → 不退市+披露；90/100 完整 → 照常退市；适配器 partial 错误） |
| P1-C | 筛选结果 LIMIT 5000 静默截断，`total` 错误等于 5,000 | `engine._build_sql` 生成同 WITH 骨架的计数查询；`run()` 以真实匹配数为 `total` 并返回 `truncated`；API 透传；`confidence_summary` 持久化 `truncated`；CSV 导出追加 `_truncated` 列；前端 `ScreeningResultsPanel` 截断警示（结果数/前 5000 条/保存与导出同限）；`MAX_RESULT_ROWS=5000` 常量声明 | `test_research_path_integrity.py` +3（6000 匹配 → truncated=True/total=6000/行数 5000；≤5000 → False；API 透传键）；`test_screening_server_runs.py` 补 API 契约键断言；前端组件测试 +1（截断告警渲染） |
| P1-D | 行业排名按 CSRC 计算却以"申万"标签暴露；正确命名的 `industry_rank` 未进 UI | `screening.py` 指标列表：`sw1_rank` 标签删除，暴露 `{field}_industry_rank/_industry_percentile`（证监会一级排名/分位）与 `sw2_rank`（证监会二级）；引擎保留 `_sw1_rank/_sw2_rank` 列供存量已保存规则兼容（口径同为 csrc 分区，仅命名遗留） | `test_research_path_integrity.py` +2（列表含 industry_rank 且无"申万"标签；`_industry_rank == _sw1_rank` 同 csrc 分区） |

## 发布阻断类 P2 关闭清单

| 编号 | 问题 | 修复 |
|---|---|---|
| P2-1 | 根 README 与 STATUS 严重脱节（BLOCK 措辞、废弃构建后端警告、启动路径矛盾、漂移/冻结哈希表） | `README.md` 对齐：裁决指向本报告、删除遗留构建后端警告、修正 start.bat 行为描述、状态与哈希章节改为重建后基线、前端 QA 数字更新 |
| P2-2 | 启动器自动设置 `VD_ENV=formal`/`VD_FORMAL_ACK` 与路径隔离合同冲突 | `docs/contracts/path-isolation-contract.md` 增加 **2026-08-02 所有者裁决**：发行版启动器为经裁决的正式引导入口（用户调用即确认）；§2.1/§2.4 的"仅检查不设置"适用于非发行入口脚本/CI；Python 侧强制仍为权威边界 |
| P2-3 | 58 个构建产物（`app/web/static/assets/`）被 git 跟踪 | `git rm --cached app/web/static/assets/*`（55 个已跟踪文件），与 `.gitignore`/AGENTS.md 一致 |

## 验证门禁（2026-08-02）

| 门禁 | 结果 |
|---|---|
| `scripts/s1-pytest.ps1 tests/regression -q --no-header` | **402 passed**（含 P1-A~D 全部回归） |
| `uv run --locked ruff check app tests/regression` | All checks passed |
| 前端 lint / node 合约 / vitest 组件 / build | 通过 / 52 passed / 10 passed / 成功 |
| `uv lock --locked` | 通过 |

## 状态

- 第五轮系统红队 4 项 P1 与 3 项发布阻断 P2 全部关闭；此前各轮（`reports/29`–`33`）
  结论不变。
- 剩余未关闭项均为报告 34 §5 的次要 P2（草稿 409 后自动保存停用、strict-only
  客户端过滤、打包恢复提示、写令牌刷新、Node engines、runbook 缺口、SQLite
  明文个性化数据解释缺口等）与数据披露项，不阻断主要研究路径；其中
  PRD §18.3 明文个性化数据加密需所有者明示意图。
