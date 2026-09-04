---
title: 多指数 ERP 与 ETF 轮动工作台实施报告
status: approved
category: reports
created: 2026-09-05
last-reviewed: 2026-09-05
---

# 多指数 ERP 与 ETF 轮动工作台实施（2026-09-05）

## 1. 裁决

**实施完成，待用户浏览器验收与 ETF 池删改。** 两个需求按 grill-me 定稿
口径落地：① 顶层「指数」页 = 指数卡片墙 + 单指数 PE/PB/ERP 分位图 +
页内「ETF 轮动策略」工作台（持仓/流水/网格信号/手动录入/Excel 导入）；
② 多指数 ERP 覆盖乐咕 12 宽基 + 申万一级 31 行业（趋势图+分位带+对比表，
回测二期未做）。数据源按申万日报/乐咕/中证官网/同花顺四源组合；正式库
已完成宽基 12 + 申万 31（118,591 行）+ 用户 16 只 ETF 行情回填与真实
Excel 导入。

## 2. 需求基线

- `.planning/2026-09-05-etf-index-dashboard/requirements.md`（grill-me 定稿）
- `reports/82` 数据前置、`reports/110` 更新链修复、STATUS 2026-09-04

## 3. 实施内容

### 3.1 共用地基（schema v21-v23）
- `index_valuation`：v21 增 `pe_metric`（ttm/static/sws_daily）与 `extra`；
  宽基 12（乐咕月末 PE/PB）、申万一级 31（sws 日度 PE/PB）、中证官网交叉。
- `etf_daily`：v22 建表；v23 增同花顺跟踪指数 PE-TTM 五年分位列。
- SQLite v16：`etf_meta`/`etf_trades`/`etf_cash_flows`/`etf_sell_plans`/
  `etf_settings`（ETF 工作台操作域）。
- 适配器：`sws`（直连 swsresearch，50000/页 + 3 并发）、`ths`
  （同花顺官方 Financial-API，httpx 客户端，Key 仅读环境变量）。

### 3.2 多指数 ERP
- `app/core/index_dashboard.py`：ERP = 1/PE-TTM − 10Y 国债（百分点）；
  近 10 年分位带 p10/p20/p50/p80/p90；样本 <30 置信度低、无数据如实
  unavailable；行业 ERP `backtest_validated=false`。
- API：`/api/index/catalog|overview|erp-compare|{code}/erp|{code}/valuation`。
- 前端：路由 `/index`、`/index/:code`，顶层导航「指数」；卡片墙/对比表/
  详情三图（ERP、PE、PB + 分位带），复用自绘 SVG `IndexValuationChart`。

### 3.3 ETF 轮动工作台
- 引擎 `app/core/etf_strategy.py`：手动预算、单档=预算÷10、默认 5% 可配置
  网格、10 档上限；卖出计划锁定单档=触发时持仓市值÷10、10 档后清尾仓；
  摊余成本含手续费；信号区 20/80；GET 只读不落卖出计划。
- 导入 `vd etf import-xlsx`：交易流水/资金流水/持仓看板/ETF基础信息；
  幂等跳过重复；手续费入成本；总资产入设置。
- 采集 `vd etf update-prices`：THS 日线 + 跟踪分位，失败保旧值+retry；
  QDII 分位日期不重叠时补 close=NULL 独立行；QDII 上游分位为 null 时
  如实 unavailable。
- API：`/api/etf overview|{code}/detail` + POST meta/trades/cash-flows/settings。
- 前端：「指数」页内「ETF 轮动策略」Tab：汇总、网格表、买卖/资金录入、
  预算/总资产编辑、每 ETF 详情弹窗（PE/PB 20/80 分位带线图）。

## 4. 正式库落库与验证

| 项目 | 结果 |
|---|---|
| 宽基 12 指数 | 全部回填（乐咕 144~261 月末点 + 中证交叉 20 日；沪深300 保留 5,204 行日度历史） |
| 申万一级 31 | 118,591 行（2006-01-04 ~ 2026-09-04），向量化插入 59s 完成 |
| ETF 行情 | 16 只全部成功（5 年日线 611~1211 行 + 跟踪分位） |
| Excel 导入 | 49 笔流水 / 3 资金 / 16 元数据 / 总资产 4100.99，逐只金额与旧表一致 |
| 沪深300 ERP | 6.07%、分位 68.3%（2026-09-03；文章 2026-07 为 5.12%/56%，方向一致） |
| ETF 信号 | 证券ETF PB分位 15.6→买入区；酒/食品饮料 PE分位 14.8→买入区；红利类 >80→卖出区；恒生科技如实不可得 |

## 5. 门禁

- 后端定向回归：**57 passed**（隔离 profile：融资/指数估值域 30、ERP 6、
  ETF 引擎 10、导入 3、API 4、采集 4）。
- Ruff：全部改动文件通过。
- 前端：lint 通过、**57 tests passed**、production build 通过。
- 官方 S1 包装器：在 WorkBuddy shim 环境下 pytest tmp 清理与进程状态冻结
  触发 PermissionError（与 reports/82 §5.4 已登记的环境问题同类）；隔离
  profile 直跑作为本阶段代码正确性证据，用户正常环境需补跑一次 S1 确认。
- 正式库写路径全部经 `vd.bat` CLI 单写者锁；本报告发布时无运行中服务。

## 6. 诚实披露

1. 乐咕上游 2026-09 起为**月度序列**（原日度 5,196 行已留存于沪深300），
   其余宽基分位按月度口径计算，UI 标注 cadence=monthly。
2. 申万日报 `pe` 上游未注明 TTM/静态：落库 `pe_metric=sws_daily`、置信度
   approximate，行业 ERP 标注"暂无回测验证"。
3. 乐咕对连发请求敏感（约 4 指数后 403）：适配器限速 2s/请求，批量回填按
   逐指数+30s 冷却执行；自动更新后续需评估分日轮转宽基组。
4. THS 对 QDII（513130/159605）跟踪分位返回 null：如实「分位不可得」，
   价格/网格/盈亏照常。
5. ETF 跟踪指数为初始映射（行业 ETF→申万一级代理），池草案已交付用户
   `D:\Mr.Q\掌控经济\A股ETF轮动策略\ETF备选池草案-2026-09-05.md` 删改。
6. ERP 回测（分位→未来一年收益）按定稿留待二期。
7. 官方 S1 待用户停用无关 python 进程（单词 App streamlit）后补跑。

## 7. 后续

- 用户浏览器验收 + ETF 池删改 + 调仓后界面补录
- ERP 回测二期；ETF 详情迷你信号线在表格内嵌（当前为详情弹窗）
- 自动更新宽基组分日轮转 + ETF 行情每日窗口（当前手动 `vd etf update-prices`）
- 官方 S1 补跑与 push 确认
