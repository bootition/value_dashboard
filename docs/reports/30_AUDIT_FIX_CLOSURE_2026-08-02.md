---
title: 审计问题修复闭环报告（2026-08-02）
status: superseded
category: reports
created: 2026-08-02
last-reviewed: 2026-08-02
supersedes: reports/27_COMPREHENSIVE_RED_TEAM_REAUDIT_2026-07-31.md
superseded-by: reports/34_SYSTEM_RED_TEAM_REVIEW_2026-08-02.md
---

# 审计问题修复闭环报告（2026-08-02）

> 本报告关闭 `reports/27`（综合红队复审）的全部开放项：P0-1 已由
> `reports/29` 数据重建关闭；本报告确认 **27 的代码级 P1-1 至 P1-20 与
> P2-1 全部关闭**（含本会话修复的 2 个维护脚本），并给出逐项证据。
> 数据层结论以 `reports/29` 为准。

## 关闭清单（对应 reports/27 编号）

| 编号 | 问题 | 状态 | 修复位置 / 验证 |
|---|---|---|---|
| P0-1 | SSE 股本单位混用 1,215 条不可能记录 | ✅ 关闭（数据重建） | `reports/29`：5,534 只重建，circ>total 1,215→0；外部真值总股本 27/27、收盘 27/27 |
| P1-1 | 正式数据不可追溯/门禁不通过 | ✅ 关闭（数据重建） | `reports/29`：ready=TRUE、warning_codes=[] |
| P1-2 | LINEAGE_INVALID 不阻断服务端筛选 | ✅ 已修复 | `app/core/data_quality.py:14-22` 阻断集合含 LINEAGE_INVALID；`app/web/api/screening.py:30-46` `_require_current_screenability` → 409 |
| P1-3 | 维护脚本绕过原子来源链路 | ✅ 已修复（本会话补完） | `scripts/repair_xdxr.py` 退役（写入被阻断，指向 canonical `repair_dividends.py`）；`scripts/repair_dividend_announcement_dates.py` 重写为单事务（UPDATE dividends + `_record_batch_in_connection` + `_record_field_audit_in_connection` + raw_response_archive）。此前已修复：SSE 股本两脚本退役、`supplement_akshare.py` 阻断、`repair_dividends/repair_prices/repair_financials` 走 canonical 单事务 |
| P1-4 | CSV 导入可删除较新财务历史 | ✅ 已修复 | `scripts/import_csv_to_db.py:262-266` main() 直接拒绝；`_upsert_frame` 改 ON CONFLICT DO UPDATE + COALESCE 不删除历史；`scripts/supplement_akshare.py:281-284` 在 DELETE 前抛错阻断 |
| P1-5 | 中断恢复被遗留维护锁永久阻塞 | ✅ 已修复 | `app/core/storage/maintenance.py` 锁带 pid 属主、`_lock_owner_is_dead` 仅回收死亡属主且须先验证 journal；`app/web/main.py:311-320` 启动先 `recover_pending_restore` 再 schema 初始化 |
| P1-6 | 同步 DB 工作跑在 async 事件循环 | ✅ 已修复 | screening/stock_detail/watchlist/data_status/dsl 全部改为同步 `def`（进 FastAPI threadpool） |
| P1-7 | 详情页可能显示上一只股票数据 | ✅ 已修复 | `frontend/src/views/StockDetailPage.vue:70-179` 全请求 generation token + kline AbortController，过期响应丢弃 |
| P1-8 | 不可信指标无统一警示 | ✅ 已修复 | 服务端 `indicator_trust`/`mask_untrusted_values`（`data_quality.py:509-538`）；前端 `IndicatorTabs.vue` 逐字段"数据不可信"标签 + `StockDetailPage.vue:330` 全局 fail-closed 告警 |
| P1-9 | 筛选草稿乱序覆盖 | ✅ 已修复 | 服务端 revision 校验冲突 → 409（`screening.py:170-188`）；前端串行化写入 + 排队重试（`ScreeningPage.vue:160-193`） |
| P1-10 | PDF 错误响应伪装 200 | ✅ 已修复 | `app/web/api/stock_detail.py:679-739` 非法路径/穿越/冷归档/缺失分别返回 400/400/409/404，成功为 application/pdf |
| P1-11 | watchlist 吞掉 DuckDB 失败 | ✅ 已修复 | `app/web/api/watchlist.py:92-93` 读取异常 → HTTPException 503 |
| P1-12 | DSL 校验引用未定义名称 | ✅ 已修复 | `app/core/dsl/validator.py:16` 显式导入 `INDICATOR_METADATA`；`uv run --locked ruff check` 零 F821 |
| P1-13 | CLI 门禁失败引用未定义名称 | ✅ 已修复 | `app/cli/main.py:59-72` `_screening_engine` 导入 `make_response`，未就绪输出 E002 JSON 协议 |
| P1-14 | 规则并发保存产生 500 | ✅ 已修复 | `screening.py:488-489` 唯一冲突 → 409；schema `UNIQUE(name, version)`（`schema.py:417`） |
| P1-15 | raw 行情未纳入日历校验 | ✅ 已修复 | `data_quality.py:52-148` `_has_complete_trading_calendar` 对 raw 与 qfq 双向校验（窗口缺口 + 表内日期必须存在日历） |
| P1-16 | 归母权益为零被替换为总权益 | ✅ 已修复 | `calculator.py:534,627` 与 `stock_detail.py:439,651` 均 `is not None` 才回退；零值保持 NULL 口径 |
| P1-17 | DSL 无资源复杂度上限 | ✅ 已修复 | `app/core/dsl/parser.py:24-28,231-249` `_validate_expression_budget`（10KB 字节 / 500 token / 50 嵌套 / 10 函数参数） |
| P1-18 | 筛选门禁全量 hash archive BLOB | ✅ 已修复 | `data_quality.py:453-485` `_archive_hash_mismatch_rows` 60s 进程内 TTL 缓存，失败 fail-closed 且不写缓存 |
| P1-19 | 恢复缺失 PDF 树保留当前世代 | ✅ 已修复 | `backup/manager.py:657-663,845-877` `_restore_pdf_tree` 先删 target，备份无 PDF 树即空树；journal 恢复同语义 |
| P1-20 | 静态同步先删线上目录 | ✅ 已修复 | `frontend/scripts/sync-static.mjs` 改为 assets 先行 + index.html 最后 + 双 manifest 校验 + 失败保留旧树 |
| P2-1 | 快照新鲜度 UTC/本地偏移一天 | ✅ 已修复 | `stock_detail.py:122-123` `build_freshness_metadata` 统一 `datetime.now(timezone.utc)`；回归用例同步校准 |

## 验证（2026-08-02 全量门禁）

| 门禁 | 结果 |
|---|---|
| `scripts/s1-pytest.ps1 tests/regression -q --no-header` | **352 passed**（报告 27 时为 288 passed + 1 failed） |
| `uv run --locked ruff check app tests/regression` | All checks passed（零 F821/F401/F841） |
| `uv run --locked ruff check scripts/repair_xdxr.py scripts/repair_dividend_announcement_dates.py` | All checks passed |
| 前端 `npm run lint` / `npm run test` / `npm run build` | 通过 / 52 passed / 构建成功（含 static 同步） |
| `uv lock --locked` | 通过 |

## 本会话变更（2026-08-02）

- `scripts/repair_xdxr.py`：退役为写入阻断入口（同 `repair_sse_*` 先例），指引 canonical `repair_dividends.py`。
- `scripts/repair_dividend_announcement_dates.py`：写入路径重写为 canonical 单事务（业务 UPDATE + archive + batch + field audit 全有或全无），保留 --dry-run/--sample CLI。

## 结论

- 数据层：ready=TRUE（`reports/29`，不再重复）。
- 代码层：`reports/27` 的 P1-1~P1-20、P2-1 全部关闭；`reports/28` 整改记录中未列出的 P1 项（P1-2/3/4/7/9/10/11/13/15/16/17/19/20）经本报告逐项核对代码确认关闭。
- 剩余披露性缺口（非阻断，见 `reports/29` §剩余缺口）：920305 退市、92 只银行/券商监管字段 NULL、2026-03-31 前历史期 CSMAR 导入无原始字节 lineage、东财源被封（回退链末端保留）、96 只无分红、8 只停牌、2 只解禁时间差。
