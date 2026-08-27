---
title: 东财交叉核验补全报告（STATUS 缺口 #7 遗留步骤关闭，2026-08-13）
status: approved
category: reports
created: 2026-08-13
last-reviewed: 2026-08-13
---

# 东财交叉核验补全报告

## 1. 裁决

**PASS（全面核验数据遗留步骤完成）。** STATUS 缺口 #7 的四项待办全部落地；
正式库 5,542 只上市股票中 **5,207 只沪深全部经东财 F10 交叉核验**、
334 只北交所如实记录"无东财交叉源"、1 只（002731 *ST萃华）主链缺失如实披露。
链上 verified 点 203,149 个（5,175 只股票）。统计域口径经用户决策维持主链口径
（verified 仅披露，`reports/74` 决策延续）。

## 2. 待办逐项关闭（STATUS 缺口 #7）

| 待办 | 落地 |
|---|---|
| ① `cross_status` 落盘缓存表 | `capital_cross_cache` 增加 `cross_status`/`error` 列（含旧表迁移）；每次交叉尝试都落盘：verified（取到事件）、empty（空事件集）、error（源异常，含原因，批次审查可见）；error/empty 绝不覆盖既有 verified 证据（防瞬时风控抹掉核验） |
| ② 批大小 × 冷却时长安全组合 | 受控探测 150 只连发（1.2s/只）无一风控；全量核验采用批 50 + 批间冷却 30-60s，全程约 5,400 次请求无一次风控。`update_many` 内置批间冷却 + 连续 8 次交叉错误/16 次空响应中止保护 |
| ③ 批次审查固化 | `vd data capital-history --check-only` 返回交叉核验审计视图（by_status/error_samples/empty_no_cross_source/队列剩余）；`--verbose` 逐股进度 |
| ④ 收紧评估与决策 | 10y 窗口 verified 覆盖 ≥90% 仅 3,264/5,553 只（平均 83.6%）vs 主链口径 5,538 只。低覆盖成因：北交所无源（334 只 0%）、新股主链点少、老股约半数定期锚点 ±10 天无东财变动事件按 P4-7 保守 fail-closed。**用户决策：维持主链口径 + verified 披露**，不收紧、不重建统计域 |

## 3. 过程中发现并修复的根因（P1 级）

| 发现 | 修复 |
|---|---|
| **vd.bat/start.bat 用系统 Python（akshare 1.18.64）运行数据路径**：`stock_zh_a_gbjg_em` 无 SECUCODE 归一化（裸代码 → `result=None` → `'NoneType' object is not subscriptable`），且 `pageSize=20` 截断历史——08-12 "46-50 次后风控"症状实为该缺陷+熔断叠加，而非纯风控 | `vd.bat`/`start.bat` 优先项目 venv（`.venv\Scripts\python.exe`，uv.lock 锁定 akshare 1.18.81：SECUCODE 归一化 + 500 条分页全量）；仅无 venv 时回退系统 Python。回归测试同步更新（含 ASCII/CRLF 契约） |
| **北交所（BSE）无东财 F10 交叉源**：920xxx 全部返回空/异常，且异常触发 cninfo_capital 适配器熔断（连续 5 次 → 5 分钟），殃及沪深股票交叉请求 | `_has_eastmoney_cross_source`：BSE 股票不请求东财，如实记 empty（error 标注 no_cross_source:bse），不再入 retry 循环 |
| 08-12 遗留 50 只交叉缓存为 1.18.64 抓取（可能截断） | 过期这 50 行（TTL 前移 8 天），全部以 1.18.81 全量重核验 |

## 4. 新增能力与代码

- `capital.py`：`update_stock(cross_only=...)` 核验补强模式（主链不重抓，读库重算 verified）；
  `update_many(batch_size/batch_cooldown_seconds)` 批量节奏；`cross_audit()` 审计视图；
  `_coverage_all` 改为单次 SQL 聚合（check-only 从分钟级降至秒级）；
  error 行 30 分钟重试冷却；`cached_stale` 回退（源瞬时失败复用旧核验证据）。
- CLI：`--cross-only` / `--batch-size` / `--batch-cooldown` / `--verbose`。
- `statistics.py`：输入指纹追加 verified 计数（verified 变化触发统计重建同步披露）。
- 适配器交叉源限速 1.5s → 1.2s（探测实测依据）。

## 5. 正式库核验结果（2026-08-13）

| 指标 | 值 |
|---|---|
| 交叉核验（verified 缓存） | 5,207 只（沪深全覆盖） |
| 北交所无交叉源 | 334 只（如实 empty） |
| 主链缺失披露 | 1 只（002731，CNINFO 源最新锚点陈旧 fail-closed → retry） |
| 链上 verified 点 | 203,149 个 / 5,175 只 |
| 核验后 error 行 | 0（全部清尾） |
| 全程东财风控 | 0 次（约 5,400 次请求，批 50 + 冷却 30-60s） |
| 统计域 | 维持 version 3 主链口径（用户决策）；指纹含 verified 计数，下次自动更新自然同步 |

## 6. 门禁

| 门禁 | 结果 |
|---|---|
| S1 隔离回归 | **574 passed**（新增 13 项：落盘/回退/冷却/cross_only/节奏/中止/审计/BSE/启动器 venv 契约） |
| Ruff | PASS |
| 前端 lint / Node / 组件 / build | PASS / 55 / 40 / PASS |
| 正式库端到端 | 交叉核验全量执行 + 审计视图 + 抽查双源数值匹配（同日期 rel≈0、冲突正确 fail-closed） |

## 7. 诚实披露

1. verified 覆盖（10y 窗口）平均 83.6%：老股约半数定期锚点附近无东财变动事件，
   按 P4-7 保守口径 fail-closed——这是语义选择（宁缺勿错），非数据缺口；
   主链口径覆盖 99.93% 不受影响。
2. 北交所 334 只无东财交叉源：其历史股本链仅 CNINFO 主链口径成立，verified 恒 False（如实展示）。
3. 002731 主链缺失：CNINFO 源该股最新锚点陈旧（fail-closed → retry），与交叉核验无关，持续披露。
4. 东财 push2/push2his 冷却期（至 2026-08-15）未触碰；本任务只使用 F10 股本接口。
5. 本次报告取代 STATUS.md 缺口 #7 的待办清单；`reports/74` 其余裁决不受影响。
