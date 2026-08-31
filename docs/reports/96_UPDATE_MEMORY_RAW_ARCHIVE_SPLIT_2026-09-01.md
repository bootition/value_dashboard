---
title: 更新过程内存治理——原始响应归档冷热分层
status: approved
category: reports
created: 2026-09-01
last-reviewed: 2026-09-01
---

# 更新过程内存治理：原始响应归档冷热分层

## 结论

✅ 已修复。财务明细回填在正式库上的 DuckDB 峰值内存从 **约 28GB** 降至
**约 3GB**（batch=50 的受控实测峰值 1.5GB），回填速率从约 9.8 只/分提升到
约 29 只/分。Schema v16 已应用到正式库。

## 根因

`raw_response_archive` 在正式库已积累 **164,652 行、约 26GB BLOB**（平均
158KB/行，单行最大约 4MB）。DuckDB 对该表任何**新行提交**都会执行主键
唯一性校验并扫描整表：

- 受控实测：插入 1 行小 BLOB 提交，进程峰值 WS 24.5GB / private 26GB，
  耗时约 134s；
- 财务明细回填每股写 3 份原始响应（balance/income/cash），旧实现每个
  50 股批次内先触发一次整表扫描，服务进程因此周期性冲到 18~28GB；
- 这不是 Python 引用泄漏：事务结束后内存会回落到约 2~3GB，但峰值足以
  逼近 32GB 物理内存上限。

## 修复内容

1. **Schema v16 冷热分层**：把既有 26GB 归档表改名为
   `raw_response_archive_history`（仅 catalog 元数据变更，不复制 BLOB）；
   新建空的小型热表 `raw_response_archive` 承接未来写入。
2. **统一读取视图**：`raw_response_archive_all =
   history ∪ active`；数据质量、就绪门禁、维护查询全部改读该视图。
3. **写入路径去 ON CONFLICT**：新增 `archive_raw_response_if_absent()`，
   在单写者锁串行保护下先探测 hash 再普通 INSERT，保持先到先得语义；
   DataInitializer / PriceBackfiller / IndicatorCalculator 三条写路径全部
   切换。
4. **冷归档与备份覆盖**：`archive.py` 与 `BackupManager` 的公共表清单
   加入 `raw_response_archive_history`。
5. **正式库清理**：删除上一轮排查脚本误写入的 10 行
   `fetch_batch_id='noop'` 实验性 source_audit 记录；清理后
   archive_gap / batch_gap / empty_payload 均为 0。

## 验证

- 受控 memtest（正式库，N=5/batch=5）：峰值 WS 0.84GB，20.7 只/分；
- 受控 memtest（正式库，N=50/batch=50）：峰值 WS 1.48GB，29.6 只/分；
- 服务重启后真实自动更新财务明细回填：WS 0.6~3.3GB，稳定约 29~38 只/分；
  本轮缺口 673/673 全部完成，active 归档 3,534 行、history 164,651 行，
  archive_gap / batch_gap / empty_payload 复检均为 0；
- 定向回归 137 passed；Ruff 全绿；
- 完整 S1：674 passed，2 failed。2 项失败在 `HEAD` 干净基线工作树上
  同样复现（`test_complete_period_policy` 与
  `test_p0_3_p0_4_regressions` 的 mixed snapshot/statement report dates
  断言），与本次改动无关。

## 剩余风险

1. 启动时的数据就绪全量核对仍使用 DuckDB 默认约 25GB memory_limit，
   冷核对期间进程仍会出现约 15~21GB 的短时峰值，随后回落。建议下一项
   工作评估为读连接设置有界 `memory_limit`（例如 8~12GB）并实测冷核对
   耗时/统计域重建的影响。
2. DuckDB 文件高水位（约 50GB）仍待离线压缩/重建评估（延续 reports/84
   缺口 #13）；本次分层不复制历史 BLOB，但新热归档表会继续增长，后续
   可按相同方式再次轮转。
