---
title: 个股详情缺失字段清理与来源材料移除报告
status: approved
category: reports
created: 2026-08-31
last-reviewed: 2026-08-31
---

# 个股详情缺失字段清理与来源材料移除报告（2026-08-31）

## 裁决

**实施完成。**
- 个股详情页不再把未采集到的指标渲染成一张张 “—” 空卡片：指标摘要区只显示确有数据的指标；财务趋势图/表只提供确有数据的指标与列。
- 按用户要求移除个股详情页底部的“来源材料”章节与粘性目录入口，同时不再请求 `/source-audit`。

## 背景

用户以海尔生物（688139）为例反馈：
1. 详情页大量指标显示 “—”，包括财务明细；
2. 底部来源材料不需要展示。

经查：
- `688139` 的核心快照指标可正常展示；但三大报表明细字段大部分为 NULL。
- 当前财务采集链路中，Sina 主源返回的是完整财报项目，但解析器只映射了 7 个核心字段（total_assets/total_liabilities/total_equity*/revenue/parent_net_profit/cf_from_operating），因此大多数股票的明细行项目确实没有入库。
- 业务概览 `company_profile` 覆盖 2,542 / 5,551 只（约 45.8%），尚未完成全市场采集。
- 这是真实的数据缺口，不是界面渲染错误；在补采完成前，界面不应把空值铺满。

## 实施内容

- `frontend/src/components/IndicatorGroupSection.vue`
  - 过滤 `value` 为 `—`/空值的指标卡片；整组无数据时整组隐藏。
- `frontend/src/components/FinancialTrendCard.vue`
  - 指标选择器和表格列根据历史序列实际可用字段动态过滤。
- `frontend/src/views/StockDetailPage.vue`
  - 删除“来源材料”章节、目录项、`DataTraceability` 组件引用与 `/source-audit` 请求。
- `frontend/tests/component/stock-detail-flow.test.ts`
  - 更新目录为 5 个章节，移除来源材料断言，并将不可信空指标测试改为“不渲染空卡片”。

## 验证

- 前端：ESLint 通过；Vitest 12 文件 58 tests 通过；`npm run build` 成功。
- 后端无接口变更。

## 数据补全边界（诚实披露）

- 核心最小财务集（总资产/总负债/权益/营收/归母净利/经营现金流）覆盖最新报告期约 5,386 只。
- 三表明细字段当前覆盖远低于此（约 20 只来自备用源），原因见上述 Sina 映射范围。
- 业务概览当前覆盖 2,542 只，另有 3,009 只尚未采集；`retry_open=0`、`missing_open=0`，说明这些缺口属于尚未排到，不是失败积压。
