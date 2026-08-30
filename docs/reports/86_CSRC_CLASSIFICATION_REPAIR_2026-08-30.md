---
title: 证监会行业分类修复与全量重抓报告
status: approved
category: reports
created: 2026-08-30
last-reviewed: 2026-08-30
---

# 证监会行业分类修复与全量重抓报告（2026-08-30）

## 裁决

**修复完成并已在正式库执行全量重抓。** 2026-08-30 全市场 5,551 只上市股票中：
- 成功写入证监会口径分类 **5,443 只**；
- CNINFO 源确认无证监会分类记录的 **108 只**（多为新股）如实置 NULL，并登记 `missing_list(field_name='csrc_industry')`；
- 重抓错误 **0**；
- 正式库 `minimum_data_readiness.ready=true`、`warning_codes=[]`。

## 根因

1. **查询日期截断**：akshare `stock_industry_change_cninfo` 的默认 `end_date=20220713`。旧适配器未传 `end_date`，因此 2022-07 之后上市或发生行业变更的股票永远查不到记录。
2. **行业标准混用**：CNINFO 同一接口一次返回巨潮/申万/中证/证监会等多套行业标准。旧适配器取第一条记录，且 `最新记录标识` 已被 akshare 过滤，导致 `csrc_l1/csrc_l2` 实际混入非证监会口径。例如正式库中 `000001` 曾为“金融/银行”（巨潮），实际证监会口径为“金融业/货币金融服务”；`600519` 曾为“主要消费/饮料”，实际为“制造业/酒、饮料和精制茶制造业”。
3. **低频节流无缺口检测**：CSRC 只按 30 天间隔刷新；期间新股上市产生 NULL 缺口不会被自动补抓。

## 修复内容

- `app/core/adapters/csrc_industry_adapter.py`
  - 显式查询 `start_date=19900101`、`end_date=今天`。
  - 只保留“分类标准”包含“证监会”的记录，取变更日期最新一条。
  - 兼容 akshare 1.18.81（`分类标准`）与 1.18.64（`行业标准`）列名。
  - 逐股应用限速，并将 CNINFO CSRC 默认限速从 1.5s 调整为 0.35s。
- `app/core/init.py`
  - `_fetch_csrc_industry(full_refresh=True)`：全市场重抓，纠正历史非证监会口径；源确认无分类时清空错误旧值。
  - 增量模式：只补 NULL 且未登记 missing 的股票；合法无分类写 `missing_list`，成功写入后自动 resolve。
  - 块内部分网络失败时只重试未返回股票，不把错误当“源无分类”。
- `app/core/update.py`
  - CSRC 30 天到期执行全量刷新；未到期但存在未登记 NULL 缺口时执行增量补抓。
- `app/cli/main.py`
  - 新增 `vd.bat data refresh_csrc`，`--full` 为全量重抓；跨进程单写者串行。
- `app/resources/config/default.yaml` / `app/core/adapters/manager.py`
  - 增加 `cninfo_csrc` 限速配置（0.35s）。

## 正式库执行结果

执行命令：`vd.bat data refresh_csrc --full`

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 上市股票总数 | 5,551 | 5,551 |
| csrc_l1 IS NULL | 627 | 108 |
| missing_list(csrc_industry) 未解决 | 0 | 108 |
| 成功写入证监会分类 | - | 5,443 |
| 源错误 | - | 0 |
| `csrc_industry_progress` | 613/613（2026-08-06） | 5,551/5,551（2026-08-30） |

抽样核验：`000001`、`600519`、`001399` 均已从非证监会口径纠正为证监会口径。

## 验证

- 定向回归：`test_storage_and_ingestion.py`、`test_update_job_and_progress.py`、`test_incremental_update_scope.py`、`test_csrc_industry_adapter.py`、`test_adapter_name_collision.py` 共 **53 passed**。
- Ruff：涉及文件全部通过。
- 前端门禁不受影响（无前端代码变更）。

## 已知边界

- 剩余 108 只为 CNINFO 当前无证监会分类记录（新股/近期上市为主），已如实进入 missing_list，不会被伪造；自动更新后续仍会在 30 天全量轮或新缺口增量轮中再次尝试。
- 当前 Web 服务进程是修复前启动的；重启 `start.bat` 后自动更新才会使用新的 CSRC 抓取/缺口检测逻辑。
