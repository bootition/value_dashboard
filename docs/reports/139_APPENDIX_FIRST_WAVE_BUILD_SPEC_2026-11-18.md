---
title: 第一波 6 个指标 · 构建规格与已跑通的 SQL（reports/139 附录）
status: approved
category: reports
created: 2026-11-18
last-reviewed: 2026-11-18
supplements: reports/139
number-collision-note: 本文件是 reports/139 的**附录**（第一波 6 个指标的构建规格与已跑通 SQL）。同编号主报告是 139_INVESTMENT_INDICATOR_FIELD_AUDIT_2026-11-18.md（42 个大师指标对照复审）；两份是「主报告 + 附录」关系，引用时请写全文件名。
---

# 第一波 6 个指标 · 构建规格（字段 / SQL / 实测 / 落地清单）

> **这份文档解决什么**：`reports/139` 说的是「哪些指标能算、要先修什么」，本附录把**第一波 6 个指标**
> 落成可以直接开工的规格：口径 → 输入字段（含 v48 新列）→ **已在本库跑通的 SQL** → 外部标杆对照 → 落地步骤。
>
> **验收方式**：下面 6 段 SQL 全部在**正式库只读**跑过（2026-11-18，截面 2025-12-31），
> 结果落在 `docs/evidence/evidence-indicator-audit-2026-11-18.json` 的 `first_wave_specs`，
> 方法与坑见 `docs/skills/composite-indicator/SKILL.md` 的 10 步清单。

---

## 0. 六个指标的统一规矩（开工前先记住）

| 规矩 | 依据 | 落地写法 |
|---|---|---|
| **金融股一律显式剔除** | `reports/131` §11.3、`reports/139` §5.1 | `{is_non_financial_sql('m')}`（`app/core/sector.py`，**禁止手写** `sw_level1 NOT IN (...)`） |
| **每一次除法过四问** | `AGENTS` R4 | 分母精确列名 / ≤0 与近零怎么办（只标 `status`，不截断不置空）/ 量级对得上标杆吗 / 空值是数学不成立还是没抓到 |
| **三列位** | `reports/131` K7 | 每个指标最终要输出 `latest` / `5y_median` / `10y_median`（只给一个中位会掩盖「历史好现在崩」） |
| **派生列三件套** | `AGENTS` R3 | 进 `calculator.derived_snapshot_domains()` + `update.py` 重建步骤 + 快照重发保全校验 |
| **空值必须有原因** | `AGENTS` R4-4、D37 | 写 `indicator_null_reason`（6 类），别留无声 NULL |
| **字段要进 3 张注册表** | `reports/139` §5.1 的 23 步 | `STATEMENT_FIELDS`（筛选白名单）/ `FIELD_METADATA`（单位与期间）/ `field_units.py` + 前端 `screening-format.ts`（RG3 两处） |

---

## 1. #1 Graham Number（格雷厄姆数，合理价格上限）

**口径**：`GN = √(22.5 × EPS × BPS)`，与 `PE × PB ≤ 22.5` 数学等价；`price/GN < 1` = 低于合理价格上限。

| 输入 | 字段 | 覆盖率（2025 年报） |
|---|---|---|
| EPS | `income_statement.basic_eps` | 5,625/5,625 |
| 归母每股净资产 | `indicator_ext.bps_parent` | 5,475/5,625 |
| 报告日收盘价 | `indicator_ext.report_date_close` | 5,448/5,625 |

```sql
WITH base AS (
  SELECT ie.stock_code, i.basic_eps, ie.bps_parent, ie.report_date_close AS px
  FROM indicator_ext ie
  JOIN income_statement i ON i.stock_code=ie.stock_code AND i.report_date=ie.report_date
  WHERE ie.report_date = DATE '2025-12-31'
    AND i.basic_eps > 0 AND ie.bps_parent > 0 AND ie.report_date_close > 0
), g AS (
  SELECT stock_code, px,
         SQRT(22.5 * basic_eps * bps_parent) AS graham_number,
         px / NULLIF(SQRT(22.5 * basic_eps * bps_parent), 0) AS price_over_gn
  FROM base
)
SELECT COUNT(*) n, MEDIAN(price_over_gn) med, QUANTILE_CONT(price_over_gn, 0.9) p90,
       SUM(CASE WHEN price_over_gn < 1 THEN 1 ELSE 0 END) n_below
FROM g;
```

**实测（2025-12-31）**：n=3,956、`price/GN` 中位 **2.499**、p90 7.47、最大 84.6、低于 GN 的 **344 只**。
**标杆对照**：A 股长期整体高于 `PE×PB=22.5` 这条线，中位 2.5 倍属合理量级。
**落地还需**：`eps <= 0` 或 `bps <= 0` 时**不置空整行**，而是 `status='INVALID_DOMAIN'`；
`report_date_close IS NULL` 记 `NOT_LISTED_AT_REPORT_DATE`（当时未上市，不是缺数）。

---

## 2. #3 盈利收益率 − 2×10Y 国债（格雷厄姆 / Kahn）

**口径**：`E/P = 归母净利 ÷ 报告期市值`，与 `2 × 10Y 国债` 比较（**必须改名**：格雷厄姆原文是 2×AAA 公司债，
本库只有国债与 AAA 曲线，见 §7 与 `docs/runbooks/ops-knowledge-base.md` D60）。

| 输入 | 字段 |
|---|---|
| 归母净利 | `income_statement.parent_net_profit` |
| 报告期市值 | `indicator_ext.market_cap_at_report`（**不要**用快照 `total_market_cap`，那是当前股本、会前视） |
| 无风险利率 | `treasury_yield_curve`（`tenor_years=10`，取 `curve_date <= 报告日` 最近一条） |

```sql
WITH gov AS (SELECT yield_pct FROM treasury_yield_curve WHERE tenor_years=10
             AND curve_date <= DATE '2025-12-31' ORDER BY curve_date DESC LIMIT 1),
e AS (SELECT i.parent_net_profit / ie.market_cap_at_report AS ep
      FROM indicator_ext ie JOIN income_statement i
        ON i.stock_code=ie.stock_code AND i.report_date=ie.report_date
      WHERE ie.report_date=DATE '2025-12-31' AND ie.market_cap_at_report>0 AND i.parent_net_profit>0)
SELECT COUNT(*) n, MEDIAN(ep)*100 med_ep_pct, (SELECT yield_pct FROM gov) gov10y,
       SUM(CASE WHEN ep*100 > 2*(SELECT yield_pct FROM gov) THEN 1 ELSE 0 END) n_pass_2x
FROM e;
```

**实测**：n=3,994、E/P 中位 **2.112%**、10Y 国债 **1.847%**、2× 阈值 3.695%、过线 **1,055 只**。
**落地还需**：`pass_2x_gov10y` 改名 `pass_2x_gov10y_proxy`；亏损股（E/P<0）单独分档，不要与「贵」混在一起。

---

## 3. #37 Altman Z（制造业经典 5 变量）

**口径**：`Z = 1.2·X1 + 1.4·X2 + 3.3·X3 + 0.6·X4 + 1.0·X5`，X1=WC/TA、X2=留存/TA、X3=EBIT/TA、
X4=市值/TL、X5=营收/TA；原始分界 1.81 / 2.99。

```sql
WITH a AS (
  SELECT b.stock_code, b.total_assets ta, b.total_liabilities tl,
         b.total_current_assets - b.total_current_liabilities wc,
         COALESCE(b.undistributed_profit,0)+COALESCE(b.surplus_reserve,0) re,
         ie.ebit, ie.market_cap_at_report mve,
         COALESCE(i.revenue, i.total_operating_revenue) sales
  FROM balance_sheet b
  LEFT JOIN income_statement i ON i.stock_code=b.stock_code AND i.report_date=b.report_date
  LEFT JOIN indicator_ext ie ON ie.stock_code=b.stock_code AND ie.report_date=b.report_date
  WHERE b.report_date=DATE '2025-12-31'
)
SELECT COUNT(*) n, MEDIAN(1.2*wc/ta + 1.4*re/ta + 3.3*ebit/ta + 0.6*mve/tl + 1.0*sales/ta) med_z,
       SUM(CASE WHEN 1.2*wc/ta + 1.4*re/ta + 3.3*ebit/ta + 0.6*mve/tl + 1.0*sales/ta > 2.99 THEN 1 ELSE 0 END) n_safe
FROM a WHERE ta>0 AND tl>0 AND wc IS NOT NULL AND ebit IS NOT NULL AND mve IS NOT NULL;
```

**实测**：n=5,347、中位 **4.052**、安全区 **3,393 只（63.5%）**、p20 = 1.81。
**标杆对照**：中位 4.05 远高于原始 2.99 ⇒ **原阈值不能照搬**（A 股无破产样本可校准）；
`1.81 ≈ p20` 说明改用「后 20% 为危险区」仍可用。
**落地还需**：`z_status ∈ {ok, tiny_liabilities, missing_market_cap, financial_sector}`；
`total_liabilities/TL` 趋零会爆表（实测 tl/ta<5% 的 35 只），要标 `status` 而不是截断。

---

## 4. #38 Altman Z''（非制造业 / 新兴市场版，账面权益替代市值）

**口径**：`Z'' = 6.56·X1 + 3.26·X2 + 6.72·X3 + 1.05·X4'`（X4' = 账面权益/TL，**不需要市值**→历史最深）。

```sql
WITH a AS (
  SELECT b.stock_code, b.total_assets ta, b.total_liabilities tl,
         b.total_current_assets - b.total_current_liabilities wc,
         COALESCE(b.undistributed_profit,0)+COALESCE(b.surplus_reserve,0) re,
         ie.ebit, b.total_equity bve
  FROM balance_sheet b
  LEFT JOIN indicator_ext ie ON ie.stock_code=b.stock_code AND ie.report_date=b.report_date
  WHERE b.report_date=DATE '2025-12-31'
)
SELECT COUNT(*) n,
       MEDIAN(6.56*wc/ta + 3.26*re/ta + 6.72*ebit/ta + 1.05*bve/tl) med_zpp,
       SUM(CASE WHEN 6.56*wc/ta + 3.26*re/ta + 6.72*ebit/ta + 1.05*bve/tl > 2.60 THEN 1 ELSE 0 END) n_safe
FROM a WHERE ta>0 AND tl>0 AND wc IS NOT NULL AND ebit IS NOT NULL;
```

**实测**：n=5,525、中位 **4.08**、安全区 3,694 只。
**本源特有**：它是**唯一不用市值**的排雷式 → 可做 20–30 年历史中位数（1994 年起 682→5,805 只，`reports/139` §3.1 #38）。
**落地还需**：负营运资本要给辅助列（2025 年 882/5,625 只 WC<0，其中「负 WC 且当年盈利」是渠道强势特征，不是危险）。

---

## 5. #39 Piotroski F 分数（9 项 0/1）

**口径**：9 个二元信号求和（0–9）：ROA>0、CFO>0、ΔROA>0、CFO>ROA（应计质量）、Δ杠杆<0、Δ流动比率>0、
未增发（此处用 Δ股本，见下）、Δ毛利率>0、Δ资产周转率>0。

```sql
WITH cur AS (
  SELECT b.stock_code, b.report_date, b.total_assets ta, b.total_current_assets ca,
         b.total_current_liabilities cl, b.long_term_loans ltd, b.bonds_payable bonds,
         i.revenue sales, i.cost_of_revenue cogs, i.parent_net_profit pnp, c.cf_from_operating ocf,
         LAG(b.total_assets) OVER w ta_p, LAG(b.total_current_assets) OVER w ca_p,
         LAG(b.total_current_liabilities) OVER w cl_p,
         LAG(b.long_term_loans) OVER w ltd_p, LAG(b.bonds_payable) OVER w bonds_p,
         LAG(i.revenue) OVER w sales_p, LAG(i.cost_of_revenue) OVER w cogs_p,
         LAG(i.parent_net_profit) OVER w pnp_p, LAG(c.cf_from_operating) OVER w ocf_p
  FROM balance_sheet b
  JOIN income_statement i ON i.stock_code=b.stock_code AND i.report_date=b.report_date
  LEFT JOIN cash_flow c ON c.stock_code=b.stock_code AND c.report_date=b.report_date
  WHERE strftime(b.report_date,'%m-%d')='12-31'
  WINDOW w AS (PARTITION BY b.stock_code ORDER BY b.report_date)
)
SELECT COUNT(*) n, AVG(f) avg_f, MEDIAN(f) med_f, SUM(CASE WHEN f>=8 THEN 1 ELSE 0 END) n_f8
FROM (
  SELECT (CASE WHEN ocf>0 THEN 1 ELSE 0 END)
       + (CASE WHEN ta>0 AND ocf/ta>0 THEN 1 ELSE 0 END)
       + (CASE WHEN ta>0 AND ta_p>0 AND ocf/ta > ocf_p/ta_p THEN 1 ELSE 0 END)
       + (CASE WHEN ta>0 AND ocf/ta > pnp/ta THEN 1 ELSE 0 END)
       + (CASE WHEN (COALESCE(ltd,0)+COALESCE(bonds,0))/NULLIF(ta,0)
                 < (COALESCE(ltd_p,0)+COALESCE(bonds_p,0))/NULLIF(ta_p,0) THEN 1 ELSE 0 END)
       + (CASE WHEN ca>0 AND cl>0 AND ca_p>0 AND cl_p>0 AND ca/cl > ca_p/cl_p THEN 1 ELSE 0 END)
       + (CASE WHEN sales>0 AND sales_p>0 AND sales > sales_p THEN 1 ELSE 0 END)
       + (CASE WHEN sales>0 AND cogs IS NOT NULL AND cogs_p IS NOT NULL
                AND cogs/sales < cogs_p/sales_p THEN 1 ELSE 0 END)
       + (CASE WHEN ta>0 AND ta_p>0 AND sales/ta > sales_p/ta_p THEN 1 ELSE 0 END) AS f
  FROM cur
  WHERE report_date = DATE '2025-12-31' AND ta>0 AND ta_p>0 AND cl>0 AND cl_p>0
);
```

**实测**：n=5,525、均值 **5.198**、中位 5、`F≥8` **569 只（10.3%）**。
**标杆对照**：Piotroski 原样本 `F≥8` 约 10% —— **量级完全吻合**，是 6 个里口径最可信的一个。
**落地还需**：①9 个 signal 全部改成「先判 NULL 再判 0/1」并输出 `f_valid_n`（现在是 `ELSE 0`，
「无法计算」被记成 0 分）；②第 7 项若严格按 Piotroski 原文应是「未增发」，本版用「营收增长」占位，
落地前需用 `funding_events` 换成真实增发判定（`reports/139` #39 必修项）。

---

## 6. #27 ROCE 持久性（10 年中位 + 最低值 + 变异系数）

**口径**：复用 `roce_history`（口径 C：`EBIT = 利润总额 + 财务费用`，`CE = 总资产 − 流动负债`），
三个列位 `roce_10y_median`（成色）/ `roce_10y_min`（地板）/ `roce_10y_cv`（稳定性，仅 `中位 ≥ 2%` 时输出）。

```sql
SELECT COUNT(*) n, COUNT(roce_10y_median) n_10y, COUNT(roce_10y_min) n_min, COUNT(roce_10y_cv) n_cv,
       MEDIAN(roce_10y_median) med_10y, MEDIAN(roce_10y_min) med_min, MEDIAN(roce_10y_cv) med_cv,
       SUM(CASE WHEN roce_10y_years>=10 AND roce_10y_median>=0.15 AND roce_10y_min>=0.10 THEN 1 ELSE 0 END) n_persistent
FROM indicator_snapshot;
```

**实测**：n=5,575、10y 中位 **4,214 只**（严格取满 10 年）、min 4,214、cv **3,730**（484 只因中位<2% 不出 cv）；
全市场中位 `roce_10y_median` **8.45%**、`roce_10y_min` 中位 **−0.27%**、`roce_10y_cv` 中位 **0.718**；
同时满足「10 年中位≥15% 且最差年≥10%」= **235 只**。
**落地还需**：`roce_eligible`（非金融 + 市值≥100 亿 + 有效年数≥5）**不进** `SNAPSHOT_COLUMNS`（内部门禁），
三个列位要在筛选里并列暴露；`cv>10` 的 14 行是真巨亏年（已逐条核过），不设上限。

---

## 7. 第一波落地清单（按顺序，8 步就够）

| # | 动作 | 落点 | 已完成? |
|---|---|---|---|
| 1 | 口径定稿（本文件 §1–§6） | 本文件 | ✅ |
| 2 | 字段存在 + 语义核对 | `information_schema` + `reports/139` §2 | ✅ |
| 3 | 金融股剔除单一化 | `app/core/sector.py` | ✅ |
| 4 | 分母状态列（`*_status`）与空值原因 | `indicator_null_reason` + 各指标列 | ⏳ 每指标落地时补 |
| 5 | schema 列（`f_score` / `altman_z` / `altman_zpp` / `graham_number` / `ep_minus_gov2x` / `roce_*`） | `schema.py` v49 | ⏳ |
| 6 | 计算器 + 派生域登记 + 重建触发 | `calculator.py` / `derived_snapshot_domains()` / `update.py` | ⏳ |
| 7 | 三张注册表（白名单 / DSL 元数据 / 单位两处） | `engine.py` / `ast_nodes.py` / `field_units.py` + `screening-format.ts` | ⏳ |
| 8 | 用例：分母≤0、金融股、历史不足、极值不设护栏、重发不丢、增量后新鲜 | `tests/regression/` | ⏳ |

> 第 5–8 步是**真正的编码工作量**（按 `reports/139` §5.1 的 23 步取最小集），
> 但前置（1–3）与数据（`macro_series`/`bond_yield_curve`/补采科目）已经就位，
> 开工前跑一次 `scripts/check_indicator_readiness.py` 必须不是 BLOCK。
