---
title: 离线重建降空间与分红融资比总股本口径阻塞
status: approved
category: reports
created: 2026-09-04
last-reviewed: 2026-09-04
---

# 离线重建降空间与分红融资比总股本口径阻塞

## 离线重建结果

- 使用 `scripts/rebuild_duckdb.py` 完成 export → import → verify → swap：
  - 主库：**41GB → 7.8GB**；
  - 旧库保留为 `data/valuedashboard.duckdb.old-20260904013322`
    （连同其 WAL 复制为同基名 `.wal` 备用回滚）；
  - verify 全部表指纹一致（source_audit_archive 按外部 Parquet 单独核验）。
- 空间释放方式：
  - `source_audit_archive` 的 30,039,082 行只存外部 Parquet
    （`D:\vd-cold-archive\source_audit_archive.parquet`），主库只保留空表；
  - `raw_response_archive_history` 的 BLOB payload 只存外部 Parquet
    （`D:\vd-cold-archive\raw_response_archive_history\`），主库只保留
    hash/来源/时间等元数据（payload=NULL）；
  - `raw_response_archive_all` 视图新增 `storage` 列区分 history/active；
    lineage 的空 payload 检查只检查 active 表，外部化 history 不误报。
- 外部冷数据核验：
  - source_audit_archive.parquet：30,039,082 行；
  - raw_response_archive_history Parquet：164,651 行。

## 分红融资比“总股本视角”为什么没有直接启用

用户要求：AH 两地上市企业应按“两地总融资 / 两地总广义分红”计算。

当前数据现实：
- `dividends` 只采集 A 股分红（A股每股股息 × A股口径股本）；
- `funding_events` 只有 A 股 IPO/增发/配股；
- **没有港股分红记录，也没有港股 IPO/配股融资记录**。

如果现在用 `total_shares`（A+H 总股本）去乘 A 股每股股息，会再次把
分给 H 股股东的钱算进来，而分母仍只有 A 股融资——这正是中国移动
825.9% 的错误来源。因此：
- 当前 `dividend_financing_ratio_pct` 保持 A 股口径并明确标注；
- 总股本总口径**暂不发布**，不输出错误数据；
- 待港股分红与港股融资数据源接入后，再增加
  `dividend_financing_ratio_total_pct`（或“全市场口径”字段）。

## 当前服务

- 服务已用 7.8GB 新库重启；
- 自动更新在后台执行；在其完成前 readiness 可能保持 false/中间态，
  属真实状态。
