---
status: approved
category: reports
created: 2026-11-15
last-reviewed: 2026-11-15
related: reports/123
---

# 血缘摘要格式错误导致全站指标被遮蔽（P0）与四份独立审计的处置

## 1. 结论（TL;DR）

1. **发现并修复一个 P0**：补采批次把 `raw_response_hash` 写成了 `xjllbAjaxNew:451ef0802792468b909` 这种 **32 位非十六进制**串，被数据质量规则判为"真血缘断裂"→ `LINEAGE_INVALID` → 命中 `_SNAPSHOT_BLOCKING_WARNINGS` → **5,579 只股票 × 34 个指标全部返回 null，`/api/ready` 503 `snapshot_values_masked`**。13 个批次（2026-11-15 10:21 起）触发。
2. **生成侧已修**：`app/core/supplement.py` 改为 `sha256(请求描述)`（64 位十六进制）**先归档 payload 再登记批次**；新增回归用例钉死"摘要必须 64 位 + 必须能查进归档"。
3. **存量侧已修**：`scripts/repair_supplement_lineage.py --yes` 已执行，13 个批次补齐归档记录，`batch_archive_gap_rows` 13 → **0**；`/api/ready` 恢复 `ready:true`、`untrusted_all:false`。
4. **其余为审计发现的"文档与事实不一致"**，已逐条修正；**未修的项见 §6**。

## 2. P0 的完整证据链

| 环节 | 证据 |
|---|---|
| 现象 | `GET /api/ready` → `{"ready":false,"reason":"snapshot_values_masked","warning_codes":["LINEAGE_INVALID","FIELD_COVERAGE_LOW"],"untrusted_all":true}`；`GET /api/stock/601398/indicators` → 34/34 指标 `value=null, untrusted=true` |
| 归因 | `build_data_quality_status()` 的 `lineage.archive_gap_rows = 13`（`audit_archive_gap_rows=0`、`empty_archive_payload_rows=0`、`batch_archive_gap_rows=13`） |
| 定位 | 13 个 `fetch_batch.raw_response_hash` 形如 `xjllbAjaxNew:451ef0802792468b909`（长度 32，含 `AjaxNew:` 前缀，逃过 `^[0-9a-f]{32}$` 的 legacy 判定）→ 被判"真缺口" |
| 源头 | `app/core/supplement.py` 旧代码 `src_hash = f"{spec.endpoint}:{batch_id}"[:32]` |
| 影响面 | 13/452,885 批次（0.003%）→ 全站 5,579 只股票、34 个指标、`/api/ready` 503 |

**为什么规则没写错**：64 位内容寻址摘要缺归档 = 真断裂，必须 fail-closed。错的是**生成侧写的摘要不是内容寻址**。

## 3. 修复

### 3.1 生成侧（`app/core/supplement.py`）
- `src_hash = hashlib.sha256(lineage_payload).hexdigest()`，`lineage_payload` = 本轮请求描述 JSON（endpoint / batch_id / codes / periods / fetched_at）；
- `archive_raw_response_if_absent()` **在写 `fetch_batch` 之前**调用，保证"先有归档、后有引用"；
- 回归用例 `tests/regression/test_financial_sector_supplement.py::test_supplement_batch_lineage_hash_is_sha256_and_archived` 钉死三件事：摘要 64 位十六进制、能查进 `raw_response_archive_valid_hash`、归档 payload 非空。

### 3.2 存量侧（`scripts/repair_supplement_lineage.py`，新增）
- 默认 dry-run，`--yes` 才写；写前 `exclusive_update` 写锁；单事务；
- 对每个缺口批次写入一条**明确标注 `lineage_backfill: true`** 的归档记录（payload 是批次描述，**不是**原始响应——原始响应当时未保留，这点在 payload 里写清）；
- 执行结果：`回填完成，剩余缺口批次 0 个`。

### 3.3 验证
| 项 | 修复前 | 修复后 |
|---|---|---|
| `batch_archive_gap_rows` | 13 | **0** |
| `warning_codes` | `["LINEAGE_INVALID","FIELD_COVERAGE_LOW"]` | `["FIELD_COVERAGE_LOW"]` |
| `/api/ready` | `ready:false, 503` | **`ready:true`** |
| `trust.untrusted_all` | `true`（34/34 null） | **`false`** |

## 4. 其它代码修复

| # | 问题 | 处置 |
|---|---|---|
| 1 | `dividend_yield` → `ttm_dividend_yield` 的废弃提示**没说量纲差 100 倍**（前者存小数、后者存百分数，实测 3,735 行全部恰为 100×）。用户把 `dividend_yield > 0.03` 换成 `ttm_dividend_yield > 0.03` 会命中几乎全市场 | 新增 `DEPRECATED_SCALE`，警告文案与 `/api/screening/indicators` 的 `unavailable` 都带上 `scale_factor` 与"注意量纲差 100 倍"；用例同步 |
| 2 | `cninfo_adapter._parse_dividend_from_announcement`（40+ 行）**零调用**，配套 3 个标题正则也零使用；注释还写"ex_date 需解析 PDF，留给 M8" | 整块删除；`_fetch_dividends` 的说明改为**能力分工**（公告文本 vs akshare 结构化分红），不再暗示"等 PDF" |
| 3 | `config/default.yaml` 与 `app/core/adapters/manager.py` 仍写"需解析 PDF，尚未实现""未来 PDF 解析就绪后的显式挂载位" | 改为能力分工口径，明确 PDF 链已于 `reports/123` 整体删除、不存在"未来挂载位" |
| 4 | `config/cold_archive.yaml` 头注释与 `scripts/cold_archive_split.py` docstring 仍写"第 17 张 pending / 16 张已切换" | 改为"17 张全部 switched、4,619,914 行 / 411.4 MB"（此前 `reports/123` §8.6 与 `STATUS` 误指为 `asset_reference.yaml`，已一并更正） |
| 5 | `app/core/quality/asset_health.py` 注释写"12 条资产健康规则"，实际 13 条 | 改为 13 条 |

## 5. 文档一致性修复

| 文档 | 问题 | 处置 |
|---|---|---|
| `reports/122` | 仍是 `status: approved`，却带着 6 处已被 `reports/123` 推翻的说法（冷库 16 张、`pypdf` 待办、PDF 74% 外推、`quick_ratio` 5,477、`upstream` 列、`hk_dividends` 全量刷新） | 标 `status: superseded` + `superseded-by` + 顶部更正横幅逐条列出；`5,477` → `5,475` |
| `reports/123` | §卷首写"本轮只改文档、代码/测试/配置/data 零改动"，但同批提交 `5987dc4` 含 1,671 行新脚本与 10 个测试文件改动；§8.6 把 `cold_archive.yaml` 的过期头注释误指为 `asset_reference.yaml` | 两处均已改正 |
| `STATUS.md` | ① S1 数字仍是 1,490（实为 **1,496**）；② "Web 需重启"已过期（22:43:59 已重启）；③ `quick_ratio` 5,477；④ `hk_dividends`"全量刷新"实为**全量重算值**（12 行新增、2,587 行 `fetch_time` 未变） | 四处均已更正 |

## 6. 四份独立审计确认**仍未修**的项（下一轮）

| # | 项 | 证据 | 性质 |
|---|---|---|---|
| 1 | **信任遮蔽只有一个出口**：`LINEAGE_INVALID` 在详情页把 34 个指标打成 null，但筛选引擎只认 `masked_fields`（只由 `EXT_PERIOD_MISMATCH`/`DEDUCTED_PROFIT_GAP` 触发），**筛选页照常出数** | `app/web/api/screening.py` 无 `untrusted_all` 引用 | 设计级（需决定筛选是否 409） |
| 2 | **补采冷却路径仍制造"水位前进、待补不降"**：`_filter_cooldown` 把冷却中股票滤掉 → `remaining_pending` 变小 → 被判"追平"；`balance_sheet_ext` 105 只（97 只金融股，F10 模板本就没有这些科目）→ 规则永久红 | `supplement.py:520/628`、`update.py:1590` | 代码级 |
| 3 | **`h_share_market_cap_cny` 无产品出口**：只在 `build_ah_snapshot()` 返回值里，无 API/CLI/落库列/前端 | 全仓 grep 仅命中 `ah_metrics.py` 与单测 | 设计级（接哪一层） |
| 4 | **CSRC 行业历史通道 0 行**：代码/CLI/测试齐全，正式库 `industry_history` 100% `csmar_indcd` | 只读 SQL | 运维级（跑一次即可） |
| 5 | **市值有 3 套独立实现**，同一 `latest_price_date` 上 185 只偏离 >1%（最差 `689009` −90%），根因是 `share_capital_history` 漏记送转 | `calculator.py` vs `etf_fundamentals.py` vs `ah_metrics.py` | 代码级（需收敛 + 补送转） |
| 6 | **4 张遗留 staging 表 274,249 行 / 17.18 GB**，零引用且被白名单豁免告警；旧库备份 **41 GB**、孤儿冷库目录 **406 MB** | 只读 SQL + `ls` | 需人工裁决保留期后才可删（AGENTS.md §5） |
| 7 | **9 个配置键全仓无人读取**（`screening.default_exclude_st` / `max_conditions` / `max_nesting_levels`(配置 3 vs 代码 50) / `backup.keep_full_backups` 等） | 配置遍历比对 | 代码级（删键或接线） |
| 8 | **`data_status.py` 的写锁守卫被 `except Exception: pass` 包住**：锁探测一抛错 409 保护整体失效 | `app/web/api/data_status.py:245-253` | 代码级 |
| 9 | **DuckDB 内存上限三处不一致**：config 8GB / 代码兜底 14GB / STATUS 14GB | `duckdb_store.py:291`、`config/default.yaml:19,61` | 代码级 |
| 10 | 20 处代码注释引用不存在的编号（`reports/120 §5 J9` 等，reports/120 无 J 编号） | grep | 文档级（低） |

## 7. 附：四份审计的总体结论

- **第一轮 J1–J12 + 未做清单**：已修复有证据 **12 项**，部分修复 **4 项**（J1 冷却路径 / J2 无三态机 / J3 CSRC 0 行 / J7 隐藏分支无测试），修了但与预期不同 **2 项**（J12 无出口 / U2 只标废弃未去重），**没修 0 项**。
- **第二轮声明**：数据落库与代码机制**基本全部兑现**；不成立的是三条结论性表述（"全站遮蔽已消除""`upstream` 落库""冷库 16 张终态"）。
- **第三轮声明**：分红数据经**现场抓源 8 只股票逐事件核对**（含 3 只随机、2 只 B 股、1 只退市股）**全部吻合**；冷库 17 张在 manifest / yaml / schema / parquet / 库内视图**五处完全一致**；A+H 三只市值与腾讯实时行情**分文不差**。
- **冷启动独立审查**：`app/` 内 `TODO/FIXME` 命中 **0**；真正的问题不是"没做的事"，而是"**做完了没接线**"（A+H 指标层最典型）。
