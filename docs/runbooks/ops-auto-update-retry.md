---
title: 自动更新与重试运行手册
status: approved
category: runbooks
created: 2026-08-03
last-reviewed: 2026-08-03
---

# 自动更新与重试运行手册

> O2（`reports/41` B1）。自动数据更新在启动时后台运行（PRD §7.3）；
> 本手册覆盖查看/暂停/重试/定向补抓与异常处理。

## 1. 查看状态

```bash
# 控制台查看自动更新状态
vd data auto-update status

# 数据整体状态（就绪/警告/重试/缺失）
vd data status
vd status
```

Web：顶部「数据状态」页可看就绪、`warning_codes`、各数据域最新日期、
重试列表与缺失列表；自动更新运行中页面每 12 秒自动刷新。

## 2. 控制自动更新

```bash
vd data auto-update enable      # 开启（默认）
vd data auto-update disable     # 关闭
vd data auto-update run         # 立即手动触发一轮
vd data auto-update pause       # 暂停
vd data auto-update resume      # 恢复
```

- 更新运行中（`STALE_RUNNING_JOBS` 警告）保存/导出会被暂时禁用，属保护行为。
- CLI 入口为唯一控制面；Web 仅只读展示（PRD §7.3）。

## 3. 重试失败项

- 失败项进入「重试列表」（Web 数据状态页 / `retry-list` 接口）。
- 仅补抓遗漏核心数据的股票：

```bash
vd data replenish_missing_core_data
```

- 定向重抓（危险操作，需 plan confirm）：

```bash
vd data refetch --help     # 查看指定范围语法（M7-问题4 保险动作）
vd data refetch_execute
```

## 4. 手工补历史价格 / 指标

```bash
vd data backfill-prices        # 历史价格回填
vd data compute_indicators     # 重算全市场指标快照（经发布门禁）
```

## 4.1 新数据域（P1-P4 低频研究辅助域）

业务概览 / 国债曲线 / 历史股本链 / 历史研究统计为独立低频域（reports/68），
失败保留旧值并进入独立 retry/missing，绝不阻断价格、财务、筛选或 readiness。

```bash
# 国债曲线（财政部官网，独立限速 0.5s/请求）
vd data treasury-curve --check-only        # 覆盖/队列状态（retry/missing 均按域过滤）
vd data treasury-curve --daily             # 抓取当日曲线并 upsert（未来日期拒绝）
vd data treasury-curve --backfill          # 按 9 个关键期限回填全历史（原子替换）
vd data treasury-curve --backfill --tenors 10,30 --max-tenors 2

# 历史股本链（CNINFO 主链 + 东财交叉核验；自动更新每轮 20 只有界续传）
vd data capital-history --check-only       # 十年窗口覆盖汇总（含 verified 占比）
vd data capital-history                    # 全量回填（有界续传：只处理缺失/陈旧股票）
vd data capital-history --stocks 600519,000001   # 定向回填
vd data capital-history --no-cross         # 跳过东财交叉核验（源风控期间）

# 历史研究统计域（输入指纹驱动原子重建）
# 自动更新在价格/财务/股本/曲线/分红输入变化时自动重建；无需手工执行
```

- 自动更新已包含三域：国债每日刷新（当日已刷新即跳过）、股本链 20 只/轮续传、
  统计域按输入指纹（含分红）变化重建；`partial` 重建不落指纹，失败股下轮自动重试。
- 东财交叉源偶发风控时主链独立成立但 `verified=false`（如实展示，不伪造验证）。

## 5. 异常处理

| 症状 | 处理 |
|---|---|
| 更新卡在 running | `vd data reconcile_jobs` 生成确认 plan → `reconcile_jobs_execute` |
| 部分源失败 | 适配器自动回退链生效；查看 retry 列表，网络恢复后重试 |
| legacy 空 payload | `vd data quarantine_legacy_records` + `_execute`（只隔离不删除证据） |
| 东财行情源被封 | 仅 push2/push2his（行情/逐股信息）被封，IP 级临时封锁；F10 财报/股本/分红源仍可用。价格已回退腾讯/BaoStock/TDX，自动更新无需干预。冷却期至 2026-08-15 勿触碰 push2 系；到期后单次探测（见 `reports/61`） |
| 国债曲线失败 | `vd data treasury-curve --check-only` 查看域内 retry/missing；网络恢复后自动更新重试 |
| 历史股本链失败 | `vd data capital-history --check-only` 查看覆盖与 verified 占比；低于 90% 的窗口 PE/PB 统计不可用（如实缺失） |
| 统计域未更新 | 输入指纹（价格/财务/股本/曲线/分红）未变则不重建，属预期行为 |

## 6. 参考

- `docs/STATUS.md`「已知剩余缺口」第 3-5 条（免费源边界如实披露）。