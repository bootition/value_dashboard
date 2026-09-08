# 112_RED_TEAM_FIX_ALL_2026-09-08

日期：2026-09-08
类型：红队修复实施闭环（代码 + 测试 + 正式库数据修复）

## 结论摘要

对 2026-09-08 全项目红队审查发现的问题实施修复：

- **数据侧**：603365 半年报漏抓已修复并正式库回填到 2026-06-30，快照重算完成。
- **更新链**：公告分类、披露季时区、三表部分成功判定、价格全量替换、融资保旧值、
  funding 变更检测、retry 耗尽转 missing、国债 retry 精确清理、摘要缓存失效均修复。
- **指标重算**：新增价格域增量重算、分红融资比三列专项刷新、累计分红窗口查询；
  未变化股票直接跳过。
- **适配器**：CNINFO/Tencent/AKShare/BaoStock/TDX 截断与 partial 全部显式化；
  CNINFO 公告日期改用北京自然日；分红公告改专用类别查询。
- **筛选/DSL**：参数绑定顺序、sort/columns/年限校验、稳定排序、DSL 简写与版本锁、
  草稿乐观锁、自选批量写、覆写过滤、readiness 指纹补全。
- **Web**：指标重算端点升级为运维令牌；详情页财务新鲜度按披露季判断；前端文案修正。
- **构建**：Vite prebuild 清理旧产物，静态资产 391→40。

## 修复清单

### app/core/update.py
- `_ANNOUNCEMENT_FINANCIAL_KEYWORDS` 增补 `"半年报"`；类别查询结果携带
  `_announcement_category` 并直接按财务/分红类别处理，不再依赖标题措辞。
- 新增 `dividend` 类别查询；general 列表截断只记 warning，不阻断游标。
- `_current_expected_financial_period` 改用 `financial_period.expected_financial_period`
  （Asia/Shanghai），修复北京时间月初凌晨误判。
- `_refresh_financials`：只有三表各自最新期均 >= 期望期才视为成功入册，否则 pending。
- `_refresh_funding` 变更检测改按 calculator 同口径
  `COALESCE(raise_funds, raise_funds_net, issue_price*issue_shares)` 求和。
- `_update_prices_incremental` 返回 `_xdxr_codes/_raw_full_refetch`；
  `_persist_price_batch` 仅对"明确全历史请求"允许 DELETE+INSERT 替换，
  并增加最老日期覆盖校验。
- 指标步骤拆分：full_codes（财务/明细/retry）完整重算；
  price_codes 走 `compute_price_sensitive_for_codes`；
  event_codes 走 `refresh_funding_dividend_fields`。
- `_stale_snapshot_codes` 拆分为价格域/财务域两个查询，财务分支过滤上市状态。
- `_mark_retry_failed`：达到 max_retries 后（announcements 除外）转
  `missing_list(reason_code=retry_exhausted)` 并移出重试队列。
- `_refresh_due` 使用北京时间日期。

### app/core/indicators/calculator.py
- 新增 `compute_price_fields_for_stock` / `compute_price_sensitive_for_codes`：
  价格日期与收盘价均未变化且非 xdxr/raw 全拉时直接跳过；只更新价格域字段。
- 新增 `refresh_funding_dividend_fields`：只重算分红融资比三列。
- 新增 `_update_snapshot_price_fields` / `_update_snapshot_funding_dividend_fields`
  原地 UPDATE + lineage（DuckDB rowcount=-1 改用 SELECT 校验）。
- `_get_cumulative_dividend_amount` 改为窗口函数定位 ex_date 时点股本
  （600519 约 1.0s → 0.04s）。

### app/core/financial_period.py（新增）
- 共享的 Asia/Shanghai 披露季期望期函数。

### app/web/api/stock_detail.py / 前端
- `build_freshness_metadata`：财务是否滞后按期望报告期判断，不再把
  report_date 自然天数当滞后；新增 `financial_expected_date/financial_lagging`。
- `DataFreshnessCard.vue` 文案改为“财务报告期”。
- `DataStatusPage.vue` 文案“财报报告期截至”；运维令牌 prompt。

### app/web/main.py / data_status.py
- 新增运维令牌 `app.state.admin_token`（持久化 data/.vd-admin-token）；
  POST /api/data-status/indicator-recompute 必须携带 `x-vd-admin-token`。
- `/api/session` 不再提供运维令牌。
- 摘要缓存在写锁结束瞬间强制后台重建，短更新不再显示旧 last_update。

### 适配器与摄入
- CNINFO：截断显式抛错；announcement_date 用北京日期；分红路径显式 unsupported。
- Tencent：分页未覆盖 start 显式 error；close 清理 thread-local。
- AKShare/BaoStock/TDX：子请求失败计入 error，部分成功不再伪装完整成功。
- manager：close 重置初始化；保留首个 partial 数据；熔断只计 error+空。
- funding：ipo/placement 分侧按来源替换，合法空保旧值；队列按 missing 出队。
- treasury：retry 按 extra_json(mode/tenor/work_date) 精确清理。
- init：`_record_failure` 改为 ON CONFLICT UPDATE 不重置 retry_count；
  财务失败先记 retry、只有 error=None 空数据才记 missing。
- index_valuation：可空字段 COALESCE 保旧值。
- schema：retry 唯一索引先去重；funding_events v12 迁移按版本门禁。

### 筛选/DSL
- 参数顺序按 SQL 文本顺序组装；sort/columns/年限严格校验；稳定排序。
- DSL 简写带 @ 不二次展开；依赖锁最新 published；字段名冲突防护。
- 草稿乐观锁原子 UPDATE；自选去重 + executemany；overrides 按池过滤并设上限；
  readiness 指纹纳入财务报表/覆写/分红/trading_dates。
- DSL 创建字节预算；publish/delete 按 id 查询；前端 right_field 不可信告警。

### 构建
- `frontend/scripts/clean-dist.mjs` + npm prebuild，修复 Vite 8.1.5 不清理旧产物。

## 测试
- ruff check app：通过。
- 前端 vue-tsc/npm test/npm run build：通过（build 后 static assets 391→40）。
- S1 定向回归：更新/公告/周期/指标价格域/价格 lineage/融资/国债/适配器/筛选/DSL/
  资本历史域等全部通过。
- 正式库数据修复：`vd data update --stocks 603365` 后三表 MAX(report_date)=2026-06-30、
  快照重算成功。
