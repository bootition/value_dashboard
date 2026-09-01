---
title: DuckDB 离线重建运行手册
status: approved
category: runbooks
created: 2026-09-01
last-reviewed: 2026-09-01
---

# DuckDB 离线重建运行手册

## 1. 前置

- 停止 Web 服务与自动更新；
- 删除/确认无 `.duckdb.write.lock`、`.value-dashboard.update.lock`；
- D 盘可用空间 ≥ 160GB；
- 建立硬链接快照：
  `fsutil hardlink create data\valuedashboard.duckdb.pre-rebuild data\valuedashboard.duckdb`

## 2. 导出

```bash
VD_DUCKDB_MEMORY_LIMIT=20GB \
python scripts/rebuild_duckdb.py export \
  --src data/valuedashboard.duckdb \
  --dest-dir D:\vd-rebuild-export
```

## 3. 导入

```bash
VD_DUCKDB_MEMORY_LIMIT=20GB \
python scripts/rebuild_duckdb.py import \
  --src D:\vd-rebuild-export \
  --dest D:\vd-rebuild-new\valuedashboard.duckdb
```

## 4. 校验

```bash
python scripts/rebuild_duckdb.py verify \
  --old data/valuedashboard.duckdb \
  --new D:\vd-rebuild-new\valuedashboard.duckdb
```

必须输出 `VERIFY OK`。

## 5. 切换

```bash
python scripts/rebuild_duckdb.py swap \
  --old data/valuedashboard.duckdb \
  --new D:\vd-rebuild-new\valuedashboard.duckdb
```

## 6. 回滚

- 旧文件：`data/valuedashboard.duckdb.old-<timestamp>`
- 硬链接：`data/valuedashboard.duckdb.pre-rebuild`
- 回滚：停服后删除当前 `valuedashboard.duckdb`，将旧文件改回原名。

## 7. 观察期

至少一个完整自动更新周期 + 一次冷核对；`ready=true` 且无新 warning 后才允许删除旧文件。
