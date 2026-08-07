---
title: 启动修复与启动耗时剖析报告（2026-08-07）
status: approved
category: reports
created: 2026-08-07
last-reviewed: 2026-08-07
---

# 启动修复与启动耗时剖析报告（2026-08-07）

## 1. start.bat 无法正常启动的根因

用户反馈「点击 start.bat 无法正常启动」。根因有二，均已修复：

1. **CMD 块解析错误（主因）**：`start.bat` 在 `if defined NEED_BUILD ( ... )` 块内的 `echo` 文案含有半角括号，如
   `echo [INFO] Building the current frontend (one-time; may take 10-20s)...`。
   CMD 对括号块按配对解析，块内 echo 的裸括号会破坏块结构，直接报
   **「此时不应有 (」** 并终止整个批处理——表现为双击后窗口一闪即退、无任何提示。
   修复：块内 echo 文案移除括号（`one-time, may take 10-20s`），并新增回归契约测试：
   构建块内所有 echo 行不得含裸括号。
2. **端口占用误判**：旧端口检查 `findstr ":8765"` 会把 `TIME_WAIT` 等短暂状态也算作占用，
   导致「端口占用」误报退出。现改为仅匹配 `LISTENING`；真正已有实例时打开浏览器并优雅退出。

## 2. 启动耗时剖析（二次启动实测 13.4s）

逐段计时（正式库、无更新写锁、Windows 本地）：

| 阶段 | 耗时 | 说明 |
|---|---|---|
| Python 解释器 + import 链（fastapi/uvicorn/duckdb 等） | ~1-2s | importtime 验证，非瓶颈 |
| schema 初始化（DuckDB+SQLite） | ~0.5s | 幂等 |
| `minimum_data_readiness(duck)` 启动计算 | **~11.8s** | 主要瓶颈 |
| uvicorn bind + 就绪 | 其余 | — |

`minimum_data_readiness` 的慢点：
- `source_audit ↔ fetch_batch ↔ raw_response_archive` 审计溯源聚合：~7.5s（`raw_response_archive.payload` 大 BLOB 全扫主导，非纯 join）。
- `price_daily_raw` 全表 MIN/MAX/COUNT 聚合：~0.7s。
- 财务三表 `arg_max` 聚合等其余 ~1-3s。

本轮为审计三表增加 ART 索引（`source_audit(stock_code, report_date, field_name)`、
`source_audit(fetch_batch_id)`、`source_audit(raw_response_hash)`，幂等 CREATE INDEX IF NOT EXISTS，
已同步正式库）。实测索引对上述查询收益有限（瓶颈在 payload 列读取），保留作为基础优化。

## 3. 验证

- `start.bat` 真机 E2E：首次启动（含构建）16.9s 到 `/api/health` 200；二次启动（无构建）13.4s。
- 契约测试 17/17（含：块内 echo 无裸括号、端口仅 LISTENING、构建按 NEED_BUILD 判定、指纹脚本契约）；Ruff 全绿。

## 4. 结论与后续建议

- 「无法正常启动」已修复并经真实 E2E 验证；构建为按需（源码变更才前台构建，平时不构建）。
- 二次启动 13.4s 的主要剩余项是启动期 `minimum_data_readiness`（~12s）。
  后续优化建议：把启动 readiness 计算放入后台线程并缓存到 SQLite（秒级 bind，
  页面短暂显示“正在核对数据”）；审计溯源深度校验保持按需/数据状态页路径执行。
  本轮未实施，避免扩大改动面。
