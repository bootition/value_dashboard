---
status: approved
category: reports
created: 2026-11-16
last-reviewed: 2026-11-16
related: reports/126
---

# 收尾：把 reports/126 §8 剩下的代码项全部做掉（2026-11-16）

用户要求："前端先不着急，其他的是什么东西？干嘛不全部搞完？"——本报告是这一轮的收尾记录。

## 1. 结论（TL;DR）

| # | 之前剩下的东西 | 是什么 | 本轮处置 |
|---|---|---|---|
| 1 | 6 只股票股本链不准 | 股本历史停在旧日期，之后发生的**股权激励 / 可转债转股 / 回购注销**不在 `xdxr` 里，所以链尾永远追不上当前股本；其中 `689009` 是**CDR 存托凭证单位差 10 倍**（整条链都是 1/10） | 新增 `scripts/align_share_capital_tail.py` 并执行：单位错位整链重标定、链尾落后补一行；**偏离 >1% 的在市股票 6 → 0** |
| 2 | 打包副本 `app/resources/config/default.yaml` 没同步 | wheel 安装且没有仓库 `config/` 时用的**兜底配置副本**，里面还留着我已删除的 4 个配置键 | 已删掉同样 4 个键，YAML 解析校验通过 |
| 3 | 11 处注释引用不存在的报告编号 | 代码注释写 `reports/120 §5 J9`，但 `reports/120` 没有 J 编号（J1–J12 实际在 `reports/122`） | 11 处全部改正，`grep "J[0-9]" app/` 现在 100% 指向 `reports/122` |
| 4 | J7「覆盖率低就隐藏」的代码分支没有测试 | 三季报不披露现金流量表补充资料 → 折旧/EBITDA 族覆盖率 <1%，界面会隐藏它们；**但"隐藏"这一步没有任何回归用例** | 新增用例：构造 33% 覆盖率 → 断言进 `unavailable(reason=not_available_for_latest_period)` 且不出现在可选列表 |
| 5 | 仓库根 `build/` 目录残留旧副本 | setuptools 构建产物（36 MB），里面有一份**旧的 `config/default.yaml` 和旧的 PDF 模块**，会让 grep 审计误判 | 已删除（未被 git 跟踪） |

## 2. 股本链对齐：判据与写法

新增 `scripts/align_share_capital_tail.py`（默认 dry-run，`--yes` 才写；写锁 + `COPY` 备份 + 单事务）：

1. **整链单位错位** → 判据：该股**所有**历史行的 `stock_meta / 行值` 都落在同一整数倍 ±6% 内（倍数只接受 10 / 100 / 0.1 / 0.01）。命中则把整条链乘该倍数，`change_reason='unit_rescale:<倍数>'`。
   - 实例 `689009`（九号公司 CDR）：整链 73,584,850 量级，meta 735,848,500（10 倍）。
2. **只有链尾落后** → 在最新价格日插一行 `total_shares = stock_meta`，`change_reason='meta_align'`、`source='meta_align'`。
   - **历史段一行不动**，不是伪造历史，只是把"当前股本"记进链尾。

执行结果：

```
链尾与 stock_meta 偏离 >1% 的在市股票：6 只
  689009：整链单位错位 → 乘 10
  600461 / 688235 / 300429 / 301297 / 300299：链尾落后 → 各插一行
完成：处理 6 只，剩余偏离 0 只
```

备份：`.planning/maintenance-backup-share-align-20261116_210910/`。

**复跑幂等**：第二次执行时 `_DRIFT_SQL` 返回 0 行，直接退出。

## 3. 验证

- `share_capital_history` 偏离 >1% 的在市股票：**176 →（上轮重抓）6 →（本轮对齐）0**；
- 重算指标后抽查：`601398` 总市值 = 9.05（最新价）× 356,406,257,089 = **3,225,476,626,655.45**，与库内一致（此前 8.85 是上一交易日价格，属正常行情更新，不是口径变化）；
- `grep -rn "J[0-9]" app/ --include=*.py | grep -v reports/122` → **0 命中**；
- `app/resources/config/default.yaml` YAML 解析通过，4 个已删键 0 命中；
- 全量 S1 结果见 `.planning/s1-r8-20261116.log`。

## 4. 现在真正只剩什么

1. **前端三块界面**（用户明确说先不急）：
   - `AhMetricsCard`（H 股市值 / AH 溢价 / 两地股息率）；
   - 筛选页收到 409 `snapshot_values_untrusted` 时的阻断提示；
   - `/api/screening/indicators` 新增的 `untrusted_all` / `warning_codes` 两个字段。
2. **`NULL_REASON_MISSING` 仍有 53 条（P2 披露级）**：扩展域/资产负债域有 NULL 但还没登记原因。这是"宁可不写、不猜"（D37）的直接结果，要降只能逐域补判定。
3. **`FIELD_COVERAGE_LOW` 1 条 P1**：`roic` 84.48%，其中 815 家本来就没有有息负债（结构性不可算）。
