---
title: 红队终审报告（2026-07-18）
status: superseded
category: reports
last-reviewed: 2026-07-26
superseded-by: reports/11_RED_TEAM_AUDIT_V2.md
---

# 红队终审报告（2026-07-18）

> 审查视角: 红队（最挑剔的对抗性审查）
> 审查对象: value-dashboard V1 全量交付物
> 审查日期: 2026-07-18
> 审查方法: 4个并行代码审查代理（逐行读37个Python+6个Vue文件）+ 数据库深度审计脚本 + PRD逐条合规检查 + 安全/运维审查
> 审查范围: 37个Python文件 + 6个Vue文件 + 25个测试文件 + 数据库 + 配置 + 打包产物

---

## 总体结论

**V1 交付物不可接受。** 发现 **16 个 P0 阻断问题、25 个 P1 严重问题、30+ 个 P2 一般问题**。

核心问题不是代码结构或功能缺失，而是**数据质量的地基是虚的**——用近似值和强行填充替代真实数据，然后通过验收。代码层面也有多个安全漏洞（SQL注入、路径遍历、两段式确认可绕过）和数学错误（TTM计算退化路径产出无意义值）。

---

## 第一部分：数据质量深度审计（26个问题）

### 1.1 数据覆盖范围不一致（P0）

| 数据表 | 股票数 | 与stock_meta差异 | 问题 |
|---|---|---|---|
| stock_meta | 5,528 | 基准 | — |
| balance_sheet | 5,828 | +300 | 300只退市股孤儿数据 |
| indicator_snapshot | 5,828 | +300 | 为退市股计算了无用快照 |
| dividends | 5,643 | +115 | 115只退市股分红数据 |

**根因**: CSMAR数据集含5828只股票（含退市股），导入时未与stock_meta做交集过滤。

### 1.2 listing_date 全部是假的（P0）

所有5528只股票的 listing_date 从 `MIN(trade_date)` 推导。上市>5年的股票 listing_date=2021-06-21（价格起点），非真实上市日。PRD §6.7 要求从 AKShare `stock_individual_info_em` 获取。`min_listing_years` 筛选对这些股票全部返回错误结果。

### 1.3 is_suspended 全部为 false（P0）

0只股票被标记停牌。PRD §12.3 要求"默认排除停牌股票"——`include_suspended` 过滤完全无效。`akshare_adapter.py` 中 `is_suspended` 硬编码为 `False`。

### 1.4 is_st 检测逻辑错误（P0）

`akshare_adapter.py:360` 用 `"ST" in name.upper()` 检测ST——会误匹配名称含"ST"的正常股票（如"BEST"、"STONE"）。应改为 `name.upper().startswith(("ST", "*ST"))`。

### 1.5 价格数据严重不足（P0）

- 最早日期: 大部分从2021-06起（差PRD要求5个月）
- PRD §7.2 要求"其余历史回填继续"——从未执行
- 每股行数: min=2, max=4878, avg=1312（最小值股票数据严重不足）

### 1.6 turnover_rate 是伪造的（P0）

用 `volume/paid_in_capital` 近似计算，但 `paid_in_capital` 用最新报告期值。历史拆股后股本变化导致历史换手率全部偏差。baostock adapter 代码已修（`turn→turnover_rate`），但历史数据未重新抓取。

### 1.7 财务数据过时15个月（P0）

CSMAR截止2025-Q1。当前2026-07，2025-Q2至2026-Q1的财报全部缺失。`vd data update` 不会补充CSMAR数据（它只检查akshare抓取的数据）。

### 1.8 溯源数据完全为空（P0）

- `fetch_batch`: 0条记录
- `source_audit`: 0条记录
- CSMAR导入的 `raw_data` 只存固定marker字符串
- PRD §14 SD10 要求的全部溯源信息一个都没有

### 1.9 指标极端值未过滤（P1）

| 指标 | 最小值 | 最大值 | 期望范围 | 问题 |
|---|---|---|---|---|
| pb_mrq | 0.0001 | **1389.57** | 0-100 | **未过滤** |
| debt_ratio | 0.015 | **50.46** | 0-1 | **未过滤** |
| gross_margin | **-14.62** | **1.29** | -1~1 | **未过滤** |

### 1.10 分红 ex_date 不准确（P1）

ex_date 用 CSMAR `Accper`（报告截止日12-31/06-30）而非真实除权日（通常次年5-7月）。影响 `dividend_yield` 时间窗口和 `consecutive_div_years` 判断。

### 1.11 interest_expense 覆盖率极低（P1）

2024年报中覆盖率仅1%（94/5482行）。已用 `financial_expenses` 近似（覆盖率99%），但近似值包含利息收入、汇兑损益等，不精确。

### 1.12 申万行业完全缺失（P1）

0只股票有申万行业。`pe_ttm_industry_rank` 全部返回NULL——性能测试名义上测了行业排名，实际全为NULL。

### 1.13 其他数据问题

- xdxr表0条记录（TDX适配器实现了但data init从未调用）
- 19个备份未清理（PRD AR10要求保留3套）
- data目录1.5GB无清理机制
- BSE qfq价格328只完全缺失

---

## 第二部分：代码审计——适配器与存储层

### 2.1 baostock_adapter.py — 代码编号处理 bug（P0）

`_normalize_stock_code("600519.SH")` 返回空字符串：
- `code.split(".")[-1]` = "SH"
- `lstrip("shSHzzSZ")` = ""
- 股票被静默跳过

### 2.2 baostock_adapter.py — 分红抓取 N×M 性能灾难（P1）

每只股票从1990年到当前年份逐年调用 `query_dividend_data`。5000股×36年=180,000次API调用。

### 2.3 tdx_adapter.py — 财务报表返回不可用的未命名字段数组（P0）

`_fetch_financial_statement` 返回 `raw_fields: list[float]`（584个未命名浮点数）。注释承认"字段级映射待后续实现"。PRD §6.5 要求"全部已完成标准化的报表科目必须可供DSL与CLI查询使用"——TDX财务数据功能上不可用。

### 2.4 tdx_adapter.py — 历史数据截断（P0）

`_MAX_BARS_PAGES = 5` × 800 = 4000条≈16年。2009年前上市的股票早期历史被静默截断。

### 2.5 cninfo_adapter.py — ex_date 永远为 None（P0）

分红记录的 `ex_date` 永远为 `None`。`dividends` 表 `PRIMARY KEY (stock_code, ex_date)` 中 NULL 作为PK部分有问题：SQLite中NULL互不相等，每次重抓产生重复行。

### 2.6 cninfo_adapter.py — 从公告标题正则解析分红不可靠（P0）

无法区分"预案"（proposal）和"实施"（implemented）。将未实施的分红预案解析为实际分红，违反 PRD §9.2。

### 2.7 init.py — DELETE后INSERT违反PRD（P0）

`_fetch_stock_universe` 先 `DELETE FROM stock_meta` 再INSERT。如果抓取返回部分数据（如BSE失败），已有BSE数据被删除丢失。PRD §7.4 L1: "保留旧值，不以空值覆盖旧值"——被违反。同样问题存在于价格和财务抓取。

### 2.8 init.py — 从未获取 listing_info（P0）

`_fetch_stock_universe` 只调用 `stock_list`（返回code/name/exchange），不调用 `listing_info`（含上市日期/ST/停牌）。PRD §6.7 D1 要求的三个字段全部缺失。

### 2.9 update.py — DuckDB INSERT OR REPLACE 兼容性（P1）

`INSERT OR REPLACE INTO price_daily_raw` 在DuckDB中仅在v0.10.0+支持。旧版本会崩溃。

### 2.10 init.py — _upsert_financial_row SQL注入（P1）

`f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table}'"` — 表名直接插入SQL。虽然来自内部代码，但仍是不安全模式。且每行每表都执行此查询——严重性能问题。

### 2.11 backfill.py — _fix_listing_dates 覆盖所有股票（P1）

无WHERE子句限制只更新listing_date确实为假的股票。更新所有股票，包括已有正确listing_date的。

### 2.12 manager.py — 熔断器将合法空结果视为失败（P1）

`if result.metadata.error is None and len(result.data) > 0:` — 无分红股票返回空数据被标记为失败，触发熔断。

### 2.13 manager.py — 熔断器探测逻辑损坏（P1）

冷却期后 `tripped_until` 置None但 `failures` 计数不清零。下次失败立即重新熔断（failures=6 > threshold=5），探测永远得不到公平机会。

### 2.14 duckdb_store.py — PID重用漏洞（P0）

`_is_process_alive` 检查PID是否存在。Windows上PID可被重用——原锁持有者崩溃后PID被新进程复用，锁永远不会被回收。

### 2.15 schema.py — 多个schema设计缺陷（P1）

- `price_daily_qfq` 缺少 `turnover_rate` 列（与raw表不一致）
- `dividends` 表 PK 含可NULL的 `ex_date`
- 财务报表 PK 不含 `report_type`——同年同期的合并/母公司报表会冲突
- 无 `as_reported` vs `latest_restated` 区分
- `screening_rules` 缺少 `locked_indicators_version`（PRD §12.2）
- 无DuckDB schema版本追踪

---

## 第三部分：代码审计——DSL引擎与指标计算

### 3.1 TTM退化路径产出无意义值（P0）

**情况3**（数据不足5个季度）：返回 `latest`（最新累计值）。如果最新是Q1，返回的是3个月数据而非12个月TTM。**这不是TTM**。

**情况2退化**（无年报数据）：`ttm[key] = curr - prev`。对于Q1 2025 - Q1 2024 = 20，但正确TTM应为~420。**差异20倍，产出完全错误的值且不标记approximate**。

### 3.2 DSL parser func_call Transformer 损坏（P0）

`keep_all_tokens=True` 导致 `items[1]` 是 `"("` Token 而非 arg_list。所有 `FUNC(field_ref)` 形式（CAGR/TTM/rank/percentile等）全部失败。

### 3.3 DSL codegen SQL注入（P0）

`generate_select` 中 `where = f"WHERE s.stock_code = '{stock_code}'"` — stock_code 直接插入SQL字符串。可通过 `'; DROP TABLE balance_sheet; --` 注入。

### 3.4 筛选引擎排序字段SQL注入（P0）

`_build_order` 中 `field` 来自 `sort_spec`，未经验证直接插入SQL：`f"{field} {direction}"`。恶意排序字段可注入SQL。

### 3.5 筛选引擎列选择SQL注入（P0）

`_build_select` 中 `columns_spec` 未经验证：`", ".join(cols)` 直接插入SELECT。恶意列名可注入SQL。

### 3.6 payout_ratio 使用历史最大DPS而非最新年（P1）

`dividends.get("max_dps")` 是 `MAX(dividend_per_share)` 跨所有年份，非最近一年。某公司2020年DPS=1.0但2024年DPS=0.5，payout_ratio用1.0计算——高估一倍。

### 3.7 preview_single/preview_sample 不展开简写（P1）

`validate` 调用 `expand_shorthand`，但 `preview_single` 直接解析 `expression_text`。用户写 `revenue / total_assets`（简写），preview 会失败。

### 3.8 验证器循环检测是空操作（P1）

`_check_cycles` 注释说"简化：仅检查直接依赖"。实际上它只检查同一层依赖集合内的重复——因为 `deps` 是 set，永远不会有重复。传递循环 A→B→C→A 完全检测不到。

### 3.9 筛选排名NULL处理缺失（P1）

`RANK() OVER (ORDER BY field)` 未指定 `NULLS LAST`。NULL值在DuckDB默认中排在最前，获得rank=1——误导性。

### 3.10 行业排名违反PRD §12.4（P1）

`PARTITION BY sw_level1` 将NULL-sw_level1股票分到同一分区并给予排名。PRD §12.4 要求"行业排名返回 null"。应改为 `CASE WHEN sw_level1 IS NULL THEN NULL ELSE RANK()...END`。

### 3.11 content_hash存储损坏ast_json（P1）

`_update_status` 将content_hash作为JSON片段追加到ast_json列。如果ast_json已有有效JSON，追加后变成无效JSON（两个JSON对象拼接）。后续 `json.loads(ast_json)` 会失败。

### 3.12 ROE/ROA使用期末值而非平均值（P1）

标准 ROE = 净利润 / 平均权益 = 净利润 / ((期初+期末)/2)。代码用 `parent_profit / equity`（期末值）。对权益变化大的公司偏差大。

### 3.13 TTM退化不标记confidence（P1）

PRD §9.1 要求approximate值标记confidence。退化路径只 `logger.debug` 不在返回值中标记。

---

## 第四部分：代码审计——Web API、CLI与备份

### 4.1 PDF serve_pdf 路径遍历（P0）

`stock_code` 和 `filename` 通过URL编码 `%2F` 可绕过FastAPI路径段匹配：
- `GET /api/stock/x/pdf/..%2F..%2F..%2Fetc%2Fpasswd`
- Starlette先匹配路径段，后URL解码
- `pdf_dir / filename` = `data/pdf/x/../../../etc/passwd` = 任意文件读取

### 4.2 备份/归档SQL注入（P0）

- `backup/manager.py:285`: `f"COPY {table} TO '{parquet_path}'"` — parquet_path来自用户输入
- `cli/main.py:633`: `f"COPY {table} TO '{output_path}'"` — output_path来自用户输入
- `backup/manager.py:444`: `f"DELETE FROM {table}; COPY {table} FROM '{parquet_file}'"` — parquet_file来自ZIP文件名

### 4.3 两段式确认可绕过（P0）

`backup_restore_execute` 不验证 `plan confirm` 是否已执行。任何人可直接调用 `restore_execute` 而无需确认——两段式确认的安全模型形同虚设。

### 4.4 override_revoke datetime未导入（P0）

`override_revoke` 函数使用 `datetime.now()` 但从未 `from datetime import datetime`。调用此命令会 `NameError`。

### 4.5 恢复密钥生成但永远不可用（P1）

`generate_recovery_key()` 生成密钥并写入文件，但 `restore_from_backup` 只接受 `user_password`。密钥丢失时无法恢复——PRD §18.3 AR11 的"离线恢复密钥"形同虚设。

### 4.6 PBKDF2迭代次数过低（P1）

100,000次低于OWASP 2023推荐值600,000次（PBKDF2-HMAC-SHA256）。

### 4.7 Web API错误响应不规范（P1）

所有错误返回HTTP 200 + `{"error": "..."}`。应使用正确的HTTP状态码（404/400/500）。错误格式不统一。

### 4.8 CLI capabilities列表不完整（P1）

`get_capabilities()` 缺少20+个实际存在的命令（data.backfill_prices, data.switch_source, data.download_pdf, override.submit_template, backup.restore, archive.clean等）。OpenCode依赖此列表会发现不了这些命令。

### 4.9 CLI data_status/status 不使用make_response（P1）

输出纯文本而非JSON。PRD §16.1 要求所有运维操作通过JSON协议。

### 4.10 screening save result_id类型不匹配（P1）

Web API返回 `str(cursor.lastrowid)`（字符串），但 `watchlist.source_result_id` 是INTEGER列。类型不匹配。

### 4.11 校正模板list_templates忽略status参数（P2）

`if status:` 和 `else` 分支执行完全相同的SQL——status参数被完全忽略。

### 4.12 备份恢复不验证内容安全（P1）

`restore_from_backup` 从加密ZIP中读取表名和列名，直接用于 `DELETE FROM {table}` 和 `INSERT INTO {table}`。恶意备份ZIP可注入任意SQL。

---

## 第五部分：代码审计——前端与测试

### 5.1 嵌套规则组不可用（P0）

`ScreeningPage.vue` 渲染嵌套组为单行"嵌套组"标签+删除按钮。无法在嵌套组内添加条件。PRD §12.2 要求"逻辑嵌套最多3层"——UI支持添加组但产出空组。

### 5.2 入选解释已获取但从不显示（P0）

后端注入 `_entry_explanation` 到每行结果，前端 `tableColumns` 过滤掉 `_` 开头的键。PRD §12.5 / §20.1 #6 要求"只对入选股票提供入选解释"——完全不合规。

### 5.3 K线图表内存泄漏（P0）

`renderKline` 每次调用 `kc.init(klineRef.value)` 但从不dispose旧图表。切换raw/qfq或天数变化时累积重叠canvas。无 `onUnmounted` 清理。~10次切换后页面无响应。

### 5.4 vite.config.ts 无开发代理（P0）

前端用 `axios.post('/api/...')` 相对路径。`npm run dev` (端口5173) 下 `/api/...` 命中Vite dev server返回404。开发模式完全不可用。

### 5.5 测试全部不是pytest测试（P0）

25个"测试"文件全是顶层脚本，无 `def test_*` 函数、无fixture、无 `conftest.py`。`pytest tests/` 收集到0个测试。

### 5.6 验收测试5项硬编码True（P0）

`test_m10_acceptance.py` 中5个验收项直接 `results["20.x.x"] = True`：
- §20.1.1: "前端路由已实现"（无验证）
- §20.2.9: "未实现这些功能"（通过了未实现功能的验收！）
- §20.3.7: "CLI不提供DB直接修改"（无负面测试）
- §20.4.1: "data status已实现"（只打印不断言）
- §20.4.3: "retry_list表已实现"（硬编码通过）

### 5.7 性能测试夹具不含复合指标（P0）

`test_m10_performance.py` 创建了 `asset_turnover` 复合指标但**从未添加到20条件夹具中**。PRD §19.1 PF3 要求"至少一个复合指标"——被静默违反。

### 5.8 CAGR测试文档化bug后跳过测试并声称通过（P0）

`test_m5_cagr_n.py` 明确记录了parser bug，SKIP了生命周期检查，然后打印"M5-5 CAGR N PARAMETER TEST PASSED"。

### 5.9 溯源表缺少5个PRD必需字段（P1）

前端只渲染 `field_name, report_date, value, source, confidence, fetch_time`。后端返回了 `effective_date, data_version, formula, as_reported_value, latest_restated_diff` 但前端不显示。PRD §14 违规。

### 5.10 无草稿自动恢复（P1）

PRD §12.1 / §20.1 #2 要求"若存在最近草稿，则自动恢复"。页面用硬编码默认条件初始化，从不持久化/恢复草稿。

### 5.11 CSV导出缺少必需元数据（P1）

PRD §12.5 / §20.1 #8 要求CSV含"数据日期、规则版本、指标版本、置信度与必要来源信息"。当前只加 `_data_date` 和 `_entry_explanation`。

### 5.12 无多字段排序（P1）

筛选页只支持单字段排序。PRD §12.5 / §20.1 #6 要求"明确多字段排序"。

### 5.13 addToWatchlist不传result_id（P1）

PRD §13 要求"记录来源于哪次筛选或哪条规则"。前端不传 `source_result_id`——来源追踪断裂。

### 5.14 死依赖（P1）

`echarts` 和 `pinia` 在package.json中但从未被任何Vue文件导入。增加node_modules体积。

### 5.15 K线无成交量面板（P1）

PRD §14 / §20.2 #3 要求"成交量"。只调用了 `chart.createIndicator('MA')`，未创建VOL指标。

### 5.16 自定义指标Tab使用硬编码列（P1）

`StockDetailPage.vue` 硬编码 revenue/parent_net_profit/gross_margin/roe，不调用 `/api/stock/{code}/available-fields` 端点。PRD §14 要求"自定义数值指标视图"——这是假的自定义。

### 5.17 其他前端问题

- 无NDialogProvider（模态框可能异常）
- 无404路由
- DataStatusPage无刷新按钮/自动刷新
- 错误显示无重试
- 无前端单元测试

---

## 第六部分：PRD合规检查

### P0 违规

| PRD条款 | 要求 | 现状 |
|---|---|---|
| §7.2 | "其余历史回填继续" | 价格仅5年，从未执行 |
| §7.4 L1 | "保留旧值，不以空值覆盖" | init/backfill 先DELETE后INSERT |
| §14 SD10 | 完整溯源 | fetch_batch=0, source_audit=0 |
| §6.7 D1 | ST/停牌/上市日期 | is_st误判, is_suspended全false, listing_date是假的 |
| §9.2 | 不允许伪装值 | CNINFO将预案解析为实施分红 |
| §16.1 | CLI JSON协议 | data_status/status输出纯文本 |
| §19.1 PF3 | 复合指标在夹具中 | 创建了但未加入条件 |
| §20.1 #6 | 入选解释 | 获取了但前端不显示 |
| §20.2 #9 | 不存在大表格/对比 | 验收硬编码True |

### P1 违规

| PRD条款 | 要求 | 现状 |
|---|---|---|
| §6.4 | 近5年价格 | 差5个月, BSE qfq全缺 |
| §6.5 | 标准化科目可供DSL | TDX返回未命名字段数组 |
| §11.4 DL7 | 强维度校验 | 循环检测是空操作 |
| §11.5 DL10-11 | 简写展开 | preview不展开简写 |
| §12.2 | 嵌套最多3层 | UI嵌套组不可用 |
| §12.5 | 多字段排序/版本/置信度 | 单字段排序, 无版本/置信度 |
| §13 | 来源记录 | addToWatchlist不传result_id |
| §14 | 溯源含7字段 | 前端只显示6个中的5个 |
| §18.3 AR11 | 离线恢复密钥可用 | 生成但永远不可用 |

---

## 第七部分：安全与运维

### 安全问题

| 问题 | 严重度 | 说明 |
|---|---|---|
| PDF路径遍历 | P0 | `%2F`编码绕过，可读取任意文件 |
| 备份SQL注入 | P0 | output_path/parquet_file直接插入SQL |
| 两段式确认绕过 | P0 | restore_execute不验证plan confirm |
| restore注入 | P1 | 恶意ZIP表名/列名注入SQL |
| PBKDF2过低 | P1 | 100K vs OWASP推荐600K |
| 密码明文 | P2 | 命令行参数可见 |
| 无内容类型验证 | P2 | PDF下载不验证Content-Type |

### 运维问题

| 问题 | 严重度 | 说明 |
|---|---|---|
| 备份未轮转 | P2 | 19个备份, PRD要求3套 |
| 日志无轮转 | P2 | data/logs/无限增长 |
| data目录膨胀 | P2 | 1.5GB无清理 |
| DuckDB单写者 | P1 | 后台回填导致Web API 500 |

---

## 第八部分：修复优先级

### P0 必须立即修复（16项）

1. 删除300只退市股孤儿数据
2. 修复is_st检测逻辑 (`startswith` 而非 `in`)
3. 获取is_suspended真实状态
4. 修复baostock `_normalize_stock_code` 编号处理
5. 完成全量历史价格回填
6. 重新抓取turnover_rate（baostock真实值）
7. 补充2025-Q2以后财报
8. 回填溯源数据
9. 过滤debt_ratio/gross_margin/pb_mrq极端值
10. 修复TTM退化路径（返回None而非无意义值）
11. 修复DSL parser func_call Transformer
12. 修复DSL codegen SQL注入（参数化查询）
13. 修复筛选引擎排序/列选择SQL注入
14. 修复PDF路径遍历
15. 修复备份SQL注入
16. 修复两段式确认绕过

### P1 建议修复（25项）

17. 修复override_revoke datetime未导入
18. 修复payout_ratio使用历史最大DPS
19. 修复preview不展开简写
20. 修复验证器循环检测
21. 修复筛选排名NULL处理
22. 修复行业排名NULL违反PRD §12.4
23. 修复content_hash损坏ast_json
24. 修复ROE/ROA用期末值而非平均值
25. 修复TTM退化不标记confidence
26. 修复init.py DELETE后INSERT违反PRD §7.4
27. 修复熔断器空结果视为失败
28. 修复熔断器探测逻辑
29. 修复恢复密钥不可用
30. 提高PBKDF2迭代次数
31. 修复Web API错误状态码
32. 补全CLI capabilities列表
33. 修复CLI data_status/status不用make_response
34. 修复screening save result_id类型
35. 修复前端嵌套规则组
36. 修复前端入选解释不显示
37. 修复K线内存泄漏
38. 修复vite.config.ts无开发代理
39. 修复测试硬编码True
40. 修复性能夹具不含复合指标
41. 修复CAGR测试跳过bug后声称通过

### P2 可延后（30+项）

42-72+. 各种代码质量、死依赖、缺少验证、运维改进等

---

## 附录：审计方法

### 数据审计
- Python脚本深度查询DuckDB和SQLite所有表
- 逐字段覆盖率统计、极端值检测、跨表一致性检查
- 抽样验证（茅台2024年报对比公开数据：总资产✓ 营收✓ 归母净利✓）

### 代码审计
- 4个并行子代理分别审查: 适配器+存储 / DSL+指标+筛选 / Web+CLI+备份 / 前端+测试+配置
- 每个代理读取所有相关文件的每一行
- 检查: 逻辑错误/边界条件/安全漏洞/PRD合规/性能问题/SQL注入/路径遍历

### PRD合规
- 逐条对照PRD §6-§20的所有要求
- 标注每条的合规状态

### 安全审计
- SQL注入/路径遍历/两段式确认绕过/密码安全/输入验证/加密强度
