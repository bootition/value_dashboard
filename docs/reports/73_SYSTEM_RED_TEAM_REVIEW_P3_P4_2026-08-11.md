---
title: P3/P4 系统红队审查（国债曲线与利差 + 历史股本链与统计）
status: superseded
category: reports
created: 2026-08-11
last-reviewed: 2026-08-12
superseded-by: reports/74_P3_P4_RED_TEAM_FIX_AND_DATA_COMPLETION_2026-08-12.md
supersedes:
  - reports/71_TREASURY_CURVE_P3_2026-08-10.md
  - reports/72_CAPITAL_HISTORY_AND_STATISTICS_P4_2026-08-10.md
---

# P3/P4 系统红队审查（2026-08-11）

## 1. 裁决

**NOT PASS（BLOCK 级，尚未到"可正式研究"新增域验收）。**

- P3 国债曲线与利差、P4 历史股本链与统计的实施**事实与门禁**保留（reports/71、72），但其
  "PASS"裁决被本报告更新：发现 1 项 P1 级**确定性数据正确性缺陷**（CNINFO 股本链被 AkShare
  默认日期硬截断，2024-10-21 后股本变动缺失并污染当前 PE/PB）、2 项 P1 级机制缺陷（有界续传
  无游标退化为"永远前 20 只"；覆盖门槛按全历史而非窗口计算），以及多项 P2/P3 与交付缺口。
- **结论**：在 P1-1 修复并重新核验前，**不得对正式库执行历史股本链全量回填与统计域发布**；
  启动自动更新的相关步骤应保持当前有界状态，修复完成后按本报告 §8 优先级处理。

## 2. 审查范围与方法

- 对象：HEAD `e1c5a14`，覆盖提交 `021e0d8`（P3）与 `e1c5a14`（P4）引入的全部代码与文档增量。
- 方法：静态代码走读 + 关键点运行时验证（akshare 1.18.64 函数签名/实现、DuckDB 行为探针）、
  与 reports/68 门槛、PRD（`decisions/01` §6.8/§7.7/§8.4/§10.7/§12.5）逐条对照。
- 隔离：全部验证使用 S1 隔离路径；未触碰正式 `data/`；未修改任何业务代码。
- 门禁复跑：P3 国债域回归 21 passed；Ruff pass；前端 lint / 55 Node 测试 / 40 组件测试 /
  build pass；完整 S1 552 项回归在工具超时前运行至 P4 模块（未获完整结果，见 §7）。

## 3. P3 遗留发现（国债曲线与股息率利差）

### P2

| # | 发现 | 位置 | 影响与建议 |
|---|---|---|---|
| P3-1 | **双线视图两条序列路径相同**：两个 `<path>` 都取 `pointPath([1,1])`（含两序列的合并路径被画两次），蓝色后画覆盖绿色，图例（绿=TTM、蓝=国债）与画面不符 | `frontend/src/components/TreasuryComparisonCard.vue:98-99,179-180` | 用户无法区分两序列，误导性图表；绿线应只画 `pointPath([1])`，蓝线只画含线 1 的路径 |
| P3-2 | **导出溯源对齐日错位**：`_field_provenance` 用 `row["_report_date"]`（财务报告期）调用 `align()`，而利差实际按**最新价格日**计算（calculator.py:700-761） | `app/web/api/screening.py:442,451` | PRD §12.5 溯源失真：CSV 的 curve_date/staleness_days 与实际数值口径可能不符；应改用快照 `latest_price_date` 对齐 |
| P3-3 | **国债比较 API 绕过服务端信任遮蔽**：`/treasury-comparison` 直接按 dividends 表实时计算展示，无 `indicator_trust`/`mask_untrusted_values`（对比 `/indicators` :458-476）；后端 `DIVIDEND_INDICATOR_FIELDS` 也未含 10 个新股息/利差字段 | `app/web/api/stock_detail.py:759-877`、`app/core/data_quality.py:701-706` | `DIVIDEND_DATES_UNVERIFIED` 生效时详情页仍展示股息率/利差，与全站信任遮蔽模型不一致；契约须同步（前端 `data-quality.ts` 已列，后端未列） |
| P3-4 | **刷新标记只写不读**：`REFRESH_MARKER_KEY` 每次 `refresh_if_due()` 无条件 `update_daily()` + 回填检查，无"当日已抓"门控（对比 business.py:284-310） | `app/core/treasury.py:253-308`、`app/core/update.py:492-505` | 与 PRD"按各自节流策略刷新"不符；每次启动/手动 run 均向财政部发请求；应读取 marker 当日已刷新即 skip |
| P3-5 | **国债失败条目无消费路径**：`_record_retry` 写 `treasury_yield_curve` retry 条目，但 `_retry_failed_tasks` 白名单直接 `continue` 跳过，清理名单亦无 | `app/core/treasury.py:394-410`、`app/core/update.py:1602-1611` | "下次自动重试"承诺不成立；历史期限回填失败无法恢复；应加入重试白名单或独立维护 |

### P3

| # | 发现 | 位置 |
|---|---|---|
| P3-6 | 国债比较/统计序列时间轴从右往左（新→旧）且按索引等距，节假日压缩 | `stock_detail.py:786-790`、`TreasuryComparisonCard.vue:78` |
| P3-7 | `status_report` 的 `missing_open` 统计全库 missing，与 `retry_open`（按 data_type 过滤）口径不一致，`--check-only` 误导 | `treasury.py:369-390` |
| P3-8 | `--tenors 99` 等非法期限静默返回 success 且 targeted=0 | `treasury.py:87-107`、`cli/main.py:279-283` |
| P3-9 | 导出溯源 N+1 查询（行×期限逐次 `align`）；`align_many` 仍是逐项循环 | `screening.py:441-459`、`treasury.py:345-355` |
| P3-10 | 收益率解析允许 `nan`/`inf` 入库（`_to_float` 无有限性检查），与"有限正数"注释不符 | `czb_mof_adapter.py:69-76,213,296` |

## 4. P4 发现（历史股本链与历史研究统计）

### P1

| # | 发现 | 位置 | 影响与建议 |
|---|---|---|---|
| P4-1 | **CNINFO 主链被 AkShare 默认日期截断**：`ak.stock_share_change_cninfo(symbol=...)` 未传日期参数。已亲验本环境 akshare 1.18.64 签名默认 `start_date='20091227', end_date='20241021'` 且直接拼入 CNINFO 请求参数；东财侧 `stock_zh_a_gbjg_em` 无日期参数不受此影响 | `capital_history_adapter.py:128` | ①2024-10-21 后全部股本变动缺失，`build_series` step 函数把旧股本延续至今 → **当前及近 22 个月 PE/PB 用陈旧股本计算**；②2009-12-27 前无链点；重跑回填幂等不自愈。必须显式传 `start_date=上市日期/1990-01-01`、`end_date=今日`，并加"最新锚点 ≥ 今日-1年"断言防回归 |
| P4-2 | **`update_all` 无进度游标**：每轮 `_listed_stock_codes()` 全量排序取 `codes[:max_stocks]`，成功股票仍在队列头；自动更新固定 `max_stocks=20` | `capital.py:241-256`、`update.py:507-519` | 第 21 只及以后**永不被处理**，"有界续传"（STATUS §缺口5 宣称）实际退化为"永远重复前 20 只"并每轮空转网络请求；应参照 `business.py:312-344` 的 `_due_stock_codes`（只选无记录/陈旧股票） |
| P4-3 | **覆盖门槛按全历史而非窗口**：`_capital_coverage` 用整段价格历史（首锚点之前占比）算覆盖，且该单一数值套用到所有窗口；价格史早于股本链首点的老股**所有窗口**（含近期 100% 覆盖的 1 年窗口）全部 `coverage_below_threshold` | `statistics.py:355-408,410-442` | 偏离 reports/68 §3.5"窗口内 ≥90%"口径；P4 两大招牌指标对老股大面积失效且掩盖近期数据完好事实。应改为按窗口计算，并读取 `verified` 分段计 |

### P2

| # | 发现 | 位置 |
|---|---|---|
| P4-4 | `_write_records` 列集合取自 `records[0].keys()`：首记录为 reason 行（无 min_date/max_date）时，后续成功行发布时**丢失 min_date/max_date**；列顺序脆弱 | `statistics.py:467-476`（reason 行结构 :371-384） |
| P4-5 | `share_capital_history` retry 条目是死条目：不在 `_retry_failed_tasks` 白名单也不在清理名单，`retry_count` 恒 0，每轮被拾取再跳过，与真实任务抢队列槽位，"retry 清零"口径被污染 | `capital.py:314-327`、`update.py:1602-1611`、`update.py:1474-1477` |
| P4-6 | 覆盖判定忽略 `verified`；`coverage_report` 有 ≥1 链点即 `coverage_pct=100`、`verified_days=price_days`；`--check-only` 向运维展示虚高覆盖，且与 `statistics._capital_coverage` 两套口径 | `capital.py:284-310`、`cli/main.py:301-306`、`statistics.py:410-442` |
| P4-7 | 双源数值比较从未用真实数据验证：CNINFO ×万→股（`capital_history_adapter.py:146`），东财 F10 **无单位换算**；`CROSS_NEIGHBOR_DAYS`（capital.py:37）声明但从未使用（实际是区间包含匹配）；测试全用同单位合成值。若东财 `TOTAL_SHARES` 亦为万股口径，`rel≈0.9999` → **每个含东财事件的区间全部判冲突 → verified 全 False**，交叉核验静默退化且无告警 | `capital_history_adapter.py:155-184`、`capital.py:196-213` |

### P3

| # | 发现 | 位置 |
|---|---|---|
| P4-8 | 负净资产股票的负 PB 进入分位分布与筛选（PE 有 `profit>0` 守卫，PB 无 `equity>0` 门槛） | `statistics.py:169-173` |
| P4-9 | 输入指纹不含 dividends：仅分红变化不触发统计重建，发布域 TTM 股息率/利差可能陈旧（详情页实时算不受影响） | `statistics.py:444-459` |
| P4-10 | 任何输入变化即全量 `rebuild_all()`（~5500 股）；且 `partial`（部分失败）也持久化指纹 → 失败股需等下次无关输入变化才重试 | `update.py:521-548` |
| P4-11 | `update_many` 无 per-stock 异常隔离，单股适配器/持久化异常中断整批 | `capital.py:217-239` |
| P4-12 | 前端统计卡：x 轴按索引等距（节假日压缩）；全历史序列与所选窗口统计线并存（样本数却属窗口）；API `window_years` 参数为死参数（总是返回全部窗口） | `ResearchStatisticsCard.vue:50-113`、`stock_detail.py:909-914` |
| P4-13 | 详情页统计实时计算（`build_series`+`window_stats`），筛选用已发布 `research_statistics` 域：两处口径存在时差漂移可能，发布域过期时详情与筛选对同一股票显示不同分位 | `stock_detail.py:907-923` vs `engine.py:436-466` |

## 5. 用户与运维交付缺口（同步完成度检查）

| # | 缺口 | 位置 |
|---|---|---|
| D-1 | 首次使用手册无 P2-P4 内容：业务概览、国债比较（期限切换/5 日陈旧规则/缺失含义）、历史研究统计、筛选利差/统计字段均未说明 | `docs/runbooks/user-first-use.md:61-68` |
| D-2 | 运维手册无新域 CLI：`vd data treasury-curve`、`capital-history`、`research-statistics`（含 `--check-only`）及财政部来源边界未收录 | `docs/runbooks/ops-auto-update-retry.md` 等 |
| D-3 | README 仍引用 2026-08-03 状态与旧门禁数字，未说明 P1-P4 功能、财政部来源与 CLI | `README.md:3,20,32,181-185` |
| D-4 | 数据状态页 retry/missing 列表混入国债/股本域条目（无类型过滤），运维与普通用户难以区分 | `data_status.py:572-599` |

`start.bat` 审查结论：启动链路本身无需为 P1-P4 改动（自动更新已包含各域），无需变更；但 §3 P3-4/P3-5 与 §4 P4-2 导致启动自动更新中"国债每日刷新"与"股本续传"未按设计工作，属功能层缺口而非启动入口缺口。

## 6. 测试缺口（本次漏网问题的共因）

| 缺口 | 漏掉的问题 |
|---|---|
| 无 akshare 日期边界断言（测试全用合成数据，不触发真实默认参数） | P4-1 |
| `test_update_job_and_progress.py:42-45` 直接 stub 掉两个 P4 步骤，无自动更新集成测试 | P4-2、P4-5、P4-9、P4-10 |
| 无"价格历史早于股本链首点"场景；覆盖测试直接传 `coverage_pct` 绕过真实 `_capital_coverage` | P4-3 |
| 无"records[0] 为 reason 行"场景 | P4-4 |
| 无跨源真实单位/数值一致性测试；`CROSS_NEIGHBOR_DAYS` 无测试 | P4-7 |
| 无负权益 PB、无 per-stock 异常隔离、无国债双线 SVG 断言 | P4-8、P4-11、P3-1 |

## 7. 验证记录

| 门禁 | 结果 | 说明 |
|---|---|---|
| `scripts/s1-pytest.ps1 tests/regression/test_treasury_curve_domain.py` | 21 passed | P3 域全绿，但未覆盖 §6 所列场景 |
| `uv run --locked ruff check app tests/regression` | PASS | |
| 前端 `npm run lint` / `test` / `build` | PASS / 55+40 / PASS | |
| 完整 S1 552 项 | 未完成 | 工具 6 分钟超时，运行至 `test_capital_history_domain.py` 中断；不构成新门禁结论 |

## 8. 修复优先级建议

1. **P4-1**（修复成本最低时点在正式库首次回填前）：显式日期参数 + 锚点新鲜度断言 + 回归测试。
2. **P4-2**：`update_all` 改为 due-cursor 语义（复用 business 模式）。
3. **P4-3**：`_capital_coverage` 按窗口 + `verified` 分段计算。
4. **P4-4/P4-5**：固定规范列集合；股本 retry 入白名单或清理名单。
5. **P3-1/P3-2/P3-3**：双线图修复、导出用 `latest_price_date`、国债 API 接入信任遮蔽并同步 `DIVIDEND_INDICATOR_FIELDS`。
6. **P3-4/P3-5、P4-6/P4-7、P3 级与 D 级项**：按序关闭；全部配套 §6 测试补测。

## 9. 约束与诚实披露

- 全部发现基于静态走读 + 运行时验证；未修改业务代码，未触碰正式 `data/`；工作区在审查结束时干净。
- P4-7 的"单位失配导致全 False"为条件性推断，需真实数据核验后定性；但"从未用真实数据验证"本身已是事实。
- 完整 S1 未跑完不改变本报告裁决（发现均经单点复现或代码确定），但修复后须补全量回归。

（本报告取代 reports/71、72 的 PASS 裁决；其实施事实与门禁记录仍有效，仅裁决部分以本报告为准。）
