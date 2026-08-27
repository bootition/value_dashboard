---
title: 记账式国债数据域需求评估报告（2026-08-09）
status: superseded
category: reports
created: 2026-08-09
last-reviewed: 2026-08-09
superseded-by: reports/68_STOCK_DETAIL_AND_TREASURY_FEASIBILITY_2026-08-10.md
---

# 记账式国债数据域需求评估报告

## 1. 已确认范围

- 仅记账式附息国债和记账式贴现国债。
- 新增独立“国债”页面，日终更新。
- 展示国债收益率曲线、交易所成交行情及银行间/中债参考；两类行情严格按来源和口径隔离。
- 仅个人本地研究，不对外服务或再分发。
- 不将国债数据写入任何 A 股表、筛选、排名、快照或 readiness 路径。
- 不自动计算股息率相对国债的利差、比值或投资结论。

## 2. 数据源审查

财政部-中国国债收益率曲线公开覆盖 3月至30年九个关键期限，工作日约17:30发布，历史可追溯至2006-03-01，适合作为曲线主候选。[1]

交易所公开免费行情和中国货币网公开现券信息可作为运行适配层候选；AKShare 文档记录了相应的新浪、货币网、巨潮和中债查询适配接口，但其本身不是授权或权威性承诺。[2]

中国债券信息网公开逐券估值页可查询更完整的估值字段，但其页面声明“未经允许请勿转载”，并将明细数据导向数据服务。因此，中债参考只能以“个人本地研究、默认不可导出、不得再分发”的保守边界纳入；实施前必须完成实际访问方式和条款复核。[3]

## 3. 架构裁决

**可立项，条件通过。** 必须建立独立国债数据域；任何复用 A 股 `stock_meta`、raw/qfq 价格表、指标快照或自动更新价格扫描的实现均为 BLOCK，因为会污染股票数据质量门禁和筛选池。

最大未解决风险是中债公开查询数据的自动采集与本地持久化是否符合其使用边界。最小减险动作是阶段 0 进行有界探测和条款复核；若不能确认，首期仅交付财政部曲线、交易所实际行情和中国货币网当日参考，并把中债显示为未启用来源。

完整开发计划：`.planning/2026-08-09-treasury-bond-domain/task_plan.md`。

## 4. 依据

1. 财政部-中国国债收益率曲线：https://yield.chinabond.com.cn/cbweb-czb-web/czb/moreInfo?locale=cn_ZH；编制说明：https://yield.chinabond.com.cn/cbweb-czb-web/czb/bzcxsmDown?locale=cn_ZH（2026-08-09 访问）。
2. AKShare 债券数据接口文档：https://akshare.akfamily.xyz/data/bond/bond.html（2026-08-09 访问）。
3. 中国债券信息网-中债估值：https://yield.chinabond.com.cn/cbweb-mn/val/val_query_list?locale=zh_CN（2026-08-09 访问）。
