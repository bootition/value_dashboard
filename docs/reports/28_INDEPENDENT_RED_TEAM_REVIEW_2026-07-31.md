---
title: 独立红队复审报告（2026-07-31）
status: superseded
category: reports
last-reviewed: 2026-07-31
superseded-by: reports/29_DATA_REBUILD_REPORT_2026-07-31.md
---

# 独立红队复审报告（2026-07-31）

## 裁决

**BLOCK。** 当前版本不得发布，也不得将正式数据库用于筛选、导出或投资研究。

本报告独立复核了现有审计材料、当前工作树、当前正式数据库文件哈希、前端质量门禁与依赖锁。正式数据库保持冻结；本轮未执行 Python、pytest、应用、CLI、DuckDB 或 SQLite 命令，未写入正式数据。

## 审查范围与方法

- 范围：Web/API 安全边界、前端与打包链、数据采集至指标和筛选链、数据质量门禁、运行可靠性、测试与依赖供应链。
- 数据证据：使用当前工作树中绑定正式库哈希的只读证据文件，并重新计算两个正式数据库的 SHA-256。
- 代码证据：逐文件静态审查当前实现；不将旧报告结论直接当作事实。
- 执行验证：`npm run lint` 通过；`npm test` 为 46/46 通过；生产依赖 `npm audit --omit=dev` 为 0 漏洞；完整锁文件审计为 5 个 High 漏洞。

## 证据基线

| 项目 | 当前证据 | 结论 |
|---|---|---|
| DuckDB SHA-256 | `21CE1CD890428E15714D4698A51653CC9C0E98E2A3FAF09BD120DB15C2DE4C70` | 与股本、质量证据文件一致 |
| SQLite SHA-256 | `283F7A8F3190AE6E8B6438AD637C7BA7F211A3F1C8FCDC41D3559417FACE0797` | 当前正式库冻结 |
| 正式质量状态 | `LINEAGE_INVALID`、`MINIMUM_DATA_NOT_READY` | 不可研究/不可交付 |
| 前端 lint | 通过 | 仅证明静态代码风格 |
| 前端合约测试 | 46/46 通过 | 仅覆盖 mock/纯函数合约 |
| 完整 npm 审计 | 5 High，0 Critical | 开发与 CI 供应链不合格 |

## P0：发布阻断

### P0-1：SSE 股本单位混用，正式研究结果不可信

- 证据：`docs/evidence-data-reaudit-share-capital-20260731.json:15-24,34-40,67-85` 绑定当前 DuckDB 哈希。2,283 只可比较 SSE 股票中，1,215 只 `circ_shares > total_shares`，最大比值为 16,459.419511。样例 `688428` 的总股本为 23,494、流通股本为 386,697,602。
- 代码路径：schema 定义两个字段的单位均为“股”，见 `app/core/storage/schema.py:37-38`；估值计算直接将收盘价乘以总/流通股本，见 `app/core/indicators/calculator.py:475-503`。
- 影响：总市值、流通市值、PE、PB、PS、PCF 及依赖市值的排序都可能严重失真。此问题不需要攻击者参与，正常读取或计算即可触发。
- 放行条件：从具有字段单位与 as-of 日期的权威来源重建股本；全市场断言 `total_shares > 0`、`circ_shares > 0`、`circ_shares <= total_shares`；保存字段级原始材料、映射和审计记录；随后重算并发布快照，并作独立外部抽样验收。

## P1：高风险问题

### P1-1：正式数据无法证明完整、当前或可追溯

- 证据：`docs/evidence-formal-share-capital-reaudit-20260731.json:244-305` 显示：5,534 只股票均缺字段级 lineage coverage；archive gap 为 2,535,176，其中 2,535,043 个空 payload；5,532 只存在价格新鲜度缺口。
- 代码：`app/core/data_quality.py:292-342,374-414` 对该类问题作检测；当前警告状态仍为 `LINEAGE_INVALID` 和 `MINIMUM_DATA_NOT_READY`。
- 影响：任何显示、保存或导出的结果都无法可靠回放到真实的上游材料。

### P1-2：指标重算发布未执行完整数据质量门禁

- 代码：`app/core/indicators/calculator.py:123-174` 仅检查三表、raw 和 QFQ 最低存在性，随后在 `:220-234` 发布全量快照。
- 对照：完整就绪标准还包括股本关系、字段级来源、交易日历、新鲜度等，见 `app/core/data_quality.py:137-289`。
- 影响：可原子发布“覆盖完整但输入不可信”的快照；原子性不等于正确性。

### P1-3：质量完整性校验在请求热路径全量读取归档 BLOB

- 代码：`app/core/data_quality.py:407-414` 每次质量状态构建都读取所有非空 archive payload 并在 Python 中逐条 SHA-256；`app/web/api/screening.py:28-44` 使筛选、保存、导出和加入自选进入该门禁。
- 影响：归档增长会使用户请求的内存、CPU 和响应时间线性增长，最终使单进程本地服务不可用。

### P1-4：不可信快照仍可由详情和自选接口以正常数值返回

- 代码：`app/web/api/stock_detail.py:263-340` 与 `app/web/api/watchlist.py:62-112` 直接返回 `indicator_snapshot` 数值，未在接口层执行质量决策。
- 当前前提：正式库已有 `LINEAGE_INVALID` 和 `MINIMUM_DATA_NOT_READY`。
- 影响：用户无需执行被门禁拦截的筛选或导出，就可能把 PE、PB、ROE 和市值当作可用研究结论。

### P1-5：筛选运行记录由任意请求全局清理，跨标签页可丢失未保存结果

- 代码：`app/web/api/screening.py:96-99` 每个运行请求删除全局一小时前的 `screening_runs`；`save_result` 在 `:194-216` 找不到 run 后失败。
- 影响：页面 A 的未保存研究结果可被页面 B 的正常筛选删除。

### P1-6：启动维护任务不可观测、不可取消，也未限制重入

- 代码：`app/web/main.py:44-90,311-319` 启动 daemon 线程执行远程初始化/增量检查，但不持有线程状态、超时、进度或取消控制。
- 影响：首次运行或上游阻塞时，端口已监听但用户无法判断数据是否可用，也无法安全诊断或终止任务。

### P1-7：开发/CI 依赖链有 5 个 High 漏洞

- 证据：本轮 `npm audit --package-lock-only --json` 报告 5 High、0 Critical。漏洞经 `eslint -> minimatch -> brace-expansion` 传播，GHSA-mh99-v99m-4gvg 可导致内存耗尽。
- 代码：发布脚本实际调用 `npm run lint`，见 `scripts/build-release.ps1:37-46`；依赖版本见 `frontend/package.json:20-30`。
- 影响：生产浏览器依赖审计为 0，但开发者或 CI 对不可信分支/模式运行 lint 时仍有拒绝服务风险。

## 已验证的有效控制

- Web 服务强制 loopback，拒绝非本地 host，见 `app/web/main.py:34-41`。
- Host 白名单、同源 Origin 校验和每次启动生成的写令牌保护 API 写入，见 `app/web/main.py:125-148`。
- SQL 值使用参数化；筛选导出对公式前缀转义，见 `app/web/api/screening.py:22-25,292-301`。
- 筛选持久化入口统一执行服务端 screenability 门禁，见 `app/web/api/screening.py:28-44,82-88,186-190,252-256,307-311`。
- 快照使用 staging 后发布，避免部分计算覆盖当前世代，见 `app/core/indicators/calculator.py:220-234`。
- 当前 `app/web/static/index.html` 引用的入口 JS、预加载 JS 和 CSS 均存在；但发布脚本未验证递归依赖和浏览器可启动性。

## 验证局限

- 未重新查询冻结正式库，数据结论来自与当前 SHA-256 一致的只读证据包。
- Host、Origin、写令牌和路由防护为静态复核，未启动 FastAPI。
- 没有外部真值抽样，无法证明财报、行情、分红、公司行为和股本的经济正确性。
- 前端通过的测试不覆盖真实 FastAPI、正式静态产物和浏览器关键路径。

## 最低放行条件

1. 关闭 P0-1：重建和独立抽样验收股本，重算快照。
2. 重建或明确隔离无来源数据，使正式 `readiness=true`，且没有 `LINEAGE_INVALID` 与 `MINIMUM_DATA_NOT_READY`。
3. 指标发布前执行完整质量门禁；详情与自选不可将不可信数值伪装为正常研究数据。
4. 将归档完整性验证移出用户请求热路径，并为启动维护增加状态、互斥、超时和取消语义。
5. 修复筛选 run 生命周期并发问题。
6. 升级并重新锁定前端 lint 依赖，使完整 `npm audit` 无 High/Critical。
7. 在隔离 profile 完成真实后端、浏览器、发布产物、恢复中断与外部真值验收。

## 整改记录（2026-07-31 同日闭环）

以下代码级 P1 项已完成修复并通过验证；P0-1 为正式数据问题，代码防护已就位，数据重建仍待权威外部材料。

| 编号 | 状态 | 修复摘要 | 验证 |
|---|---|---|---|
| P1-2 快照门禁不完整 | 已修复（代码） | 新增 `share_capital_violations` 与 `snapshot_publish_gate`（fail-closed），`compute_snapshot_for_all` 发布前强制门禁，可注入完整 `screening_readiness`；不通过返回 `rejected` 且不触碰已发布快照。见 `app/core/data_quality.py:117-152`、`app/core/indicators/calculator.py:114-204` | 隔离回归含 circ>total 拒绝发布与保留旧快照用例；全量 309 passed |
| P1-3 归档校验热路径 | 已修复（代码） | archive hash 校验改为 60s 进程内 TTL 缓存，支持 `force_archive_hash_recheck`，查询失败 fail-closed 且不写缓存；热路径不再每次全扫 BLOB。见 `app/core/data_quality.py:358-428` | 缓存命中/强制刷新/fail-closed 回归；全量 309 passed |
| P1-4 详情/自选返回不可信数值 | 已修复（代码） | 新增服务端 `indicator_trust`/`mask_untrusted_values`/`read_warning_codes`（30s TTL，fail-closed）；`/indicators` 与 watchlist `/list` 在阻断警告下遮蔽快照数值并附 `trust` 契约；前端渲染"数据不可信"而非数字 | 后端 8 项 + 前端 6 项合约测试；52/52；build 通过 |
| P1-5 筛选 run 跨标签页丢失 | 已修复（代码） | TTL 常量化（24h），过期清理为参数化单语句惰性回收；有效期内 run 不再被其他请求删除。见 `app/web/api/screening.py:20-21,99-104` | 新增 TTL/并发用例；定向 13 passed |
| P1-6 启动维护不可观测/可重入 | 已修复（代码） | 模块级锁防重入，终态（idle/running/done/failed+error）写入 `app.state.startup_maintenance`，新增 GET `/api/maintenance/status`。见 `app/web/main.py:33-34,86-114,139,240-246` | 新增 5 个用例（重入/失败/初始化失败）；定向 13 passed |
| P1-7 ESLint 供应链 5 High | 已修复 | eslint 9.39.5→10.8.0、@eslint/js→10.0.1、新增 globals；lockfile 更新 | `npm audit --package-lock-only`：0 High/0 Critical/0 Moderate/0 Low；lint/test/build 通过 |
| P0-1 SSE 股本单位混用 | **未关闭（数据）** | 代码侧已加 `circ_shares<=total_shares` 发布门禁，防止错误数据再次物化为快照；但正式库中 1,215 条不可能记录需要带单位/as-of 日期的权威外部来源重建，禁止猜测性改写 | 待外部真值与事务化导入验收 |

**整改后整体验证（本人独立执行）：**
- `scripts/s1-pytest.ps1 tests/regression` 全量：**309 passed**，正式库哈希前后一致（DuckDB `21CE...4C70`、SQLite `283F...0797` 未变）。
- 前端：`npm run lint`、`npm test`（52/52）、`npm run build`（含静态同步）全部通过。
- `uv run --locked ruff check app tests/regression`：All checks passed（顺带清除 3 个存量 F401/F841）。
- `npm audit --package-lock-only`：0 High / 0 Critical。

**仍然 BLOCK 的原因：** P0-1 正式股本数据未重建；正式库仍带 `LINEAGE_INVALID` 与 `MINIMUM_DATA_NOT_READY`（字段级 lineage coverage 缺口、价格新鲜度缺口等需权威外部数据补齐）；外部真值抽样、真实后端浏览器验收与发布产物端到端演练未完成。代码级 P1 已全部关闭，剩余为数据与外部验收工作。
