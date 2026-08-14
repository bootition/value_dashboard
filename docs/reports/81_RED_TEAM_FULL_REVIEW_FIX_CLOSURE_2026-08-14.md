---
title: 系统红队全面审查（第二轮）修复闭环 — 6×P1 + 21×P2 + 20×P3
status: approved
category: reports
created: 2026-08-14
last-reviewed: 2026-08-14
supersedes: reports/80_SYSTEM_RED_TEAM_FULL_REVIEW_2026-08-14.md
---

# 81. 系统红队全面审查（第二轮）修复闭环

- 修复日期：2026-08-14
- 发现基线：`reports/80`（6×P1 + 21×P2 + 20×P3，NOT PASS）
- 修复范围：后端核心链路 / 前端 / 入口与运维 / 测试与门禁 / 正式库数据
- 验证口径：代码级 + 数据级双证；门禁复验见 §5

## 0. 总体裁决

**PASS（修复完成，门禁全绿）。** `reports/80` 全部 6×P1 + 21×P2 + 20×P3 均已关闭；其中
"dividends ex_date 占位符"（原列长期待办）本次一并完成正式库数据修复。剩余项为有界
续传中的增量工作与已记录的长期评估项（见 §6），不构成阻断。

## 1. P1 修复（6 项）

### F1 share_capital_history ART 索引损坏死循环 —— 已修复
- 根因：DuckDB 1.5.5 同一事务内 `DROP INDEX`+`CREATE INDEX` 触发
  `InternalException: BoundIndex::CreateDeltaIndex`，索引进入损坏态；300479 删除
  38 行时 FATAL；`update.py` retry 消费无 per-task 隔离 → 整轮 failed 且 retry 永不递增。
- 修复：
  1. `scripts/repair_share_capital_history_index.py` —— 重建索引拆分为**两个独立事务**
     （DROP 提交后再 CREATE），并对 300479 + 9 只抽样做 DELETE+ROLLBACK 净零验证；
  2. `app/core/update.py` retry 循环 per-task try/except 隔离（与 capital.py 对照路径一致），
     `_is_duckdb_fatal` 识别 FatalException/invalidated 后计入 retry_count 并隔离该股，
     不再整轮失败。
- 正式库验证（2026-08-14 单写者窗口执行）：索引重建成功，delete_verify **10/10 通过**
  （含曾 100% FATAL 的 300479）；`retry_list` 中 300479 已消费清空。
- 后续：DuckDB 升级评估（1.5.5 → 修复版）列入 §6 长期项。

### F2 国债曲线缺失日永久不补 —— 已修复
- 根因：`update_daily` 只抓今天；source_empty 记 missing 却无任何消费路径。
- 修复：`app/core/treasury.py` 新增 `backfill_missing_days(max_days=3)` ——
  交易日历最近 N 日 ∪ 未解决 missing_list 条目，剔除已满期限覆盖日与未来日，
  按升序有界补抓；`refresh_if_due` 每轮调用并汇报 gap_fill；单日成功即自动解决
  对应 missing 条目。`app/core/update.py` 补缺接线。
- 证据：新增 5 项回归测试 `test_treasury_curve_domain.py`（缺口目标升序有界 /
  max_days 上限 / missing_list 合并 / 未来日排除 / 全覆盖跳过）全绿。

### F3 百分比单位口径双向不一致 —— 已修复（单一来源）
- 根因：前端静态 PCT_FIELDS 无法覆盖 小数/百分数 两种存储口径；`77f7a3b` 对
  百分数字段 ÷100 构成回归（显示 529%）。
- 修复：
  - `app/core/screening/field_units.py` ——
    单位元数据单一来源：`PCT_DECIMAL_FIELDS`（小数存储）、`PERCENT_STORED_FIELDS`
    （百分数存储）、`RATIO_FIELDS`、`PRICE_FIELDS` → `field_unit()`；
  - `app/web/api/screening.py` `/indicators` 下发 `unit`（pct|percent|price|ratio|plain）；
  - 前端 `screening-format.ts` `applyIndicatorUnits()` 以服务端 unit 为准（静态集仅兜底），
    `RuleConditionRow.vue` toRaw ÷100 仅 pct、toDisplay ×100 toFixed(6)（含 P3 精度修复）。
- 证据：7 项契约测试 `frontend/tests/screening-format.test.ts` 钉死两种口径；后端
  单元测试覆盖 unit 下发；正式库字段分类与 80 表 3 数据实测一致。

### F4 双击 exe 无 env 崩溃 —— 已修复
- 修复：`app/core/storage/path_policy.py` frozen 分支：无 env 时自动推导 exe 同级
  `data/`（formal），formal 分支豁免 ACK（双击即显式意图）并校验；test/staging 保持
  fail-closed。`docs/runbooks/user-first-use.md` 表述同步。
- 证据：`test_path_isolation.py` 4 项 F4 测试 + wheel 打包测试（`test_wheel_contents.py`）全绿。

### F5 自选页配置过列 TDZ 白屏 —— 已修复
- 修复：`WatchlistPage.vue` `allColumnOptions` 声明前移至使用点之前。
- 证据：`frontend/tests/component/watchlist-flow.test.ts` 预置 localStorage 挂载不抛错。

### F6 详情页国债/统计卡片跨股票串数据 —— 已修复
- 修复：`TreasuryComparisonCard.vue` / `ResearchStatisticsCard.vue` 增加
  `watch(() => props.stockCode)` 清空数据并重拉。
- 证据：组件测试 + 全量前端门禁。

## 2. P2 修复（21 项）

| # | 修复 | 位置 |
|---|---|---|
| P2-1 | `any_write_lock_active` = update 锁 OR duckdb 写锁；data_status/screening/watchlist 三处统一 | `app/core/storage/update_lock.py`、`data_status.py`、`screening.py`、`watchlist.py`；回归 `test_screening_gate.py` |
| P2-2 | read_connection 显式 read_only=True（同进程写连接并存时自动回退同配置连接） | `duckdb_store.py` |
| P2-3 | `_connect_writer` 有界重试（0.5/1/2/3/4s）对齐读侧 | `duckdb_store.py` |
| P2-4 | strict_only 恒空 → 引擎回传 `strict_mode_warning`（无 strict 血缘字段清单），前端结果面板告警横幅（空结果也显示） | `screening/engine.py`、`ScreeningResultsPanel.vue`；组件测试 |
| P2-5 | S1 证据从 5 个 DB 文件扩展到整棵 data/ 树指纹 | `s1-path-preflight.ps1` |
| P2-6 | freeze 收窄：任意 python 进程不再误伤，仅 写锁<30s 或 8765 监听 冻结 | `s1-path-preflight.ps1` |
| P2-7 | PS 5.1 入口守卫（实测 5.1 拒绝 + 7+ 通过） | `s1-pytest.ps1` |
| P2-8 | 方案 A 门禁零测试 → `test_screening_gate.py` 6 项直接测试（no-lock/update 锁/duckdb 锁/空快照 409/run 标注持久化） | `tests/regression/test_screening_gate.py` |
| P2-9 | collection_safety 去嵌套 pytest 收集与正式库依赖；AST 守卫扩展（Config.current、赋值形态） | `test_collection_safety.py` |
| P2-10 | node 测试入口按目录自动发现（run-unit-tests.mjs）；engines `>=22.6.0` | `frontend/package.json`、`run-unit-tests.mjs` |
| P2-11 | start.bat 失败分支 pause + exit /b 错误码 | `start.bat` |
| P2-12 | start.bat venv 优先（不再先 where python） | `start.bat`、`vd.bat` |
| P2-13 | TestClient 统一 close（portal 线程回收）+ 等后台 refresh 线程退出；`_unlink_with_retry` 耗尽后抛错而非裸 unlink | `test_data_status_cache.py` |
| P2-14 | CLI 读型命令 initialize=False（screening_list/override_list/screening_export_csv/data_list_pdfs） | `app/cli/main.py` |
| P2-15 | dist 冒烟残留清理（build-release.ps1 冒烟后删除临时库） | `build-release.ps1` |
| P2-16 | import_csmar/patch_deducted_profit `--yes` + 自动 parquet 备份 | `scripts/`（`_maintenance_safety.py`） |
| P2-17 | pip 安装态无 env → 协议化 E004 + Exit(2)，不再裸 traceback | `app/cli/main.py`；`test_cli_quality_integration.py` |
| P2-18 | vd.bat/start.bat setlocal（env 不泄漏父会话） | 两 bat |
| P2-19 | 空过滤分组仍返回全量 groups（侧栏按钮不消失） | `watchlist.py` |
| P2-20 | 运行按已保存版本执行时明确警告（P2-20 已有）；与 P3 覆盖确认协同 | `ScreeningPage.vue` |
| P2-21 | axios 全局超时 30s | `frontend/src/http.ts` |

## 3. P3 修复（20 项）

后端：
1. 国债 N+1 查询 → `calculator.py` 批量查询（一次取全部期限再分组）。
2. 业务概览 60/5,543 覆盖 → 自动更新每轮有界续传（20 只/轮，STATUS #5 机制）+ `business.py:346`
   死代码删除；全量回填由续传渐进完成（§6）。
3. dividends_quarantine 50,359 行占位符 → **本次完成正式库数据修复**（§4）。
4. `transaction()` `conn.begin()` 移入 try，失败由 finally rollback/close 兜底。
5. update_lock 空/半写文件宽限期（5s）+ PID 复用检测（记录进程创建时间比对）。
6. DSL validator source_period 死赋值修复（循环检测路径恢复真实来源报告）。
7. research_statistics ART 索引同型风险 → 全库无 `DROP INDEX` 代码路径（已核验），
   DELETE+INSERT 重建不触发 1.5.5 同事务索引 bug；F1 修复脚本覆盖通用重建模式。
8. `_build_select` AS-alias 死分支 + 死参数 `rank_fields` 删除。
9. pyproject markers 注册（integration/data_dependent，`--strict-markers` 收集通过）。
10. ruff 规则集扩展 E4/E7/E9/F + I/UP/B/SIM：326 处自动修复 + 37 处手工收敛。

入口与运维：
11. start.log 10MB 轮转 + PYTHONIOENCODING=utf-8（中文不再乱码）；
    历史 1,086 条 Traceback 随轮转归档，新日志从干净状态开始。
12. `vd backup` → `vd backup create` 文档修正（README、runbook）。
13. README Verdict 滞后 → 指向 STATUS + reports/80/81。
14. `config/sw_industry_raw.xls`、`sw_industry_fallback_result.json` 取消 git 跟踪。
15. `scripts/evidence/` 15 个 JSON 取消 git 跟踪（加入 .gitignore）。
16. chain-finalize.ps1 去硬编码 PID（≤0 跳过等待）+ venv python 优先。
17. check_*.py（6 个）锚定仓库根 `PROJECT_ROOT`，不再依赖 CWD。
18. data/ 测试残留（test_codes.csv/test_results.json/test_writeback.csv）迁
    `_legacy/2026-08-14-redteam-test-residue/`。

前端：
19. 搜索页 requestSeq 竞态守卫 + 10s 超时 + 错误展示；DataStatusPage isPolling 修复；
    K 线换 loader+resetData 增量更新 + 卸载 abort；加载规则覆盖确认（相对上次同步快照）；
    toFixed(4) 精度修复（toFixed(6)）；between 切单值收敛；auto-update 10s/60s 轮询 +
    卸载清理；PDF URL encodeURIComponent。

S1/门禁：
20. S1 用仓库 venv python（不再 PATH 3.14）；证据哈希排除 backup/archive 产物目录内容
    哈希（保留 存在性+长度+修改时间，每轮读盘 67GB→约 24GB）；root conftest 内层证据
    `pytest_configure/unconfigure`（合约 §8.2，纯 stdlib 逐块 SHA-256 → VD_TEST_EVIDENCE_ROOT）；
    `test_hash_preservation.py` 哈希完整性测试（合约 §8.5/§10.1，合成哨兵文件）；
    S1 证据 JSON 读取改 `ConvertFrom-Json -AsHashtable`（修复 PSCustomObject.Keys
    在 StrictMode 下崩溃，首次全量 S1 复跑中暴露并修复）。

## 4. 数据层修复：dividends ex_date 占位符（STATUS 缺口 #4）

- 问题：`dividends_quarantine` 50,359 行（reason=unverified_period_end_placeholder），
  ex_date 为 12-31/06-30 期末占位；`dividends` 表 56,879 行。
- 修复脚本：`scripts/repair_dividend_ex_dates.py`（默认只读 dry-run；`--yes` 写前自动
  parquet 备份 dividends + dividends_quarantine；单写者守卫拒绝锁活跃期运行）。
- 匹配规则（保守 fail-closed）：同股票 xdxr.category=1 除权除息事件，
  event_date ∈ [占位日−30天, +460天]，派息 |fenhong−dps|≤0.005 或相对差≤1%；
  唯一候选才处理；跨行映射同一真实除权日的整组保留隔离。
- 结果（正式库实测）：
  - 恢复真实 ex_date 并入 dividends：**278 行**；
  - 判重删除（同股票同除权日已有派息一致的行）：**41,614 行**；
  - 保留隔离（无候选 7,050 / 歧义 787 / 与现有行冲突 18 / 跨行冲突 602）：**8,467 行**。
- 修复后：dividends 57,157 行；quarantine 8,467 行（15→可核实率大幅提升；
  剩余为真正不可核验项，继续如实隔离）。
- 证据：`docs/evidence/evidence-dividend-exdate-repair-20260814-131744.json`；
  备份 `.planning/maintenance-backup-dividend-exdate-repair-20260814_131718/`。

## 5. 门禁复验（2026-08-14）

| 门禁 | 结果 |
|---|---|
| S1 隔离全量回归（`scripts/s1-pytest.ps1 tests/regression`，PS7 + venv python + 全树指纹） | **613 passed + 正式库前后指纹一致（exit 0）**。过程中另修复两处门禁自身缺陷：① 证据 JSON 读取 `ConvertFrom-Json -AsHashtable`（StrictMode 下 PSCustomObject.Keys 崩溃）；② 门禁按设计拦截了一次 sqlite -wal/-shm 生命周期变化（审查探针在门禁运行期间打开正式 SQLite 所致），安静窗口复跑全绿 |
| Ruff（`app tests/regression conftest.py scripts/repair_dividend_ex_dates.py`，扩展规则集） | 全绿（0 violations） |
| 前端 lint | 全绿 |
| 前端 node 单测 | 62/62 |
| 前端 vitest | 50/50 |
| 前端 build（vue-tsc + vite + sync-static） | 通过，产物已同步 app/web/static |

## 6. 遗留与后续（不阻断 PASS）

1. **DuckDB 1.5.5 同事务索引重建 bug**：以"分事务重建"绕过；升级评估列入后续
   （当前 1.5.5 单写者+分事务模式下无复发路径）。
2. **业务概览全量回填**：自动更新每轮有界续传（20 只/轮）渐进覆盖 5,543 只。
3. **dividends_quarantine 剩余 8,467 行**：真实除权日不可核验（无 xdxr 候选或歧义），
   继续隔离并如实披露，不伪造 ex_date。
4. **08-13 旧保存规则的原始值**：在 77f7a3b 单位 bug 期间保存的规则条件可能持有错误
   原始值（如 ttm_dividend_yield 存 0.02 而实际口径为百分数）；前端已按 unit 元数据
   正确换算显示，旧规则建议用户复核保存一次新版本。
5. **start.log 历史 Traceback**：随 10MB 轮转归档；新日志从干净状态开始。
6. **方案 F（常驻托盘）**：远期 UX 项（reports/79 已列）。

## 7. 证据位置

- 机器证据：`docs/evidence/evidence-dividend-exdate-repair-20260814-131744.json`
- 会话产物：`.planning/2026-08-14-red-team-full-review/`
- 门禁证据：`docs/evidence/evidence-s1/`（每次 S1 轮次 before/after 指纹，不入 git）
