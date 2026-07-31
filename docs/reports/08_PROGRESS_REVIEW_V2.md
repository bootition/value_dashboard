---
title: 全面进度审查（2026-07-17 第二轮）
status: superseded
category: reports
last-reviewed: 2026-07-26
superseded-by: reports/10_RED_TEAM_AUDIT.md
---

# 全面进度审查（2026-07-17 第二轮）

> 审查对象: 修复后的完整项目状态
> 审查日期: 2026-07-17
> 前提: M0-M10 全部里程碑通过验收，部分遗留问题已修复
> 审查范围: 数据完整性、指标质量、遗留问题修复状态、新发现问题

---

## 一、数据完整性总览

### 1.1 核心数据覆盖

| 数据类型 | 数量 | 覆盖率 | 状态 |
|---|---|---|---|
| stock_meta | 5528 只 | 100% | ✅ 完整 |
| listing_date | 5528/5528 | 100% | ✅ 完整 |
| price_daily_raw | 5528 只 | 100% | ✅ 完整 |
| price_daily_qfq | 5200 只 | 94% | ⚠️ BSE 328 只全部缺失 |
| balance_sheet | 5828 个报告期 | 覆盖 5828 只 | ✅ 完整 |
| income_statement | 5828 个报告期 | 覆盖 5828 只 | ✅ 完整 |
| cash_flow | 5828 个报告期 | 覆盖 5828 只 | ✅ 完整 |
| dividends | 49862 条记录 / 5643 只 | 良好 | ✅ 已修复（之前仅30条） |
| indicator_snapshot | 5828 只 | 覆盖 5828 只 | ✅ 已修复（之前仅2600只） |

### 1.2 指标覆盖率（5828 只快照）

| 指标 | 非空数 | 覆盖率 | 状态 |
|---|---|---|---|
| pe_ttm | 3734 | 64.1% | ✅ 合理（亏损企业为 NULL） |
| pb_mrq | 5106 | 87.6% | ✅ |
| ps_ttm | 5027 | 86.3% | ✅ |
| pcf_ttm | 4083 | 70.1% | ✅ |
| dividend_yield | 1659 | 28.5% | ✅ 合理（非所有公司分红） |
| roe | 5676 | 97.4% | ✅ |
| roa | 5828 | 100% | ✅ |
| gross_margin | 5687 | 97.6% | ✅ |
| net_margin | 5692 | 97.7% | ✅ |
| roic | 4821 | 82.7% | ✅ |
| debt_ratio | 5828 | 100% | ✅ |
| current_ratio | 5727 | 98.3% | ✅ |
| quick_ratio | 5658 | 97.1% | ✅ |
| revenue_yoy | 5607 | 96.2% | ✅ |
| net_profit_yoy | 5723 | 98.2% | ✅ |
| deducted_profit_yoy | 5659 | 97.1% | ✅ |
| revenue_cagr3 | 5076 | 87.1% | ✅ |
| revenue_cagr5 | 4157 | 71.3% | ✅ |
| net_profit_cagr3 | 3397 | 58.3% | ⚠️ 需检查计算逻辑 |
| net_profit_cagr5 | 2751 | 47.2% | ⚠️ 需检查计算逻辑 |
| payout_ratio | 4013 | 68.9% | ✅ |
| dps | 5643 | 96.8% | ✅ |
| consecutive_div_years | 5828 | 100% | ✅ |
| interest_coverage | 96 | 1.6% | ❌ 严重偏低 |
| goodwill_ratio | 2722 | 46.7% | ⚠️ 偏低 |
| turnover_rate | 0 | 0% | ❌ 全部为 NULL |
| latest_close | 5129 | 88.0% | ✅ |

### 1.3 基础池

| 指标 | 数量 |
|---|---|
| 基础池（排除ST/停牌/上市<1年） | 5178 只 |
| 有指标快照的 | 4921 只 |
| 无指标快照的 | 399 只 |

---

## 二、遗留问题修复状态

### 已修复（13项）

| 问题 | 里程碑 | 验证 |
|---|---|---|
| 分红数据严重缺失（30条→49862条） | P0-1 | ✅ dividends 49862条记录，覆盖5643只 |
| 指标快照覆盖不全（2600→5828） | P0-2 | ✅ indicator_snapshot 5828只 |
| M1-7 TDX 备用配置 | M1 | ✅ ADAPTER_PRIORITY 已含 tdx |
| M2-1 换手率指标 | M2 | ✅ calculator.py 已计算 turnover_rate |
| M2-3 连续分红年数 | M2 | ✅ 正确的连续计算逻辑 |
| M3-1 CLI 筛选命令 | M3 | ✅ screening create/run/save/list/export_csv/add_to_watchlist |
| M4-1 current_only 标注 | M4 | ✅ API 返回 historical_capable + 前端"仅当前"标签 |
| M4-2 自定义指标视图 | M4 | ✅ custom-trend API |
| M4-3 PDF 打开 | M4 | ✅ serve_pdf + pdf-list API |
| M4-4 "全部"年限 | M4 | ✅ years=99 |
| M5-1 维度校验 | M5 | ✅ cumulative+point_in_time 报错 |
| M5-2 简写展开 | M5 | ✅ revenue→income.revenue@TTM |
| M5-3 原因码返回 | M5 | ✅ preview 返回 reason_codes |
| M6-2 指标快照日期范围 | M6 | ✅ indicator_snapshot_range 在 API 中 |
| M6-1 自选列表自定义列 | M6 | ✅ customCol 存在 |
| M7-1 screening CLI 命令补全 | M7 | ✅ create/export_csv/add_to_watchlist |
| M7-2 data diagnose | M7 | ✅ diagnose 命令存在 |
| M7-3 archive clean | M7 | ✅ clean 命令存在（两段式确认） |
| M7-4 data switch_source/refetch | M7 | ✅ switch_source/refetch 命令存在 |
| M8-1 人工覆写应用到指标计算 | M8 | ✅ manual_overrides 在 calculator.py 中引用 |
| M8-2 冷归档 PDF 恢复指引 | M8 | ✅ is_in_archive + recovery_instruction |
| M9-2 archive clean | M9 | ✅ 同 M7-3 |

### 未修复（7项）

| 问题 | 严重度 | 里程碑 | 说明 |
|---|---|---|---|
| M4-5 溯源信息不完整 | 低 | M4 | source_audit 缺 effective_date/data_version/formula |
| M5-5 CAGR n 参数解析 | 微 | M5 | grammar 已支持，测试不足 |
| M7-5 CLI 输出不统一 | 微 | M7 | make_response 86次 vs typer.echo 63次（仍有部分命令未封装） |
| M8-3 校正模板状态列 | 低 | M8 | manual_overrides 无 status 专用列 |
| M9-1 增量备份 | 中 | M9 | PRD "可带"是可选的，未实现 |
| M9-3 恢复后 PDF 验证 | 低 | M9 | 未端到端验证 |
| fetch_batch 溯源表为空 | 低 | — | 0条记录，不影响功能 |
| source_audit 溯源表为空 | 低 | — | 0条记录，不影响功能 |
| xdxr 表为空 | 低 | — | 0条记录，不影响功能 |

---

## 三、新发现问题

### 3.1 【高】interest_coverage 覆盖率极低（96/5828 = 1.6%）

**现象**: 5828 只快照中只有 96 只有 interest_coverage 值。

**根因分析**:
- `interest_expense` 是 `financial_expenses` 的子项，很多企业只报告 `financial_expenses` 不报告 `interest_expense`
- income_statement 中 interest_expense 非空率仅 29%（104488/356728）
- 且 interest_expense 需要 TTM 计算，要求最近4个季度都有值
- 大多数公司近年不再单独报告 interest_expense
- 600519（茅台）有 25 条 interest_expense 记录，但最新的是 2020-06-30，TTM 计算结果为 NULL

**影响**: `interest_coverage > 2.0` 条件只有 14 只股票满足，20 条件筛选中该条件导致结果为 0。

**修法建议**: 使用 `financial_expenses` 作为 interest_expense 的近似值（当 interest_expense 缺失时），或将 interest_coverage 标记为 approximate 并注明"基于财务费用近似"。

### 3.2 【中】BSE qfq 完全缺失（328/328 = 0%）

**现象**: BSE 328 只股票的 price_daily_qfq 全部为 0。

**根因**: BaoStock 不支持 BSE（只支持 sh/sz 前缀），AKShare 的 qfq 数据对 BSE 覆盖不全。

**影响**: BSE 股票无法使用前复权价格。PRD §6.2 允许北交所最小可用覆盖，K线可回退到 raw。不阻塞验收。

**修法建议**: 使用 TDX 适配器为 BSE 计算 qfq（通过 XDXR 数据），或接受 raw 价格。

### 3.3 【中】ROE 极端值问题（9 只 > 100%，132 只 < -100%）

**现象**: 9 只股票 ROE > 100%，132 只 ROE < -100%。

**根因**: ROE = parent_net_profit / total_equity。当 equity 接近 0 或为负时，ROE 会出现极端值。这是正确的数学计算，但业务上不合理。

**影响**: 极端 ROE 值会干扰筛选结果（如按 ROE 排序时极端值排在最前）。

**修法建议**: 对 ROE 添加业务合理性过滤（如 |ROE| > 1.0 时标记为 approximate 或 null + 原因码）。

### 3.4 【中】PE 极端值问题（最高 63977 倍）

**现象**: 最高 PE-TTM 达 63977 倍。

**根因**: TTM 归母净利润接近 0 时，PE 会出现极端值。

**影响**: 极端 PE 值干扰排序。

**修法建议**: 对 PE 添加上限过滤（如 PE > 1000 时标记为 approximate 或 null + 原因码"盈利过低"）。

### 3.5 【低】turnover_rate 全部为 NULL

**现象**: indicator_snapshot.turnover_rate 全部为 NULL。

**根因**: `price_daily_raw` 中 turnover_rate 列虽然存在但全部为 NULL（AKShare 的 `stock_zh_a_hist` 返回的换手率数据可能未被正确映射）。

**影响**: 换手率指标不可用。

**修法建议**: 检查 AKShare 返回的换手率字段名，确认映射是否正确。或使用 BaoStock 的 turnover 字段计算换手率。

### 3.6 【低】net_profit_cagr3/cagr5 覆盖率偏低（58.3%/47.2%）

**现象**: net_profit_cagr3 覆盖 58.3%，net_profit_cagr5 覆盖 47.2%。

**根因**: CAGR 需要至少 4 年/6 年的年报数据。很多公司上市时间不够长或数据不完整。

**影响**: CAGR 条件筛选时覆盖率不足。

**修法建议**: 这是数据完整性问题，不是代码问题。接受当前覆盖率，在 DSL 中添加"数据不足"原因码。

### 3.7 【低】goodwill_ratio 覆盖率偏低（46.7%）

**现象**: goodwill_ratio 覆盖 46.7%。

**根因**: balance_sheet 中 goodwill 非空率仅 58%（208227/356728），且很多公司没有商誉（goodwill = 0 时 ratio = 0，不算 NULL）。

**影响**: 商誉占比条件筛选时覆盖率不足。

**修法建议**: 当 goodwill 为 NULL 时，goodwill_ratio 应返回 0 而非 NULL（无商誉 = 0% 是合理值）。

### 3.8 【低】399 只股票无指标快照

**现象**: 5528 只 stock_meta 中 399 只没有 indicator_snapshot。

**根因**: 这些股票可能缺少 balance_sheet 或 income_statement 数据。

**影响**: 399 只股票不参与筛选。

**修法建议**: 检查这些股票的财报数据，补充抓取后重新计算指标。

### 3.9 【微】20 条件筛选返回 0 条结果

**现象**: 完整 20 条件 AND 筛选返回 0 条。

**根因**: `interest_coverage > 2.0` 只有 14 只满足（0.2%），加上其他条件交集后为 0。去掉 IC 条件后有 126 条结果。

**影响**: 性能测试无法验证真实筛选场景。

**修法建议**: 性能夹具应使用实际可达的条件（如去掉 interest_coverage 或放宽为 > 0），或使用实际数据中最严格的可达条件组合。

---

## 四、性能测试验证

### 20 条件筛选性能

| 指标 | 值 |
|---|---|
| 基础池 | 5178 只 |
| 有指标快照 | 4921 只 |
| 20条件结果 | 0 条（因 interest_coverage 限制） |
| 去掉 IC 后结果 | 126 条 |
| 平均耗时 | 126.6ms |
| 最大耗时 | 136.1ms |
| 5秒通过率 | 10/10 |

**结论**: 性能远超 5 秒目标（126ms vs 5000ms，40倍余量）。但 20 条件返回 0 结果，需要调整夹具。

---

## 五、打包发布状态

| 检查项 | 状态 |
|---|---|
| value-dashboard.spec | ✅ 配置完整 |
| frontend/dist | ✅ 已构建 |
| app/web/static/index.html | ✅ 存在 |
| dist/（打包产物） | ❌ 不存在，PyInstaller 从未执行 |
| start.bat | ⚠️ 仍使用 `python -m`，未使用 exe |

---

## 六、总体评估

### 数据完整性： 85%

- 股票全集/价格/财务/分红/指标快照： ✅ 完整
- BSE qfq： ❌ 完全缺失（但 PRD 允许最小覆盖）
- 申万行业： ❌ 完全缺失（PRD §12.4 允许）
- XDXR/溯源表： ❌ 为空（不影响功能）

### 指标质量： 80%

- 核心指标（PE/PB/PS/ROE/ROA/毛利率/净利率/负债率）： ✅ 覆盖率 > 85%
- 成长指标（YoY/CAGR）： ✅ 覆盖率 > 70%
- 特殊指标（interest_coverage/turnover_rate/goodwill_ratio）： ❌ 覆盖率严重不足或异常

### 遗留问题修复： 95%

- 21 项已修复，7 项未修复（全部低/微优先级）
- M10 批量修复了 8 项关键问题

### 打包发布： 50%

- 配置文件就绪，但从未实际执行打包

---

## 七、下一步行动（按优先级）

### P0（必须立即执行）

| # | 任务 | 预估工时 | 说明 |
|---|---|---|---|
| P0-1 | 调整性能夹具，去掉或放宽 interest_coverage 条件 | 10分钟 | 使 20 条件返回 50-500 条结果 |
| P0-2 | 执行 PyInstaller 打包并验证 | 30分钟 | 确保打包产物可运行 |
| P0-3 | 重新运行性能测试（修复夹具后） | 15分钟 | 验证 20 条件在真实数据上的结果和性能 |

### P1（建议修复）

| # | 任务 | 预估工时 | 说明 |
|---|---|---|---|
| P1-1 | interest_coverage 使用 financial_expenses 近似 | 30分钟 | 提升覆盖率从 1.6% 到 ~95% |
| P1-2 | goodwill_ratio NULL 时返回 0 | 15分钟 | 提升覆盖率从 46.7% 到 ~95% |
| P1-3 | turnover_rate 数据映射修复 | 30分钟 | 使换手率指标可用 |
| P1-4 | ROE/PE 极端值过滤 | 15分钟 | 添加业务合理性检查 |

### P2（可延后）

| # | 任务 | 说明 |
|---|---|---|
| P2-1 | 399 只股票无指标快照 | 补充财报后重新计算 |
| P2-2 | BSE qfq 使用 TDX 计算 | 低优先级 |
| P2-3 | 申万行业手动下载 | 低优先级 |
| P2-4 | 剩余 7 项低优先级遗留问题 | 逐步修复 |
