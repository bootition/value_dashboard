---
title: 数据源官方调研、限速校准与并发抓取落地报告（2026-08-07）
status: approved
category: reports
created: 2026-08-07
last-reviewed: 2026-08-07
---

# 数据源官方调研、限速校准与并发抓取落地报告（2026-08-07）

## 1. 官方文档检索结论（联网核实）

| 源 | 官方文档是否声明限流 | 结论 |
|---|---|---|
| BaoStock（baostock.com pythonAPI） | **无任何限流声明**（官方宣传 "free, stable, no rate limits"） | 我们实测的 ~90s 惩罚是**未文档化的隐式服务端节流**；官方只要求断线重登 |
| 腾讯行情 qt.gtimg.cn | 无公开限流文档 | 高频易风控/拒连，无固定 QPS；实测 0.2s 间隔稳定 |
| 新浪 hq.sinajs.cn | 无官方文档 | 社区惯例：单次 ≤100 只、每秒 ≤1 次、需 Referer；价格能力有限 |
| 东方财富 push2his | 无官方文档 | 社区实测阈值：>5 req/s、并发 ≥10、1min≥200/5min≥300 → RemoteDisconnected/封禁（本项目当前 IP 已被断，与 `reports/29` 一致） |
| TDX 协议 | 无官方文档（社区实现按交易阶段限流：盘中 15/盘前盘后 30/休市 60 req/s/连接） | 实测 0.2s 稳定 |

**对上一轮问题的直接回答**：此前未联网核对官方限流声明；官方确实"无声明"，但隐式节流真实存在——这就是为什么必须用**本机实测**定值，而不是信任"官方无限制"。

## 2. 本机校准实验（scripts/calibrate_source_rates.py + evidence JSON）

对 600519 连续抓取，逐源逐间隔统计延迟与惩罚：

| 源 | 间隔 | p50 | max | 惩罚(≥30s) | 结论 |
|---|---|---|---|---|---|
| tencent | 0.2s×10 | 250ms | 266ms | 0 | **价格主源（HTTP、快、可并发）** |
| tdx | 0.2s×10 | 516ms | 547ms | 0 | 备源（TCP 快） |
| baostock | 0.5s×10 | 500ms | 1641ms | 0 | socket 源，0.8s 保守保底 |
| baostock | 0.2s×10 | 891ms | 2172ms | 0 | 高频档延迟升高，不采用 |
| akshare_eastmoney | 0.6s×8 | 219ms | 282ms | 0（8/8 断连） | 当前 IP 被源封，仅留兜底 |
| sina | — | — | — | 8/8 失败 | 价格不支持，仅财务备源 |

证据：`docs/evidence/evidence-source-rates-2026-08-07.json`。

## 3. 落地变更

1. **价格主源切换**：`price_daily` 优先级 `baostock → tencent`，改为 **`tencent → baostock → tdx → akshare_eastmoney`**（config/default.yaml 与 Manager 默认同步）。
2. **按实测定速**：tencent 0.2s、tdx 0.2s、baostock 0.8s（保守）、eastmoney 0.5s、cninfo 1.5s、sina 0.35s。
3. **价格抓取并发化**（`update.py._update_prices_incremental`）：
   - `ThreadPoolExecutor` 并发抓取 raw+qfq（默认 4，`config/update.price_fetch_concurrency` 可调），**DB 写入仍由主线程串行**（保原子提交与单写者）。
   - 单股失败不再中断整体：失败进 retry_list，下次续传（断点续传语义保持）。
   - **BaoStock 加全局线程锁**（socket 协议非线程安全），并发池中自动串行。
4. 进度回调按完成计数（并发乱序安全），4s 轮询展示不变。

## 4. 验证

- 后端全量隔离回归 **452 passed**、1 deselected；Ruff 全绿。
- 价格断点续传测试适配新语义：单股失败=记 retry、已提交股票不重抓（28 项定向 + 全量）。
- 并发路径不改变现有进度/续传/重试契约。

## 5. 预期效果

- 主源切换后单请求 ~0.25s（腾讯）且支持 raw+qfq；并发 4 下吞吐约为原 baostock 串行的数倍。
- 长尾惩罚预期大幅下降（腾讯实测无惩罚；baostock 退居兜底且限速已降频）。
- 未做、可后续迭代：超时兜底（45s 放弃重试）、自适应退避、研究优先名单续传。

## 6. 诚实披露

- 实验样本为单只股票、短窗口；全市场长时运行仍可能有波动，需以正式 job 实际运行速率回看校准。
- 当前正式 job 为旧代码（主源 baostock）；新配置与并发在**下次启动的更新**生效。