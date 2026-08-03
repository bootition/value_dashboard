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

## 5. 异常处理

| 症状 | 处理 |
|---|---|
| 更新卡在 running | `vd data reconcile_jobs` 生成确认 plan → `reconcile_jobs_execute` |
| 部分源失败 | 适配器自动回退链生效；查看 retry 列表，网络恢复后重试 |
| legacy 空 payload | `vd data quarantine_legacy_records` + `_execute`（只隔离不删除证据） |
| 东财源被封 | 已自动迁移腾讯/Sina/BaoStock；无需人工干预 |

## 6. 参考

- `docs/STATUS.md`「已知剩余缺口」第 3-5 条（免费源边界如实披露）。