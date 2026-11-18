---
title: 42 个大师指标 × 本库科目对照矩阵（reports/139 附录）
status: approved
category: evidence
created: 2026-11-18
last-reviewed: 2026-11-18
---

# 42 个大师指标 · 科目对照与可构建性矩阵

> 由 7 个流派审查 agent 产出、主控按 `docs/reports/139` §2「主控复核更正」逐条校正后的逐指标明细。
> 机读版：`docs/evidence/evidence-indicator-audit-2026-11-18.json`；DB 实测：同目录 round1–4 探针。
>
> ⚠️ **本文件保留 7 个 agent 的原始判定与原始数字**（未经主控删改，便于追溯）。已知需要按 `reports/139` §2.4 修正的有 3 处：
> ① 凡把「P0-9 市值 10 倍」列为阻断级的条目（16 条）——实测**已修好**（689009 八期比值 1.0、最新期 bad_rows=0）；
> ② #30 的送转单位判反了——`xdxr.songzhuangu` 实测是**每股比例**，`1 + songzhuangu` 正确；
> ③ #17/#18 的"指数序列缺失"实为字段路径写错（`index_valuation.extra->>'index_close'` 有 122,695 行）。
> 另：#4 的「扇出 +36%」按行数重算为 **+56.7%**。

裁决口径：`buildable_now` = 可直接落成正式指标；`buildable_after_fix` = 字段/数据够，但必须先改 X；`blocked` = 缺数据或结构性不可算。

## #1 格雷厄姆数（合理价格上限）Graham Number — `buildable_after_fix`
- **理由**：字段与口径基本自洽（bsp_parent/book_to_market 同为归母口径，EPS 有兜底），2016-2025 每年 2600+ 只可算；但必须先堵 P0-9（689009 市值与每股族错 10 倍，2024-12-31 起 8 个报告期）、补 bps_parent 的单位登记、并按 K3 禁止给 price/GN 设上限，否则「便宜榜」会被 10 倍错值污染。
- **分母体检**：三处除法逐条过 R4：(a) 精确分母：price_to_graham_number 的分母是 GrahamNumber=sqrt(22.5×eps×bvps)，pe_annual 的分母是 income_statement.parent_net_profit，pb_annual 的分母是 indicator_ext.book_to_market。(b) 会不会 ≤0：GN 由 eps>0 AND bvps>0 双守卫保证 >0；pnp 与 book_to_market 各有 >0 守卫，均属「置 NULL 不截断」，符合 R4-2。(c) 量级对得上标杆吗：price/GN 逐年中位 1.45-4.68（reviews 实测）、2025 中位 2.50、过关 344 只，与「A 股常年整体高于格雷厄姆红线」的常识相符；但 bvps→0 的壳公司会把比值放大到 931 倍（27 行 >100），方向全在「贵」一侧，不污染最便宜榜。(d) 空值语义要分三类写进 indicator_null_reason：eps≤0/bvps≤0 = 数学不成立（INVALID_DOMAIN）、report_date_close 为 NULL = 当时未上市而非缺数据（NOT_LISTED_AT_REPORT_DATE）、689009 = 派生表陈旧错值（STALE_DERIVED_10X，必须修不能置空）。
- **历史可行性**：可做 2016-2025 共 10 期年报截面（每年 2614-4139 只样本，schools/graham.json#L52），满足 K6「一年一个截面点、10 年中位数要 10 期」；同口径真正起点是 2007（basic_eps 覆盖率跳 93%），2000-2006 只能靠派生 EPS 补成 approximate。断点：FY2007 新会计准则（basic_eps/扣非/商誉成建制出现，报告 §9.4）、FY2025 report_type 断崖（故一律 month(report_date)=12，不用 report_type）、P0-9 自 2024-12-31 起 8 个报告期。落地须给「10 年中位 / 5 年中位 / 最新值」三个列位（SKILL 第 10 步、K7）。
- **通病标签**：T3, T2, T5
- **ROCE 同型**：否
- **必修项**：
  - P0-9 闸门（阻断级）：在落库 SQL 里对 |market_cap_at_report/(report_date_close×当期股本)-1| > 1% 的股票（当前仅 689009）强制重算或置 NULL 并写 indicator_null_reason='STALE_DERIVED_10X'；根治按 reports/131 §9.2 P0-9 修法：extended.py 输入指纹增补 share_capital_history + _refresh_indicator_ext() 加 force_codes，重跑 vd data compute-extended-indicators。
  - 单位登记（RG3）：bps_parent 加入 app/core/screening/field_units.py 的 PER_SHARE_FIELDS（现仅 bps，field_units.py:80）；新派生列 graham_number（元/股）、price_to_graham_number（倍数）、pb_annual（倍数）不得沿用 book_to_market 的 pct 小数口径（field_units.py:40），须同时改前端 frontend/src/utils/screening-format.ts。
  - 禁止护栏（K3/R4-2）：不要给 price_to_graham_number 设上限或 abs 过滤；极端值只打 status 并按横截面分位展示（reviews 实测 27 行 >100、max 931.4）。
  - 补 traps⑤ 的交叉校验列（报告 §3.1 修法④至今只在文字里）：同时输出 pe_annual<=15 与 pb_annual<=1.5 两个布尔，避免 P/E=40 配 P/B=0.56 也算过关。
  - 三列位：graham_number 最新值 + price_to_gn 的 5 年/10 年中位（SKILL 第 10 步、K7）。
  - 落地走 SKILL 第 8 步：derived_snapshot_domains() 登记 + update.py 重建步骤，否则快照重发会清成 NULL（K5）。
  - 价格新鲜度标记：对 report_date_close 取价日滞后 >30 天的行（§9.4 记 116 行、最长 2,169 天）打 PRICE_STALE 标记，避免停牌价当阈值价。
- **证据**：reports/131 §3.1「1. 格雷厄姆数」字段映射 exact4/approx1/missing0 与修法①②③④；reports/131 §2 总表#1：审查=🟡需修正、ROCE同型=低；reports/131 §4.3 + §9.2 P0-9（689009 市值/每股族 10 倍，2024-12-31 起 8 期，43,478 观测仅 1 只）；reports/131 §9.4 P2 勾稽完整性#5（report_date_close 116 行滞后 >30 天、最长 2,169 天）；schools/graham.json#L16-L54（formula_sql 与 traps①-⑥）；reviews/graham.json#L12-L20（bps_parent 实测 5475/5625 而非 5512、中位 P/GN 1.45-4.68、27 行 >100）；app/core/storage/schema.py:2070-2071（v30 市值五列）、:2090-2099（bps vs bps_parent 口径），实为 fields-inventory.md 缺失的 23 列基线外列；app/core/screening/field_units.py:40（book_to_market 为 pct 小数）、:80（PER_SHARE_FIELDS 只有 bps，无 bps_parent）

## #2 净流动资产价值（净净股）NCAV 与净现金折价 — `buildable_after_fix`
- **理由**：主线 NCAVPS（= (流动资产合计 − 负债合计) ÷ 期末股本）字段全真、恒等式已验、2008-2025 可做 10 年分位；但 schools/graham.json#L123 的「金融股靠 total_current_assets IS NOT NULL 天然剔除」被 §9.4 P2 证伪（27 只金融股该列存 0 而非 NULL），必须先改成显式 sw_level1 剔除；同时 minority_interest 的 COALESCE 0 不安全，NNWC/净现金两个变体只有 2019-2025 共 7 期，做不了 10 年历史。
- **缺列/缺数据**：balance_sheet.受限货币资金（库内无此列 → 净现金/NNWC 偏乐观）；balance_sheet.应收款项融资（库内无此列 → NNWC 跨 2019 结构性缺口）
- **分母体检**：主分母是 share_capital_history.total_shares；比率分母是 ncav_ps=(total_current_assets − total_liabilities)/shares。R4 四问：(a) 精确列 = share_capital_history.total_shares（effective_date<=report_date 最近一条），不用 stock_meta.total_shares（那是当前股本）。(b) ≤0：shares 有 WHERE shares>0 守卫；ncav_ps ≤0 在 A 股非常常见（CA<TL 公司），裸除会出巨负比值（reviews/graham.json#L33 实测 301565 ≈ −28,629、920981 ≈ −13,747），故 ratio 只在 ncav_ps>0 时输出——但按 R4-2 应保留 ncav_ps 原值、只给 status='INVALID_DENOMINATOR'，不是把整行置空。(c) 量级：2025 中位 price/NCAVPS ≈7.59、net-net 命中每年 0-5 只（2025=3、2021=5），与「A 股净净股濒危」的共识一致；裸比值最大到 261,790（123 行 >1000），方向在「贵」一侧，不污染最便宜榜。(d) 空值分四类：CA/TL 为 NULL = 金融股结构性不适用（INDUSTRY_NOT_APPLICABLE）、shares 取不到 = 股本链缺失（SOURCE_MISSING，2003 年只有 68% 覆盖）、ncav_ps≤0 = 数学不成立（INVALID_DENOMINATOR）、report_date_close 为 NULL = 当时未上市（NOT_LISTED）。四类必须分开写 indicator_null_reason。
- **历史可行性**：主线 NCAVPS：2003-2025 每年可算（2003 年 1055 只 → 2010 年 1808 只 → 2025 年 5347 只），但股本链 2003-2006 只有 68%，干净起点 2008（18 年）；10 年历史分位（2016-2025 每年 2752-5347 只）可行。K6：NCAVPS 是单期量、不需要上一期，10 年中位数要的是 10 个年报截面（有），但落地若同时给「5 年/10 年中位 + 最新值」三列位，不能只看上市年限。NNWC 与净现金两个变体受 trading_financial_assets/notes_receivable 限制，只有 2019-2025 共 7 期 → **做不了 10 年历史中位数（K6 不足）**，只能当期截面 + 7 年分位。断点：FY2019 新金融工具准则（交易性金融资产口径 +28.3pp、应收票据归零）、FY2016 起 0/NULL 写法切换（goodwill/bonds_payable/trading_financial_assets/notes_receivable）、FY2025 现金流明细半空壳（不直接影响 NCAV，但任何要现金流的变体在 2025 失去加项）。
- **通病标签**：T2, T3, T5
- **ROCE 同型**：否
- **必修项**：
  - 把行业剔除写进 SQL（阻断级）：COALESCE(m.sw_level1,'') NOT IN ('银行','非银金融')，不能只写在 traps 里（reports/131 §4.2 P1-a 的典型病）；同时把 total_current_assets = 0 视为 NULL/不适用（§9.4 P2：27 只金融股、361 行零填充），否则 CA−TL 会算出巨负 NCAVPS 并被 CASE 静默变 NULL。
  - 少数股东权益降级：minority_interest 标 approximate，COALESCE 0 改为 CASE WHEN minority_interest IS NULL AND minority_shareholder_profit > 0 THEN NULL END（2025 年 32 家反例，reviews/graham.json#L29）。
  - NNWC / 净现金变体限定 2019-2025 并在列名或元数据里标口径版本号；同时在 indicator_null_reason 登记「无应收款项融资」「无受限资金」两个缺口（§9.4 跨年#6、schools/graham.json#L88）。
  - R4-2：不要给 price_to_ncav 设上限/abs 过滤；ncav_ps≤0 时保留原值 + status='INVALID_DENOMINATOR'。净净股每年 0-5 只，二元筛选等于空转，必须输出横截面分位（schools/graham.json#L123 trap③）。
  - 三列位：最新 pd/NCAVPS + 5 年、10 年中位（SKILL 第 10 步、K7）。
  - 落地走 SKILL 第 8 步：derived_snapshot_domains() 登记 + update.py 重建步骤（K5）。
- **证据**：reports/131 §3.1「2. NCAV」字段映射 exact7/approx2/missing0 与修法①②；reports/131 §2 总表#2：审查=🟡需修正、ROCE同型=低；reports/131 §9.4 P2 单位量纲#1（金融股流动/非流动小计被零填充）+ P2 勾稽#4（27 只金融股 361 行存 0 而非 NULL）；reports/131 §9.4 P2 跨年#6（FY2019 应收票据归零、库内无「应收款项融资」列）与 NULL 语义#4（78 只 minority_interest 为 NULL、32 只损益>0）；reports/131 §11.3（凡分母用营运资本/流动比率/总资产周转，金融股一律失效，判据用 sw_level1）；schools/graham.json#L62-L124（formula_sql + traps①-⑥）；reviews/graham.json#L29-L37（minority_interest 32 家反例、恒等式 5520/5520、301565/−28,629、中位 7.59）；docs/skills/composite-indicator/SKILL.md 第 3/5/7 步（恒等式、分母≤0 只标 status、金融股用 sw_level1 剔除）

## #3 盈利收益率与 10 年期国债利差（E/P − 无风险利率） — `buildable_after_fix`
- **理由**：本流派里最干净的一条：分子分母字段全真、分母有 >0 守卫、国债曲线 2006-03 起 20 个年报截面可做 10 年历史中位（report 审查判 ✅ 通过）。只有两件事必须先做：① 689009 的 market_cap_at_report 小 10 倍会把 E/P 放大 10 倍直接顶到最前（P0-9）；② 格雷厄姆原式是「2×AA/AAA 公司债」，本库只有国债（G3 缺口），阈值必须改名 + 参数化，否则是「名义格雷厄姆、实际另一把尺子」。
- **缺列/缺数据**：treasury_yield_curve 的信用债/信用利差（AA/AAA 曲线）—— 库内为零命中
- **语义错配**：indicator_snapshot.pe_ttm（用它反算 E/P 会把全部亏损样本静默丢掉，非空仅 3751/5575）；indicator_snapshot.div_yield_spread_10y（是「股息率−10Y国债」，衡量分红回报不是盈利回报，与 E/P 利差不可互换）
- **分母体检**：分母是 indicator_ext.market_cap_at_report（比率侧还有 gov10y_pct）。R4 四问：(a) 精确列 = indicator_ext.market_cap_at_report，SQL 有 WHERE market_cap_at_report > 0 过滤，不用 total_market_cap（那是当前股本口径，会造成历史前视）。(b) 会不会 ≤0：市值理论上恒正，守卫已挡；gov10y_pct 在 2006-2025 落在 1.744%–4.156% 之间、远离 0，且 ep_over_gov10y 用 NULLIF(gov10y_pct,0) 兜底。(c) 量级：2025 中位 E/P 2.112%（盈利样本）/1.273%（全样本），2×国债过关 1163/5447 —— 与「A 股股权风险补偿极薄、该阈值实际是全市场估值水位线」的结论一致；|E/P|>100 的极端行 75 行（0.12%）全部来自小市值（加 ≥1 亿市值下限即可）。(d) 空值：parent_net_profit 全非空，故空值只来自 ① 当时未上市（无市值/无价格，NOT_LISTED）② 市值脏值（689009，STALE_DERIVED_10X，必须修不能置空）；没有「数学不成立」这一类。
- **历史可行性**：本库时间深度最好的复合指标之一：国债 2006-03 起日频，配年报截面得 2006-2025 共 20 个点（K6：10 年中位数要 10 期年报，2016-2025 满足；要做 20 年长分位也够）。断点：①FY2007 新会计准则（若改用扣非/EBIT 口径才受影响，本口径不受影响）；②FY2025 report_type 断崖（一律 month(report_date)=12）；③FY2019 新金融工具（不影响 E/P）；④P0-9 自 2024-12-31 起 8 个报告期。若按 trap⑤ 改成「近 3 年平均归母净利润 ÷ 当期市值」——那需要额外 2 期年报，起点后移到 2008，且需先把 3 年平均的口径断点（2018 减值符号翻转 P0-7）处理掉。
- **通病标签**：T1
- **ROCE 同型**：否
- **必修项**：
  - P0-9 闸门（阻断级）：对 market_cap_at_report 与 report_date_close×当期股本偏差 >1% 的股票（当前仅 689009）置 NULL + indicator_null_reason='STALE_DERIVED_10X' 或先重算（reports/131 §9.2 P0-9 修法）；否则 E/P 被放大 10 倍、直接污染「最便宜」榜单首位。
  - 阈值语义改造：pass_2x_gov10y 改名为 pass_2x_gov10y_proxy，并把「信用利差 bp」做成可配置参数（默认 0，建议同时输出 +100bp 敏感度列）；文档必须写「以国债替代 AA 债 ⇒ 门槛系统性偏低 50-150bp」（reports/131 §5 G3、schools/graham.json#L153-L156）。
  - T1 量纲登记：E/P、利差、国债收益率三者都按**百分数**（3.99 = 3.99%）计算；落库若用 pct 小数比例必须与 field_units.py / screening-format.ts 同步登记（RG3），否则前端 100 倍漂移。
  - 市值下限：对 |E/P|>100 的 75 行加 mcap >= 1 亿元的过滤或标记（reviews/graham.json#L52 建议），但**不能**改成截断/上限（R4-2、K3）。
  - 禁止用 1/pe_ttm 反算 E/P（亏损样本 pe_ttm 为 NULL，会静默丢样本）；如要复用快照，必须直接用 parent_net_profit/market_cap。
  - 口径增强（可选但建议）：按 trap⑤ 用近 3 年平均归母净利润做「正常化 E/P」，并在列名标注 normalised，避免单年一次性损益（资产处置/投资收益）污染。
- **证据**：reports/131 §3.1「3. E/P 利差」字段映射 exact4/approx1/missing1 与审查裁决 ✅ 通过；reports/131 §2 总表#3：审查=✅通过、ROCE同型=低；reports/131 §5 G3（无信用债收益率/信用利差，只有国债曲线；「2×AAA 企业债」用国债替代必须改名）；reports/131 §4.3 + §9.2 P0-9（689009 市值 0.1 倍，2024-12-31 起 8 期）；schools/graham.json#L131-L175（formula_sql、trap①-⑥、AA/AAA 标 missing 的说明）；reviews/graham.json#L46-L54（2007=17/1354、2020=592/3934、2025=1163/5447、国债年均值、75 行 |E/P|>100、BETWEEN 会污染均值）；app/core/storage/schema.py:2070-2071（market_cap_at_report 为 v30 ALTER 增量列）；app/core/screening/field_units.py:1-30（pct 小数 vs percent 百分数两套口径的区别）

## #4 防御型投资者七准则合规分（0-7） — `buildable_after_fix`
- **理由**：7 个科目全部真实存在、数据够（1996-2025 年报截面齐全），但现实现的 c4 有结构性缺陷：streak 按每段分红区间出行再以 last_y>=T 连接，导致 (stock,date) 一对多扇出（108,482 行 vs 69,242 个真实样本）且允许「分红区间起点晚于报告年」的前视偏差；叠加 c3/c5 用行数冒充日历年、金融股只靠 WHERE cl>0 侥幸挡住、689009 的 PE/PB 双伪命中（评分 +2），必须返工后重测才能上线——这也是本流派唯一判 ROCE 同型=高 的一条。
- **缺列/缺数据**：balance_sheet.长期应付款（库内无此列 → c2 的长期负债系统性偏小）
- **语义错配**：dividends.ex_date + dividends.dividend_per_share（列本身真实，但 streak + last_y>=T 的实现带来前视偏差与一对多扇出）
- **分母体检**：四处分母逐个过 R4：(a) 精确列：c2 分母 = balance_sheet.total_current_liabilities（另有 (ca−cl) 作被比对象）、c5 分母 = 7-9 年前的 3 年均 EPS、c6 分母 = income_statement.parent_net_profit、c7 分母 = indicator_ext.book_to_market。(b) ≤0：cl 有 WHERE fin.cl > 0（顺带挡掉 0 填充的 27 只金融股）；c5 有 eps_avg3_7to9y_ago > 0；c6 有 pnp > 0（漏了会把低 PE 从 249 虚增到 1,668）；c7 有 book_to_market > 0。四处都是「整条置 0 分」而不是截断，方向保守但**会把「数据不适用」和「不达标」混为一谈**——金融股/次新股 c2 得 0 分与真正杠杆过高的公司无法区分，这是本指标最需要改的分母语义。(c) 量级：2025 年分档 7→1 只(600511)/6→35/5→107/0→1236，7/7 全过只有 1 只 ⇒ 证明「绝不能做二元筛选」；c6=249 只与 reviews 实测一致。(d) 空值：①金融股 CA/CL 为 NULL 或 0 = INDUSTRY_NOT_APPLICABLE（不是缺数）②次新股不足 10 个年报 = INSUFFICIENT_HISTORY（不是不达标）③689009 = STALE_DERIVED_10X（错值，必须修）④dividends 无记录 = 真没分红。四类现在全被压成 0 分，必须拆开。
- **历史可行性**：字段与数据都支持长历史：1996-2025 共 30 个年报截面，但**真正 >=2,000 只的只有 2011-2025 共 15 年**（1998 年 2,131 行其实只有 667 只，早期年份的行数是被 streak 扇出膨胀的，reviews/graham.json#L65）。K6：c3 要 10 个年报点、c5 要 10 个连续年报（近 3 年 + 7-9 年前 3 年，实际需 10 期），c4 要 20 个分红年份 ⇒ 合法起点 c3/c5 约 2007（新会计准则后口径统一）、c4 为 2011-2012（分红数据 1991 起 + 20 年）。故历史中位数窗口建议 2011-2025（15 年）或更稳的 2016-2025（10 年），且必须显式剔除「数据年数不足 10 年」的样本，否则把「样本不足」误判为「不达标」（K6 原始事故就是这么来的）。断点：FY2007 新会计准则（EPS/扣非成建制出现）、FY2016 起 0/NULL 写法切换、FY2025 report_type 断崖（一律 month(report_date)=12）。
- **通病标签**：T5, T3, T2
- **ROCE 同型**：是
- **必修项**：
  - ★c4 改造（阻断级）：先把分红年份截断为 yy <= year(report_date)，再按股计算「截至 T 年已连续 >=20 年」的滚动年数；删除 last_y >= T 这种跨未来条件（reviews/graham.json#L71 修法②）。
  - ★消除扇出（阻断级）：streak 必须收敛到每股一行（或直接改成 per-(stock,T) 的相关子查询），输出层再加 GROUP BY/DISTINCT 兜底，并补断言 n_rows = n_distinct(stock_code, report_date)——现状 108,482 vs 69,242（+36%），早期年份膨胀达 3.7 倍（reviews/graham.json#L69）。
  - c3/c5 增加日历年连续性校验：count = 10 且 max(year)-min(year) = 9；c5 窗口改为按年份定位的 3 年均值，不用 ROWS 偏移（P3 探针会给出 calendar_gap_stocks）。
  - 金融股显式剔除：加 COALESCE(m.sw_level1,'') NOT IN ('银行','非银金融')（§11.3、SKILL 第 7 步）；并把金融股/次新股的 0 分改成 status=INDUSTRY_NOT_APPLICABLE / INSUFFICIENT_HISTORY，不要把「不适用」混进「不达标」（R4-4）。
  - P0-9 闸门：689009 的 PE/PB 双伪命中会给它 +2 分（足够把 5 分推到 7 分，而 2025 年全市场 7/7 只有 1 只），必须置 NULL + indicator_null_reason='STALE_DERIVED_10X'。
  - 阈值参数化：c1 的 50 亿营收、c2 的 2 倍流动比率、c6 的 15 倍 PE 全部做成可配置参数并在 UI 标注（格雷厄姆 1972 年的美元阈值不能直接套 A 股）。
  - 输出改造：不做二元筛选，输出 0-7 分 + 历史分位（2025 年 7 分只有 1 只、0 分 1,236 只，二元筛选等于空转）；并给三列位（最新分/5 年中位/10 年中位）。
  - history_years 更正：把「1998 年起每年 >=2,000 只」改为「2011 年起」，把「c4 在 2011 年前恒为 0」改为「2012 年前无合法命中、现实现会输出约 197 只伪命中」（reviews/graham.json#L110）。
- **证据**：reports/131 §3.1「4. 防御型七准则」审查裁决 ❌ 不通过 + 三条 ⚠️（match/样本量/ROCE 同型）与四条修法；reports/131 §2 总表#4：审查=❌不通过、ROCE同型=高（本流派唯一一条）；reports/131 §11.1/§11.3（金融股无流动/非流动划分、资产负债率 90%，凡用营运资本/流动比率的指标一律失效，判据用 sw_level1）；reports/131 §9.4 P2 单位量纲#1 + 勾稽#4（27 只金融股 361 行零填充，不是 NULL）；reports/131 §4.3 + §9.2 P0-9（689009 市值 0.1 倍 → PE/PB 双伪命中）；schools/graham.json#L182-L232（formula_sql、trap①-⑦，其中 trap⑥ 的 last_y>=T 正是缺陷来源）；reviews/graham.json#L63-L71 与 critical_issues#L109-L111（108,482/69,242、695 个 (stock,date) 分数不唯一、1998 年仅 667 只、c4 伪命中 197 只）；docs/skills/composite-indicator/SKILL.md 第 7 步（金融股剔除）与第 9 步（有效数据年数不等于上市年限）

## #5 格雷厄姆成长股价值公式 V = EPS × (8.5 + 2g)（含利率重标定版） — `buildable_after_fix`
- **理由**：字段全真（basic_eps + 派生兜底 + 10Y 国债 + 收盘价）、且本指标不吃 P0-9（只用了正确的 report_date_close，不用市值/每股族），10 年历史可行；但两处必须先修：① g 未定义被 DuckDB 的 GREATEST(NULL,0)=0 静默记成「零增长」，实测 13,920/46,810（29.7%）样本、其中 13,005 行仍产出非空内在价值——与它自己写的 trap④ 直接冲突；② 8.5 是 1962 年校准常数，必须走 ×4.4/Y 重标定并明确「只能横截面排序、跨年不可比」。
- **缺列/缺数据**：treasury_yield_curve 的信用债/信用利差（AA/AAA）—— 库内为零命中，Y 只能用 10Y 国债替代
- **分母体检**：本指标两处除法：(a) 精确分母：g 的基期是 eps_avg3_7y_ago、margin_of_safety 的分母是 report_date_close、rate_adj 的分母是 avg(yield_pct)。(b) 会不会 ≤0：g 有 eps3_now>0 且 eps3_7y>0 双守卫（但见下条：守卫失败时的补救是错的）；price 未加 >0 守卫（0 价会除爆，必须补）；Y 落在 1.744%-4.156% 且 NULLIF 兜底。★关键缺陷：g 未定义时 SQL 用 LEAST(GREATEST(g_raw*100,0),15)，而 DuckDB 的 GREATEST(NULL,0)=0 ⇒ 13,920 行（29.7%）被当成「零增长」，其中 13,005 行仍算出非空 V 与安全边际（例 002432 2023 年 rate_adj=182.83、安全边际 +200%），这是 T4「护栏掩盖看不懂的数字」的教科书案例。(c) 量级：V 与股价同量级（002379：g_raw 68.11%、v1962=32.72、rate_adj=82.57、股价 23.92，reviews/graham.json#L84），中位安全边际在 0 附近；但 2025 年 rate_multiplier=4.4/1.744=2.52 会把 V 放大 2.5 倍，跨年不可比。(d) 空值：g 未定义 = 数学不成立（INVALID_DOMAIN，必须 NULL 且不出 V）、Y 缺失 = SOURCE_EMPTY、price 缺失 = NOT_LISTED、n_years_10<10 = INSUFFICIENT_HISTORY（K6）。
- **历史可行性**：1999-2025 共 27 个年报截面可算（1999=54、2007=939、2016=1996、2025=4567 只，reviews/graham.json#L82 精确复现），10 年历史中位数（2016-2025 每年 1996-4567 只）完全可行。K6：g 需要 10 个连续年报点（近 3 年 + 7-9 年前 3 年，共 10 期）⇒ 2007 年前可算的都是「有 10 年连续报表的老股」，存在明显幸存者/长上市期偏差，真正可比起点是 2007（19 年）。断点：FY2007 新会计准则（EPS/扣非/公允价值成建制出现）、FY2025 report_type 断崖（一律 month(report_date)=12）、利率环境本身（2014 年 4.16% vs 2025 年 1.74%，同一 g 的 V 差 2.4 倍）。另注意 schools/graham.json 的 formula_sql 末尾已有 WHERE n_years_10 = 10（与 reviews 修法③「只输出不过滤」的描述不符，以源码为准，待 graham_5.sql P1 复算确认）。
- **通病标签**：T4, T3, T5
- **ROCE 同型**：否
- **必修项**：
  - ★g 置 NULL（阻断级）：改成 CASE WHEN eps3_now > 0 AND eps3_7y > 0 THEN LEAST(GREATEST(g_raw*100,0),15) END，并在 g 为 NULL 时不输出 graham_value / margin_of_safety（现在 29.7% 样本被静默记成零增长，reviews/graham.json#L84）。
  - ★截断不够（T4）：g 截到 [0,15] 只是遮住问题（002379 用 3 年均值口径仍得 g_raw=68.11%），必须再加「基期 3 年均 EPS 绝对下限（如 >=0.1 元）」+「基期连续为正」两个过滤器，否则榜单头部全是周期反转股和 ST 复活股。
  - ★price > 0 守卫：margin_of_safety 的分母 report_date_close 目前未过滤 0 值（0 价会除爆），补 WHERE price > 0 并写 INVALID_DENOMINATOR。
  - 跨年可比性声明：graham_value_rate_adj 只能在横截面内排序或与自身历史比；输出层必须带 rate_multiplier（4.4/Y）列，并禁止把 2014 与 2025 的 V 直接比较（报告 trap② 已明确，但 SQL 没体现）。
  - 口径单一化：固定「优先 basic_eps、缺失才派生」，并在结果里输出 eps_source 标记；禁止同一序列内混排两种股本口径（会产生 1-3% 的假增长拐点）。
  - 窗口定位修正：eps_avg3_7to9y / eps_avg3_now 用「按年份定位」的 join 而不是 ROWS 偏移，避免年报断档时基期错位（与 #4 的 c3/c5 同源问题）。
  - trap① 的实测数字要更正：报告写的「g=84.5%、V=394.66」不可复现，实测为 g_raw=68.11%、v1962=32.72、v_adj=82.57（股价 23.92）——带「实测」字样的数字必须全部复算（reviews/graham.json#L84 与 critical_issues#L115）。
- **证据**：reports/131 §3.1「5. 成长股价值公式」字段映射 exact2/approx3/missing0 与修法①②③；reports/131 §2 总表#5：审查=🟡需修正、ROCE同型=低；reports/131 §5 G3（无信用债曲线 → Y 只能替代）；reports/131 §4.1 T4（护栏拍脑袋/失效：LEAST/GREATEST 硬截断让指标退化）；reports/131 §4.3（P0-9 波及的是市值与每股族，收盘价不受影响 → #5 不吃这个 bug）；schools/graham.json#L239-L277（formula_sql、trap①-⑤）；reviews/graham.json#L82-L88 与 critical_issues#L112（13,920/46,810=29.7%、13,005 行仍出 V、002432 rate_adj=182.83、002379 实测 68.11%/32.72/82.57）；docs/skills/composite-indicator/SKILL.md 第 5 步（分母≤0 只标 status，不截断不设上限）与第 9 步（有效数据年数）

## #6 施洛斯式深度价值四因子（低 PB + 低杠杆 + 长期盈利 + 多年低位） — `buildable_after_fix`
- **理由**：6 个科目全真、当期截面可算（5,206 只 → 四因子全过 34 只）且是本流派唯一把金融股剔除写进 SQL 的一条；但现交付物只是「今天」的快照：f3 用「上市以来全部年报」的 count/min + n_years>=10 冒称「过去 10 个年报」（77% 样本实际是 11-24 年全历史），f4 用 price_daily_qfq 最新日为基准的前复权序列算「过去 5 年最高」，直接套到历史截面就是前视偏差。修完 f3 口径 + 把三条数据源换成报告期口径后即可上线历史回测。
- **分母体检**：三处分母逐条过 R4：(a) 精确列：f2 分母 = balance_sheet.total_equity、f4 分母 = 5 年窗口内 max(close)（high_5y）、f1 分母侧是 pb_mrq 本身。(b) 会不会 ≤0：total_equity 有 WHERE total_equity > 0 + NULLIF 双守卫；high_5y/low_5y 有 NULLIF(...,0)；pb_mrq 有 >0 过滤——四道门都在，故本指标没有 ROCE 式负分母事故（reviews/graham.json#L103 判 low）。真正的问题在**口径**而非符号：total_equity 含少数股东会把杠杆低估（可低到一半）。(c) 量级：中位 close/5 年高点实测 0.469（报告 0.47），price_off_5y_low 量级正常，四因子全过 34 只、base 5,206 —— 与「低 PB + 低杠杆 + 长期盈利 + 多年低位」四条同时成立时池子很小（约 0.65%）的常识一致。(d) 空值：①total_equity<=0/为 NULL = INVALID_DENOMINATOR（§4.1 T2 的同一批样本，2025 年归母权益<=0 有 54 只）②价格史不足 5 年 = INSUFFICIENT_HISTORY ③sw_level1 为 NULL = INDUSTRY_UNKNOWN（750 只，不能当「非金融」）④ST/退市 = 主动剔除（要留痕，不是空值）。
- **历史可行性**：当期截面完全可算（5,206 只，价格史 >=10 年日线的股票约 2,739 只，够做 10 年回测的物理条件）；但现 formula_sql 是「今天」的快照实现（indicator_snapshot.report_date 实测 2026-09-30 + 价格窗口止于全表最大交易日），**不能当历史序列用**（它自己的 trap⑧ 已声明）。要做 10 年历史中位数必须同时改三处：pb → indicator_ext.book_to_market（报告期口径）、价格窗口改成「截至该 report_date 的 5 年」、财务三项取对应报告期；且价格要用 raw × 当期因子或截面日当时的前复权基准，避免前视。K6：f3 要 10 个年报点、f4 要 5 年日线 ⇒ 四因子齐全的最早可用截面约 2007 年报（老股更早），10 年窗口 2016-2025 可行。断点：FY2007 新会计准则、FY2019 交易性金融资产口径扩大、FY2025 report_type 断崖。
- **通病标签**：T3, T5
- **ROCE 同型**：否
- **必修项**：
  - ★f3 口径改正（阻断级）：改为「最近 10 个年报」取 min（ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY report_date DESC) <= 10 再聚合），且要求 count = 10；同时把该 db_field 的 match 由 exact 降为 approximate，或把 note 改写成「口径 = 上市以来全部年报」。现状 77% 样本（3,991/5,206）实际用的是 11-24 年全历史 min，与自己的 trap⑤ 矛盾。
  - ★历史化改造（要做回测才阻断）：三段数据源换成报告期口径——pb 用 1/book_to_market、价格窗口截至 report_date、财务取对应报告期；并禁止用「最新日为基准」的 price_daily_qfq 直接回测（trap①，前视偏差）。
  - 行业标签兜底：COALESCE(sw_level1,'') 会放行 750 只标签缺失的股票，需补 csrc_l1='金融业' 交叉核验（并注意 csrc_l1 里混着同花顺/指南针等软件股，只能做兜底不能当主判据）。
  - P0-9 闸门：若把 f1 换成报告期口径的 1/book_to_market，689009 的 PB 会错 10 倍 → 同样需要置 NULL + STALE_DERIVED_10X；当期快照 pb_mrq 那条线也要用 graham_6.sql P6 验一遍是否也是唯一例外。
  - 杠杆口径固定：披露 total_equity（含少数股东）会低估杠杆，或直接改用 total_equity_parent 并同步改阈值；不能两种口径混跑。
  - 补有形 PB 交叉验证列：1 − (goodwill + intangible_assets)/total_equity_parent 口径的 PB'，用于识别「商誉虚高造成的假便宜」（trap⑥）。
  - 价格序列一致性：qfq 与 raw 行集不一致（313 只缺 35,077 行 / 289 只多 11,599 行，§9.3），f4 必须在一致子集上算或先修数据。
  - funnel 数字更正：把 5,223 统一为实测 5,206、f4 由 4,167 改为 4,154、价格史深度由 2,651/1,697 改为 2,739/1,693（reviews/graham.json#L99）。
- **证据**：reports/131 §3.1「6. 施洛斯四因子」字段映射 exact5/approx1/missing0 与修法①②③④；reports/131 §2 总表#6：审查=🟡需修正、ROCE同型=低；reports/131 §9.3 P1 派生表陈旧#5（price_daily_qfq 与 raw 行集不一致：313 只缺 35,077 行 / 289 只多 11,599 行）；reports/131 §9.4 P2 勾稽#4 + §4.1 T2（27 只金融股零填充、2025 年归母权益<=0 的 54 只）；reports/131 §11.3（判据用 sw_level1，不用 csrc_l1）；reports/131 §4.3 + §9.2 P0-9（book_to_market 对 689009 错 10 倍）；schools/graham.json#L284-L328（formula_sql、5 个 db_field、trap①-⑧，其中 trap⑤ 要求 count=10、trap⑧ 自认只能做当期截面）；reviews/graham.json#L97-L105 与 critical_issues#L114（5,206/34/446/2068/1782/4154、n_years=10 仅 269 只、n_years>10 有 3,991 只）；docs/skills/composite-indicator/SKILL.md 第 6 步（字段历史覆盖率与跨年断点）与第 7 步（金融股用 sw_level1 剔除）

## #7 股东盈余与股东盈余收益率（Owner Earnings / OE Yield） — `buildable_after_fix`
- **理由**：11 个科目全部真实存在（含 migration 加的 market_cap_at_report），无幻觉字段；但金融股未剔（121 只）、归母净利配合并折旧/capex、credit_impairment_loss 2025 整列空、689009 市值 1/10，四项修完才可入库。
- **语义错配**：income_statement.parent_net_profit（归母）与 cash_flow_indirect.*/cash_flow_activity.capex（合并）口径混搭——290 家少数股东权益>25% 的公司被系统性扭曲（万科 50.4%/中建 40.2%/保利 41.6%）
- **分母体检**：分母=indicator_ext.market_cap_at_report。R4：①精确列名已确认存在（migration，非清单内）；②会不会≤0：SQL 有 mcap>0，NULL 也挡，实测无需担心负值；③量级：全 A 2025 合计 122.9 万亿（reviews#L31），单只中位数量级正常，但 689009 被低估 10 倍→其 oe_yield 虚高 10 倍；A+H 股本高估市值（601398 28,263 亿 vs 纯 A 约 2.1 万亿）→ 收益率系统性偏低；④空值：2014 覆盖率 51%、2025 97%，属'没抓到'而非'数学不成立'，必须落 indicator_null_reason。
- **历史可行性**：可做 10 年中位数（2016–2025），不可做 20 年。逐年可算行数 2012=2,586 / 2016=3,431 / 2020=4,577 / 2025=5,537（reviews#L14）。K6：OE 是当年流量、单年值不需 N+1；但'10 年 OE 增长/中位'需 10 个年报点，宣称'10 年'必须有 10 期完整数据。断点：2021 使用权资产折旧、2025 credit_impairment_loss 整列空、2018 减值符号。
- **通病标签**：T5, P1-a, P1-b
- **ROCE 同型**：是
- **必修项**：
  - 加金融股剔除：用 app/core/supplement.py:507 的 FINANCIAL_SECTOR_UNIVERSE_PREDICATE（sw_level1 IN ('银行','非银金融') OR (sw_level1 IS NULL AND csrc_l1='金融业')）；禁止裸 NOT IN，否则 750 只 NULL 被三值逻辑静默丢
  - 分子口径统一：parent_net_profit + minority_shareholder_profit（或用 net_profit）以匹配合并 D&A/capex
  - 2025 改用 income_statement.credit_impairment_loss，或显式标注断点并禁止跨 2024→2025 比较 OE 增速
  - 输出 n_da_effective（D&A 非空只数）——trap ④ 自己的要求，shipped SQL 没做
  - 先修 689009（重跑 vd data compute-extended-indicators）并补 indicator_ext 重建触发；新列入库须登记 derived_snapshot_domains()（现仅有 roce）
- **证据**：schools/buffett.json#L14-L15；schools/buffett.json#L88；reviews/buffett.json#L12；reviews/buffett.json#L18；reports/131 §3.2#7；reports/131 §4.3；reports/131 §5 G2；fields-inventory.md#L80；fields-inventory.md#L89；app/core/storage/schema.py#L2085；app/core/indicators/extended.py#L277

## #8 巴菲特指标（证券化率）与 A 股可执行适配版 — `buildable_after_fix`
- **理由**：真·证券化率（市值/GDP）结构性 blocked——库内 65 张表 GDP 零命中（§5 G1）；但同构适配版（Σ市值/Σ营业总收入）字段全真、可算，前提是改名、锁池、加样本门槛、先修 689009。
- **缺列/缺数据**：gdp_annual（外部宏观 GDP；库内 65 张表零命中，属结构性缺口 G1）
- **分母体检**：分母=Σ income_statement.revenue（池内强制 revenue>0）→ 不会≤0、不存在单公司分母爆炸。真实风险在'池子漂移'：1997 年 576 只 → 2025 年 5,445 只（reviews#L35），早期缺失的多是小市值股，会把覆盖变好读成估值上升；1990 年报仅 5 只时市值/营收=183.7978（reviews#L33），现有 HAVING COUNT(*)>=500 是正确的 T5 护栏。量级标杆：2018=1.07 / 2021=1.52 / 2025=1.69（schools#L98），2007=4.33（reviews#L35）。
- **历史可行性**：真证券化率：0 年（GDP 缺）。A 股适配版：锁池 + HAVING>=500 后序列从 1997 年起共 29 年（reviews#L33）；依赖 market_cap_at_report 时只有 2020 年后可靠（2014 51%/2016 55%/2020 66%），改用自建市值（price_daily_raw 1990 年代起 + share_capital_history 5,551 只）可回到 2000 年前后。K6：'10 年中位数'需 10 个年度聚合点。
- **通病标签**：T5, T3
- **ROCE 同型**：否
- **必修项**：
  - 改名：明确写「A 股适配版 = Σ市值/Σ营业总收入，不是证券化率」，禁止被当成巴菲特指标使用（§5 G1）
  - GDP 写成显式外部注入接口（含'10 年 GDP 均值'分母选项，因 GDP 是流量、市值是存量）
  - 自建市值 SQL 目前只算 2025 单点，须改成按年循环/窗口出全序列，否则 20 年曲线算不出来
  - 锁池 + 样本门槛写进正式口径（HAVING>=500），并输出 n_stocks 列
  - 修 689009（对全A加总影响 0.0003% 可忽略，但个股层不可用）
  - 修正「2014 年 51%（3,338/5,955）」这对错配绝对数：3,338 实为 2018 年、5,955 实为 2023 年（reviews#L31）
- **证据**：schools/buffett.json#L95-L96；schools/buffett.json#L98；schools/buffett.json#L139；reviews/buffett.json#L31；reviews/buffett.json#L35；reports/131 §3.2#8；reports/131 §5 G1；fields-inventory.md#L11；fields-inventory.md#L59；app/core/storage/schema.py#L2085

## #9 十年中位数 ROE ≥15% 且真正低负债 — `buildable_after_fix`
- **理由**：12 条科目全部真实、归母净利配归母权益正确、历史最深（≥10 点 5,109 只）；但金融股一条没剔（招商银行实测通过）、LAG 跨年、HAVING>=8 冒充'十年'，三处必须改。
- **语义错配**：balance_sheet.short_term_loans / long_term_loans 对银行体系语义失效（银行用 financial_sector_items.borrowing_from_cb / interbank_borrowing / customer_deposits），COALESCE 0 后'有息负债/权益'对银行是伪造的 0
- **分母体检**：ROE 分母=(total_equity_parent_t + t−1)/2，SQL 强制两期均>0 且 eq_prev IS NOT NULL → 恒正、无爆炸。杠杆分母 total_equity_parent>0 同理。残余三处：①银行 short/long_term_loans 为 NULL 被 COALESCE 0 → 有息负债=0，招商银行 ROE 中位 15.2%、有息负债/权益中位 0.499 恰好通过两道闸（reviews#L50）——正是本流派最脏的点；②LAG 取在负权益过滤之后，55,531 个相邻年报对里 282 对（0.51%）跨年（reviews#L46），(期初+期末)/2 变成'跨年平均'；③按 SKILL 步骤5，极值不设上限是对的，但须输出 status。
- **历史可行性**：本流派最深：≥5 点 6,142 只 / ≥8 点 5,839 / ≥10 点 5,109 / ≥15 点 3,311（reviews#L48）。K6：ROE_t 需 t 与 t−1 两期资产负债表 → 10 个 ROE 值需 11 期年报；现 HAVING COUNT(*)>=8 只保证 8~10 年，与指标名'十年中位数'不符。断点：2020 新收入准则、2019 新金融工具、2021 租赁。
- **通病标签**：T5, P1-a
- **ROCE 同型**：否
- **必修项**：
  - 金融股双保险剔除：FINANCIAL_SECTOR_UNIVERSE_PREDICATE（app/core/supplement.py:507）+ i.cost_of_revenue IS NOT NULL；现 shipped SQL 一条都没有
  - LAG 改在'未过滤负权益'的完整年报序列上取前值，前值≤0 时置 NULL，避免跨年平均
  - HAVING COUNT(*)>=8 与'十年中位数'不一致：改 >=10，或把指标名改成'8~10 年中位数'（K6）
  - 银行存款类负债口径单列：金融股若保留，须改用 financial_sector_items 的有息负债科目，否则不许进榜
  - 输出三列位：10 年中位 / 近 5 年中位 / 最新年报值（K7）
- **证据**：schools/buffett.json#L146-L147；schools/buffett.json#L226；reviews/buffett.json#L46；reviews/buffett.json#L48；reviews/buffett.json#L50；reviews/buffett.json#L54；reports/131 §3.2#9；reports/131 §4.2 P1-a；reports/131 §10.1 K6；app/core/supplement.py#L507

## #10 毛利率十年稳定性（定价权代理） — `buildable_after_fix`
- **理由**：7 条科目全真、revenue−cost_of_revenue 配对正确、分母 revenue>0 有界无爆炸；但要补显式金融股剔除、2020 准则断点分段、最新值列位（K7）、regr_slope 注释与实现不一致，且 HAVING>=8 不等于十年。
- **分母体检**：分母=income_statement.revenue，WHERE revenue>0 强制 → 不会≤0、不会除零。真实风险是'近零分母'：revenue∈(0,某极小值] 时 1−cost/revenue 可飞出 ±1000%，现靠 MIN(gm)>=20% 的事后门槛挡掉尾部（不是护栏，是筛选）。实测 revenue<=0 共 2 只（688759 revenue=0、002977 revenue=−1.71 亿，reviews#L67；报告写 1 只属小错）。金融股靠 cost_of_revenue IS NULL 排除，属隐性依赖不是显式规则。2020 新收入准则把运输费从销售费用重分类进营业成本 → 全样本中位 ΔGM 2020=−0.86pp（reviews#L67），跨 2020 算 σ 会把政策变更读成定价权恶化。
- **历史可行性**：很好，仅次于 ROE：2004 年 cost_of_revenue 1,674/1,739（96%）、2012 年 3,653/3,747、2025 年 5,530/5,625（98%）；2016 年后有 ≥8 个毛利率点的 5,482 只、全历史 ≥10 点 5,023 只。K6：GM 是流量/流量，单年值不需 N+1，但'十年稳定性'须 10 个年报点（现 HAVING>=8）。断点：2020 新收入准则（主要）、2017 研发费用单列（不影响 GM）、2019 新金融工具。
- **通病标签**：P1-b
- **ROCE 同型**：否
- **必修项**：
  - 输出'最新年报毛利率'列位（K7）：现 SQL 只有 median/min/max/range/std/trend，正是'中位数很长、最新值很惨'看不出来的形态
  - 默认输出 2016-2019 / 2020-2025 两段 σ，或对 2020 做断点校正（P1-b）
  - 金融股改用显式 FINANCIAL_SECTOR_UNIVERSE_PREDICATE 剔除，不要只依赖 cost_of_revenue IS NULL
  - HAVING COUNT(*)>=8 改为 >=10（或改名'8~10 年'）
  - regr_slope 注释写'近 5 年'但 WHERE fy>=2016 让它跑满 10 年——注释与实现二选一对齐（reviews#L63）
  - 把'2025 年 revenue≤0 有 1 只'更正为 2 只；跨行业排名前按 sw_level1 行业内标准化
- **证据**：schools/buffett.json#L233-L234；schools/buffett.json#L281；schools/buffett.json#L283；reviews/buffett.json#L63；reviews/buffett.json#L65；reviews/buffett.json#L67；reports/131 §3.2#10；reports/131 §4.2 P1-b；reports/131 §10.1 K7；fields-inventory.md#L20

## #11 一美元测试（Retained-Earnings Market-Value Creation） — `buildable_after_fix`
- **理由**：字段全真、7 个样例可复现、自建市值 diff=0；但分母 Δ留存收益 的护栏按 ROCE 同型失效（过闸后仍 ±219）、金融股 121 只未剔、689009 市值 1/10，必须先改口径再入库。
- **语义错配**：schools/buffett.json#L346 trap④：「送股/转增不影响 undistributed_profit」——转增(资本公积→股本)确实不影响，但送红股是从未分配利润转出，ΔRE 被送股污染；须用 xdxr.songzhuangu 还原
- **分母体检**：分母=Δ留存收益 RE_t−RE_{t−N}。R4：①精确列名已确认（undistributed_profit + surplus_reserve，均为归母口径）；②会不会≤0：常见为负（成熟派息公司/累计亏损），现有 guard 只要求 ΔRE≥5%×期初权益；③量级：过闸的 1,667 家里仍有 118 家>10 倍、38 家>20 倍、157 家<−10、20 家<−50，极值 +219.2/−212.0（reviews#L86）——与 ROCE 事故完全同型；万科A 2015→2025 ΔRE 仅 11.42 亿 → 算出 −187.79；④空值：分母趋零属'数学不成立'，必须落 indicator_null_reason=INVALID_DENOMINATOR，不能当'没抓到'。量级标杆：600519=8.00、600276=8.32、600887=3.25、002415=2.40、600036=0.98、000651=0.92（reviews#L82）。
- **历史可行性**：可做 2015→2025 十年窗口（价格 1990 年代起、share_capital_history 5,551 只、最早 1990-12-19）；K6：窗口 N 年需两端年报即 N+1 个财年，2015→2025 需 11 期。窗口选择主导结论：5 年(2020→2025) 通过率 60.5%（1,420/2,346） vs 10 年(2015→2025) 39.1%（651/1,667）（reviews#L84）→ 必须同时报 5/10/15 年。断点：累计亏损公司 ΔRE 跨符号区间（期初符号须记录）。
- **通病标签**：T2, T4, P1-a
- **ROCE 同型**：是
- **必修项**：
  - 护栏收紧（T4）：ΔRE ≥ max(5%×期初归母权益, 2%×期末市值)，或分母改三年滚动平均 ΔRE；并对 |one_dollar|>10 输出 status（不是静默置 NULL——用户裁决极值要么对要么去修）
  - 金融股剔除：FINANCIAL_SECTOR_UNIVERSE_PREDICATE（supplement.py:507）；现输出混入 121 只金融股（§4.2 P1-a 点名的就是本指标）
  - 先修 689009（indicator_ext 重建触发）；自建市值路径已正确但表内路径错 10 倍
  - 送股还原：用 xdxr.songzhuangu / share_capital_history.change_reason 还原送股对 ΔRE 的污染，或改用'每股留存'口径
  - 把 dividend_interest_paid 的 match 由 missing 改为 approximate(2025 全空不可用)
  - 输出 5/10/15 三个窗口 + n_dropped
- **证据**：schools/buffett.json#L290-L291；schools/buffett.json#L344；schools/buffett.json#L346；reviews/buffett.json#L80；reviews/buffett.json#L82；reviews/buffett.json#L84；reviews/buffett.json#L86；reports/131 §3.2#11；reports/131 §4.2 P1-a；reports/131 §4.3；reports/131 §10.1 K5

## #12 留存资本化率（经营口径增量资本回报 RCR） — `buildable_after_fix`
- **理由**：12 条科目全真、EBIT 口径已实证可复现、主口径正确地没有减货币资金（避开 K2）；但分母只有符号 guard 导致 +108.5、分子合并÷分母归母、金融股 EBIT 崩坏、样本量被高估（5,071 → 4,363/2,507），四修后方可入库。
- **语义错配**：分子 NOPAT 为合并口径（EBIT 来自合并利润表 + 合并财务费用）÷ 分母归母 Δ留存收益——少数股东权益>25% 的 290 家（万科 50.4%/保利 41.6%/中建 40.2%）RCR 被系统性抬高
- **分母体检**：主口径分母=Δ留存收益（re_end−re_begin），SQL 只有 re_end>re_begin 的符号 guard、无相对阈值：2,507 行里 11 家 RCR>2、2 家>5，最大 +108.5、最小 −41.2（reviews#L103）——一美元测试至少用了 5% 相对 guard，这里没有，属 ROCE 同型。对照口径 ROIIC 分母 ΔIC>0 同理无阈值。K2 纪律遵守良好：主口径投入资本未扣货币资金（扣了会让茅台 2,446→1,930 亿、净现金公司趋零）。量级标杆：RCR≥1 为自我增值（schools#L355）；分布中位待 SQL P7 实测。
- **历史可行性**：两端齐全 4,363 只（2015 与 2025 都有 EBIT），叠加两端盈利 2,963 只，实际输出 2,507 行（reviews#L99）——报告声称的 5,071（≥10 个年报点的只数）不是本指标的实际样本量，高估约 49%。K6：N 年窗口需两端年报；5/10/15 年均可做。断点：端点 total_profit>0 在亏损年份静默剔除（非随机缺失）。
- **通病标签**：T4, T2, P1-a, P1-c
- **ROCE 同型**：是
- **必修项**：
  - 分母加相对 guard（ΔRE ≥ 5%×期初归母权益，与一美元测试对齐），或对 |RCR|>10 输出 status 并单列 n_dropped（T4）
  - 分子改归母口径 parent_net_profit×(1−税率)，或并列输出'剔除少数股东'版本
  - 金融股剔除：FINANCIAL_SECTOR_UNIVERSE_PREDICATE；银行/券商无财务费用行（实测 106 只 financial_expenses 为 NULL）、无营业成本，EBIT 口径完全崩坏
  - 税率 CLIP 必须输出命中率（n_rate_neg / n_rate_gt40），属 K3 所说'护栏'，不能静默
  - 把 interest_expense 由 missing 改为 approximate(12.6% 覆盖不可跨年)；删除'与 indicator_snapshot.interest_bearing_debt 定义一致'这句错述
  - 样本量从 5,071 更正为 4,363（两端齐全）/ 2,963（两端盈利）/ 2,507（实际输出）
- **证据**：schools/buffett.json#L353-L354；schools/buffett.json#L362；schools/buffett.json#L413；schools/buffett.json#L415；reviews/buffett.json#L97；reviews/buffett.json#L99；reviews/buffett.json#L101；reviews/buffett.json#L103；reports/131 §3.2#12；reports/131 §4.2 P1-a；reports/131 §10.1 K3

## #13 PEG（市盈率 / 盈利增速比） — `buildable_after_fix`
- **理由**：可算字段全部真实且样本可复现（1,738 只、中位 2.72），但 689009 市值 10 倍 bug、0.5% 增速下限护栏失效（16.7% PEG>10、max 418.8）、PE 分母被一次性收益污染、EPS 增速未修增发/配股，必须先修口径再落地。
- **缺列/缺数据**：盈利预测/一致预期表（PEG 原意所需，实库无；非幻觉列名）
- **分母体检**：① 分母1=income_statement.parent_net_profit（PE）；SQL 过滤 >0，不会 ≤0，但 000656 靠 658.96 亿投资收益做出 PE 0.52、扣非 -355.76 亿，属分母被一次性收益污染。② 分母2=g_pct=EPS 5 年 CAGR 百分点；SQL 带 0.5%≤g≤100% 护栏，0.5% 近零仍使 290/1,738（16.7%）PEG>10、p99=106.69、max=418.84（reports/131 §4.1 T4）。③ 量级：PEG<1 占 19.0%、中位 2.72，与 A 股成长股偏贵常识不矛盾；但 16.7% 的 >10 不可用于排名。④ 空值：eps_base/eps_now/pnp_now≤0 被 WHERE 静默排除，未写 indicator_null_reason；689009 市值 0.1×，若进入样本则 PE/PEG 偏小 10 倍（是否通过过滤待 SQL [13-2]/[13-1] 确认）。
- **历史可行性**：报告称 2001–2025 不成立：basic_eps 2000/2001/2002 非空仅 1/2/1 行，2006 才 1,521 行，2011 年 1,267 只、2012 起 1,915+ → 真实 PEG 序列 2011–2025。K6：5 年 CAGR 需 6 个年报点；10 年中位需 15 个年报点（2011–2025），即 2016 年起才有 10 个 PEG 点。2018 减值符号、2020 新收入准则均不改变归母净利；2025 现金流半空壳本指标不用。
- **通病标签**：T4, T2, T5
- **ROCE 同型**：是
- **必修项**：
  - 先重跑 vd data compute-extended-indicators 修 689009 市值（400.4 亿 vs 表内 40.04 亿），或在 SQL 显式剔除 689009；并把 share_capital_history 纳入 indicator_ext 输入指纹/重建触发（reports/131 §4.3、§9 P0-9）。
  - 增速下限从 0.5% 提到 ≥5%（或对 g 做 1%/99% winsorize），并输出 g<5% 样本数；不要用 0.5–100% 硬护栏掩盖近零分母（§4.1 T4）。
  - 并行输出扣非 PEG（deducted_net_profit），并剔除 PE<2（000656）这类一次性收益样本。
  - 用 funding_events.issue_shares/issue_price 修增发/配股/回购造成的 EPS 与净利增速背离（1,738 只中 563 只背离>5pp、36 只>15pp）。
  - 空值写 indicator_null_reason（NEGATIVE_EARNINGS / INVALID_DENOMINATOR / DELISTED），历史声明改为 2011–2025。
- **证据**：docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:381-399（§3.3 #13 全节）；docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:1032（§4.1 T4：PEG 16.7%>10、max 418.8）、1041（P1-c：2001–2005 样本为 0）；docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:1044-1069（§4.3 689009）、1257-1264（§9 P0-9）；fields-inventory.md:20（parent_net_profit/basic_eps/deducted_net_profit）、:29（xdxr.songzhuangu）、:89（indicator_ext 仅 23 列）；app/core/storage/schema.py:2070-2073（v30 market_cap_at_report）、app/core/indicators/extended.py:277（市值生成）；.planning/2026-11-16-masters-survey/context.md:28（indicator_ext 实际含 market_cap_at_report）、:38-40（2025/2020/2016 覆盖）；.planning/2026-11-16-masters-survey/reviews/growth.json（#13 实测 1,738 / 2.72 / 19.0% / 290 只>10）

## #14 PEGY（股息调整后的 PEG） — `buildable_after_fix`
- **理由**：分红/股本/市值字段真实、1,738 只与中位 2.27 可复现；但 689009 市值 bug 会同时把 PE 缩小 10 倍、股息率放大 10 倍，且 g+DY 近零时护栏无效、特别分红未降噪、历史深度被夸大。
- **缺列/缺数据**：2025 年可用分红现金流（dividend_interest_paid 全空）；一次性特别分红标记（无字段，只能用滚动 3 年中位数降噪）
- **语义错配**：cash_flow_activity.dividend_interest_paid（2025 全空，列在义失）
- **分母体检**：① 分母1=PE_FY 的 parent_net_profit（>0 过滤）；分母2=PEGY 的 g_pct+dy_pct，g 限 0.5–100%、dy≥0，故 g+dy>0、不会 ≤0，但可低至 0.5%：PE 20 倍、g+dy=0.6% 时 PEGY=33，属护栏而非修分母。② 样本 1,738；中位 2.27、23.9%<1，比 PEG 的 19.0% 多 4.9pp，量级与『高股息慢增长股被 PEG 错杀』解释一致。③ 空值：div.cash_div 缺失被 coalesce(...,0) 当 0，混淆『没分红』与『没采到』，应写 null reason。④ 若 689009 进入样本，市值 0.1× → PE 偏小 10×、股息率偏大 10×，PEGY 可低至真值 1/100（SQL [14-2]/[14-1] 确认），必须重算或剔除。
- **历史可行性**：分红数据 1991–2026，但 2012 前每年约 1,600 只有记录、滚动 12 个月口径 2010 后才稳定；EPS 5 年基数受 basic_eps 覆盖约束，真实可算约 2011–2025（稳定 2012+），审查保守口径 2015–2025 共 11 点 → 『10–15 年历史中位数』不成立，10 年中位需 15 个年报点。2018 减值/2020 收入准则不改变归母净利；2025 现金流半空壳本指标不用。
- **通病标签**：T4, T2, T5
- **ROCE 同型**：是
- **必修项**：
  - 与 PEG 同步修 689009：重跑 compute-extended-indicators 或剔除；并修正 market_cap_at_report 的重建触发（§4.3、§9 P0-9）。
  - 增速下限从 0.5% 提到 ≥5%，或对 (g+DY)<2% 的样本不排名；输出 min(g+DY) 与 <2% 计数。
  - 对特别分红降噪：用滚动股息率 3 年中位数替代当期值；600519 在 2025-12-19 派 23.957 元/股，滚动股息率被推到 3.755%（§3.3 #14 trap ④）。
  - 分红缺失不要 coalesce 0，写 indicator_null_reason；历史窗口封在报告日，避免 dividends 表未来除权日（max 2026-11-26）引入未来函数。
  - 历史可用性下调为约 2011–2025，删除『10–15 年』声明。
- **证据**：docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:400-417（§3.3 #14 全节）；docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:1032（§4.1 T4）、1041（P1-c 历史夸大）；docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:1044-1069（§4.3 689009）、1257-1264（§9 P0-9）；fields-inventory.md:26（dividends）、:59（share_capital_history）、:32（ttm_dividend_yield）、:86（cash_flow_activity）、:89（indicator_ext 仅 23 列）；app/core/storage/schema.py:2070-2073（v30 market_cap_at_report）；.planning/2026-11-16-masters-survey/context.md:52（dividend_interest_paid 2025 全空）、:28（indicator_ext 实际列）；.planning/2026-11-16-masters-survey/reviews/growth.json（#14 实测 1,738 / 2.27 / 23.9% / 600519 DY 3.755%）

## #15 林奇六类股票分类器（含周期股标记） — `buildable_after_fix`
- **理由**：七类分布 5,419 可复现，但 SQL 没落地自己写下的金融股剔除：118 只 asset_play 中 60 只（50.8%）是银行/非银；824 只三列全 NULL 的债务被 coalesce 成 0；689009 市值 0.1×会把 net_cash/mcap 放大 10×。
- **缺列/缺数据**：订单/产能/产品价格数据（困境反转的经营驱动，库内无）；投资性房地产/开发支出/资源储量（林奇『隐蔽资产』，库内无）
- **分母体检**：① 分母=indicator_ext.market_cap_at_report，SQL 过滤 >0，不会 ≤0；样本 5,419，118 只 asset_play（2.2%）方向与林奇『资产股罕见』一致。② 但分子 net_cash 对金融股系统性虚高：118 只 asset_play 中银行 4 + 非银 56 = 60（50.8%），券商客户资金存款计入 monetary_funds（§3.3 #15、§4.2 P1-a）。③ 824 只 2025 年报 short/long/bonds 三列全 NULL 被 coalesce 0（context.md 陷阱 7），净现金虚高。④ 若 689009 进入 asset_play 候选，市值 0.1× → net_cash/mcap 放大 10×，可能误判 asset_play（SQL [15-1]/[15-3] 确认）。⑤ 空值：pnp_now/pnp_base 空 0 只（review 实测），但净现金 NULL 被当 0 是主要空值问题。
- **历史可行性**：分类需 4 年 CAGR→5 个年报点，5 年 CV→5 点；2010–2025 共 16 个年报点，10 年分布（2016–2025）需 14 个年报点，数据够。2018 减值符号/2020 新收入准则不改变归母净利；分红率 2010 后才稳定；2025 现金流半空壳本指标不用。金融股与零负债 NULL 是横截面问题，不是时间断点。
- **通病标签**：T3, T4
- **ROCE 同型**：是
- **必修项**：
  - 落地 SQL 自己写的 trap ③：WHERE sm.sw_level1 NOT IN ('银行','非银金融')（实测 42 银行 + 79 非银 = 121 只），并输出剔除前后 asset_play 数。
  - 净现金改为 CASE WHEN short_term_loans IS NULL AND long_term_loans IS NULL AND bonds_payable IS NULL THEN NULL ELSE coalesce(...) END；别把 824 只缺数当零负债。
  - 补减 lease_liabilities 与 balance_sheet_ext.non_current_liab_due_1y（确认是否双计），并输出 net_cash 绝对值便于抽查。
  - 修 689009 市值 0.1×（§4.3/P0-9）或剔除；asset_play 的 0.5 阈值改为全市场分位（如 p90），报告写清阈值来源。
  - 修正报告数字：profits_volatility_3y p90=0.094（非 0.08）、周期占比 41.4%（非 40.7%）。
- **证据**：docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:418-436（§3.3 #15 全节）；docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:1038-1039（§4.2 P1-a 金融股剔除只写不做）；docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:1044-1069（§4.3 689009）、1257-1264（§9 P0-9）；fields-inventory.md:17（balance_sheet 五个净现金原子列 + lease_liabilities）、:26/:59（分红/股本）、:113（non_current_liab_due_1y）、:89（indicator_ext 仅 23 列）；.planning/2026-11-16-masters-survey/context.md:54（NULL 不能盲目 SUM 当 0）、:28（indicator_ext 实际列）；.planning/2026-11-16-masters-survey/reviews/growth.json（#15 实测七类 1906/1443/684/589/416/263/118、asset_play 金融 60、零负债 824）

## #16 研发强度与销售组织效率（费雪十五要点中可算的部分） — `buildable_now`
- **理由**：可算部分字段全部真实（rd_expenses/revenue/cost_of_revenue + indicator_ext 三个费用率列），比率与源表差值恒 0，样本 4,413/研发中位 5.08%/销售费用率 3.65% 可复现；分母是 revenue 且 5 年中位抗极值，无 689009 传导，报告唯一 ✅ 通过项。落地前只需做显式金融股剔除与两处口径加固（非阻塞）。
- **缺列/缺数据**：开发支出/资本化研发（balance_sheet 与 balance_sheet_ext 均无）；费雪定性要点的结构化数据（company_profile 只有文本）
- **分母体检**：① 分母=income_statement.revenue；毛利率 CTE 已 revenue>0，费用率用 indicator_ext 现成比率未强制 revenue>0，极小收入公司可能放大，但被 5 年中位与 rd>2% 门槛过滤，风险低。② 样本 4,413；研发强度中位 5.08%、销售费用率中位 3.65%，与 A 股制造业常识量级一致；金融股 42 银行 + 79 非银的费用率基本为空，自然落选，但 SQL 未显式剔除。③ 空值：n_rd_years>=4 过滤；研发缺失是数据没抓到（2018 前覆盖低），不是数学不成立，应写 indicator_null_reason。④ 689009 不涉及市值/每股，10× bug 对比率无影响。
- **历史可行性**：研发费用 2015 后才逐步有数（2015 86、2016 627、2017 3,932、2018 5,128），2017–2018 从管理费用剥离 → 研发强度只有 2018–2025 共 8 个年报点；5 年中位（2021–2025）可行，3 年趋势（2023–25 vs 2018–20）可行，但 10 年中位不可行。2020 新收入准则把运费移入营业成本：销售费用率 2019 4.82%→2020 3.41%、毛利率 −1.03pp，跨 2020 的销售效率/毛利率趋势失效。2018 减值符号不进入本指标；2025 现金流半空壳不用。
- **通病标签**：T3, T1, T5
- **ROCE 同型**：否
- **必修项**：
  - 非阻塞加固：显式 WHERE sm.sw_level1 NOT IN ('银行','非银金融')，或至少输出被自然排除的金融股只数，避免未来数据补齐后污染。
  - 毛利率 CTE 补 cost_of_revenue>0（不只 IS NOT NULL），避免成本为 0 的样本把毛利率顶到 100%。
  - 趋势改为 2021–2025 线性斜率/Theil-Sen；销售费用率/毛利率不要跨 2020 比较，研发趋势 2018–2020 vs 2023–2025 可保留但要标注中间缺 2021–22。
  - 按 K7 至少输出三列：5 年中位 / 3 年趋势 / 最新值；空值写 indicator_null_reason（区分『研发未披露』与『收入为 0』）。
  - 单位登记：indicator_ext.*_expense_ratio 是小数，SQL 输出乘 100 为百分数，需在 field_units.py 与前端 screening-format.ts 同步登记。
- **证据**：docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:437-455（§3.3 #16 全节，审查裁决 ✅ 通过）；docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:1039（§4.2 P1-b 2020 新收入准则断点）；fields-inventory.md:20（rd_expenses/revenue/cost_of_revenue/selling_expenses/administrative_expenses）、:89（indicator_ext 仅 23 列）、:113（balance_sheet_ext 无开发支出）；app/core/storage/schema.py:1990-1992（v26 费用率三列）、app/core/indicators/extended.py:182-184（费用率生成）；.planning/2026-11-16-masters-survey/context.md:40（2016 研发 627 只）、:28（indicator_ext 实际列）；.planning/2026-11-16-masters-survey/reviews/growth.json（#16 实测 4,413 / 5.08% / 3.65%，全部 traps 成立）

## #17 CANSLIM 盈利引擎（C 季度加速 + A 年度复利） — `buildable_after_fix`
- **理由**：C/A 样本 4,142/1,094/182/157 可复现，YTD 差分与 300750 例子成立；但 C 档单季同比无上限（139/1,094 只 ≥500%）、131 只缺季差分污染、银行未剔除，M 项写成 price_daily_qfq 指数序列是语义空壳（该表 0 行指数），必须改路径。
- **缺列/缺数据**：price_daily_qfq 指数序列（M 项；0 行非 6 位股票代码）；基金持仓/机构持股表（I 项；information_schema 零命中）
- **语义错配**：price_daily_qfq 指数序列（表在列在但无指数行）；index_valuation.pe_ttm 被当作指数价格（只有估值，无收盘价）
- **分母体检**：① 分母1=上年同季单季归母净利 b.sq_pnp；SQL 仅 b.sq_pnp>0 过滤，不挡近零：2026Q3 全市场单季同比 p95=332.7%、max=50,526.4%；1,094 只 c_pass 中 139 只（12.7%）≥500%，是基期利润趋零的假加速（T4/T2）。② 分母2=3 年 EPS 基期，power 前有 eps_base>0 AND eps_now>0；ROE 分母 (本期+上期)/2 归母权益且 eq_prev>0，A 档干净。③ 样本 4,142 有同比 / 1,094 C / 182 加速 / 157 A，与报告一致。④ 空值：131/5,665（2.3%）缺 Q1/Q2 之一，lag() 把跨两季累计差当单季；银行拨备调节使 C 失真，需剔除。689009 basic_eps 10.05×，若 2022/2025 同倍则 CAGR 不变，必须 SQL 确认。
- **历史可行性**：利润表 2000 起每年 4 期，单季同比需 t−4 期→2001 起；basic_eps 2006 前几乎全空（2005 170 只）→A 档 2006–2025 共 20 年。K6：3 年 EPS CAGR 需 4 个年报点（2022–2025）；季度同比需 5 个季度（t 与 t−4）；连续三季加速需 6 个季度（Q1–Q3 本期+上年）。2018 减值/2020 收入准则不改变归母净利；2025 现金流半空壳不用；2025 中报 deducted_net_profit 覆盖降至 4,514/5,627，故 C 用 parent_net_profit 正确。
- **通病标签**：T4, T2, T3
- **ROCE 同型**：是
- **必修项**：
  - 单季同比加双边约束 25%≤g3≤300%，并要求单季归母净利绝对值 >约 2,000 万；输出 g 分位与 ≥500% 计数（目标清掉 139 只/12.7%）。
  - 补 Q1–Q3 三个累计期齐全的完整性校验（三季齐全才差分），修 131 只（2.3%）跨两季差分污染。
  - C 档剔除银行（sw_level1='银行'），保险/券商单列；报告已说明银行拨备调节使 C 无意义。
  - M 项 db_field 改为 index_valuation.extra->>'index_close'（43 个申万指数 + 000300，2006/2010→2026），删除 price_daily_qfq 指数序列；I 项老实标 missing。
  - 用 share_capital_history 复核 689009 2022/2025 basic_eps 是否同倍；若不同倍，A 档剔除 689009。
- **证据**：docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:456-477（§3.3 #17 全节）；docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:1032（§4.1 T4 同型）、1039（P1-a 金融股只写不做）；docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:1257-1264（§9 P0-9 689009）、1301（C9 basic_eps 10.05×）；fields-inventory.md:20（parent_net_profit/deducted_net_profit/basic_eps）、:29（xdxr）、:17（total_equity_parent）、:74（index_valuation.extra）；app/core/storage/schema.py:773-802（indicator_ext DDL）与 1950-2090（v26/v30 迁移）；.planning/2026-11-16-masters-survey/context.md:28（indicator_ext 实际列）、:38（2025 中报扣非覆盖）；.planning/2026-11-16-masters-survey/reviews/growth.json（#17 实测 4,142/1,094/182/157、p95 332.7%、139 只≥500%、131 只缺季）

## #18 相对强度 RS 评级（含 52 周高点距离） — `buildable_after_fix`
- **理由**：价格字段真实、5,403 只与中位 −7.37% 可复现；但 52 周高点实际取约 325 个交易日（1,465 只/27.1% 被多算、最大 −26.5pp）、ST/停牌未剔除、指数基准写成不存在的 price_daily_qfq 指数序列，必须改窗口与基准。
- **缺列/缺数据**：price_daily_qfq 指数序列（行业/指数基准；0 行非 6 位股票代码）
- **语义错配**：price_daily_qfq 指数序列（表在列在但无指数行）；52 周高点实现（文档写近 250 交易日，SQL 取约 325 交易日 max(close)）
- **分母体检**：① 分母=p3m/p6m/p9m/p12m 四个历史收盘价，SQL 全部 >0 过滤，价格比率不会 ≤0；样本 5,403，RS_raw 中位 −7.37%、min −91.25、max 317.60，与 2026-09-30 熊市量级一致。② 分母不是市值/每股，689009 的 market_cap 10× bug 不直接传导；但 CDR 复权因子需抽查 qfq/raw 跨 2024 是否跳 10×。③ 52 周高点 SQL 取 2025-06-01→2026-09-30 全窗口 max(close)（约 325 交易日），不是近 250 日：1,465/5,403（27.1%）在 rn>250 有更高收盘价，偏离度中位多算 −4.7pp、最大 −26.5pp。④ 分位分母含 197 只 ST/退市整理，SQL 未按 note 剔除；ntile(99) 早期样本不足会退化。
- **历史可行性**：价格 1990-12-19 起，250 交易日窗口+全市场分位→1996 后有意义；2005 年 1,200+、2015 年 2,500+、2025 年 5,450 只，10–15 年序列可行。K6：52 周窗口需 250 个交易日观测，不是上市年限；年末非交易日实测 2016/2017/2018/2022/2023 共 5/10 个 12-31 无行情，必须 as-of join 取 <= 该日最后一条；历史 RS 必须用 price_daily_qfq，不能用 indicator_ext.report_date_close（不复权，§9.3 P2）。2018 减值/2020 收入准则/2025 现金流断点均不进入价格序列。
- **通病标签**：T3, T5
- **ROCE 同型**：否
- **必修项**：
  - hi_52w 改为 max(CASE WHEN rn<=250 THEN close END) 或 high 的近 250 日 max；输出受影响只数与偏差（实测 1,465 只/27.1%、最大 −26.5pp）。
  - 落地 note 自己的要求：WHERE NOT is_st AND NOT is_suspended 后再 ntile(99)，并输出剔除只数（197 只 ST / 12 只停牌）。
  - 指数基准改 index_valuation.extra->>'index_close'（43 个申万指数、2006/2010→2026），或老实标 missing；删除 price_daily_qfq 指数序列。
  - 年末截面用 as-of join（<= report_date 最后交易日），不要 trade_date=report_date；历史 RS 用 qfq，不用 report_date_close。
  - 次新股 n_obs>=125 单列分档；ntile 横截面 <99 只的早期年份不输出。
  - 抽查 689009 qfq/raw 比值跨 2024 是否 10× 跳变（CDR 单位重标定），并修正 trap ④ 的『20.02』为实测 27.35。
- **证据**：docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:478-496（§3.3 #18 全节）；docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:1036（§4.1 T5 样本下限）、1040（P1-d 声称与交付不符）；docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:1113-1125（§6.3 第三档）、1283（§9.4 P2 report_date_close 不复权）；fields-inventory.md:14（price_daily_qfq close/high）、:11（price_daily_raw volume/turnover_rate）、:8（stock_meta is_st/is_suspended）、:74（index_valuation.extra）；.planning/2026-11-16-masters-survey/context.md:31-32（价格/股票元数据）、:45（复权价格完整）；.planning/2026-11-16-masters-survey/reviews/growth.json（#18 实测 5,403 / 中位 −7.37% / 1,465 只受影响 / 197 ST）

## #19 神奇公式（资本回报率 × 盈利收益率 双排名合成） — `buildable_after_fix`
- **理由**：字段全真（indicator_ext.ebit / market_cap_at_report 均存在：schema.py:214 与 schema.py:2069-2075 的 v30 ALTER），会计恒等式 TA−CL ≡ 净营运资本+全部长期资产 成立；但必须先把 CE 从 exact 降级为 approximate（K1/T3）、把 |ROC|≤1 / |EY|≤1 两道置空护栏改成 status（K3/R4-2）、并等 689009 的 10 倍市值 bug（P0-9）修完，否则 EY 腿排名不可发布。
- **语义错配**：balance_sheet.total_assets − total_current_liabilities 被标 exact 冒充 Greenblatt『净营运资本 + 净固定资产』（实为净营运资本 + 全部长期资产，K1/T3）；stock_meta.sw_level1 剔除集合含『公用事业』，与项目 ROCE 裁决（roce.py:170 只剔银行/非银金融）不一致，导致与 roce_5y_median 不同源；『roce_history.ce 就是 Magic Formula 分母，完全同源』不成立：平均资本 vs 期末资本、EBIT 表达式不同、行业剔除口径不同
- **分母体检**：R4-1 分母到底是什么：ROC 腿 = balance_sheet.total_assets − total_current_liabilities（期末值）；EY 腿 = indicator_ext.market_cap_at_report + 短期借款 + 长期借款 + 应付债券 + 少数股东权益 − 货币资金。R4-2 会不会 ≤0：会。CE 可 ≤0，SQL 用 WHERE ce>0 **直接删行**；EV 可 ≤0（货币资金 ≥ 市值+有息负债+少数股东权益），SQL 用 WHERE ev>0 丢掉审查员实测的 6 只净现金公司——等于系统性剔除资产负债表最健康的一批，且违反 AGENTS.md R4-2『只标 status，不截断、不置空』与 roce.py:56 的既有裁决。R4-3 量级对得上标杆吗：护栏后 ROC 中位 6.6% / p99 30.6%、EY 中位 3.4% / p99 18.5%，与 Greenblatt 组合 20%–50% ROC 同处一档，未见 ±10⁴% 暴走；但单点仍有中钢国际 EY 1063.4%、石药景峰 ROC 581.0%。R4-4 空值语义：CE≤0 数学不成立、EV≤0 数学不成立、市值 NULL 是没抓到 —— 三者被同一句 WHERE 合并成『不出现』，没有 indicator_null_reason 区分。
- **历史可行性**：ROC 腿可做 10 年（roce_history 年报 1989–2025 共 37 期，2011 年后每年 2,669–5,819 只），但按 K6『N 年需 N+1 期』且平均资本法还要多一期 → 最早可用年份应记 1991 而非 1990。EY 腿**不可做 10 年**：market_cap_at_report 在 FY2010–FY2019 覆盖仅 52.6%–65.6%（审查员实测），10 年截面中位数带小盘剔除偏差（FY2015 缺失组中位总资产 5.04 亿 vs 有值组 36.62 亿）。口径断点：2020 新收入准则（运费入营业成本）、2018 asset_impairment_loss 符号翻转（经留存收益影响 CE>0 判定）、2019 新金融工具准则（trading_financial_assets 重分类影响 EV 现金扣减项）、FY2025 cash_flow_activity 半空壳（不进本指标但影响后续吊尾年）。
- **通病标签**：T3, T4, K1, K3, K6, K8
- **ROCE 同型**：是
- **必修项**：
  - ①把 CE 的 match 由 exact 改为 approximate，formula 正文改为『已动用资本 = 总资产 − 流动负债（本项目 ROCE 口径 C，≠ Greenblatt 原文净营运资本+净固定资产）』（reports/131 §4.1 T3）
  - ②|ROC|≤1.0 / |EY|≤1.0 的 CASE WHEN 置空改为 status 标注（INVALID_DENOMINATOR / OUTLIER_REVIEW），原值保留；依据 AGENTS.md R4-2 与 roce.py:56 既有裁决（K3）
  - ③WHERE ev>0 不要删净现金公司（实测 6 只），改成负 EV 单列标记、展示但排除出 EY 排名
  - ④统一行业剔除口径：要么本指标也剔公用事业并写进 formula，要么只剔银行/非银以与 roce_history 同源，二者选一并写明
  - ⑤P0-9 修完（重跑 vd data compute-extended-indicators）后再发布任何依赖 market_cap_at_report 的排名；SQL 外层加市值交叉体检 abs(market_cap_at_report/(close*时点股本)-1) < 0.05（689009 比值恰为 0.1）
  - ⑥加营收/总资产同比兜底剔除 002379 类追溯重述股
  - ⑦删掉/重测『FY2024 营收或总资产同比 >2 倍的有 461 只』（实测 rev>2× 83、TA>2× 32、任一 99 只）
  - ⑧EY 闸门 1.0 过宽：600710 苏美达 EY 56.7% 仍排 r_ey 第 1；应改为 status 标记交使用者裁决而非收窄阈值
  - ⑨落地必须登记 derived_snapshot_domains() + 重建触发（K5），否则快照重发会清空新列
- **证据**：reports/131 §3.4 #19（字段映射 exact 5 / approximate 3 / missing 0；审查裁决 🟡 需修正、ROCE 同型=低）；reports/131 §4.1 T3（ROC 分母标 exact 实为 TA−CL，『与 ROCE 第一版一模一样』）；reports/131 §4.3 与 §9 P0-9（689009 市值小 10 倍，24 期、14 个派生列，全库仅此 1 只）；reports/131 §10 K1/K3/K6/K8；§5 G5；reviews/quant_value.json#indicators[0]（PPE/CE 0.258、3,208 只<30%、EV 1.136 亿、EY 1063.4%、ROC 581.0%、461→99 只）；app/core/indicators/roce.py:56（不做截断）:67-68（市值≥100亿）:118-122（平均资本）:145-146（CL 非空）:148-155（ebit 口径）:170（只剔银行/非银）；app/core/indicators/extended.py:154-158（EBIT 回退式）:277（market_cap_at_report 定义）:296（non_current_liab_due_1y 已在用）；app/core/storage/schema.py:2069-2075（v30 新增五个市值/估值列）；fields-inventory.md 的 indicator_ext 段只列 23 列基础 DDL，**不含** market_cap_at_report —— 该清单漏列 ALTER 增列，按清单核对会误判 missing_column

## #20 聂夫总回报率（低 PE + 高股息 + 盈利增长） — `buildable_after_fix`
- **理由**：六个字段全部真实、股息率名义/名义配对与 ex_date 划窗口径都正确（本流派最干净的一条），但三处必须先修：①TTR 榜首 689009 是 P0-9 的 10 倍市值脏数据（pe 3.1 / ttr 8.147 第 1 名）；②g 用 LEAST(GREATEST(...,0),25) 硬截断，是 R4-2 明令禁止的护栏（K3/T4），且实测封顶后周期股仍霸榜（中远海控第 2）；③『样本 3,251 只』与 SQL 实出 2,695 差 20%。
- **语义错配**：（无列名层面错配）唯一语义问题是 g 的计算：用已实现 CAGR 冒充预期增速，报告已如实标注 approximate
- **分母体检**：R4-1 分母是什么：TTR 的分母 = PE = indicator_ext.market_cap_at_report ÷ income_statement.parent_net_profit；股息率分母 = indicator_ext.report_date_close（名义收盘价）。R4-2 会不会 ≤0：PE 分母 parent_net_profit 在 SQL 里已被 c.np_t > 0 强过滤（正确）；dy 分母 px 用 NULLIF 保护；但 g 的分母 np_0（2020 归母净利）**没有挡负**——np_0 ≤ 0 时 CASE WHEN np_0>0 ... END 直接返回 NULL，再被 LEAST/GREATEST 变成 0，等于『基期为负的公司在增长维度被静默记 0 分』，这是 R4-2 禁止的置空变形。R4-3 量级对得上标杆吗：dy 中位应落在 1%–2%（A 股 2024 中位约 1.2%）、pe 中位 15–25，但实测 TTR 榜首达 8.147（689009 脏数据）、中远海控 5.922、云天化 3.847，而聂夫合格线只有 2 → 排序被周期股与脏数据双杀。R4-4 空值语义：np_0≤0（数学不成立/基期亏损）、无 2020 年报（没抓到）、窗口内无分红（真实 0）三种情况被同一句 COALESCE(d.dps,0) 与 CASE 合并，没有 indicator_null_reason 区分。
- **历史可行性**：可做，但比报告说的窄：PE 与股息率需要 income_statement 年报 + indicator_ext.market_cap_at_report，后者非金融覆盖率 FY2010 65.5% / FY2013 52.6% / FY2015 53.9% / FY2020 65.6% / FY2022 81.7%（审查员实测），故『10 年历史中位数完全可行』只在有市值的子样本上成立，且带小盘剔除偏差（FY2015 缺失组中位总资产 5.04 亿 vs 有值组 36.62 亿）。按 K6，4 年 CAGR 需 5 个年报期（2020 与 2024 两个端点足够，但若做 5 年中位则需 6 期）——报告写『5 年中位数需要 4 年增长历史，实际起点约 1996 年』低估了一期。口径断点：2020 新收入准则（不影响归母净利，但影响 PE 的可比性通过营收增速间接传导）、2018 asset_impairment_loss 符号翻转（大额减值会打穿 np_0，令 g 对一批股票不可算）、2019 新金融工具准则（投资收益/公允价值变动重分类，影响非经常性净利）、FY2025 cash_flow_activity 半空壳（与分红表无关，分红走 dividends 不受影响）。
- **通病标签**：T4, K3, K6, P1-c
- **ROCE 同型**：是
- **必修项**：
  - ①把『样本 3,251 只』改成实测 2,695，并列出漏斗（净利>0 4,345 → 有市值 3,874 → 有分红 3,427 → 两期齐全且有分红 3,355 → 输出 2,695）
  - ②外层加市值交叉体检 abs(market_cap_at_report/(close*时点股本)-1) < 0.05（或把 689009 列入黑名单），否则 TTR 永远由脏数据霸榜；根因修法见 reports/131 §4.3：重跑 vd data compute-extended-indicators
  - ③去掉 LEAST(GREATEST(g,0),25) 硬截断，改为 g_raw 原值 + status 标注（G_BASE_NEGATIVE / G_OUTLIER），依据 AGENTS.md R4-2『不截断、不设上限、不置空』；实测封顶后周期股仍占前 10，证明护栏无效（T4）
  - ④把 formula 里的配套纪律落地或明确标注『本版只展示不筛选』：PE ≤ 0.7×行业中位数、股息率 ≥ 市场平均、连续分红 ≥ 5 年三条，SQL 目前只算了 pe_vs_ind 并不过滤
  - ⑤行业分区必须 AND m.sw_level1 IS NOT NULL，否则 750 只 NULL 行业股会组成一个伪行业参与中位数
  - ⑥一次性特别股息护栏：dy > 3×本行业中位数的样本 winsorize 或打标（美盈森 FY2024 15.92% 目前是同类指标的第 1 名）
  - ⑦重测云天化 CAGR（103.8% → 110.46%）与 TTR（14.13 → 15.00），报告数字需更正
  - ⑧落地须登记 derived_snapshot_domains() + 重建触发（K5）
- **证据**：reports/131 §3.4 #20（字段映射 exact 5 / approximate 1 / missing 0；审查裁决 🟡 需修正、ROCE 同型=低）；reports/131 §4.3 与 §9 P0-9（689009 市值小 10 倍，TTR 第 1 名、Dreman 第 2 名）；reports/131 §4.2 P1-c（样本量夸大：聂夫 3,251 实为 2,695）、P1-a（金融剔除只写在陷阱里）；reports/131 §10 K3（护栏掩盖）、K6（上市年限冒充数据年数）；reviews/quant_value.json#indicators[1]（2,695 实出、云天化 110.46%/TTR 15.00→3.847、中远海控 5.922、689009 pe 3.1/ttr 8.147、FY2024 非金融市值 5,075/5,644=89.9%）；schools/quant_value.json#indicator[1].formula_sql（LEAST(GREATEST(...)) 截断、median(pe) OVER (PARTITION BY sw_level1)）；app/core/indicators/extended.py:277（market_cap_at_report = 报告日收盘价 × 时点股本）；app/core/storage/schema.py:2069-2075（v30 新增 report_date_close / market_cap_at_report）；indicator_snapshot.consecutive_div_years（fields-inventory.md indicator_snapshot 段，70 列之一）

## #21 德雷曼逆向价值复合（低 PE + 低 PB + 低 P/CF，叠加行业折价） — `buildable_after_fix`
- **理由**：字段全部真实（PE/PB/PCF 三个分母分别落在 parent_net_profit / total_equity_parent / cf_from_operating，均逐列核对通过），但这是本流派唯一『产出被已知垃圾数据霸榜且零护栏』的指标：FY2024 第 1 名 002379 宏桥控股 dreman_score = 1.0000（PE 0.59 / PB 0.239），源头是同一控制下企业合并追溯重述配上重述前市值；同时 percent_rank 在 3,117 只上算、在 1,629 只上输出，分位与名单不同源。修法明确（加同比兜底 + 前移过滤 + 换分位门槛），故判 buildable_after_fix 而非 blocked。
- **语义错配**：『或取 1/indicator_ext.book_to_market』被描述为与 mcap/total_equity_parent 等价——不等价（COALESCE 回退 + BOOK_TO_MARKET_MAX=10 护栏，extended.py:286-292 与 :114）；mcap >= 2e9 被当作『市值处于全市场前 1,500–2,000 名』的等价物——绝对门槛跨年漂移，不是分位；『sw_level1 为 NULL 的 750 只不进行业比较』与 PARTITION BY sw_level1 的 SQL 语义不符（NULL 会被归成同一分区）
- **分母体检**：R4-1 分母是什么：PE 分母 = income_statement.parent_net_profit；PB 分母 = balance_sheet.total_equity_parent；P/CF 分母 = cash_flow.cf_from_operating；三者共用分子 indicator_ext.market_cap_at_report。R4-2 会不会 ≤0：三个分母都会被 SQL 的 >0 条件挡住（正解），但**分母逼近 0 的情况没有挡**——002379 这类重述股分母被放大 36–56 倍、分子滞留旧值，等于分母失真而非趋零，任何 >0 过滤都拦不住；反过来 PB 分母在资不抵债时 ≤0 已被剔除。R4-3 量级对得上标杆吗：未加护栏时 dreman_score 最高 1.0000（三项满分），来源不是估值真便宜而是重述；中远海控 FY2024 EY 43.57%（PE≈2.3）已是周期顶信号。R4-4 空值语义：负 PE（亏损）/负 PB（资不抵债）/负 CFO（经营失血）三类被同一个 >0 过滤合并成『不出现』，没有 indicator_null_reason 区分『数学不成立』与『数据没抓到』；且 indicator_ext.book_to_market 的 NULL 还额外混入了 BOOK_TO_MARKET_MAX=10 的护栏置空。
- **历史可行性**：可做 5 年，10 年勉强：PE/PB/PCF 全部依赖 indicator_ext.market_cap_at_report + 三张主表，非金融年报覆盖率 FY2010 65.5% / FY2013 52.6% / FY2015 53.9% / FY2020 65.6% / FY2022 81.7%（审查员实测），故『10 年历史中位数可行（2010 起）』只在有市值子样本上成立且带小盘偏差（FY2015 缺失组中位总资产 5.04 亿 vs 有值组 36.62 亿，缺失股 1,701 只有价格历史、1,698 只至今仍在市）。按 K6：PE/PB/PCF 是单期截面比，做 N 年中位数只需 N 期，但 20 亿市值门槛若改成『按年分位』则每年门槛不同，跨年中位数必须在每年内部先标准化。口径断点：2019 新金融工具准则（投资收益/公允价值变动重分类 → 影响归母净利 → PE）、2018 asset_impairment_loss 符号翻转（大额减值打穿 PCF 与权益）、2020 新收入准则（合同负债替换预收款项，不影响本指标三项分母）、FY2025 cash_flow_activity 半空壳（**cf_from_operating 走 cash_flow 主表，不受影响**——须写明，避免使用者误以为 P/CF 也被 P0-3 污染）。
- **通病标签**：T2, T4, P1-a, P1-b, P1-c, K6, K8
- **ROCE 同型**：是
- **必修项**：
  - ①加与 Magic Formula 同源的重述兜底：AND NOT (营收同比 > 2 OR 总资产同比 > 2)，或对 PE<1 / PB<0.3 的样本强制打标复核；否则榜首永远是 002379 类
  - ②把『样本 3,274 只』改为实测 1,629，并注明 percent_rank 分母 3,117 与输出 1,629 的差异；更彻底的做法是把 pe<40 AND pb<4 过滤前移到 base，使分位与名单同源
  - ③把 mcap >= 2e9 换成按年截面的市值分位（如前 30%），消除绝对金额门槛的跨年漂移
  - ④加 AND m.sw_level1 IS NOT NULL，禁止 NULL 行业组成伪分区参与中位数
  - ⑤PB 口径二选一并写清：用 mcap/total_equity_parent（归母，与 PE 同源）就不要同时给 1/book_to_market 这个含 COALESCE 回退与 BOOK_TO_MARKET_MAX 护栏的替代式
  - ⑥陷阱④建议的『3–5 年平均经营现金流』应落地为字段或明确标注本版未实现
  - ⑦全局前置：market_cap_at_report 的 P0-9（689009 小 10 倍）与逐年覆盖率必须在指标卡上披露；本指标第 2 名即为 689009（pb 0.553 / score 0.9973）
  - ⑧负 PE / 负 PB / 负 CFO 三种剔除要写进 indicator_null_reason，区分『数学不成立』与『数据没抓到』（R4-4）
- **证据**：reports/131 §2 总表（#21 审查 ❌ 不通过、ROCE 同型=高）；reports/131 §3.4 #21（字段映射 exact 5 / approximate 1；审查发现『零闸门、零重述过滤、零营收同比校验』）；reports/131 §4.1 T2/T4、§4.2 P1-b/P1-c；reports/131 §4.3 与 §9 P0-9（689009 在 Dreman 排第 2，pb 0.553 / score 0.9973）；reviews/quant_value.json#indicators[2]（3,274 vs 3,792/3,117/1,629；002379 PE 0.59/PB 0.239/score 1.0000；中远海控 EY 43.57%；370 只 NULL 行业）；schools/quant_value.json#indicator[2].formula_sql（percent_rank 与输出不同源、mcap>=2e9、median(pe) OVER (PARTITION BY sw_level1)）；app/core/indicators/extended.py:114（BOOK_TO_MARKET_MAX=10）:277（market_cap_at_report 定义）:286-292（book_to_market 的 COALESCE 回退）；app/core/storage/schema.py:2069-2075（v30 新增 book_to_market 等五列）

## #22 趋势价值（价值前 10% ∩ 6 个月动量最强） — `buildable_after_fix`
- **理由**：十个字段全部真实（含 financial_report_dates.announce_date 与 report_metadata.declare_date 两张边缘表），防御前视偏差的思路是全流派最好的；但 formula 承诺的『价值前 10% 内再取动量前 10%』没实现——SQL 是 334 只里 ORDER BY mom6 DESC LIMIT 100（≈十分位的 30%），且榜首美盈森的 15.92% 股息率是一次性特别派息直接推上去的，无任何护栏。修法只需两行（LIMIT 33 / 百分位切割 + dy winsorize），故 buildable_after_fix。
- **语义错配**：formula 的『价值前 10% ∩ 动量前 10%』在 formula_sql 里退化成『价值前 10% 后取动量前 100 名』（LIMIT 100 vs 应约 33）；股息率把一次性特别派息（美盈森 15.92%）当作永久股息率计入截面分位，未做任何 winsorize/行业相对截断
- **分母体检**：R4-1 分母是什么：PE=market_cap_at_report/parent_net_profit、P/S=market_cap_at_report/total_operating_revenue、P/CF=market_cap_at_report/cf_from_operating、股息率=Σdividend_per_share/report_date_close、动量=p1/p0−1（p0 来自 price_daily_qfq.close，用 NULLIF 保护）。R4-2 会不会 ≤0：五个分母都被 >0 过滤（np/rev/ocf/px）或 NULLIF 保护（p0），是六个指标里分母纪律最完整的一条；但 p0 若为 0 或 NULL 会让该股整条记录消失而非标 status，缺 indicator_null_reason。R4-3 量级对得上标杆吗：dy 应落在 1%–2% 中位，实测榜首先 15.92% —— 与『A 股股息率天花板约 8%』的常识相差一个数量级，属 K8 该停下来查数据的信号（真因是一次性派息 + 名义口径，不是公式错）。R4-4 空值语义：无分红（真实 0，用 COALESCE 0 正确）、无价格（没抓到）、动量窗口无数据（停牌/次新股）三种被混在一起，无 indicator_null_reason。
- **历史可行性**：可做，但截面前期很薄：价值腿受 market_cap_at_report 覆盖率约束（非金融 FY2010 65.5% / FY2013 52.6% / FY2015 53.9% / FY2020 65.6%），动量腿受价格截面约束（审查员实测 FY2015 的 12-31 价格截面仅 2,576 只 / 年报 4,778 只 ≈ 54%）。故『动量最早约 1991 年可用』过于乐观，『10 年历史中位数可行（2010 起）』只在有价格+有市值的交集上成立，且该交集系统性偏大盘。按 K6：动量需 t 与 t−6 个交易日的两个点，属同期内数据不额外扣年；但价值腿若做 N 年中位数需 N 期年报，且 2010–2019 每年只有半数样本。口径断点：2020 新收入准则（营收口径变化直接进 P/S 分母）、2018 asset_impairment_loss 符号翻转（影响净利 → PE）、2019 新金融工具准则（投资收益重分类）、FY2025 cash_flow_activity 半空壳（不影响 cf_from_operating）。
- **通病标签**：T1, T5, P1-d, K6, K8
- **ROCE 同型**：否
- **必修项**：
  - ①补上公式承诺的第二层 10% 切割：LIMIT 100 → LIMIT 33，或先 ntile(10) 再取第 1 档；否则指标名与实现不符
  - ②一次性股息护栏：dy > 3×本行业中位数的样本 winsorize 或打标（美盈森 15.92% 目前就是榜首）
  - ③把『FY2010：2,452/2,452』改成实测 2,895/2,895
  - ④把『动量最早约 1991 年可用』改成实测覆盖率（FY2015 价格截面 2,576 只 / 年报 4,778 只 ≈ 54%），并说明历史回测的幸存/流动性偏差
  - ⑤回测必须用 announce_date + 1 个交易日建仓（覆盖率 100%），并在代码层禁用 declare_date（覆盖率 1.8%）
  - ⑥1 - percent_rank() 在 ties 上不稳定，建议统一方向并显式说明；动量只用两个时点价格、未处理停牌/涨跌停，需在文档标注
  - ⑦market_cap_at_report 的 P0-9 修复前置（本指标三项估值分母全部依赖它）
  - ⑧落地须登记 derived_snapshot_domains() + 重建触发（K5）
- **证据**：reports/131 §3.4 #22（字段映射 exact 6 / approximate 0 / missing 0；审查裁决 🟡 需修正、ROCE 同型=低）；reports/131 §4.2 P1-d（声称与交付不符：SQL 硬编码单年、宣称可做历史）；reports/131 §10 K6/K7/K8；reviews/quant_value.json#indicators[3]（3,385 只、334 只十分位 vs LIMIT 100、美盈森 15.92% 第 1、FY2010 实为 2,895、declare_date 98/5,482、FY2015 价格 2,576/4,778≈54%）；schools/quant_value.json#indicator[3].formula_sql（quantile_cont(value_score,0.90) + ORDER BY mom6 DESC LIMIT 100）；app/core/indicators/extended.py:277（market_cap_at_report 定义）；fields-inventory.md 的 financial_report_dates 段（announce_date）与 report_metadata 段（declare_date）

## #23 Fama-French 五因子单股暴露输入（价值 / 规模 / 盈利 / 投资） — `buildable_after_fix`
- **理由**：match 分级是本流派最克制的一份（RMW 因 interest_expense 不可用而主动降级 approximate、并单列 INT 为 missing，实测 FY2016 覆盖 643/5,085 = 12.65% 完全支持该判定），四个因子的列全部真实、分母全部有 >0 保护；但三类必须先修：(a) 报告里 RMW 三项覆盖率整段是 FY2025 的数被写成 FY2024；(b) 报告自己在陷阱⑦建议的 CMA/HML winsorize 在 SQL 里一行没实现（002379 CMA=35.226、HML=4.188）；(c) 只有暴露没有 2×3 分组收益，不能当信号用。属『字段够、口径要修』，故 buildable_after_fix。
- **语义错配**：（列名层面无错配）RMW 因 interest_expense 语义不稳定 + FY2016 覆盖 12.65% 而退化为息前版本，报告已诚实标注；HML 少加递延所得税项（FF 原式要求），影响小但严格应标 approximate 而非 exact
- **分母体检**：R4-1 分母是什么：HML 分母 = indicator_ext.market_cap_at_report；SMB = ln(market_cap_at_report/1e8)（**单位是亿元，必须写进字段说明**，否则换单位整体平移）；RMW 分母 = balance_sheet.total_equity_parent；CMA 分母 = 上年报 balance_sheet.total_assets。R4-2 会不会 ≤0：四个分母都写了 >0（be>0、mcap>0、ta>0、ta_prev>0），无一落入 ROCE 首版『分母为负』事故，是本流派分母纪律最好的一条；但 CMA 分母逼近 0（壳公司/资产清零）时增速会发散，SQL 只挡了 >0 没挡『接近 0』——002379 的 35.23 就是分子被重述放大而非分母趋零，属反方向失真。R4-3 量级对得上标杆吗：CMA p99 = 0.754 / p01 = −0.296 / max 35.226，HML max 4.188 —— 极端值与 FF 文献量级相差一个数量级以上，正是 K8 该停下来的信号。R4-4 空值语义：缺上年报（NO_PRIOR_PERIOD）、COGS 缺失（5,190−? 只）、sell/adm 缺失（54 只）三类都被静默过滤，无 indicator_null_reason。
- **历史可行性**：可做 10 年，但要扣两期：CMA 需 t 与 t−1 两个年报期（K6：N 年序列需 N+1 期），且 2000 年前 RMW 三费覆盖率明显下降（cost_of_revenue / selling_expenses / administrative_expenses 2007 年后 95%+、1998–2006 约 90%）。market_cap_at_report 的非金融年报覆盖率 FY2010 65.5% / FY2013 52.6% / FY2015 53.9% / FY2020 65.6% / FY2022 81.7%（审查员实测），因此 HML 与 SMB 的『10 年历史中位数』只在有市值子样本上成立，且缺失系统性偏小盘（FY2015 缺失组中位总资产 5.04 亿 vs 有值组 36.62 亿）——对 SMB（规模因子）这个偏差是致命的。口径断点：2017 研发费用从管理费用单列（直接改变 administrative_expenses → 影响 RMW 分子，2017 前后不可比）、2020 新收入准则（运费入营业成本 → 影响 cost_of_revenue 与 selling_expenses，RMW 台阶）、2019 新金融工具准则（投资收益重分类，不进 RMW 但进净利）、FY2025 cash_flow_activity 半空壳（本指标不用现金流，不受影响）。
- **通病标签**：T4, P1-b, P1-c, K6, K8
- **ROCE 同型**：否
- **必修项**：
  - ①把 RMW 覆盖率三个数换成 FY2024 实测值 5,809 / 5,734 / 5,800（分母 5,904），把现在的 5,530 / 5,456 / 5,521（FY2025，分母 5,625）单独标注年份
  - ②按报告自己在陷阱⑦里的建议真的加 LEAST(GREATEST(cma,-2),2) 或 1%/99% winsorize（代价仅 11 只），HML 同样处理并把 002379 类重述样本单列
  - ③size_ln 明确写成『Ln(总市值/1e8)（单位：亿元）』或改回 ln(元)，避免与 FF 文献口径混淆
  - ④若要真做 FF 归因，必须补『2×3 分组构建六个组合算多空收益』的流水线；当前只给暴露不给收益，不能当买卖信号（报告已如实说明，保留该提示）
  - ⑤缺上年报的样本标 NO_PRIOR_PERIOD 而不是静默丢（R4-4）
  - ⑥market_cap_at_report 的 P0-9 与逐年覆盖率必须在指标卡披露（SMB 直接是 ln 市值，对 10 倍错误最敏感）
  - ⑦HML 加递延所得税项或把 match 由 exact 降为 approximate
  - ⑧落地须登记 derived_snapshot_domains() + 重建触发（K5）
- **证据**：reports/131 §3.4 #23（字段映射 exact 5 / approximate 1 / missing 1；审查裁决 🟡 需修正、ROCE 同型=低）；reports/131 §4.2 P1-b（跨年口径断点：2017 研发费用单列、2020 新收入准则）、P1-c（样本量夸大）；reports/131 §5 G5（没有 gross PPE）、§10 K6/K8；reviews/quant_value.json#indicators[4]（5,190/5,644=91.96%；FY2024 实为 5,809/5,734/5,800；interest_expense FY2016 643/5,085=12.65%；CMA 35.226、仅 11 只 >2、p99 0.754；hml_bm 4.188；rmw 5,136/5,190）；schools/quant_value.json#indicator[4].formula_sql（无 winsorize）+ data_mapping[3]（INT 标 missing）；app/core/indicators/extended.py:277（market_cap_at_report = 收盘价 × 时点股本）；context.md 陷阱 3（interest_expense 语义不稳定 + 2016 覆盖 12.6%）

## #24 质量减垃圾（QMJ）综合 z 分 — `buildable_after_fix`
- **理由**：六项输入的列全部真实（accruals 定义与 extended.py:345-348 逐字对得上，审查员实测 349,369/349,369 全等），样本量 5,193 也精确；但这是六个指标里 ROCE 式风险最高的一条：LEAST(0.6·MVE/TL, 3.0) 这道护栏实测 2,108/5,193 = 40.6% 顶格，等于把『安全』这一维结构性压平成 0/1，而报告只写『截断到 3 就没事了』；同时 Altman Z 里含市值项，与报告『QMJ 是纯质量因子、与估值正交』的论断直接矛盾。修法是换 winsorize / 移出市值项（明确、不动数据），故 buildable_after_fix。
- **缺列/缺数据**：beta（安全组理论必需；库内无字段，需 price_daily_qfq 日收益回归自建）
- **语义错配**：indicator_ext.accruals 的分子用 net_profit（全部股东），而同指标 ROE 用 parent_net_profit（归母）→ 同一 QMJ 内两种净利口径并存（报告陷阱⑤未兑现）；Altman Z 用市值口径 MVE/TL，使 QMJ 携带估值/市值暴露，与报告『QMJ 全部由基本面构成、与估值正交』直接矛盾；Altman Z 的 RE 只用 undistributed_profit，未加 surplus_reserve（盈余公积），严格口径偏小
- **分母体检**：R4-1 分母是什么：GP/A 分母 = balance_sheet.total_assets；ROE 分母 = total_equity_parent；CFO/A 分母 = total_assets；杠杆 = total_liabilities/total_assets；应计 = total_assets；Altman 的 0.6 项分母 = **balance_sheet.total_liabilities**（这是全指标最危险的一个）。R4-2 会不会 ≤0：total_assets / total_equity_parent / total_liabilities 都被 >0 挡住，没有重演 ROCE 分母为负事故；真正的风险是**分母趋零**：total_liabilities 在准零负债公司上让 0.6·MVE/TL 发散到 297，报告的处置是 LEAST(...,3.0)——实测 40.6%（2,108/5,193）顶格，这不是护栏而是把这一维压平（T4/K3，与 ROCE 第一版『给 ROCE 设 300% 上限』同型）。R4-3 量级对得上标杆吗：Altman Z 的经典分界 1.8 / 3.0，而未截断最大 296.97；z 分合成的榜首实测是 003010（商贸零售），而非报告声称的贵州茅台（第 9）/山西汾酒（不在前 12）——报告的『榜首』说法是编的。R4-4 空值语义：cost_of_revenue 缺失被过滤、COALESCE(acc,0) 把缺失当 0、undistributed_profit 用 COALESCE(...,0)——三种语义被合并，无 indicator_null_reason。
- **历史可行性**：5 年可行、10 年打折：三张主表 + indicator_ext 的年报覆盖在 2000 年后接近 100%，但 QMJ 的 Altman 项硬依赖 market_cap_at_report，而该字段非金融年报覆盖率 FY2010 65.5% / FY2013 52.6% / FY2015 53.9% / FY2020 65.6% / FY2022 81.7%（审查员实测），且 FY2015 的 2,199 只市值缺失股中 1,701 只有价格历史、1,698 只至今仍在市，中位总资产 5.04 亿 vs 有值组 36.62 亿——缺失系统性偏小盘。故『5 年 / 10 年历史中位数完全可行』只在有市值子样本上成立，且质量因子的行业构成会随年份漂移。按 K6：QMJ 是同期截面比率，做 N 年中位数需 N 期年报，不需额外 N+1；但若要算 ΔROE/盈利增长（Asness 原始 QMJ 的『增长』腿，本版未纳入），则需 N+1 期。口径断点：2020 新收入准则（运费入营业成本 → GP/A 台阶，同面板毛利率 −1.28pct）、2017 研发费用单列（管理费用口径变，本版未用 admin 进 QMJ，但影响后续扩展）、2018 asset_impairment_loss 符号翻转（打穿 ROE 与应计项）、2019 新金融工具准则、FY2025 cash_flow_indirect.net_profit 0 行（本指标用 cash_flow 主表 cf_from_operating，不受影响——须写明）。
- **通病标签**：T4, P1-b, P1-c, K3, K6, K8
- **ROCE 同型**：是
- **必修项**：
  - ①Altman Z 的 0.6·MVE/TL 不要用固定 3.0 截断（40.6% 顶格），改为按年截面 1%/99% winsorize，或把该项从安全组移出（Asness 原始 QMJ 安全组是低 beta / 低杠杆 / 低破产风险，不含市值项），以恢复与价值因子的正交性
  - ②删掉或真正实现 data_mapping 里的 dilution_3y / payout_ratio / beta 三条（现在六项 z 分里一个都没有，属声称与交付不符）
  - ③把『QMJ 是纯质量因子、与估值正交』改成『本版因 Altman Z 含市值项而带市场/估值暴露，归因时需剥离』
  - ④修正『FY2024 榜首正是贵州茅台、山西汾酒』（实测榜首 003010，茅台第 9，汾酒不在前 12）
  - ⑤统一净利口径：accruals 换成 (parent_net_profit − cf_from_operating)/total_assets，或把 ROE 也改成 net_profit/total_equity，二选一
  - ⑥Altman Z 的 RE 改为 undistributed_profit + surplus_reserve（两列都在库）
  - ⑦补一句护栏命中率（40.6%），否则使用者会把 altman_z 当作有效判别变量
  - ⑧做 5/10 年历史中位数时显式给出 market_cap_at_report 逐年覆盖率（FY2013 52.6% / FY2015 53.9%），并说明小盘剔除偏差
  - ⑨落地须登记 derived_snapshot_domains() + 重建触发（K5）
- **证据**：reports/131 §2 总表（#24 审查 🟡 需修正、ROCE 同型=高）；reports/131 §3.4 #24（字段映射 exact 6 / approximate 3 / missing 0；审查发现护栏顶格 40.6%、正交性论断矛盾）；reports/131 §4.1 T4（QMJ 用 LEAST(x,3.0) 硬截断，实测 40.6% 顶格，指标退化成 0/1）、T3、§4.2 P1-c；reports/131 §5 G4（库内无 beta / WACC / 永续增长率，全库零命中）；reports/131 §10 K3/K6/K8；reviews/quant_value.json#indicators[5]（5,193 全可算；2,108/5,193=40.6% 顶格；未截断 296.97/300561；accruals 349,369 全等；FY2025 accruals 5,618/5,625；榜首 003010 而非茅台）；schools/quant_value.json#indicator[5].formula_sql（LEAST(...,3.0)、六项 z 分、COALESCE(acc,0)）；app/core/indicators/extended.py:97（ACCRUALS_MAX=2.0）:114（BOOK_TO_MARKET_MAX=10）:345-348（accruals 定义用 net_profit）；app/core/storage/schema.py:2166（accruals ALTER）:2189-2192（dilution_3y / buyback_ratio_3y / bonus_share_ratio_3y / top_segment_share ALTER）

## #25 现金转换率（自由现金流 / 净利润） — `buildable_after_fix`
- **理由**：6 个字段全部真实（含 indicator_ext.free_cash_flow 恒等式已由独立审查员实测 132,196 行 0 处不符），但 formula_sql 完全没做 sw_level1 金融股剔除、也没设 cc_years_5y 门，实测把齐鲁银行 6.789 / 江苏银行 5.903 顶在榜首，且 6,145 只输出里 1,375 只不足 5 年仍叫 cash_conversion_5y —— 字段够、口径必须修。
- **分母体检**：分母 = SUM(net_profit)（近 5 个年报、只累计 net_profit>0 的年）。R4 四问：① 精确列 income_statement.net_profit；② 会不会 ≤0 —— 不会，WHERE net_profit>0 整行剔除 + NULLIF(SUM,0) 双保险，独立审查员实测未复现负分母爆量级；③ 量级 —— 非金融 p50 待 SQL 25-4 实测（期望 0.6–1.2），金融组中位实测银行 2.36 / 非银 1.68，已确认无经济含义；④ 空值 —— capex 为 NULL 时 free_cash_flow 为 NULL（fail-closed，正确），属「数据没抓到」而非「数学不成立」，应登记 indicator_null_reason=SOURCE_EMPTY。真正风险不在分母符号，而在**分母的样本年数**：674 只只有 ≤2 年、1,375 只 <5 年，仍输出名叫 5y 的比率。
- **历史可行性**：可做 5 年累计（需 5 个有效年报，非 6 —— 本指标是同期比率，不像 ROCE 要上期资本，K6 的 N+1 规则**不适用**于此，但「亏损年被剔除后窗口实际跨度会变长」是另一种 K6：必须输出窗口实际起止年）。10 年口径只有 3,400 只（占 6,242 的 54.5%），报告已如实自曝。断点：FY2025 cash_flow_activity 20/21 列全空但 **capex 仍有值**（reports/131 §9.2 P0-3 L1207），故 2025 不被半空壳击穿；2018 减值符号翻转与 2020 收入准则不直接进本式。
- **通病标签**：T2, T4, T5
- **ROCE 同型**：是
- **必修项**：
  - annual CTE 加 `JOIN stock_meta m USING (stock_code)` 并 `COALESCE(m.sw_level1,'') NOT IN ('银行','非银金融')`，判据与 app/core/indicators/roce.py:170 完全一致（**不要**用 csrc_l1）
  - 外层加 `WHERE cc_years_5y = 5`（或 >=4 且额外输出窗口实际起止年份），不足年数只保留 cc_years_5y 不输出比率
  - trap ④ 的「招商银行 CC_5y=1.82 / 中国平安=2.31」改为「10 年累计 CC=1.818/2.309，5 年窗口为 2.619/2.868」（reviews/quality_compound.json#IDX0 实测）
  - 「1998 年 993/1,192」改 1,201；「2000 年起 >93%」改「2001 年起 >93%，2000 年 91.5%」
  - 落地时必须保留 fail-closed：**禁止** COALESCE(capex,0)（现有 SQL 已正确，别在实现时“修”坏）
  - 按 SKILL 步骤 10 输出三列位：cc_5y / cc_5y_median（或 cc_10y）/ 最新单年 cc，只给一个累计值会重演 K7
- **证据**：docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:631-651（§3.5 #25 正文）；:93（§2 总表 ROCE 同型=高）；:1108（§6.2「现金转换率｜T4 同型」）；:1039（§4.2 P1-a）；:1409（§11.3）；.planning/2026-11-16-masters-survey/schools/quality_compound.json#L9-L60（formula_sql 全文，确认无 stock_meta / sw_level1）；.planning/2026-11-16-masters-survey/reviews/quality_compound.json#IDX0（实测：榜首齐鲁银行 6.789 / 江苏银行 5.903 / 中泰证券 5.824 / 东吴证券 5.464 / 北京银行 5.248；银行 CC 中位 2.36、非银 1.68，共 118 只有值；6,145 只中 1,375 只 <5 年、674 只 ≤2 年）；.planning/2026-11-18-indicator-feasibility-audit/fields-inventory.md#cash_flow / #cash_flow_activity / #income_statement / #indicator_snapshot / #stock_meta；app/core/indicators/roce.py:170（项目官方金融股判据 `COALESCE(meta.sw_level1,'') NOT IN ('银行','非银金融')`）；dbq 实测未取到（库被自动更新独占锁），以上样本数字均为独立审查员实测值，非本人复现

## #26 毛利率与费用率双稳态（含「规模效应共享」检验） — `buildable_after_fix`
- **理由**：8 个字段里 6 个在 CREATE TABLE 里、2 个（selling/admin_expense_ratio）在 ALTER 迁移里，全部真实无幻觉；分母侧防护到位（revenue>0 + cost_of_revenue NOT NULL，金融股自然掉出）。必须先修的是三处口径：revenue_cagr4 按行号相除会静默跨 6 年、gm_5y_std 无 n5>=4 门、以及 Fundsmith 0.40 门槛在 A 股会把格力(29%)/伊利(33%)/海康(44%)全挡掉。
- **分母体检**：分母 = income_statement.revenue。R4 四问：① 精确列 revenue；② 会不会 ≤0 —— 不会，WHERE revenue>0 已挡；③ 量级 —— gm_5y_median 全市场 p50 待 SQL 26-5 实测（期望 0.15–0.35），已实测格力 29.81%/伊利 34.58%/海康 45.88%（reviews/quality_compound.json#IDX1），量级合理；④ 空值 —— cost_of_revenue 为 NULL 的行占 2–5%（主要是金融股），属「该行业本来就没有营业成本」= 数学不成立，不是没抓到，应登记 INVALID_DENOMINATOR 而非 SOURCE_EMPTY。真正的分母隐患是 opex_ratio 的**费用侧**：管理费用的口径在 2017 断裂（见 field_checks）。
- **历史可行性**：年报「营业收入+营业成本」双非空：6,054 只有 ≥5 个、5,023 只有 ≥10 个、平均序列 17.0 年（报告声明，独立审查员三条全部精确复现）。推荐主窗口 = 最近 5 个年报（2021–2025，全在 2020 准则之后）；10 年窗口必然跨 2020 CAS14 断点（固定面板 −1.28pct）与 2017 研发单列断点，只能标注后作参考。K6：本指标是同期比率，不需要 N+1 期；但 STDDEV_SAMP 需 ≥2 点，n5<4 时统计意义很弱，必须同时输出 n5。
- **通病标签**：T4, T5
- **ROCE 同型**：否
- **必修项**：
  - revenue_cagr4 增加连续性校验：「rn=1 与 rn=5 的 report_date 相差恰为 4 年」，否则置 NULL（SQL 26-7 探针）
  - gm_5y_std / opex_5y_std 增加 n5>=4 才输出的门，并把 n5 一并落库
  - 显式加 `COALESCE(m.sw_level1,'') NOT IN ('银行','非银金融')`，不要依赖 cost_of_revenue IS NULL 这个巧合（实测输出里仍残留 32 只金融股）
  - Fundsmith 门槛 0.40 改为行业分层（白酒/软件 vs 制造），否则选出来全是白酒+软件，格力/伊利/海康这类真复利机器被系统性挡掉（报告 trap ⑥ 已自曝）
  - 费用率固定为「销售+管理」且窗口不跨 2017；若必须跨 2017，则把 rd_expenses 合并进管理费用后再算
  - indicator_ext.selling_expense_ratio / admin_expense_ratio 是 ALTER 补的列（schema.py:1990-1991），落地时必须补进 derived_snapshot_domains() 保全，否则快照重发会重演 K5
  - 删除 opex_ratio 里的 COALESCE(selling,0)+COALESCE(admin,0)（WHERE 已要求两者非空，属死代码，无害但会误导后续维护者）
- **证据**：docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:652-672（§3.5 #26 正文）；:94（§2 总表 ROCE 同型=低）；:1035-1043（§4.1 T3/T4/T5 定义）；.planning/2026-11-16-masters-survey/schools/quality_compound.json#L61-L200（formula_sql 全文）；.planning/2026-11-16-masters-survey/reviews/quality_compound.json#IDX1（实测：返回 6,130 行、gm_5y_std 空值率 0.07%、revenue_cagr4 空值率 1.96%、固定面板 4,188 只、毛利率 28.66%→27.38%、销售费用率 4.77%→3.45%、管理费用率 9.43%→6.70%、rd 86/627/3,932/5,128）；.planning/2026-11-18-indicator-feasibility-audit/fields-inventory.md#income_statement / #indicator_snapshot / #indicator_ext（注意：indicator_ext 只列 23 列，缺 ALTER 补的费用率族）；app/core/storage/schema.py:773-801（indicator_ext CREATE TABLE 23 列）与 :1982-1991（ALTER 补 bps/费用率族）；app/core/indicators/extended.py:182-183（selling_expense_ratio / admin_expense_ratio 的写入来源）

## #27 ROCE 持久性（10 年中位数 + 最低值 + 变异系数） — `buildable_after_fix`
- **理由**：字段最扎实的一条（roce_history 三条恒等式 0 处不符、ce≤0 全部落 INVALID_DENOMINATOR 零泄漏、4,610 只有 ≥10 个有效年报），但报告把它列进第一档并称「只需在现有 ROCE 域上加两个聚合列」—— 实测 roce_10y_min / roce_10y_cv **这两个列在库里根本不存在**（indicator_snapshot 只有 roce_latest/5y_median/5y_years/10y_median/10y_years/eligible 六列），必须先做 schema 迁移 + derived_snapshot_domains 登记。
- **缺列/缺数据**：indicator_snapshot.roce_10y_min；indicator_snapshot.roce_10y_cv
- **分母体检**：本指标有两级分母，一级好、一级有残留风险。① ROCE 自身的 CE：ce≤0 的 769 行 0 行被标成 OK（723 INVALID_DENOMINATOR + 4 MISSING_EBIT + 42 NO_PRIOR_PERIOD）—— 这是**本项目 ROCE 事故的反面教材级防护**，零泄漏；② CV 的分母 MEDIAN(roce)：只用 `CASE WHEN MEDIAN(roce) > 0` 保护，对 0<中位数<0.01 的 91 只无效 —— 实测 158 只 cv>10、12 只 cv>100，数值上已无「变异系数」含义（但判据是 cv≤0.35，方向保守不产生假阳性）。R4 四问：精确列 roce_history.roce；会不会≤0 —— 会（roe 可为负），已用 CASE 挡；量级 —— p0.1=−1.41/p50=0.103/p99=0.637，|roce|>5 仅 9/105,168 行（600259 2007 年 CE 仅 261 万而 EBIT 3,664 万，属真实微型分母），与事故的 −8,311%~+21,285% 不是一个量级；空值 —— 7.55% 是数学不成立，不是没抓到。
- **历史可行性**：**完全可做，且是本批历史最扎实的一条**：roce_history 105,168 行、1989-12-31~2025-12-31；≥10 个有效年报 4,610 只、≥5 个 6,006 只、平均序列 15.85 年；status 分布自洽（OK 97,650 + NO_PRIOR_PERIOD 6,168 + INVALID_DENOMINATOR 998 + MISSING_EBIT 352 = 105,168）。K6 已天然满足：roce_10y_years 计的是 roce 非空年数，序列首年因缺上一年 CE 必然 NULL(status=NO_PRIOR_PERIOD)，已被 WHERE roce IS NOT NULL 排除，所以「10 年中位数」用的是 10 个真值而不是上市 10 年。断点：2018 减值符号翻转与 2020 收入准则都不直接进 EBIT/CE（EBIT=利润总额+财务费用、CE=总资产−流动负债），2025 现金流半空壳也不影响（本指标不用现金流）。
- **通病标签**：T5
- **ROCE 同型**：否
- **必修项**：
  - 新增两列：schema.py 加 v46 迁移 `ALTER TABLE indicator_snapshot ADD COLUMN IF NOT EXISTS roce_10y_min DOUBLE / roce_10y_cv DOUBLE`，同时把两列加进 app/core/indicators/roce.py:198 的 SNAPSHOT_DERIVED_COLUMNS 与 SNAPSHOT_UPDATE_SQL —— 否则快照重发会把它们清成 NULL（K5 重演）
  - CV 增加 MEDIAN(roce) >= 0.02 的门（或改用 IQR/中位数），避免 158 只 cv>10 的伪变异系数
  - financial_expenses 的 match 由 exact 改 approximate，与 #28 对齐，并注明「EBIT=利润总额+财务费用是项目既定近似，净现金公司被低估」
  - 三处「121 只金融股 TCL 为 NULL / 根本没有 ROCE」改为「74 只全历史 TCL 全空被自动排除；另 47 只（券商为主）TCL 非空、已算出 783 个有效 ROCE 年」
  - 删掉「金融股导致 EBIT 缺失」的因果，改为「financial_expenses 为空即 MISSING_EBIT（352 行，跨 20 余行业）」
  - 「年报覆盖 100%」改 ≈100%（total_assets 缺 4 行、total_profit 缺 49 行）
  - 显式加 sw_level1 剔除（roce_history 本身不按行业过滤，券商那 783 行 status=OK 的 ROCE 中位约 4%，会污染分布左尾）
  - 按 SKILL 步骤 10 输出三列位：roce_10y_median / roce_5y_median / roce_latest —— 快照已有前两者与 roce_latest，本指标必须同时暴露 latest，否则重演 K7（智飞生物 10 年 36.9% vs 最新 −66.4%）
- **证据**：docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:673-694（§3.5 #27 正文）；:95（§2 总表 ROCE 同型=低）；:1097（§6.1 第一档「零新增数据成本，只需加两个聚合列」）；:1314-1326（§10.1 K5/K6/K7）；.planning/2026-11-16-masters-survey/schools/quality_compound.json#L201-L270（formula_sql 全文，直接读 roce_history，含 MEDIAN/MIN/STDDEV_SAMP）；.planning/2026-11-16-masters-survey/reviews/quality_compound.json#IDX2（实测：返回 6,161 行、cv 空值率 7.55%、158 只 cv>10 / 12 只 cv>100、91 只 0<median<0.01、ce≤0 零泄漏、|roce|>5 仅 9 行、352 MISSING_EBIT 里金融仅 23 行、roce_history 105,168 行与四类 status 分布）；app/core/storage/schema.py:313（roce_10y_years 列声明）；:2238-2262（v42 只 ALTER 五列 + v45 roce_eligible）；app/core/indicators/roce.py:161-170（SNAPSHOT_UPDATE_SQL 六列）；:198-205（SNAPSHOT_DERIVED_COLUMNS 六列，**无 min / cv**）；app/core/indicators/calculator.py:67-77（derived_snapshot_domains 注册表：新增派生列必须在此登记）；.planning/2026-11-18-indicator-feasibility-audit/fields-inventory.md#indicator_snapshot（70 列，无 roce_10y_min / roce_10y_cv）与 #roce_history

## #28 增量资本回报率（5 年，双口径） — `buildable_after_fix`
- **理由**：字段全真（无一个幻觉列，NOPAT 老实标 missing 并给出派生式），分母防护是本文件做得最好的一条（ic>0 / ic_l5>0 / ΔIC>0 / ΔIC/ic_l5≥5% 四重门实装，等价于宁可不出数也不出爆炸数）。必须先修的是三处口径缺陷：COALESCE(income_tax,0) 让缺税数据公司按 0% 税率计 NOPAT（虚高上限 35%）、ex-cash 口径漏扣 lease_liabilities、以及报告 §4.2 P1-a / §11.3 把 #25 的锅（齐鲁银行 CC=6.79、江苏银行 5.90）扣在了本指标头上。
- **缺列/缺数据**：NOPAT（全库无，须派生；非幻觉，报告已声明）
- **分母体检**：分母 = ΔIC（= IC(t) − IC(t−5)），有两套。R4 四问：① 精确列 balance_sheet.total_assets − total_current_liabilities；② 会不会 ≤0 —— 会，且**四道门全部实装**：ic>0 AND ic_l5>0 AND ΔIC>0 AND ΔIC/ic_l5≥0.05，实测 incr_roic_5y 空值率 41.3%、ex-cash 45.3%（护栏+未满 5 年所致），等价于「宁可不出数也不出爆炸数」，与 ROCE 事故正好相反；③ 量级 —— 主口径 p50 待 SQL 28-4（期望 0.02–0.15），|x|>5 应 <1%；④ 空值 —— 41% 的空值同时是「数学不成立（ΔIC≤0/趋零）」与「历史不足（未满 5 年报）」，必须在 indicator_null_reason 里**分开两种 reason_code**（INVALID_DENOMINATOR vs 历史不足），现状未区分。另一处分母隐患是 ic_excash 的构成：漏扣 lease_liabilities 会让分母偏大（ΔIC 被稀释），方向是低估增量回报。
- **历史可行性**：依赖 income_statement × balance_sheet 主表，2007 年后每年 5,000+ 家。**报告说「2013 年起才有第一个可算点、共 13 个可算年度」是错的**：LAG(…,5) 按行取，实测 1994 年即有非空（1995 年 32 个、2000 年 658 个、2013 年 2,240 个），可算年度实为 1994–2025 共 32 个，只是越早覆盖越薄（reviews/quality_compound.json#IDX3）。这对「能不能做 10 年历史中位数」的判断影响很大：深度远超报告所称。K6：5 年增量需要 t 与 t−5 两个年报，即 6 个年报点、且必须校验连续性（LAG(5) 是「往前数 5 行」不是「往前 5 年」，中间缺年报会静默错配到 t−6）。断点：FY2018 资产减值损失移出 total_operating_cost 不影响本式（不用 total_operating_cost）；FY2020 预收→合同负债重分类不影响 CE（CE 只减流动负债合计，不受明细重分类影响）；FY2025 现金流半空壳完全不影响（本指标不用现金流）。
- **通病标签**：T2, K2
- **ROCE 同型**：否
- **必修项**：
  - `COALESCE(income_tax, 0)` 改为「income_tax IS NULL → 0.25 中性税率」，否则缺税数据公司 NOPAT 最多虚高 35%
  - ex-cash 口径补扣 `balance_sheet.lease_liabilities`（与 interest_bearing_debt 的三项口径对齐说明）
  - 增加「rn=1 与 rn=6 的 report_date 相差恰为 5 年」的连续性校验，否则置 NULL（LAG(5) 行号错配）
  - 显式加 `COALESCE(m.sw_level1,'') NOT IN ('银行','非银金融')`，不能只靠 total_current_liabilities IS NOT NULL —— 47 只券商 TCL 非空会漏进来
  - **修报告的错归因**：§4.2 P1-a(L1039) 与 §11.3(L1409) 写「质量学派『增量 ROIC』让齐鲁银行（CC=6.79）、江苏银行（5.90）占据榜首」——6.79/5.90 是**现金转换率 #25 的值**，且银行 TCL 全空在本指标里根本无值，本条指责应移到 #25
  - trap ② 的「ic_excash≤0 占 2.6%（1,600/62,347）」改为实测「1.94%（2,040/105,089；2013 年后 1,320/70,017=1.89%）」，分母 62,347 无法重现
  - 海天案例的「ΔNOPAT 仅 +8.9 亿」改为 +5.9 亿（66.72−60.80），否则与自身 2.7% × ΔIC 218.8 亿不自洽
  - history_years 改为「1994 年起即有可算点，2007 年后覆盖变厚，共 32 个可算年度」，删除「2013 年起才有第一个可算点」
  - 第六处「121 只金融股 TCL 为 NULL」同样改 74 只 / 47 只
  - 指标落地时必须双列输出（incr_roic_5y + incr_roic_5y_excash），单列就不成立 —— 这是 K2「修正项」的唯一合法处理方式（两口径并列而不是二选一）
- **证据**：docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:695-719（§3.5 #28 正文）；:96（§2 总表 ROCE 同型=低）；:1039（§4.2 P1-a，错误归因处）；:1409（§11.3，同一错误重复）；:1044-1069（§4.3 689009 与「派生表陈旧」机制）；.planning/2026-11-16-masters-survey/schools/quality_compound.json#L271-L340（formula_sql 全文，四重护栏均在）；.planning/2026-11-16-masters-survey/reviews/quality_compound.json#IDX3（实测：返回 105,089 行、主口径空值率 41.3%、excash 45.3%、海天 IC 204.5→423.3 亿与 2.7%、1994 年起即有可算点、2,040/105,089=1.94%）；app/core/indicators/extended.py:142（`_EBIT = net_profit + income_tax + financial_expenses`，证明 income_tax 为正数费用语义）；.planning/2026-11-18-indicator-feasibility-audit/fields-inventory.md#balance_sheet（含 lease_liabilities 第 32 列、trading_financial_assets、bonds_payable）/ #income_statement / #indicator_ext；app/core/storage/schema.py:792（indicator_ext.ebit 列声明）

## #29 再投资率 × 增量 ROIC = 内生增长率 — `buildable_after_fix`
- **理由**：与 #28 同一 base、同一套 ΔIC/IC 护栏，分母爆炸风险已被挡住；NOPAT 老实标 missing。必须先修的是三处：data_mapping 把 indicator_snapshot.revenue_cagr5 / net_profit_cagr5 挂在「再投资率」这个 component 下（语义完全不同，会误导快速扫表者）、RR 单指标输出无 [0.30,1.50] 上下限（ΣNOPAT 趋零的微利公司 RR 可到 2.17）、以及与 #28 同源的 income_tax 空值当 0% 税率。
- **缺列/缺数据**：NOPAT（全库无，须派生）；再投资率（库内无任何此字段，须按 ΔIC/ΣNOPAT 现算 —— 报告已声明）
- **语义错配**：indicator_snapshot.revenue_cagr5；indicator_snapshot.net_profit_cagr5
- **分母体检**：两个分母。① RR 的分母 ΣNOPAT(近 5 年报)：用 `n_win5 = 5 AND nopat_sum5 > 0` 挡住 ≤0，ΣNOPAT≤0 时置 NULL（正确：RR 会符号翻转）；② incr_roic 的分母 ΔIC：与 #28 同一套四道门。R4 四问：精确列已定；会不会 ≤0 —— 已挡；量级 —— RR 实测 p50 待 SQL 29-2，已知存在 RR=1.386/1.300/2.168 的公司（ΣNOPAT 趋零造成的伪高再投资率），**本指标输出层完全没有 [0.30,1.50] 上限护栏**（护栏只写在 #30 腿 2 里），单用本指标筛选会把微利公司排到前面，这是 T4「护栏失效/缺失」的典型；空值 —— reinvest_rate_5y 空值率 37.3% 是数学不成立，rr_flag 空值率 95.5% 是设计内（只在 ΔIC<0 时赋值）。
- **历史可行性**：窗口比 #28 更严：需要 5 个连续年报的 NOPAT 累计（n_win5=5）外加 t−5 的 NOPAT/IC，即至少 6 个年报点。报告称「2013 年起可算、13 个年度」—— 与 #28 是同一个错误（LAG/ROWS 按行取，实测 1994 年起即有非空，共 32 个可算年度）。可以做「近 3 个重叠 5 年窗口的中位数」抗噪；不建议做 10 年窗口（需 11 个年报、覆盖掉一半以上，且跨 2020 疫情与准则变更）。K6：窗口必须校验「rn=1 与 rn=6 相差恰 5 年」，否则 ROWS BETWEEN 4 PRECEDING 会在缺年报时静默跨 6 年。断点：2018 减值/2020 收入准则都不进 NOPAT 与 IC；FY2025 现金流半空壳不影响（本指标不用现金流）。
- **通病标签**：T4, K2
- **ROCE 同型**：否
- **必修项**：
  - 把 data_mapping 的「再投资率（已物化近似）」整行删除，或把 component 改名为「实际增速（不可替代再投资率）」
  - 输出层加 rr_flag='NOPAT_TOO_SMALL'（RR>1.5）与 'BUYBACK_OR_SHRINK'（RR<0）两档标记，使单指标使用时也能识别分母趋零
  - 与 #28 同步修正 `COALESCE(income_tax,0)` → income_tax IS NULL 时取 0.25
  - 增加 rn=1 与 rn=6 的 report_date 相差恰 5 年校验
  - business_breakdown 行数「203 万」改 1,012,409（reviews/quality_compound.json#IDX4 实测）
  - history_years 与 #28 同步改为「1994 年起即有可算点，共 32 个可算年度」
  - 文档必须写清 g_implied = RR × incr_roic ≡ ΔNOPAT/ΣNOPAT 是**恒等变形、没有独立信息量**（报告 trap ① 已自曝，这点写得很好，落地时不能丢）
- **证据**：docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:720-736（§3.5 #29 正文）；:97（§2 总表）；:1035-1043（§4.1 T4）；.planning/2026-11-16-masters-survey/schools/quality_compound.json#L341-L420（formula_sql 全文 + data_mapping）；.planning/2026-11-16-masters-survey/reviews/quality_compound.json#IDX4（实测：返回 105,089 行、reinvest_rate_5y 空值率 37.3%、rr_flag 空值率 95.5%、格力 RR=0.3208 与股本 60.157→56.014 亿(−6.89%)、海天 RR=0.7414 与 g_implied=0.0200、business_breakdown 1,012,409 行、RR=1.386/1.300/2.168 存在）；.planning/2026-11-18-indicator-feasibility-audit/fields-inventory.md#indicator_snapshot（revenue_cagr5 / net_profit_cagr5 存在，但库内无再投资率字段）/ #business_breakdown

## #30 Akre 三腿凳质量复利评分 — `buildable_after_fix`
- **理由**：无幻觉字段（管理层诚信 / TAM 两条老实标 missing 并写明「不能让 stool_score 看起来有跑道数据支撑」，是本文件最诚实的一条），三腿 64/300/291 与全过 3 只的样本声明已被独立审查员逐条复现。但本指标独有一个 K1 级语义错误：xdxr.songzhuangu 的真实单位是「每 10 股送转 X 股」，式子里当成「每股比例」用（`1 + songzhuangu` 应为 `1 + songzhuangu/10`），会把送转算成巨额回购，使腿 2「股本不稀释」对有送转的公司免费放行；另腿 1 依赖的 roce_10y_min / roce_10y_cv 两个列在库里根本不存在。
- **缺列/缺数据**：indicator_snapshot.roce_10y_min（若复用物化路线）；indicator_snapshot.roce_10y_cv（若复用物化路线）
- **语义错配**：xdxr.songzhuangu（每 10 股送转 X 股被当成每股比例）；indicator_snapshot.revenue_cagr5（在 #29 里语义错位；本指标里作 4 年 CAGR 用，正确）
- **分母体检**：没有大分母除法，但有三处小分母 + 一处「缺失当 0」。① roce_10y_cv 的分母 MEDIAN(roce)：CASE 只挡 ≤0，未挡 0<median<0.01；② revenue_cagr4 的分母 revenue(rn=5)：有 NULLIF(...,0) 且盈利行保证，但 rn=1/rn=5 可能跨 6 年；③ net_dilution_5y 的分母 = 送转累积倍数：**口径错位（见 field_checks）**，且 `sh_5ago > 0` 之外没有「股本链完整」的校验。**最严重的一处不是分母而是缺失语义**：腿 2 写 `COALESCE(d.net_dilution_5y, 0) <= 0.02`，把「股本链缺失」当「零稀释」—— 实测约 22.3% 的评分样本（约 1,218 只）该列为 NULL，等于「股本不稀释」这条腿在 1/5 样本上被无成本放行，与项目既有「缺失不得当 0」裁定相悖。R4 四问：精确列已定；会不会 ≤0 —— 本指标的除法都不产生负分母；量级 —— 三腿 64/300/291、全过 3 只，量级与 Akre 实际持仓数吻合；空值 —— 22.3% 属「数据没抓到」，必须标 dilution_data_missing 而不是默认通过。
- **历史可行性**：只能做最新截面 + 逐年回看快照，**不适合做历史中位数**：三条腿的窗口长度不同源（ROCE 10 年、增量 5 年、增长 4 年），且股本链会断（share_capital_history 5,551 只、1990 起、244,691 行；change_reason 99.96% 为 NULL 无法区分送转与定增）。受最短板约束：腿 1 要 ≥8 个有效 ROCE 年报，腿 2 要 6 个年报（n_win5=5 + t−5），腿 3 要 5 个盈利年报。K6：本指标最容易犯的就是把「各腿窗口年数」当成固定值 —— 必须把 roce_10y_years 与另外两腿的实际跨度一并落库并在 UI 展示，否则无法复核「为什么今年进了、去年没进」（报告 trap ⑦ 已提出，SQL 未实现）。断点：2018 减值符号、2020 收入准则（影响腿 1 毛利率与腿 3 增长）、FY2025 现金流半空壳（capex 仍有值，故腿 3 不被 2025 击穿）都需标注。
- **通病标签**：T4, T5, K1
- **ROCE 同型**：否
- **必修项**：
  - **【最高优先·K1】送转折算改口径**：`EXP(SUM(LN(1 + x.songzhuangu)))` → `EXP(SUM(LN(1 + x.songzhuangu / 10.0)))`（依据 extended.py:399-401 的 /10 语义；SQL 30-1/30-2 给实测探针）。不改则腿 2「股本不稀释」对有送转的公司是免费放行
  - 腿 2 稀释判据改为 `d.net_dilution_5y IS NOT NULL AND d.net_dilution_5y <= 0.02`（缺失判「不可评估」而非「通过」），或单独输出 dilution_data_missing 标记列
  - 腿 1 若走「复用 indicator_snapshot」路线，必须先按 #27 建 roce_10y_min / roce_10y_cv 两列；若保持现算则可直接落地
  - leg_cc 改为「近 5 个日历年」（亏损年置 NULL 而不是跳过），并输出窗口实际跨度年数
  - xdxr 子查询补 `AND x.category = 1`（category 2–10 是股本变动类，混进来会污染送转倍数）
  - 三腿拆成三个可独立筛选的字段暴露给使用者（报告自己已建议），不要让 0–3 的 stool_score 成为唯一出口
  - 把各腿实际窗口年数一并落库（roce_10y_years 已有，另两腿缺）
  - business_breakdown「203 万行」改 1,012,409
  - 与 #28 同步：`COALESCE(income_tax,0)` → income_tax IS NULL 时取 0.25
- **证据**：docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md:737-760（§3.5 #30 正文）；:98（§2 总表，可算=partial）；:1122（§6.3 第三档把 Akre 三腿凳列为「不建议整体评分」）；.planning/2026-11-16-masters-survey/schools/quality_compound.json#L421-L520（formula_sql 全文 + data_mapping；`EXP(SUM(LN(1 + x.songzhuangu)))` 与 `COALESCE(d.net_dilution_5y, 0)` 两处原文）；.planning/2026-11-16-masters-survey/reviews/quality_compound.json#IDX5（实测：返回 5,460 行、leg1/leg2/leg3 = 64/300/291、stool_score=3 仅 3 只且名单为 605499/600809/603605、去重前 6,161/72/350/326/4、share_capital_history 244,691 行/5,551 只/change_reason 空 244,584(99.96%)、xdxr 184,486 行/58,634 非空、net_dilution NULL 占 22.3%）；app/core/indicators/extended.py:399-401（**送转单位的关键证据**：`SUM(COALESCE(x.songzhuangu, 0)) / 10.0 AS bonus`，注释「每 10 股送转 X 股 → 换算成比例」）；app/core/adapters/akshare_adapter.py:972,993（`capitalization_ratio（每10股转增X股）→ transfer_share`，`float(cap) / 10.0`）；app/core/adapters/tdx_adapter.py:427（xdxr category=1 除权除息：fenhong/songzhuangu/peigu/peigujia）；app/core/storage/schema.py:246（xdxr.songzhuangu DOUBLE 列声明）；scripts/align_share_capital_tail.py:24,172（`change_reason='unit_rescale:<倍数>'`，判据「所有行的 meta/行值 都落在同一倍数」→ 支持「689009 股本校正是整链一致」）；.planning/2026-11-18-indicator-feasibility-audit/fields-inventory.md#share_capital_history / #xdxr / #stock_meta / #indicator_snapshot

## #31 净流动资产折价率（Net-Net 折价） — `buildable_after_fix`
- **理由**：4 个科目全部真实、口径与 Klarman 原文一致、SQL 可跑；但市值列 689009 错 10 倍（P0-9）会把该股 NCAV/市值 放大 10 倍直接冲到「最便宜」端，且金融股剔除只靠 CA IS NOT NULL 隐式兜底、报告自述「100 只 NULL 里 98 只属 financial_sector_items」与 context.md「78 只金融股」自相矛盾，两处都须先修。
- **缺列/缺数据**：indicator_ext.market_cap_at_report —— 不在 fields-inventory.md（该清单 indicator_ext 仅 23 列），但**不是幻觉列**：schema.py:2073（v30 ALTER）新增 + extended.py:277 计算，实库存在；indicator_ext.report_date_close / tobin_q / book_to_market / ev_ebitda / bps_parent 同样未登记（同一批 v30/v32/v37 ALTER）
- **分母体检**：分母 = indicator_ext.market_cap_at_report，SQL 已 `> 0` 硬约束 → denom_le0 恒为 0，无除零。R4 四问：①分母精确列名即此列；②不会 ≤0（WHERE 已滤）；③分子 NCAV=CA−TL 与分母同为人民币元，实测区间 −45.2~+4.2 属合理量级；④空值是「市值没抓到」而非数学不成立，须写进 indicator_null_reason。分子 NCAV 为负是经济含义（CA<TL）不是分母污染。真实分母风险是 P0-9：689009 分母 ÷10 → 比率 ×10。
- **历史可行性**：横截面分位可做 1990–2025（审查员实测 36 个年报点，1990-1992 仅 5/8/41 只不可用，可用区间 1993–2025；报告写「33 点」须改）。单股「十年中位 NCAV/市值」需 10 个年报点；NCAV 是存量比率、不需要上期数据，故 K6「N 年需 N+1 期」**不适用**，但筛选必须用 count(distinct year)>=10 的「有效数据年数」，不能用「上市满 10 年」（SKILL.md §9）。本式不碰利润表/减值，2020 新收入准则与 2018 减值符号两个断点不影响它；但 market_cap_at_report 早期覆盖薄（2000 年 864 只、2010 年 1,840 只）。
- **通病标签**：T5, T1
- **ROCE 同型**：否
- **必修项**：
  - ① 先修 P0-9 或加黑名单：任何按 market_cap_at_report 排序的排名都要先排除 689009（`change_reason='unit_rescale:10'`），否则该股恒居净网榜首
  - ② 金融股从「隐式 NULL 兜底」改为显式剔除 `COALESCE(m.sw_level1,'') NOT IN ('银行','非银金融')`，并用 financial_sector_items 兜底（仓库现成判据 app/core/indicators/roce.py:170，但该判据对 750 只 sw_level1=NULL 的股票放行，必须补兜底）
  - ③ 修正样本声明：33 → 36 个年报点、1990-1992 弃用；并裁决「100 只 NULL 里 98 只属 financial_sector_items」与 context.md「78 只金融股」的矛盾（SQL P2）
  - ④ 只做连续分位，禁止二值筛选：NCAV>市值 的股票 2020-2025 仅 16/16/13/18/11/7 只（§3.6#31 trap②）
  - ⑤ 落地必须走 derived_snapshot_domains() + 重建触发（K5、SKILL.md §8）
- **证据**：reports/131 §3.6 #31；reports/131 §4.3；reports/131 §9.2 P0-9；reports/131 §11.1；reports/131 §11.3；.planning/2026-11-16-masters-survey/schools/risk_cycle.json#indicators[0]；.planning/2026-11-16-masters-survey/reviews/risk_cycle.json#indicators[0]；app/core/indicators/extended.py:277；app/core/storage/schema.py:2068-2073；app/core/indicators/roce.py:170；.planning/2026-11-18-indicator-feasibility-audit/fields-inventory.md#balance_sheet；.planning/2026-11-16-masters-survey/context.md:35

## #32 折价清算价值/市值（安全边际折价率） — `buildable_after_fix`
- **理由**：8 个科目全部真实、SQL 可执行；但三处 coalesce 把「无此资产」与「没抓到」混为一谈（trading_financial_assets 2025 仅 61.6% 非空、contract_assets 2020 前不存在），折扣系数 0.85/0.50 是外生参数且未按行业分档，另需补 total_liabilities IS NOT NULL 与 689009 处理。
- **缺列/缺数据**：折扣系数（真实变现率）——结构性缺失，无替代口径
- **分母体检**：分母 = indicator_ext.market_cap_at_report，`> 0` 硬约束 → 无除零。R4 四问：①精确列名即此列；②不会 ≤0；③LV 与市值同为人民币元，区间 −95.2~+1.59 属清算价值应有的负偏分布，量级合理；④空值=市值没抓到。分子侧风险是 coalesce(total_liabilities,0)：负债 NULL 会让 LV 虚高，须加 IS NOT NULL（当前影响 0 行）。
- **历史可行性**：显式要求 total_current_assets IS NOT NULL → 实际可用的是非金融股，2025 年 n=5,347（报告实测）。横截面分位可做 2000 年后。三个跨年断点：trading_financial_assets（2016-2018 覆盖骤降）、contract_assets（2020 才存在）、存货折扣（地产）。做历史分位必须按段或统一用「不含这三个分项」的稳健口径。#31/#32 高度共线（2025 年 corr=0.77，审查员实测），不能同时上漏斗（等于重复计权）。
- **通病标签**：T5, P1-b
- **ROCE 同型**：否
- **必修项**：
  - ① SQL 加 `AND b.total_liabilities IS NOT NULL`（潜在护栏，当前 0 行受影响，防数据变动）
  - ② 三个 coalesce 改为区分「无此资产 / 未披露」：至少把 trading_financial_assets 与 contract_assets 的 NULL 记进 indicator_null_reason，不静默当 0
  - ③ 折扣系数按 sw_level1 分行业配置（地产/工程/贸易各一套）并暴露为可配置参数
  - ④ 禁止二值筛选：2025 年 LV≥市值 0 只、LV≥2/3 市值 2 只 → 只能做分位（§3.6#32 trap①）
  - ⑤ 与 #31 二选一入漏斗（共线 0.77）
  - ⑥ 689009 同 #31 处理；报告「接近全覆盖」改 61.6%、「相关系数接近 1」改 0.77、「33 个年报点」同 #31 改
- **证据**：reports/131 §3.6 #32；reports/131 §9.2 P0-8；reports/131 §4.3；.planning/2026-11-16-masters-survey/schools/risk_cycle.json#indicators[1]；.planning/2026-11-16-masters-survey/reviews/risk_cycle.json#indicators[1]；app/core/indicators/extended.py:277；.planning/2026-11-16-masters-survey/context.md#二.7

## #33 扣非低 PE 分位 + 预期总回报率（双闸门） — `buildable_after_fix`
- **理由**：五个科目全部真实、扣非净利润 2025 年覆盖 99.9%、历史分位参数化后完全可行；但交付的 formula_sql 有 P0 单位错配——股息率 = 每股派息 ÷ 总市值，实测该列最大值 8.73e-08（等同全 0），第二道闸门的股息项恒为 0 且不报错，与 ROCE 同型；另年份硬编码只能出单年快照，与自称的 19 个年报点不符。
- **缺列/缺数据**：全球跨市场相对估值（Templeton 原方法是跨国最低 PE）——库内只有 A 股，结构性缺失；indicator_ext.market_cap_at_report 未在 fields-inventory.md 登记（非幻觉列，见 #31 note）
- **语义错配**：dividends.dividend_per_share ÷ indicator_ext.market_cap_at_report（股息率：每股 ÷ 总市值，量级错 1e9）
- **分母体检**：本指标有三个分母，逐一按 R4 四问：①PE_ded 分母 = income_statement.deducted_net_profit，SQL 有 `ded > 0` 硬约束 → 负/零分母已被挡，但**不设下限**，实测 pe_ded 最大 20,751x（扣非趋零），任何绝对阈值（Templeton 原书 PE<10）都会失效；②CAGR 分母 = 2020 年扣非，HAVING >0 已挡；③股息率分母 = 总市值（实为错误组合，应改年末收盘价或 ×总股本），当前产出恒 0。量级：mc/ded 同为人民币元正确。
- **历史可行性**：可做 2007–2025 共 19 个年报点（扣非入库起点限制）。参数化年份后实测可用只数 2007=1,105 / 2010=1,592 / 2015=2,034 / 2020=3,163 / 2025=3,749（审查员实测），说明历史**完全可行**，问题只是交付物把年份硬编码成 2025。K6：PE_ded 是同年度「市值/当年扣非」的时点配对，不需要 N+1 期；但 5 年 CAGR 闸门需要两端（2020 与 2025）都有扣非且 >0 → 这是「N 年需两端」的真实样本收缩点，2020→2025 实测可用约 2,400 只（报告 2,423 / 审查 2,441，须实测裁决）。口径断点：2018 减值符号翻转与 2020 新收入准则都会影响扣非口径，跨 2018 的扣非 PE 序列须标注。
- **通病标签**：T1, T5, P1-d, P1-c
- **ROCE 同型**：是
- **必修项**：
  - ①【P0】股息率改为 `100.0*coalesce(dps_12m,0)/nullif(close_end,0)`（price_daily_raw 中 trade_date<=报告日的最后一条收盘价），或等价地用 `dps_12m*total_shares/market_cap_at_report`；total_return_no_rerating_pct 同步修正
  - ② 加自检断言：div_yield_pct 中位数必须落在 0.1%–5%，否则拒绝发布（防止同类静默归零再次发生）
  - ③ 年份参数化（去掉 DATE '2025-12-31' / 2020 / 2025 三处硬编码），否则「19 个年报点」是空头承诺（P1-d）
  - ④ pe_ded 加下限保护或直接只用分位（ded/mc < 1e-6 时标 UNSTABLE），避免 20,751x 进 UI
  - ⑤ 修正数字：扣非<=0 1,696→1,722、净利>0但扣非<=0 259→267、CAGR 可用 2,423→2,441（均以 SQL P2/P4 实测为准）
  - ⑥ 689009 同 #31 处理（P0-9 会把该股 PE_ded 压小 10 倍 → 更便宜）
  - ⑦ 股息率未做送转还原：跨年比较须标注，或改用 dividends.stock_dividend/transfer_share 还原
- **证据**：reports/131 §3.6 #33；reports/131 §4.1 T1；reports/131 §4.2 P1-c；reports/131 §4.2 P1-d；reports/131 §4.3；.planning/2026-11-16-masters-survey/schools/risk_cycle.json#indicators[2]；.planning/2026-11-16-masters-survey/reviews/risk_cycle.json#indicators[2]；app/core/indicators/extended.py:277

## #34 个股周期调整市盈率（10 年正常化盈利） — `buildable_after_fix`
- **理由**：归母净利润与市值两列真实、10 年年报窗口实测 2025 年可算 3,597 只、SQL 可执行（返回 38,878 行）；但分母只挡 avg_np10>0 不设下限（实测 55 只 cape10>1000x、最大 43,766.8x），且缺 Shiller 原版的通胀调整（全库无 CPI 表），另需补「过去 10 年至少 8 年扣非为正」的加护条件。
- **缺列/缺数据**：CPI / 通胀序列 —— 结构性缺失（G1 同类），Shiller 实际值口径做不了
- **分母体检**：分母 = 10 年均归母净利润（sum/10），SQL 有 `avg_np10 > 0` 硬约束 → 负分母/零分母已挡，无 ROCE 式符号翻转。R4 四问：①精确列名 = income_statement.parent_net_profit 十期求和/10；②不会 ≤0（已挡）；③分子分母同为人民币元、量级合理；④空值来自「当年无市值」或「10 年均值≤0」——后者是数学不成立，前者是没抓到，必须分开记 indicator_null_reason。**次生问题**：只挡 >0 不设下限 → 2025 年 55 只 cape10>1000x（最大 000068 = 43,766.8x、000526 = 20,850.9x），因下游只用 percent_rank 故排序不受影响，但绝对阈值一律失效。
- **历史可行性**：个股 CAPE10 可做 2000–2025（需 10 年窗口，最早窗口 1990+9）。2025 年实测可算 3,597 只；≥10 年年报盈利史 5,109 只、≥7 年 6,028 只、≥5 年 6,142 只；2016-2025 连续 10 年窗口完整的个股 4,571 只 → 缺口约 974 只来自「10 年均值≤0」或「当年无市值」。K6：CAPE10 需要 10 个**完整年报点**（窗口完整性 count(*)=10 已强制），不是 N+1 的流量问题，但「过去 10 年至少 8 年扣非为正」的加护条件会再砍样本。市场级 agg_cape10 只能做 2010–2025 共 16 点（更早年份市值覆盖 <1,500 只不可靠）。口径断点：2018 减值符号翻转不影响 parent_net_profit 本身；2020 新收入准则主要影响毛利率/费用率，对净利影响小。
- **通病标签**：T4, T5, P1-b
- **ROCE 同型**：否
- **必修项**：
  - ① 分母加下限或显式标记：cape10>1000 标 UNSTABLE/不参与绝对阈值（**不要**用 LEAST 截断——那是 T4 反面教材）
  - ② 补加护条件「过去 10 年至少 8 年扣非为正」，否则均值被亏损年拉低成假便宜（§3.6#34 trap②）
  - ③ 借壳/重组股按 stock_meta 上市日期或主营变更年份截断 10 年窗口，避免把非同一经营主体的利润加总
  - ④ 输出三列位：10 年 CAPE 中位 / 5 年 CAPE 中位 / 最新 CAPE（K7，SKILL.md §10）
  - ⑤ 跨行业必须同 sw_level1 内比较或用 index_valuation 行业 PE 归一（银行 CAPE 5 与半导体 CAPE 80 不可比）
  - ⑥ 复核「单年 PE 中位 47.3」的口径（审查员重算 45.61，偏差 3.6%），写明是否含亏损股/是否用扣非
  - ⑦ 689009 同 #31 处理（市值 ÷10 → CAPE ÷10 → 假便宜）
- **证据**：reports/131 §3.6 #34；reports/131 §4.1 T4；reports/131 §4.1 T5；reports/131 §5 G1；reports/131 §9.2 P0-7；reports/131 §10.1 K6；reports/131 §10.1 K7；.planning/2026-11-16-masters-survey/schools/risk_cycle.json#indicators[3]；.planning/2026-11-16-masters-survey/reviews/risk_cycle.json#indicators[3]；app/core/indicators/extended.py:277

## #35 市场温度计（全 A 横截面聚合，市场级而非个股） — `buildable_after_fix`
- **理由**：6 个刻度里 5 个有真实数据源（聚合 PE / 聚合 CAPE10 / ERP / 破净占比 / 换手率 / 翻倍股），唯一 missing 的信用利差已确认全库无替代；但 agg_cape10 无样本量下限（1998 年 n=2 却输出 67x~106x 垃圾值）、book_to_market 有 BOOK_TO_MARKET_MAX=10 的静默置空护栏、且 pre-IPO 财报不清洗会让 2015-2019 聚合有 31%-36% 幽灵公司。
- **缺列/缺数据**：信用利差 / 信用债收益率序列 —— 结构性缺失（§5 G3），无替代口径
- **语义错配**：index_valuation.div_yield（列存在但实质全空，标 missing 而非 approximate）
- **分母体检**：本指标是聚合比值，分母有四处：①agg_pe 分母 = Σ(net_profit>0 的净利)，nullif 兜底；这是**市值加权口径**，亏损股计入分子不进分母 → 系统性低估 PE（2025=18.65x vs 同年单年 PE 中位 47.34x，两个都对但混用会得出相反结论，必须在 UI 标口径）；②agg_cape10 分母 = Σ(10 年均净利>0)，nullif 兜底但**无样本量下限**（T5：1998 n=2 / 1999 n=54 / 2000 n=70 仍输出 67.05x / 66.06x / 94.32x、2007 年 106.33x）；③pct_pb_below_1 分母 = count(*)（含 btm 为 NULL 的）→ 与 count(book_to_market) 口径差 0.04pp（2025: 5.69% vs 5.73%）；④ERP 分母 = index_valuation.pe_ttm，有 >0 约束。均为同类相加、单位一致，无 ROCE 式负分母爆炸。
- **历史可行性**：各刻度年份区间不同，必须分段：聚合 PE 1993–2025（33 点，2025=18.65x、历史 59 分位）；聚合 CAPE10 只能 2010–2025（16 点，2025=23.25 / 2018 低点 17.48 / 2010 高点 39.33），SQL 若不加 HAVING 会吐到 1998 年 28 点；ERP 2006-03-01 起 5,051 个日频点；破净占比 1993–2025；换手率 1994–2025（审查员指出 SQL 因按财年 join 实际只到 2025，不是 2026）；翻倍股 2012–2026（2026 是残缺年，数据到 11-16，不可与完整年份比）。K6：这是年度横截面聚合，不需要 N+1 期；但每个刻度的「历史分位」窗口必须各自限定，混窗口算分位等于自欺。
- **通病标签**：T5, T4, P1-c
- **ROCE 同型**：否
- **必修项**：
  - ①【T5】聚合 CAPE10 加样本量下限（`HAVING count(*) >= 1500` 或只在 yr>=2010 输出），否则 1998-2009 的 67x~106x 垃圾值会混进温度分位
  - ② 每个刻度的 temp_* 分位窗口显式限定（agg_pe 1993-2025 / agg_cape10 2010-2025 / ERP 2006-2026 / 换手 1994-2025 / 翻倍股 2012-2025）
  - ③ 破净占比分母改 count(book_to_market) 以与报告口径一致；同时处理 BOOK_TO_MARKET_MAX=10 的护栏（极便宜股被置 NULL，破净占比被低估）——要么放开护栏，要么在 NULL 原因里区分「护栏置空」与「真缺失」
  - ④ pre-IPO 清洗必须写进 SQL：`stock_meta.listing_date <= report_date`（不清洗时 2015-2019 聚合有 31%-36% 是幽灵公司）
  - ⑤ 修正三处峰值/谷值断言：破净峰值是 2005 年 13.38%（非 2018 年 11.78%）、换手谷值是 2002 年 0.45%（非 2023 年 0.98%）、翻倍股 2018 与 2026 并列 0.66%
  - ⑥ 信用利差刻度保持 missing，禁止用 10Y 国债水平或等级差冒充（§5 G3）
  - ⑦ 689009 对聚合值影响可忽略（5,000 分之 1），但若做「个股贡献度」分解则须排除
- **证据**：reports/131 §3.6 #35；reports/131 §4.1 T4；reports/131 §4.1 T5；reports/131 §5 G3；reports/131 §9.3；.planning/2026-11-16-masters-survey/schools/risk_cycle.json#indicators[4]；.planning/2026-11-16-masters-survey/reviews/risk_cycle.json#indicators[4]；app/core/indicators/extended.py:114；app/core/indicators/extended.py:285-292

## #36 资产负债表弹性（净营运资本弹性 + 净现金占比） — `buildable_after_fix`
- **理由**：8 个科目全部真实、CA/TL 与净现金/市值两个刻度口径正确；但两个「÷归母权益」的比率用 nullif(x,0) 只挡零不挡负——2025 年 36 只归母权益≤0 直接产出 33 个负比率（区间 −36.77~+90.99）并被「反向计分」推到分位两端，属 ROCE 同型的 T2；另有息负债漏掉「一年内到期的非流动负债」，lease_liabilities 2020→2021 有准则断点。
- **缺列/缺数据**：indicator_ext.market_cap_at_report 未在 fields-inventory.md 登记（非幻觉列，见 #31 note）；「受限货币资金」列 —— 结构性缺失，净现金无法扣减受限部分
- **分母体检**：四个刻度四个分母，逐一 R4：①CA/TL 分母 = total_current_liabilities，nullif(0) + 恒 ≥0 → 安全；②净现金/市值分母 = market_cap_at_report，`>0` 硬约束 → 安全（但受 P0-9 污染）；③ibd_to_equity 分母 = total_equity_parent，**只挡 0 不挡负** → 2025 年 36 只归母权益≤0 产出 33 个负值、区间 −36.77~+90.99，1% 分位 0 / 99% 分位 6.28，极端分组全是负权益公司（600185 91x、002069 51x、300266 42x）；④goodwill_to_equity 同 ③，11 个负值（−4.43~+9.41）。这正是 ROCE 首版「219 只分母为负」的同型复发，必须加 `total_equity_parent > 0`。注意 reports/131 §4.1 T2 写「54 只里 52 只、区间 −111.1x~+337.0x」，与 §3.6/审查员的「36 只/33 个/−36.77~+90.99」矛盾，以 SQL P1/P2 实测为准。
- **历史可行性**：含市值字段的观测 1993–2025 共 69,312 个（口径同 #31，实际 36 个年报点）。CA/TL 单独可回溯到 1994 年（覆盖 99%），即使没有市值也能做历史中位。实测年度中位 CA/TL：1993 年 2.01 → 2005 年谷底 1.12 → 2025 年 1.78；净现金/市值中位：2010 年前长期为负（2005 年 −0.144）→ 2010 年后转正（2025 年 +0.034）。**断点三处，做历史分位必须分段**：①lease_liabilities 2020/2021 跳升（含租赁口径不可跨 2020 比）；②trading_financial_assets 2016-2018 覆盖骤降；③market_cap_at_report 2000 年前覆盖薄。K6：CA/TL、ibd_to_equity 都是同一报告期的存量比 → 不需要上期数据；「十年中位弹性分」需 10 个年报点，筛选须用 count(distinct year)>=10。
- **通病标签**：T2, P1-b, P1-a
- **ROCE 同型**：是
- **必修项**：
  - ①【T2/P0】两个「÷归母权益」的比率加正数护栏：`total_equity_parent > 0`（或比率处 GREATEST/nullif 组合），并把排除掉的资不抵债公司单独标注为「弹性分不可计算」写进 indicator_null_reason，**不要**静默置空也不要截断
  - ② traps 补第 7 条：归母权益≤0 会让 ibd_to_equity / goodwill_to_equity 符号翻转，必须先剔除（报告完全没提这条）
  - ③ 有息负债补 balance_sheet_ext.non_current_liab_due_1y（与 extended.py:298 的 ev_ebitda 口径对齐）
  - ④ lease_liabilities 断点：历史分位分段，或统一用「不含租赁负债」的稳健口径（indicator_snapshot.interest_bearing_debt 就是不含租赁的口径）
  - ⑤ 金融股显式剔除（sw_level1 NOT IN ('银行','非银金融') + financial_sector_items 兜底），不能只靠 CA IS NULL
  - ⑥ CA/TL 单刻度会选出高库存地产股（2025 年 CA>2×TL 有 2,297 只）→ 必须与净现金刻度叠加，不做单刻度筛选
  - ⑦ 689009 同 #31 处理（P0-9 会把净现金/市值放大 10 倍 → 误入「最富裕」榜）
  - ⑧ 报告「2025 年接近全覆盖」改 61.6%
- **证据**：reports/131 §3.6 #36；reports/131 §4.1 T2；reports/131 §4.2 P1-a；reports/131 §4.2 P1-b；reports/131 §9.2 P0-9；reports/131 §11.3；.planning/2026-11-16-masters-survey/schools/risk_cycle.json#indicators[5]；.planning/2026-11-16-masters-survey/reviews/risk_cycle.json#indicators[5]；app/core/indicators/extended.py:298；app/core/indicators/roce.py:170；.planning/2026-11-18-indicator-feasibility-audit/fields-inventory.md#balance_sheet_ext

## #37 Altman Z 值（制造业经典 5 变量） — `buildable_after_fix`
- **理由**：8 个输入列全部真实存在、口径可辩护、恒等式无风险，但必须先修 4 件事：金融股未在 SQL 里显式剔除（P1-a）、X4 用小分母 total_liabilities 会爆表且无 status（K3）、689009 市值错 10 倍未修（§9.2 P0-9）、原阈值 2.99 在 A 股失效（中位 3.47）。
- **分母体检**：分母共 2 个：① balance_sheet.total_assets（X1/X2/X3/X5）——恒为正，SQL 已有 WHERE ta>0；② balance_sheet.total_liabilities（X4）——WHERE tl>0 已挡负值，但『正得极小』没挡：2024 年报约 40 只 tl/ta<5%，其中 12 只 Z>50、最大 297.7（300561，tl/ta=5.7%），这是典型的『分母趋零 ⇒ 比率爆表』，必须输出 z_status 而不是 LEAST/GREATEST 截断（K3）。R4 四问：①分母精确列名=balance_sheet.total_assets / total_liabilities；②会不会≤0=已由 WHERE 挡住，但会趋零（爆表窗口 tl/ta<0.05）；③量级对不对外部标杆=不对，中位 3.47 已超 Altman 2.99 安全线，安全区占 57%（原样本约 60%+ 但其实 A 股无破产样本可校准）；④空值语义=两种混在一起：金融股 wc 为 NULL 是『数学不成立/行业不适用』，market_cap_at_report 缺失是『没抓到』（2015 覆盖 54%），必须用 indicator_null_reason 分开登记。
- **历史可行性**：可做 1994-2025 共 32 个年报点，但可用性被 market_cap_at_report 掐住：1994 年 224 只(32%)/2007 年 1,329 只(59%)/2015 年 2,555 只(54%)/2020 年 3,847 只(66%)/2024 年 5,234 只(90%)/2025 年 5,347 只(97%)。K6 扣一期：10 年历史中位数需要 2015-2025 共 11 个年报点，技术上成立，但 2015-2019 覆盖只有 54%-63%，样本偏向持续交易的老股（T5）。跨年断点：①2020 新收入准则把预收款项重分类进合同负债（§9.2 P0-8，prepayments_received 单列掉 96.6%）→ X1 营运资本跨 2020 有全市场性断点；②2021 新租赁准则把租赁负债推入负债、使用权资产推入非流动资产 → X1 亦可能跳变；③market_cap_at_report 口径 2024-12-31 起对 689009 错 10 倍。2018 减值符号翻转（P0-7）不影响本式（Z 不用减值列）。→ 建议长周期改用 Z''（#38，不需市值），Z 只做 2019 年起的横截面分位。
- **通病标签**：T2, T4, T5
- **ROCE 同型**：否
- **必修项**：
  - SQL 里显式 LEFT JOIN stock_meta 并输出 industry / in_financial 标记；金融股剔除写成 coalesce(sw_level1,'__NULL__') NOT IN ('银行','非银金融')，不要用裸 NOT IN（会在 370 行 NULL 上静默丢行，且裸 NOT IN 会误杀证券Ⅱ/多元金融里确实有流动划分的 22 只）
  - X4 加分母量级护栏的『标注』而非截断：输出 z_status ∈ {ok, tiny_liabilities(tl/ta<0.05), missing_market_cap, financial_sector}，并保留原始 Z 不置空（K3/K5 精神：不截断、不设上限、不置空）
  - 先修 689009 的 10 倍市值 bug（reports/131 §4.3、§9.2 P0-9：indicator_ext 输入指纹不含 share_capital_history，_refresh_indicator_ext 不看 share_capital_changed），否则 X4 对该股 2024-12-31 起 8 个报告期系统性低 10 倍；同时把 _INPUT_TABLES 补上 share_capital_history
  - 阈值不能照搬：Z>2.99 会把 57% 样本划进安全区（T4）→ 改为横截面分位（后 20% = 危险）或 sw_level1 行业内标准化，并保留原始 Z 与 zone 供对照
  - 必须补 K7 三列位：altman_z_latest / altman_z_5y_median / altman_z_10y_median（只给一个中位数会掩盖正在崩塌的公司）
  - 派生列必须登记 derived_snapshot_domains() + update.py 重建触发（K5），否则每次快照重发都会清成 NULL
  - 负营运资本要单独出辅助列：A 股预收/合同负债驱动的负 WC 是渠道强势特征，2024 年危险区 1,217 只里有 260 只『负 WC 且当年净利>0』，会被 X1 误判（reviews/forensic.json 已实测）
- **证据**：reports/131 §2 总表第 37 行（审查=🟡 需修正，ROCE 同型=低）；reports/131 §3.7 #37（字段映射 exact 4 / approximate 4 / missing 1）；reports/131 §4.2 P1-a（金融股剔除只写在陷阱里，没写进 SQL）；reports/131 §4.3 + §9.2 P0-9（689009 市值/每股族错 10 倍，43,478 观测中仅此 1 只）；reports/131 §5 G5（该条把 Altman Z 的 X3 说成 EBIT/固定资产原值，属报告自身错误）；reports/131 §10.1 K3（护栏掩盖）+ K8（外部标杆）；§10.3 步骤 4/5/6/7/10；schools/forensic.json#L15-L78（formula / formula_sql / data_mapping / history_years / traps）；reviews/forensic.json#L6-L20（样本数字逐条复现；非银 NULL 实为 57/79；sw_level1 NULL 370 行全库 750）；fields-inventory.md#L8（stock_meta.sw_level1）、#L17（balance_sheet 48 列）；app/core/storage/schema.py:773-802（indicator_ext 建表，ebit 在 :792）、:2068-2073（v30 ALTER 追加 market_cap_at_report）；app/core/indicators/extended.py:277（market_cap_at_report = px.close × sh.total_shares）

## #38 Altman Z'' 值（非制造业/新兴市场版，账面权益替代市值） — `buildable_after_fix`
- **理由**：唯一不用市值、历史最深（1994 年 99% 覆盖、5,025 只 ≥10 期）的破产预警式，字段全部真实且口径自洽；但必须先修：负营运资本的语义反转（危险区 26% 是假阳性）、阈值 1.10/2.60 在 A 股失效（中位 4.28）、金融股未显式剔除、商誉覆盖数写错 4,602→2,766。
- **分母体检**：分母 2 个：① balance_sheet.total_assets（X1/X2/X3）—— WHERE ta>0 已挡；全库年报 ta<=0 实测仅 1 行（survey 说「1990s 早期有 ta=0 或负值脏行」是编的，护栏无害但理由不实）。② balance_sheet.total_liabilities（X4）—— WHERE tl>0 已挡负值，但会趋零；且分子 total_equity 可为负（2024 年报 23 只），X4 变负 ⇒ Z'' 崩到极端负值（实测 min=-32.4）。R4 四问：①分母精确列名=total_assets / total_liabilities；②会不会≤0=挡住了，但趋零与分子为负两个方向都没挡；③量级 vs 外部标杆=不对齐，中位 4.2833 远高于 2.60 安全线（原阈值下 68%-84% 落安全区），必须改横截面分位；④空值语义=wc 为 NULL 是行业结构性不适用（金融），ebit 为 NULL 是没抓到，两者必须用 indicator_null_reason 分开。
- **历史可行性**：1994-2025 共 32 个年报点，覆盖极高（1994 年 682/692=99%、2007 年 2,262、2024 年 5,805、2025 年 5,525），reviews 已独立验证「5,025 只 ≥10 个年报点、1,815 只 ≥20 个年报点」→ 是本流派唯一能直接做 20-30 年历史中位数、可直接复用 roce_history 框架的指标。K6 扣一期：10 年中位需 2015-2025 共 11 期、5 年中位需 6 期（2019-2025）。断点：①2020 新收入准则（预收→合同负债，§9.2 P0-8）改 X1；②2021 新租赁准则（负债+使用权资产同时膨胀）改 X1；③2018 减值符号翻转（P0-7）不影响本式；④FY2025 cash_flow 半空壳（P0-3）不影响本式（不用现金流）。→ 长周期序列必须在 2020/2021 标断点，或做「经营性营运资本（剔预收+合同负债）」辅助列。
- **通病标签**：T2, T4
- **ROCE 同型**：否
- **必修项**：
  - ★ 增加「剔除预收款项+合同负债后的经营性营运资本」辅助列，并与原 X1 版并列输出危险区名单：2024 年危险区 951 只中 657 只(69%)为负营运资本、其中 249 只(26%)『负 WC 且当年净利>0』是渠道强势特征的假阳性（与 ROCE 事故同型：比率量级正常但语义方向反了）
  - 阈值不能照搬：1.10/2.60 是 Altman 用 1990s 美国非制造业定的，A 股中位 4.2833 ⇒ 原阈值下 68%-84% 落安全区；改为横截面分位（后 20% = 危险）或 sw_level1 行业内标准化，并同时保留原始 Z'' 供复核
  - SQL 显式 LEFT JOIN stock_meta 输出 industry / in_financial；剔除写 coalesce(sw_level1,'__NULL__') NOT IN ('银行','非银金融')，裸 NOT IN 会静默丢 370 行无行业标签股票
  - 商誉覆盖数 4,602 改实测 2,766/5,904；「1990s 脏行」改「全库年报 ta<=0 仅 1 行，护栏属防御性写法」
  - 输出 status ∈ {ok, insolvent_equity(bve<0), financial_sector, missing_ebit}，不做截尾、不置空；另输出 x4 的 p1/p99 供分析层自行决定是否在分位前截尾
  - 必须出两版：total_equity 版（含少数股东，主口径）+ 有形权益版（扣 goodwill+intangible），否则商誉型公司在 Z'' 里永远安全；比例上明确 X4 用 total_equity 而非 total_equity_parent
  - 补 K7 三列位：zpp_latest / zpp_5y_median / zpp_10y_median + 各自的 years 计数（roce_* 那套列位直接复用）
  - 派生列登记 derived_snapshot_domains() + update.py 重建触发（K5）
- **证据**：reports/131 §2 总表第 38 行（审查=🟡 需修正，ROCE 同型=低）；reports/131 §3.7 #38（字段映射 exact 3 / approximate 3 / missing 0）；reports/131 §4.2 P1-a（金融股剔除只写在陷阱里）；reports/131 §4.3（派生域没有重建触发是本项目通病）；reports/131 §9.2 P0-7（2018 减值符号）/ P0-8（2020 预收→合同负债，X1 断点）；reports/131 §10.1 K3（护栏掩盖）/ K6（数据年数）/ K7（只给中位数不给现况）；reports/131 §6.1（第一档：Z'' 施工前必做「处理负营运资本+当年盈利的语义反转」）；schools/forensic.json#L84-L129（formula / formula_sql / data_mapping / history_years / traps）；reviews/forensic.json#L22-L37（2024 n=5,805 / 中位 4.2833 / p10=-0.0005 / p90=11.362 / 危险区 951=16.38%；商誉实测 2,766；657/951 与 249 只负 WC）；fields-inventory.md#L8（sw_level1）、#L17（balance_sheet）；app/core/storage/schema.py:792（indicator_ext.ebit DDL 注释）

## #39 Piotroski F 分数（9 项 0/1 基本面改善评分） — `buildable_after_fix`
- **理由**：42 个指标里口径争议最小、分母护栏最齐全的一个（ta/cl/sales 全套 >0，同比强制紧邻上年），字段 36 处全部真实；但落地前必须修 3 件事：f7 的 funding_events 枚举与送转污染需实测钉住、9 个 CASE 的 ELSE 0 会把「无法计算」静默记 0 分、金融股被 WHERE 静默剔除而输出层不可见。
- **分母体检**：F 有 6 个分母，是 6 个指标里护栏最全的一条：f3/f5/f9 分母 balance_sheet.total_assets（本期与上期都 >0）、f6 分母 total_current_liabilities（>0）、f8 分母 income_statement.revenue（>0）。R4 四问：①分母精确列名=total_assets / total_current_liabilities / revenue（+ 各自的 lag 值，且强制 (report_date−1YEAR)=lag(report_date) 的紧邻上年）；②会不会≤0=不会，WHERE 已写 ta>0 AND ta_p>0 AND cl>0 AND cl_p>0 AND sales>0 AND sales_p>0；但 f5 的分母虽安全、分子 ltd 大量为 0 会造出「假下降/假持平」；③量级 vs 外部标杆=对得上，F 理论区间 0-9、中位 5、F≥8 占 8.57%，与 Piotroski 原样本同量级（原样本 F≥8 约 10%）；④空值语义=本式把「无法计算」和「无信号」混同了：CASE WHEN <NULL 比较> THEN 1 ELSE 0 END 会让任何 NULL 输入静默记 0 分（reviews/forensic.json#L52 指出；因 WHERE 已挡，2024 实测影响≈0，但金融股整批被挡掉这一事实在输出里不可见）。
- **历史可行性**：1998-2025 共 28 个年报点（1997 年实测只有 3 只，视为不可用）；各年可算只数 1998=962 / 2007=1,859 / 2015=4,465 / 2024=5,796（reviews 实测，survey 声称 965/1,862/4,468/5,800，每年差 3-4 行）→ 完全可做「10 年 F 中位数」与「F 同比改善」序列。K6 扣一期：F 本身已是同比指标，每个值吃掉 2 个年报点（t 与 t−1），所以「10 年 F 中位数」实际需要 2015-2025 共 11 个年报点，首年可用值出现在 1999 年（用 1998 做基期）。断点：①2020 新收入准则（运费入营业成本）→ f8；②2018 研发费用单列（rd_expenses 从管理费用拆出，2015 年 86 条/2017 年 3,932 条/2018 后约 94%）→ 影响 M 的 SGAI(#40)，对 F 无直接影响；③2021 新租赁准则 → f5 已用「长期借款+应付债券」规避，但 lease_liabilities 仍计入 total_current_liabilities？需实测（P3 探针覆盖）；④FY2025 cash_flow_indirect/cash_flow_activity 半空壳（§9.2 P0-3）不影响 F（只用直接法 cf_from_operating）。
- **通病标签**：T3, T4
- **ROCE 同型**：否
- **必修项**：
  - ★ 9 个 CASE 全部改成「先判 NULL 再判 0/1」，并输出 f_valid_n（可算信号数）与 f_score；现在的 ELSE 0 会把「无法计算」静默记成 0 分（report §4.1 T4 同型：静默失效）
  - 输出层带 in_financial / industry 标记：现在 99 只金融股被 WHERE 静默剔除，样本减少在筛选界面完全不可见（contributes to P1-a）
  - f7 落地前用 P5 探针把 funding_events.event_type 的真实取值集合与 2024 年只数钉死（代码侧枚举已确认为 ipo/a_placement/rights，但表内实际取值与齐全度必须实测）；如果事件表早年缺失，「未查到事件」必须记「无信号」而不是「未增发」
  - 修正 survey 的两处编造事实：balance_sheet_ext 是 6 期 27,303 行（不是 1 期 5,477 行）；lag 为空是 1,523 行（不是 152 行，132 行不连续那条是准的）；2024 年 n 应为 5,796（不是 5,800）
  - f8 在 2020 年加断点标记（新收入准则运费入营业成本，全市场毛利率 −1.28pct），或改用「剔除运输费的毛利率」辅助列
  - f5 必须输出 ltd_basis 标记 ∈ {ok, both_null, both_zero}，否则「长期借款+应付债券都是 0」的公司会拿到恒定的 0 分而不是「不适用」
  - 补 K7 三列位：f_score_latest / f_score_5y_median / f_score_10y_median；并明确分布极窄（中位 5、p10 3、p90 7）不要用 F≥7 这类绝对阈值
  - 派生列登记 derived_snapshot_domains() + update.py 重建触发（K5）；F 依赖 lag，快照重发必须整体重算而不是增量
- **证据**：reports/131 §2 总表第 39 行（审查=🟡 需修正，ROCE 同型=低）；reports/131 §3.7 #39（字段映射 exact 6 / approximate 4 / missing 1）；reports/131 §6.1（第一档：Piotroski F 是 42 个里口径争议最小的一个；施工前必做=修正 balance_sheet_ext 行数引用 + 确认 9 项历史覆盖率）；reports/131 §4.1 T4（护栏静默失效）/ §4.2 P1-b（2020 新收入准则断点）/ P1-c（样本量夸大）；reports/131 §9.2 P0-3（FY2025 明细两表半空壳）；§10.1 K1/K5/K6；schools/forensic.json#L135-L210（formula / formula_sql / data_mapping 11 条 / traps ⑥ 条）；reviews/forensic.json#L40-L54（2024 n=5,796/均值 5.199/F≥8=497 只；balance_sheet_ext 实测 27,303 行 6 期；lag 为空 1,523 行）；reviews/forensic.json#L48（1997 年仅 3 只，逐年只数与声称各差 3 行）；fields-inventory.md#L17,L20,L23（balance_sheet / income_statement / cash_flow）、#L59（share_capital_history）、#L65（funding_events）、#L89（indicator_ext）、#L113（balance_sheet_ext）；app/core/storage/schema.py:541（event_type 枚举注释）、:2166（accruals ALTER v37）；app/core/indicators/calculator.py:1410 与 app/core/update.py:1454（event_type IN ('ipo','a_placement','rights') 两处硬编码）

## #40 Beneish M 分数（8 变量财务舞弊识别） — `buildable_after_fix`
- **理由**：11 处字段全部真实（含最可疑的 indicator_ext.accruals、cash_flow_indirect 折旧两列），语义分档诚实、分母普遍有 nullif；但必须先修 4 件事：AQI 分母 (1−(CA+PPE+证券)/总资产) 无零保护会产出 NaN/Inf 并被排序推到「最可疑」端、DEPI 在 FY2025 会因间接法折旧列被 P0-3 打空而静默换轨、M>−1.78 的只数写错（607→669）、毛利率≤0 的 135 只被静默剔除。
- **缺列/缺数据**：balance_sheet.gross_ppe（固定资产原值）；balance_sheet.accumulated_depreciation（累计折旧）
- **分母体检**：8 个变量共 11 个分母，绝大多数有护栏：balance_sheet.total_assets（ta>0 且 ta_p>0）、income_statement.revenue（sales>0 且 sales_p>0）、毛利率 gm（gm>0）、折旧 dep（nullif）、dep+ppe（nullif）。★ 唯一漏网的是 AQI 的分母：`1 − (CA + PPE + 证券)/总资产`，当三项之和恰等于总资产时该式为 0 → aqi = x/0，以及上期 aqi_den_p=0 → 除零。reviews 实测全库 118 个 NaN + 124 个 Inf（2024 年 3 个），而 DuckDB 把 NaN 排成最大值 ⇒ 算分位/排名时这些「不可算」会被推到「最可疑」端，与 ROCE「异常量级先怀疑口径」的教训同型。另 DSRI/SGAI 也各有 3/9 行算出 NaN/Inf，survey 的 8 条 trap 一字未提。R4 四问：①分母精确列名=total_assets / revenue / gm / dep / (dep+ppe) / 【aqi: 1−(ca+ppe+sec)/ta，无保护】；②会不会≤0=不会，但 aqi 分母会 =0；③量级 vs 外部标杆=对得上，2024 年 p50=−2.4869，与 Beneish 原样本非操纵组均值约 −2.5 一致，M>−1.78 占 11.83%，量级健康；④空值语义=三态混同：gm≤0 的 135 只是「数学不成立」、dep 缺失（间接法折旧 2016 年 68%/2020 年 77%）是「没抓到」、aqi NaN 是「分母为 0」—— 现状一律输出 NULL/NaN，下游无法区分。
- **历史可行性**：8 变量全齐受 DEPI 约束：1999-2025 共 27 个年报点，各年可算只数 1999=876 / 2007=1,450 / 2013=2,488 / 2015=2,686 / 2020=3,917 / 2024=5,133 / 2025=4,891（reviews 实测，与 survey 声称差 ≤8 行）。K6 扣一期：M 本身是同比指标（每个值吃 t 与 t−1 两期），做「10 年 M 中位数」实际需要 2015-2025 共 11 个可算点，即 2014 年起要有 12 期原始报表。断点：①2018-2019 应收合并列报 → DSRI 两年口径性突增（survey traps ③）；②2018 研发费用单列 → SGAI 全市场性下移，跨 2018 不可比（traps ④）；③2020 新收入准则（运费入营业成本）→ GMI；④2021 新租赁准则 → LVGI 已通过「不含 lease_liabilities」规避；⑤★ 新增发现：FY2025 cash_flow_indirect 18/35 列为空（report §9.2 P0-3，CSMAR cutoff 2025-03-31 后东财 F10 只写 9 个字段）→ 若 fixed_asset_depreciation 在 2025 全空，DEPI 会静默回退到 indicator_ext.depreciation_amortization（含无形摊销+使用权资产折旧），同一条时间序列换轨（用 sql/forensic_40.sql 的 P4 探针验证）；⑥2013-2020 覆盖率 56%-77% 漂移 ⇒ 分位随覆盖率移动（T5），历史序列必须同时输出「可算只数/年报只数」。
- **通病标签**：T4, T5
- **ROCE 同型**：否
- **必修项**：
  - ★ AQI 分母加零保护：`nullif(1-(ca+coalesce(ppe,0)+sec)/ta, 0)`；同时给 dsri/gmi/aqi/depi/sgai/lvgi 各列加 isfinite 判定，输出 m_status ∈ {ok, missing_dep, nonfinite, gm_le0}，把 118 NaN + 124 Inf 显式隔离（现状 NaN 会被排序推到最可疑端）
  - ★ 修 DEPI 换轨：在 M 的历史序列里固定折旧口径（要么全程用 cash_flow_indirect 折旧、缺则 NULL；要么全程用 indicator_ext.depreciation_amortization），并在 DEPI 上加 dep_source ∈ {ci, fallback} 列；FY2025 直接受 report §9.2 P0-3 影响
  - 「M>−1.78 占 11.8%（607 只）」改 669 只（11.83%）；「毛利率≤0 约 250 只」改实测 135 只；cost_of_revenue 覆盖 5,734 改 5,809
  - output 必须带 in_financial 标记并把金融股按 sw_level1 显式剔除（银行 cost_of_revenue/inventory 全空，survey traps ⑦ 写了但 SQL 没实现，属 §4.2 P1-a）
  - 每年同时输出「可算只数 / 当年年报只数」，覆盖率漂移（2013-2020 在 56%-77%）必须可见，否则分位不可跨年比较
  - AQI 的 sec 用「交易性金融资产+长期股权投资」会把长期资产混入短期有价证券（2016 年 trading_financial_assets 只有 1,000/5,085）→ 要么改名为「A 股适配口径」不要叫 exact，要么补短期投资类科目
  - 补 K7 三列位：m_score_latest / m_score_5y_median / m_score_10y_median；舞弊识别尤其要看「恶化方向」而不是单点绝对值
  - 派生列登记 derived_snapshot_domains() + update.py 重建触发（K5）；M 依赖 13 个 lag 值，快照重发必须整体重算
- **证据**：reports/131 §2 总表第 40 行（可算=partial，审查=🟡 需修正，ROCE 同型=低）；reports/131 §3.7 #40（字段映射 exact 3 / approximate 6 / missing 1）；reports/131 §5 G5（本指标是 G5「无 gross PPE」的真实受害者之一）；reports/131 §4.1 T4/T5、§4.2 P1-a/P1-b/P1-c；reports/131 §9.2 P0-3（FY2025 明细两表半空壳 → DEPI 换轨的根因）；§10.1 K1/K6/K8；schools/forensic.json#L216-L285（formula / formula_sql / data_mapping 10 条 / traps ⑧ 条）；reviews/forensic.json#L57-L71（2024 n=5,653、DEPI 子集 5,140、三处 p10/p50/p90 精确；M>−1.78 实测 669 只；118 NaN + 124 Inf；aqi 分母无零保护）；fields-inventory.md#L17（balance_sheet 48 列，无 gross PPE/累计折旧）、#L20、#L80（cash_flow_indirect 38 列）、#L89（indicator_ext 建表 23 列）；app/core/storage/schema.py:2166（v37 ALTER 元组）（v37 ALTER 追加 accruals = indicator_ext 第 40 列后的真实来源）；app/core/indicators/extended.py:348（accruals = (net_profit − cf_from_operating)/total_assets）

## #41 Ohlson O 分数（9 变量破产概率 Logit 模型） — `buildable_after_fix`
- **理由**：9 个变量全部有真实列、SIZE 的单位近似已显式声明、O>0.5 占比 4.84% 与 Ohlson 原样本约 5% 吻合（量级可信）；但必须先修 3 件事：①history_years 声称「1995-2025 共 31 个年报点」是编的（1995 年 0 个有效值，真实 1998-2025 共 28 点）；②SQL 不过滤 NULL，结果集里 4,928 行（4.7%）o_score 全空；③CHIN 的「无法计算」被写成 ELSE 0、TLTA>2 的极端值不截尾也不标 status。
- **缺列/缺数据**：宏观价格平减指数（GNP/CPI/deflator，全库零命中）
- **分母体检**：6 个分母：SIZE 分母是常量 1e6（无风险）；TLTA/WCTA/NITA 分母=balance_sheet.total_assets（WHERE ta>0）；CLCA 分母=total_current_assets（WHERE ca>0）；FUTL 分母=total_liabilities（WHERE tl>0）；CHIN 分母=|ni|+|ni_p|（有 >0 保护）。R4 四问：①分母精确列名=total_assets / total_current_assets / total_liabilities / |ni|+|ni_p| / 常量 1e6；②会不会≤0=都挡住了；真正的问题是「分子侧极端」——TLTA>2（总负债 > 2×总资产）时 6.03·TLTA 直接给出荒谬 O，2020 年 max o_score 实测 1,359、2024 年 17.8，不截尾也不标 status；③量级 vs 外部标杆=对得上，O>0.5 占 4.84% 与 Ohlson 原样本约 5% 几乎一致（这同时反证了 ln(总资产/1e6) 这个单位选择落点正确）；但绝对值只在「同 SIZE 定义 + 同一年」内可比，通胀会让全市场 O 逐年漂移；④空值语义=三态混同：金融股 CA/CL 为 NULL 是「结构性不适用」、1995-1997 年 cf_from_operating 没抓到、CHIN 双零是「数学不成立」—— 现状一律静默为 NULL 或 0，且 SQL 末尾不过滤 NULL，导致结果集 105,022 行里 4,928 行（4.7%）全空。
- **历史可行性**：★ survey 声称「1995-2025 共 31 个年报点，覆盖极高」是编的。reviews 实测：1990-1995 年 o_score 100% 为 NULL（1995 年 813 行 / 0 个有效值），1996 年仅 1 个、1997 年仅 4 个，首个可用年份是 1998（987/1,177）→ 真实为 1998-2025 共 28 个年报点。K6 扣一期：O 依赖 INTWO/CHIN 两个同比项（每个值吃 2 期），做「10 年 O 中位数」实际需要 2015-2025 共 11 个可算点、即 2014 起 12 期报表；20 年序列（2006-2025）在数据上可行。断点：①2015 年前 cash_flow 覆盖低（cf_from_operating 最早 1996-12-31 且早年极稀）→ FUTL 在早年大面积 NULL，不能靠 1995-1997 凑年数；②2020 新收入准则改 WCTA/CLCA（预收→合同负债，§9.2 P0-8）；③2021 新租赁准则同时抬高负债与使用权资产 → TLTA/WCTA 跳变；④2018 减值符号翻转（P0-7）不影响本式（不用减值列）；⑤FY2025 现金流半空壳（P0-3）对 FUTL 有风险 —— 但 2025 年 cf_from_operating 在直接法表里仍有 5,624/5,625，暂不构成断点（P2/P5 探针验证）。
- **通病标签**：T4, T5
- **ROCE 同型**：否
- **必修项**：
  - ★ history_years 从「1995-2025 共 31 个年报点」改为「1998-2025 共 28 个年报点」（1995 年 813 行 0 个有效值、1996 年 1 个、1997 年 4 个，属编造）
  - ★ SQL 末尾加 WHERE o_score IS NOT NULL（或 WHERE all inputs non-null），避免输出 4,928 行全 NULL（占 4.7%）；下游做 count(*) 覆盖率统计时会被这些行抬高
  - INTWO 的 lag 必须加「紧邻上年」校验：(report_date − INTERVAL 1 YEAR)::DATE = lag(report_date)，否则停牌/重组公司会拿 5 年前的亏损判定连续亏损（与 #39/#40 的口径对齐）
  - CHIN 的 CASE … ELSE 0 改成先判 NULL 再判 0，输出 chin_status ∈ {ok, both_zero, missing_prior}；「双零无法计算」不能当成「无变化」（T4）
  - 加 o_status 标注 TLTA>2 / OENEG 等极端杠杆情形，保留原始 O 不截断、不设上限（K3：用户裁决「极值要么是对的要么是错的，对的就该显示」）；同时输出 o_pct_rank 供跨年比较，绝不用原始 O 做时间序列
  - SIZE 的单位必须写死在文档与 field_units 里（ln(总资产/1e6)），并注明「缺 GNP 平减指数、绝对值不可跨年比较、只看排序」
  - output 带 in_financial 标记；金融股按 sw_level1 显式剔除，不要只依赖 ca>0 的巧合（§4.2 P1-a）
  - 补 K7 三列位：o_score_latest / o_score_5y_median / o_score_10y_median + o_pd 分位秩
  - 派生列登记 derived_snapshot_domains() + update.py 重建触发（K5）；O 依赖 2 个 lag，快照重发必须整体重算
- **证据**：reports/131 §2 总表第 41 行（审查=🟡 需修正，ROCE 同型=低）；reports/131 §3.7 #41（字段映射 exact 7 / approximate 2 / missing 1；history_years 的 31 年声明）；reports/131 §5 G1（没有 GDP/宏观序列，全库零命中）；reports/131 §4.1 T4/T5、§4.2 P1-a/P1-c（样本量夸大）；reports/131 §9.2 P0-3（FY2025 现金流半空壳）、P0-7（2018 减值符号）、P0-8（2020 预收→合同负债）；reports/131 §10.1 K3/K6/K8；§10.3 步骤 1/4/5/6；schools/forensic.json#L291-L360（formula / formula_sql / data_mapping 10 条 / traps ⑦ 条）；reviews/forensic.json#L74-L88（2024 中位 −2.8612 / p10 −5.2637 / p90 −0.4429 / O>0.5=281 只 4.84%；1995 年 813 行 0 个有效值；1998 年 987/1,177；4,928 行全空；2020 max=1,359）；fields-inventory.md#L17（balance_sheet）、#L20（income_statement）、#L23（cash_flow）、#L55-L56（treasury_yield_curve / fx_rate_daily 均非价格指数）；app/core/storage/schema.py（grep -niE 'deflator|gnp|cpi_|gdp' 零命中 → 平减指数缺口为真）

## #42 Montier C 分数（6 项造假信号计数） — `buildable_after_fix`
- **理由**：6 个信号全是 0/1 计数、不存在分母符号翻转，字段 10 处全部真实；但交付 SQL 与它自己的陷阱文案自相矛盾：6 个 CASE 全是「NULL → ELSE 0」，把「无法计算」静默记成「无信号」（2024 年 95 行 COGS≤0 全部被记 c3=0，c_score NULL 数 = 0），等于对贸易类/空壳公司——造假最高发的群体——系统性放水；另 c5 只有净 PPE、c4 漏掉合同资产、c6 的 10% 绝对阈值在 A 股不可比。
- **缺列/缺数据**：balance_sheet.other_current_assets（其他流动资产，汇总列）；balance_sheet.gross_ppe（固定资产原值）
- **分母体检**：6 个信号里有 4 处除法：c2 分母 sales/sales_p、c3 分母 cogs/cogs_p、c4 分母 sales/sales_p、c5 分母 (dep+ppe)/(dep_p+ppe_p)；c1/c6 是纯比较无分母。所有分母都用了 nullif，不会除零爆表，也不会符号翻转（销售额与成本均为正）。R4 四问：①分母精确列名=income_statement.revenue / cost_of_revenue、balance_sheet.fixed_assets（配 cash_flow_indirect 折旧）；②会不会≤0=nullif 挡住 0，但「挡住」的方式是让整项变 NULL 再被 CASE…ELSE 0 吃掉 → 变成「无信号」而不是「无法计算」（这是本指标的核心缺陷，2024 年 95 行 COGS≤0 全部 c3=0）；③量级 vs 外部标杆=对得上，C 理论区间 0-6、均值 2.512、中位 2、C≥5 占 7.81%，量级健康；④空值语义=完全混同：prepayments/other_receivables 用 coalesce 0 让「没抓到」=「0」；cogs=0 让 c3 不可算但被记 0；dep 缺失时静默回退到另一种折旧口径。必须输出 c_valid_n 并把 c_score 只对 c_valid_n=6 的行做排名。
- **历史可行性**：6 个信号里只有 c5 依赖折旧（cash_flow_indirect），其余 5 个从三张主表即可算 → 整体 1999 年起可用（1998 年折旧列为空）。各年可算只数 reviews 实测：2015=2,792（survey 称 2,737）/ 2020=4,109（survey 称 3,934，差 175 只 4.5%）/ 2024=5,380（与 survey 一字不差）。K6 扣一期：C 的 6 项里 5 项是同比（每值吃 2 期）⇒「10 年 C 中位数」需 2015-2025 共 11 个可算点、12 期原始报表。断点：①2018-2019 应收合并列报 → c2 口径跳；②2018 研发费用单列对 c4 无影响但影响 M 的 SGAI；③2020 新收入准则（运费入营业成本）→ c3 的 COGS 分母口径跳，同时 contract_assets 才是新准则的产物 → c4 在 2020 年前后不可比；④2021 新租赁准则 → c5 的折旧口径（使用权资产折旧进 indirect 表）；⑤★ 新增：FY2025 cash_flow_indirect 18/35 列为空（report §9.2 P0-3）→ 若 fixed_asset_depreciation 在 2025 全空，c5 会静默回退到 indicator_ext.depreciation_amortization（含无形摊销+使用权资产折旧），c5 的 0/1 在 2025 年与 2024 年不是同一把尺（P5 探针验证）。
- **通病标签**：T4, T5
- **ROCE 同型**：否
- **必修项**：
  - ★ 6 个 CASE 全部改成「先判 NULL 再判 0/1」，并输出 c_score 与 c_valid_n（可算信号数）；c_status ∈ {ok, cogs_missing, ar_missing, dep_missing, oca_missing}。现状 CASE WHEN <NULL> THEN 1 ELSE 0 让 2024 年 95 行 COGS≤0 全部 c3=0、c_score NULL 数 = 0 —— 正是 survey traps ④ 自己警告的错误，文案与 SQL 必须一致
  - c_score 的排名/分位只对 c_valid_n=6 的行计算；c_valid_n<6 的行输出原始 c_score 但明确降级（不要让「缺 3 个信号只拿 2 分」的公司看起来比「6 个信号全算 5 分」干净）
  - c4 把 contract_assets（2024 年 2,521 条有值）纳入分子，并把 coalesce(prepayments,0)+coalesce(other_receivables,0) 改成「任一为 NULL 则 c4 不可算」，否则「没抓到」被当成「0」
  - c5 必须输出 dep_source ∈ {ci, fallback} 并固定口径（FY2025 受 §9.2 P0-3 影响会静默换轨）；同时无法解决 gross PPE 缺口，须在字段说明里标注「c5 对重资产老公司系统性不触发」
  - c6 的 10% 绝对阈值改成年度分位或 sw_level1 行业中位数（实测触发率 23%-61% 波动），并同时输出两版（绝对阈值版 + 分位版）供对比
  - 修正 survey 的历史覆盖数：2015=2,792（不是 2,737）、2020=4,109（不是 3,934）、2024 均值 2.512（不是 2.54）
  - output 带 in_financial 标记；金融股按 sw_level1 显式剔除（银行无存货/无 COGS/折旧口径不同），不要只靠 WHERE 巧合（§4.2 P1-a）
  - 「C 高 + PS>2」组合条件若要做，历史 PS 必须用 market_cap_at_report/revenue 自算 —— 先修 689009 的 10 倍市值 bug（§4.3/§9.2 P0-9），否则该股 PS 只有真实值 1/10
  - 补 K7 三列位：c_score_latest / c_score_5y_median / c_score_10y_median；舞弊信号尤其要看「连续两年 C≥5」而不是单点
  - 派生列登记 derived_snapshot_domains() + update.py 重建触发（K5）；C 依赖 9 个 lag 值，快照重发必须整体重算
- **证据**：reports/131 §2 总表第 42 行（可算=partial，审查=🟡 需修正，ROCE 同型=低）；reports/131 §3.7 #42（字段映射 exact 4 / approximate 3 / missing 2）；reports/131 §4.1 T4（护栏拍脑袋/失效）+ T5（样本下限）；reports/131 §4.2 P1-a（金融股剔除只写在陷阱里）/ P1-b（2020 新收入准则断点）；reports/131 §4.3 + §9.2 P0-9（689009 市值错 10 倍 → 历史 PS 自算路径被污染）；reports/131 §9.2 P0-3（FY2025 间接法半空壳 → c5 换轨）；§10.1 K1/K3/K6/K8；schools/forensic.json#L366-L429（formula / formula_sql / data_mapping 10 条 / traps ⑦ 条）；reviews/forensic.json#L91-L105（2024 n=5,380 精确、C≥5=420 只 7.81%、均值实测 2.512；2015=2,792、2020=4,109；95 行 COGS≤0 被静默记 c3=0；c6 逐年触发率 56.3%/61.1%/54.5%/23.3%）；fields-inventory.md#L17（balance_sheet）、#L20（income_statement）、#L23（cash_flow）、#L32（indicator_snapshot.ps_ttm）、#L80（cash_flow_indirect）、#L89（indicator_ext）；app/core/storage/schema.py:786（depreciation_amortization DDL）、:2166（accruals ALTER v37）
