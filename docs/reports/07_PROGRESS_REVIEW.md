---
title: 当前进度审查（2026-07-17）
status: superseded
category: reports
last-reviewed: 2026-07-26
superseded-by: reports/10_RED_TEAM_AUDIT.md
---

# 当前进度审查（2026-07-17）

> 审查对象: DELIVERY_PLAN.md 中定义的 7 个阶段的执行进度
> 审查日期: 2026-07-17
> 前提: M0-M10 全部里程碑通过验收

---

## 总体进度

| 阶段 | 状态 | 说明 |
|---|---|---|
| 阶段1: 真实数据初始化 | ⚠️ 部分完成 | 股票全集/价格/财务已完成，分红/申万行业未完成 |
| 阶段2: 集成测试 | ❌ 未执行 | |
| 阶段3: 性能测试 | ⚠️ 已执行但有问题 | 20条件返回0结果，因分红数据缺失 |
| 阶段4: 验收测试 UAT | ❌ 未执行 | |
| 阶段5: 打包发布 | ❌ 未执行 | dist/ 不存在 |
| 阶段6: 冒烟测试 | ❌ 未执行 | |
| 阶段7: 缺陷修复 | ❌ 未执行 | |

---

## 阶段1: 真实数据初始化 — 详细审查

### 已完成的数据

| 数据类型 | 数量 | 质量评估 |
|---|---|---|
| stock_meta | 5528 只 | ✅ 完整（SSE 2308 + SZSE 2892 + BSE 328） |
| listing_date | 5528/5528 | ✅ 100% 覆盖 |
| is_st | 211 只 ST | ✅ 正确标记 |
| is_suspended | 0 只停牌 | ✅ |
| price_daily_raw | 5528 只 | ✅ 完整（2021-06-21 ~ 2026-07-17） |
| price_daily_qfq | 5200 只 | ✅ 基本完整（328只缺失，可能是BSE） |
| balance_sheet | 5828 个报告期 | ✅ 覆盖 1990-12-31 ~ 2025-03-31 |
| income_statement | 5828 个报告期 | ✅ |
| cash_flow | 5828 个报告期 | ✅ |
| indicator_snapshot | 2600 条 | ⚠️ 只覆盖 2600 只股票（应有 5528） |

### 未完成的数据

| 数据类型 | 状态 | 影响 |
|---|---|---|
| **dividends（分红记录）** | ❌ 严重不足 — 仅 30 条记录，只覆盖 1 只股票（600519） | 导致 dividend_yield/payout_ratio/dps 全部为 NULL，consecutive_div_years 全部为 0 |
| **sw_industry（申万行业）** | ❌ 完全缺失 — sw_industry_cache.csv 不存在，stock_meta.sw_level1 全部为 NULL | 行业排名不可用（全市场排名仍可用，符合 PRD §12.4） |
| **indicator_snapshot 覆盖不全** | ⚠️ 2600/5528 = 47% | 47%的股票无指标快照，不参与筛选 |

### 数据质量问题

#### 问题 1【高】分红数据严重缺失

**现状**: dividends 表只有 30 条记录，全部来自 600519（茅台）。5528 只股票中 5527 只没有分红数据。

**根因**: `DataInitializer._fetch_financial_statements()` 只抓取了三大报表，没有抓取分红数据。分红数据需要单独调用 `stock_dividend_cninfo` 或 CNINFO 公告搜索。

**影响**:
- `dividend_yield` = 0/2600（全部为 NULL）
- `payout_ratio` = 0/2600（全部为 NULL）
- `dps` = 0/2600（全部为 NULL）
- `consecutive_div_years` = 0/2600（全部为 0，因为 5527 只股票没有分红记录）
- 20 条件筛选中 3 个分红相关条件导致结果为 0

**修法**: 在 `DataInitializer` 中添加分红数据抓取步骤（Step 4.5），或在 `data init` 后单独运行分红抓取。

#### 问题 2【中】指标快照只覆盖 47%

**现状**: 5528 只股票中只有 2600 只有指标快照。

**根因**: `compute_snapshot_for_all()` 只计算有 balance_sheet + income_statement 的股票。5828 个报告期来自约 2600 只股票（每只约 2 期），其余 2928 只股票的财报数据可能在抓取时失败或未抓取。

**影响**: 47% 的股票不参与筛选，基础池有效股票约 2600 只而非 5000+。

**修法**: 检查 retry_list/missing_list，重新抓取失败股票的财报数据，然后重新计算指标快照。

#### 问题 3【中】申万行业完全缺失

**现状**: `config/sw_industry_cache.csv` 不存在，stock_meta.sw_level1 全部为 NULL。

**根因**: 申万行业需要从 swsresearch.com 手动下载，初始化流程不会自动获取。

**影响**: 行业排名不可用。但 PRD §12.4 允许缺申万归属时行业排名返回 NULL，全市场排名仍可用。不阻塞验收。

**修法**: 从 swsresearch.com 下载申万行业分类 CSV，放到 `config/sw_industry_cache.csv`，然后运行 `vd data init` 重新加载。

#### 问题 4【低】fetch_batch 溯源表为空

**现状**: fetch_batch 表 0 条记录。

**根因**: `DataInitializer._record_batch()` 可能未被正确调用，或在初始化过程中出错。

**影响**: 数据状态页的批次溯源信息为空。不影响核心功能。

**修法**: 检查 `_record_batch()` 调用路径。

---

## 阶段3: 性能测试 — 详细审查

### 测试结果

| 测试 | 条件数 | 结果数 | 平均耗时 | 5秒通过率 |
|---|---|---|---|---|
| 3条件筛选 | 3 | 44 条 | 37.5ms | 3/3 |
| 20条件筛选 | 20 | **0 条** | 17.6ms | 10/10 |

### 问题分析

20 条件筛选返回 0 条结果，原因是 3 个分红相关条件全部不满足：

| 条件 | 通过数 | 通过率 |
|---|---|---|
| `dividend_yield > 0.0` | **0** | **0%** |
| `payout_ratio > 0.1` | **0** | **0%** |
| `consecutive_div_years > 3` | **0** | **0%** |
| `interest_coverage > 2.0` | 13 | 0.5% |

其他条件的通过率也有问题：

| 条件 | 通过率 | 分析 |
|---|---|---|
| `pe_ttm < 100` | 6.7% | 合理（高PE排除） |
| `roe > 0.05` | 4.7% | **偏低**（ROE 5%是很低的门槛，应该有更多通过） |
| `roa > 0.02` | 5.5% | **偏低** |
| `net_profit_cagr5 > 0.0` | 29.1% | 合理 |

`roe > 0.05` 只有 4.7% 通过率，说明 ROE 计算可能有问题（TTM 计算简化导致值偏小或为负）。

### 性能结论

**性能本身没有问题** — 17.6ms 远低于 5 秒目标。但测试结果无效（0 条结果），因为数据不完整。

**真正需要验证的**: 在分红数据补全后，20 条件应该返回 50-500 条结果，且仍在 5 秒内。

---

## 阶段5: 打包发布 — 状态

- `value-dashboard.spec` 配置文件存在 ✅
- `frontend/dist/` 已构建 ✅
- `app/web/static/index.html` 存在 ✅
- `dist/` 目录不存在 ❌ — PyInstaller 从未执行

---

## 阻断问题汇总

按优先级排序，以下问题阻断后续阶段：

### P0（必须修复才能继续）

| # | 问题 | 阻断阶段 | 修法 |
|---|---|---|---|
| P0-1 | 分红数据严重缺失（5527/5528只股票无分红） | 阶段3/4 | 补充分红数据抓取 |
| P0-2 | 指标快照只覆盖47%（2600/5528） | 阶段3/4 | 补充财报数据后重新计算 |

### P1（建议修复）

| # | 问题 | 阻断阶段 | 修法 |
|---|---|---|---|
| P1-1 | 申万行业完全缺失 | 阶段4（行业排名验收） | 手动下载 CSV |
| P1-2 | ROE/ROA 通过率偏低 | 阶段3（性能测试有效性） | 检查 TTM 计算逻辑 |
| P1-3 | PyInstaller 未实际执行 | 阶段5/6 | 执行打包 |

### P2（可延后）

| # | 问题 | 说明 |
|---|---|---|
| P2-1 | fetch_batch 溯源表为空 | 不影响功能 |
| P2-2 | price_daily_qfq 缺 328 只 | 可能是 BSE 股票 |

---

## 下一步行动建议

### 立即执行（按顺序）

**Step 1: 补充分红数据**（P0-1）

```python
# 在 DataInitializer 中添加或单独执行：
# 对所有 stock_meta 中的股票抓取分红记录
from app.core.adapters.manager import AdapterManager
from app.core.adapters.base import FetchRequest

mgr = AdapterManager()
stocks = duck.read_query("SELECT stock_code FROM stock_meta")
for stock in stocks:
    result = mgr.fetch(FetchRequest(data_type="dividends", stock_codes=[stock["stock_code"]]))
    # 写入 dividends 表
```

**Step 2: 补充财报数据 + 重新计算指标**（P0-2）

```bash
# 检查哪些股票缺少财报
vd data status

# 重新抓取失败的
vd data update

# 重新计算指标
vd data compute_indicators
```

**Step 3: 下载申万行业**（P1-1）

从 swsresearch.com 下载申万行业分类 CSV，放到 `config/sw_industry_cache.csv`，然后：
```bash
vd data init  # 会加载申万行业缓存
```

**Step 4: 重新运行性能测试**（验证 P0 修复）

```bash
python tests/test_m10_performance.py
# 预期：20条件返回 50-500 条结果，平均 < 100ms
```

**Step 5: 运行验收测试**

```bash
python tests/test_m10_acceptance.py
```

**Step 6: 打包**

```bash
pyinstaller value-dashboard.spec
# 验证 dist/value-dashboard/value-dashboard.exe 可运行
```

---

## 预估修复工时

| 任务 | 预估工时 |
|---|---|
| 补充分红数据（5528只×0.5s限流） | ~45分钟 |
| 补充财报数据 + 重新计算指标 | ~30分钟 |
| 下载申万行业 CSV | ~10分钟 |
| 重新性能测试 | ~5分钟 |
| 验收测试 | ~30分钟 |
| PyInstaller 打包 + 验证 | ~30分钟 |
| 冒烟测试 | ~15分钟 |
| **合计** | **~2.5小时** |
