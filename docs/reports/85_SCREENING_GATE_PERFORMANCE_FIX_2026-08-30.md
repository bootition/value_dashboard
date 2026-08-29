---
title: 筛选就绪门禁性能修复报告
status: approved
category: reports
created: 2026-08-30
last-reviewed: 2026-08-30
---

# 筛选就绪门禁性能修复报告（2026-08-30）

## 裁决

**修复完成。** 正式库冷缓存 `screening_readiness` 从约 54s 降至约 19-23s；启动维护会自动预热 `screening_readiness_cache`，因此服务启动完成后用户筛选通常命中缓存、不再等待全库核对。前端 `/api/screening/run` 单请求超时放宽至 120s，避免默认 30s 中止仍在正常计算的请求。回归测试与前端门禁通过。

## 根因

1. 无写锁时，`_require_current_screenability` 在请求线程内同步执行 `screening_readiness` → `build_data_quality_status`。
2. 正式库 `source_audit` 约 3,693 万行、`raw_response_archive` 约 14 万行/6.25GB。旧实现：
   - `_missing_lineage_coverage` 把约 957 万行 evidence 全部拉回 Python 再逐股判空（约 25s）；
   - lineage 结构检查单条大 join 扫描全部 `source_audit`（约 14s）；
   - 归档 payload 每次冷进程全部重新 SHA-256（约 42s）。
3. 前端 axios 全局超时为 30s。请求 30s 被浏览器中止时，服务端仍在计算并在约 54s 后完成、把 run 写入 SQLite；用户看到的是“一直 loading，最终没结果”。

## 修复内容

- `app/core/data_quality.py`
  - `_missing_lineage_coverage`：改为 DuckDB 内先按每只股票的 raw/qfq 最新交易日与完整财务报告期裁剪 evidence，再聚合出缺失代码；语义保持不变（新股/停牌股仍豁免）。
  - lineage 结构检查：拆成利用现有索引/哈希列的目标计数查询。
  - `_archive_hash_mismatch_rows`：写路径在插入前校验并置 `integrity_verified=TRUE`；冷扫描只重新校验未标记行，避免每次读 6.25GB 归档。
  - 新增 `screening_readiness_cache_key` / `load_screening_readiness_cache` / `store_screening_readiness_cache` / `warm_screening_readiness_cache` 共享实现。
- `app/web/api/screening.py`
  - 改用共享缓存函数。
  - 修复缓存命中时错误返回 screening 决策 dict 的问题：现在返回 gate 形状 `{"lock_active": False, "data_as_of": None}`，避免 `run_screening` 读取 `gate["lock_active"]` 抛 KeyError。
- `app/web/main.py`
  - 启动维护在自动更新后预热 `screening_readiness_cache`（非请求线程）。
- `frontend/src/views/ScreeningPage.vue`
  - `/api/screening/run` 单独设置 `timeout: 120_000`。
- `tests/regression/test_screening_gate.py`
  - 新增缓存命中 gate 形状、not-ready 缓存 409、warm 缓存 round-trip 回归。
- `frontend/tests/component/*`
  - 同步断言 run 请求的第三参数（120s timeout）。

## 验证

- 正式库只读测量（服务空闲，本机）：
  - `screening_readiness` 冷缓存：54.19s → 19.35-23.15s。
  - `_missing_lineage_coverage`：25s → 4.5-8.1s。
  - 归档 hash 冷扫描：42s → 4.4-6.2s（只查未标记行）。
- 后端回归：`test_minimum_data_readiness.py`、`test_data_quality_status.py`、`test_screening_gate.py`、`test_screening_published_dsl.py`、`test_lineage_materials.py` 共 46 passed。
- Ruff：`app/core/data_quality.py`、`app/web/api/screening.py`、`app/web/main.py`、新增/修改测试全部通过。
- 前端：`npm run lint`、62 个脚本断言 + 56 个 Vitest 组件测试全部通过，`npm run build` 成功。

## 已知边界

- 冷缓存第一次全量核对仍约 20s，但不再超过前端超时；启动预热后正常点击为缓存命中。
- CLI 外部更新会改变数据指纹，下一次筛选会重新全量核对一次（fail-closed 保持）。
- `integrity_verified=TRUE` 行由写路径在插入前校验；若有人为直接改库，不在本修复承诺范围内。
