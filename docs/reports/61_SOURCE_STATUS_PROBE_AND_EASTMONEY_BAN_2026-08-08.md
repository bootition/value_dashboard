---
title: 数据源状态探测与东财封禁范围调查（2026-08-08）
status: approved
category: reports
created: 2026-08-08
last-reviewed: 2026-08-08
---

# 数据源状态探测与东财封禁范围调查（2026-08-08）

> 起因：用户对「东财源被封是否永久、是否应放弃 AKShare」的疑问。
> 方法：两轮轻量探测（串行、单请求、间隔≥限流配置），一轮走 akshare 直连，一轮走**生产 AdapterManager 适配器路径**。
> 结论：**封禁仅覆盖东财行情 host（push2/push2his），属 IP 级临时封锁；AKShare 的财报/分红/股本能力当前全部可用，不应放弃。**

## 1. 探测方法（保持轻量）

| 轮次 | 覆盖 | 请求数 | 证据 |
|---|---|---|---|
| A. akshare 直连 | 9 类接口（行情/三表/分红/上市/列表/股本/日历） | 9，间隔 6s | `docs/evidence/evidence-akshare-probe-2026-08-08.json` |
| B. 生产适配器路径 | 7 适配器 × 1 数据类（600519） | 7，间隔=各源限流配置 | `docs/evidence/evidence-sources-probe-2026-08-08.json` |

## 2. 探测结果

### 2.1 AKShare 直连（9 例，7 OK）

| 接口 | Host | 结果 |
|---|---|---|
| 历史行情 `stock_zh_a_hist` | **push2his** | ❌ RemoteDisconnected |
| 个股信息 `stock_individual_info_em` | **push2** | ❌ RemoteDisconnected |
| 资产负债表 `stock_balance_sheet_by_report_em` | emweb（F10） | ✅ 102 行 |
| 利润表 `stock_profit_sheet_by_report_em` | emweb（F10） | ✅ 102 行 |
| 现金流量表 `stock_cash_flow_sheet_by_report_em` | emweb（F10） | ✅ 98 行 |
| 分红 `stock_dividend_cninfo` | cninfo.com.cn | ✅ 31 行 |
| 上市日期/股本 `stock_tfp_em` | datacenter-web | ✅ 6 行 |
| 股票列表 `stock_info_a_code_name` | 交易所官方 | ✅ 5,539 行 |
| 交易日历 `tool_trade_date_hist_sina` | 新浪 | ✅ 8,797 行 |

### 2.2 生产适配器路径（7 例，6 OK）

| 适配器 | 数据类 | 结果 |
|---|---|---|
| tencent | price_daily qfq | ✅ 5 行，0.24s |
| baostock | price_daily qfq | ✅ 5 行，0.85s |
| tdx | price_daily raw | ✅ 5 行，0.72s |
| sina | balance_sheet | ✅ 102 行，0.56s |
| cninfo | dividends | ⚠️ **0 行（潜在 bug，见 §3.2）** |
| cninfo_csrc | csrc_industry | ✅ 1 行，0.37s |
| akshare_eastmoney | balance_sheet（F10） | ✅ 102 行，6.06s |

## 3. 发现

### 3.1 东财封禁是 host 级别、IP 级、临时性

- 被封：**push2.eastmoney.com（实时行情/个股信息）、push2his.eastmoney.com（历史行情）**——均为 `RemoteDisconnected`，与 `reports/29`、`reports/58` 一致。
- 可用：**emweb.securities.eastmoney.com（F10 三大报表）、datacenter-web.eastmoney.com（股本/上市）、cninfo.com.cn（分红）**。
- 判定：典型「行情接口 WAF 按 host 计数封 IP」的临时封锁（社区阈值：>5 req/s、并发≥10、1min≥200/5min≥300，见 `reports/58` §1）；非永久、非全站。
- 根因：7 月下旬全市场爬取对 push2/push2his 持续高压（价格 raw+qfq 约 1.1 万次 + 逐股上市信息 5,000+ 次，0.5s 间隔 + 并行脚本），见 `.planning/2026-08-08-akshare-ban-investigation/findings.md`。
- 持续未解原因：封禁窗口数天~数周，且每次探测（8-07 校准 8 次、8-08 探测 2 次）都可能续期。

### 3.2 CNINFO 分红适配器潜在 bug（本次新发现）

- 现象：`cninfo.dividends` 恒返回 0 行、无错误、不触发熔断（manager.py:321 把空数据视为合法）。
- 根因：`_parse_dividend_from_announcement`（cninfo_adapter.py:515-517）要求 `ann["ex_date"]`，但 `_normalize_announcement` 从不填充该字段（注释承认「ex_date 需解析公告 PDF，留给 M8 阶段」）。
- 影响：分红主源 cninfo 实际不可用，数据由回退链 `akshare_eastmoney`（cninfo 分红接口，31 行 OK）或 `baostock`（25 行 OK）填充——**当前回退可用，无数据缺口，但主源路径是死代码**。
- 验证：baostock dividends 600519 全历史 25 行 ✅；akshare dividends_cninfo 31 行 ✅。

### 3.3 其他源状态

- 价格：tencent（0.24s）/baostock（0.85s）/tdx（0.72s）全 OK，主源链 `tencent → baostock → tdx` 健康。
- 财务：sina 102 行（0.56s）✅、akshare F10 102 行 ✅。
- 行业：cninfo_csrc ✅。
- 结论：除东财 push2 系外，全链路无异常；自动更新可正常运行（price_daily 链已不含 akshare，config/default.yaml:26）。

## 4. 封禁时间线（已记录，供后续重试）

| 日期 | 事件 |
|---|---|
| 7-20 ~ 7-25 | 全市场爬取：push2his ~11,000 次 + push2 ~5,000 次（0.5s/次 + 并行脚本） |
| 7-26 | 首个症状：逐股上市日期接口（push2）「因上游限频未全部成功」（`reports/19`） |
| 7-31 | push2/push2his 全部 RemoteDisconnected，重建改用 Sina/Tencent/BaoStock（`reports/29`） |
| 8-07 | 校准实验 push2his 8/8 断连（0.6s 连打，可能续期）（`reports/58`） |
| 8-08 | 本调查：push2/push2his 仍断连；F10/datacenter-web/cninfo/交易所 7/9 OK |

**重试计划**：最后接触 2026-08-08 → 冷却 7 天（至 2026-08-15，期间对 push2 系零请求、勿用浏览器开东财行情页）→ 单次探测：

```bash
uv run --locked python -c "import akshare as ak; df=ak.stock_zh_a_hist(symbol='600519', period='daily', start_date='20260810', end_date='20260814', adjust=''); print(len(df), 'rows OK' if len(df) else 'STILL BLOCKED')"
```

若单次成功 → 恢复时限速 ≤2 req/s、并发 ≤5、≤100/min，东财仅用于无替代接口；若仍断连 → 再冷却 7 天或检查宽带 IP 是否轮换。

## 5. 后续可做（未实施，待用户决策）

1. **熔断器分级**（manager.py:141-142）：对 `RemoteDisconnected` 这类封禁信号长冷却（6-24h）+ 跨重启持久化 + 指数退避，防止反复探测续期。
2. **适配器优先级**：三大报表链 `["sina","tdx","akshare_eastmoney"]` 中 akshare（F10）实测可用且为财务原主源，可评估提回主位。
3. **CNINFO 分红 bug 修复评估**：要么实现 PDF ex_date 解析，要么把 `ex_date` 要求降级为「公告日 + 后续校验」，或明确依赖回退链并披露。

## 6. 证据清单

- `docs/evidence/evidence-akshare-probe-2026-08-08.json` — akshare 直连 9 例
- `docs/evidence/evidence-sources-probe-2026-08-08.json` — 生产适配器路径 7 例
- `.planning/2026-08-08-akshare-ban-investigation/findings.md` — 封禁根因与时间线详述（会话产物）
