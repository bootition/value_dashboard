---
title: 系统性代码完善度与漏洞审查报告（2026-07-26）
status: superseded
category: reports
last-reviewed: 2026-07-26
superseded-by: reports/23_INDEPENDENT_RED_TEAM_AUDIT_2026-07-29.md
---

# 系统性代码完善度与漏洞审查报告（2026-07-26）

> **Verdict: CONDITIONAL PASS（代码结构）/ BLOCK（数据层面不变）**
>
> 审查对象：全部前端源码、后端 API、CLI 命令、核心模块、类型定义
> 审查方法：按图索骥——从 PRD 功能清单 → 前端页面 → API 端点 → 核心模块 → 数据存储，逐层追踪
> 本报告承接 `docs/11_RED_TEAM_AUDIT_V2.md` 和 `docs/17_PRODUCTION_READINESS_AUDIT.md`

---

## 1. 功能完善度总览

### 1.1 PRD §5 页面覆盖

| 页面 | 路由 | 前端文件 | 状态 |
|---|---|---|---|
| 筛选页（默认首页） | `/screening` | `ScreeningPage.vue` | ✅ 已实现 |
| 自选列表页 | `/watchlist` | `WatchlistPage.vue` | ✅ 已实现 |
| 个股详情页 | `/stock/:code` | `StockDetailPage.vue` | ✅ 已实现 |
| 只读数据状态页 | `/data-status` | `DataStatusPage.vue` | ✅ 已实现 |
| 404 页 | `/:pathMatch(.*)*` | `NotFoundPage.vue` | ✅ 已实现 |

**结论：PRD §5 四个页面 + 404 全部实现。**

### 1.2 前端组件覆盖

| 组件 | 文件 | 被引用 | 状态 |
|---|---|---|---|
| 筛选规则编辑器 | `ScreeningRuleEditor.vue` | ScreeningPage | ✅ 递归渲染、深度限制 |
| 筛选结果面板 | `ScreeningResultsPanel.vue` | ScreeningPage | ✅ 保存/导出/加入自选 |
| 规则条件行 | `RuleConditionRow.vue` | ScreeningRuleEditor | ✅ |
| 指标标签页 | `IndicatorTabs.vue` | StockDetailPage | ✅ 6 类指标 + 自定义 |
| 财务趋势卡 | `FinancialTrendCard.vue` | StockDetailPage | ⚠️ 已实现但未被使用 |
| DSL 指标管理 | `DslIndicatorManager.vue` | ScreeningPage | ✅ CRUD + 发布 |
| 数据溯源 | `DataTraceability.vue` | StockDetailPage | ✅ 字段/批次溯源 + PDF |
| 数据新鲜度 | `DataFreshnessCard.vue` | StockDetailPage | ✅ |

### 1.3 后端 API 端点覆盖

| 端点 | 文件 | 状态 |
|---|---|---|
| `GET /api/health` | `main.py` | ✅ |
| `GET /api/db/status` | `main.py` | ✅ |
| `GET /api/data-status/summary` | `data_status.py` | ✅ |
| `GET /api/data-status/retry-list` | `data_status.py` | ✅ |
| `GET /api/data-status/missing-list` | `data_status.py` | ✅ |
| `POST /api/screening/run` | `screening.py` | ✅ |
| `POST /api/screening/save` | `screening.py` | ✅ |
| `GET /api/screening/results` | `screening.py` | ✅ |
| `GET /api/screening/results/{id}` | `screening.py` | ✅ |
| `POST /api/screening/export_csv` | `screening.py` | ✅ |
| `POST /api/screening/add_to_watchlist` | `screening.py` | ✅ |
| `GET /api/screening/indicators` | `screening.py` | ✅ |
| `POST /api/screening/rules/save` | `screening.py` | ✅ |
| `GET /api/screening/rules` | `screening.py` | ✅ |
| `GET /api/screening/rules/{id}` | `screening.py` | ✅ |
| `GET /api/watchlist/list` | `watchlist.py` | ✅ |
| `POST /api/watchlist/add` | `watchlist.py` | ✅ |
| `DELETE /api/watchlist/remove` | `watchlist.py` | ✅ |
| `POST /api/watchlist/move` | `watchlist.py` | ✅ |
| `GET /api/watchlist/groups` | `watchlist.py` | ✅ |
| `GET /api/stock/{code}/info` | `stock_detail.py` | ✅ |
| `GET /api/stock/{code}/kline` | `stock_detail.py` | ✅ |
| `GET /api/stock/{code}/indicators` | `stock_detail.py` | ✅ |
| `GET /api/stock/{code}/financial-trend` | `stock_detail.py` | ✅ |
| `GET /api/stock/{code}/source-audit` | `stock_detail.py` | ✅ |
| `GET /api/stock/{code}/custom-trend` | `stock_detail.py` | ✅ |
| `GET /api/stock/{code}/available-fields` | `stock_detail.py` | ✅ |
| `GET /api/stock/{code}/pdf/{filename}` | `stock_detail.py` | ✅ |
| `GET /api/stock/{code}/pdf-list` | `stock_detail.py` | ✅ |
| `GET /api/dsl/expressions` | `dsl.py` | ✅ |
| `POST /api/dsl/expressions` | `dsl.py` | ✅ |
| `POST /api/dsl/validate` | `dsl.py` | ⚠️ 桩实现 |
| `PUT /api/dsl/expressions/{id}/publish` | `dsl.py` | ✅ |
| `DELETE /api/dsl/expressions/{id}` | `dsl.py` | ✅ |

---

## 2. 未完善实现（按严重度排序）

### 🔴 P0：功能性缺陷

#### 2.1 DSL 验证 API 是桩实现
**位置：** `app/web/api/dsl.py:87-109`
**问题：** `/api/dsl/validate` 只检查括号匹配和字段名包含，不执行真正的 DSL 解析/校验/代码生成。前端 `DslIndicatorManager.vue` 调用此 API 预览表达式，得到的 `previewResult` 永远显示 `valid: true`。
**影响：** 用户创建的 DSL 表达式可能语法错误但被报告为有效。
**对比：** CLI 路径 `DSLEngine.validate()` 有完整的解析→校验→代码生成流程，但 Web API 未接入。
**修复建议：**
```python
@router.post("/validate")
async def validate_expression(req: ValidateExpressionRequest, request: Request):
    from app.core.dsl.engine import DSLEngine, expand_shorthand
    from app.core.dsl.parser import parse
    engine = DSLEngine(sqlite=request.app.state.sqlite)
    expanded = expand_shorthand(req.expression)
    try:
        ast = parse(expanded)
        return {"valid": True, "expanded": expanded, "ast": ast.to_dict()}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
```

#### 2.2 公告检查未实现
**位置：** `app/core/update.py:180-189`
**问题：** `_check_new_announcements()` 固定返回 `{"status": "not_implemented"}`。PRD §7.3 要求启动时检查新公告/财报。
**影响：** 增量检查无法发现新的财务披露，`needs_update` 判断不完整。
**当前缓解：** 状态 API 和前端已展示此限制，但实际检测能力为零。

#### 2.3 前端 FinancialTrendCard 组件未被使用
**位置：** `frontend/src/components/FinancialTrendCard.vue`
**问题：** 该组件已完整实现（SVG 图表 + 表格双视图），但 `StockDetailPage.vue` 未引用它。个股页的财务趋势功能通过 `IndicatorTabs.vue` 的"自定义指标"标签页部分实现，但缺少图表视图。
**影响：** PRD §14 SD4-SD6 的财务趋势图表功能不完整。

### 🟡 P1：安全与数据完整性

#### 2.4 筛选引擎 SQL 字段名注入风险
**位置：** `app/core/screening/engine.py:27-38, 161-466`
**问题：** `OP_MAP` 将用户提供的操作符映射到 SQL，字段名通过 `_extract_fields()` 从规则 JSON 提取后直接拼入 SQL。虽然 `SNAPSHOT_COLUMNS` 白名单限制了可用字段，但 `_build_sql()` 中的 `rank_fields` 直接插入窗口函数 SQL 而不二次验证。
**当前缓解：** `SNAPSHOT_COLUMNS` 白名单间接限制了注入面。
**风险等级：** 中（白名单有效，但防御层次不足）。

#### 2.5 归档导出使用 f-string 拼接 SQL
**位置：** `app/cli/main.py:647`
**问题：** `duck.execute_script(f"COPY {table} TO '{output_path}' (FORMAT PARQUET);")` 中 `output_path` 来自用户输入 `--target` 选项，直接拼入 SQL。
**影响：** 路径中包含单引号可导致 SQL 注入（虽然是本地 CLI，风险较低）。

#### 2.6 Watchlist DELETE 使用请求体
**位置：** `app/web/api/watchlist.py:125-139`
**问题：** `DELETE /api/watchlist/remove` 使用 request body 传递参数。HTTP 规范中 DELETE 请求体是可选的，部分客户端/代理不支持。
**修复建议：** 改为 `DELETE /api/watchlist/remove/{stock_code}?group_name=xxx` 或改用 POST。

#### 2.7 前端 `confirm()` 对话框阻塞
**位置：** `frontend/src/components/DslIndicatorManager.vue:117`
**问题：** 使用浏览器原生 `confirm()` 进行删除确认。该调用阻塞主线程，且在某些浏览器设置下可能被自动阻止。
**修复建议：** 使用 Naive UI 的 `useDialog().warning()` 替代。

### 🟢 P2：代码质量与类型安全

#### 2.8 前端大量使用 `any` 类型
**统计：** 39 处 `any` 使用
**主要位置：**
- `WatchlistPage.vue`：17 处（items、groups、columns 全部为 `any`）
- `DslIndicatorManager.vue`：6 处（previewResult、columns、catch 块）
- `DataStatusPage.vue`：4 处（retryList、missingList、recent_jobs）
- `ScreeningPage.vue`：1 处（locked_indicators）
- `IndicatorTabs.vue`：1 处（column type assertion）
- `StockDetailPage.vue`：1 处（K线数据映射）

**影响：** 类型安全被削弱，重构时无法获得编译器保护。
**建议：** 为 Watchlist、DataStatus 响应定义专用接口。

#### 2.9 后端静默异常吞没
**位置：** 多处 `except Exception: pass`
**关键位置：**
- `app/core/adapters/akshare_adapter.py:150, 163, 361, 370`：适配器错误处理中的 pass
- `app/core/adapters/tdx_adapter.py:153, 164, 571, 626, 647`
- `app/core/adapters/cninfo_adapter.py:455, 462, 469`
- `app/cli/main.py:650`：归档导出失败静默跳过
- `app/core/storage/schema.py:500, 504`：迁移错误静默

**影响：** 错误被吞没后无法诊断，数据可能处于不一致状态。

#### 2.10 筛选页面 `strictOnly` 过滤逻辑无效
**位置：** `frontend/src/components/ScreeningResultsPanel.vue:60-77`
**问题：** `filteredResults` 检查 `result._confidence` 字段，但后端 `/api/screening/run` 返回的结果中没有 `_confidence` 字段。因此 `strictOnly` 开关始终显示全部结果。
**影响：** 用户切换"仅 strict"时看不到任何变化，功能形同虚设。

#### 2.11 自选列表页缺少类型定义
**位置：** `frontend/src/views/WatchlistPage.vue`
**问题：** `items`、`groups` 均为 `ref<any[]>([])`，没有定义 `WatchlistItem`、`WatchlistGroup` 接口。
**对比：** ScreeningPage 和 StockDetailPage 都有完整的类型定义。

#### 2.12 数据状态页类型不完整
**位置：** `frontend/src/views/DataStatusPage.vue:21, 30-31`
**问题：** `recent_jobs?: any[]`、`retryList = ref<any[]>([])`、`missingList = ref<any[]>([])` 缺少类型定义。

#### 2.13 Watchlist API 响应与前端消费不匹配
**位置：** `app/web/api/watchlist.py:36-108` vs `frontend/src/views/WatchlistPage.vue`
**问题：** 后端返回 `net_margin`、`revenue_yoy` 等字段，但前端 `allColumnOptions` 中列出的 `net_margin` 和 `revenue_yoy` 依赖后端 LEFT JOIN LATERAL 查询中的字段。后端查询中 `s.net_margin` 和 `s.revenue_yoy` 来自 `indicator_snapshot`，但查询 SQL 只选择了 `s.roe, s.gross_margin, s.debt_ratio, s.revenue_yoy`，缺少 `s.net_margin`。
**影响：** 前端显示 `net_margin` 列时始终为 `—`。

---

## 3. 前端→后端数据流完整性

### 3.1 已验证的完整数据流

| 前端操作 | API 调用 | 后端处理 | 数据写入 | 状态 |
|---|---|---|---|---|
| 筛选运行 | `POST /api/screening/run` | `ScreeningEngine.run()` | 只读 | ✅ |
| 保存筛选结果 | `POST /api/screening/save` | 写入 `screening_results` | SQLite | ✅ |
| 导出 CSV | `POST /api/screening/export_csv` | 生成 CSV 字符串 | 无 | ✅ |
| 加入自选 | `POST /api/screening/add_to_watchlist` | 写入 `watchlist` | SQLite | ✅ |
| 加载自选 | `GET /api/watchlist/list` | 读取 `watchlist` + `indicator_snapshot` | 无 | ✅ |
| 添加自选 | `POST /api/watchlist/add` | 写入 `watchlist` | SQLite | ✅ |
| 移除自选 | `DELETE /api/watchlist/remove` | 删除 `watchlist` 行 | SQLite | ✅ |
| 个股信息 | `GET /api/stock/{code}/info` | 读取 `stock_meta` + `price_daily_raw` | 无 | ✅ |
| K 线数据 | `GET /api/stock/{code}/kline` | 读取 `price_daily_raw/qfq` | 无 | ✅ |
| 指标摘要 | `GET /api/stock/{code}/indicators` | 读取 `indicator_snapshot` | 无 | ✅ |
| 财务趋势 | `GET /api/stock/{code}/financial-trend` | 读取三表 | 无 | ✅ |
| 溯源信息 | `GET /api/stock/{code}/source-audit` | 读取 `source_audit` + `fetch_batch` | 无 | ✅ |
| 数据状态 | `GET /api/data-status/summary` | 聚合查询 | 无 | ✅ |
| DSL 创建 | `POST /api/dsl/expressions` | 写入 `dsl_expressions` | SQLite | ✅ |
| DSL 发布 | `PUT /api/dsl/expressions/{id}/publish` | 更新状态 | SQLite | ✅ |
| DSL 删除 | `DELETE /api/dsl/expressions/{id}` | 删除 | SQLite | ✅ |

### 3.2 断裂的数据流

| 前端操作 | API 调用 | 问题 |
|---|---|---|
| DSL 预览 | `POST /api/dsl/validate` | ⚠️ 后端是桩实现，不返回真实预览 |
| 筛选 strictOnly 过滤 | 前端本地过滤 | ⚠️ 后端不返回 `_confidence` 字段 |
| 自选 net_margin 列 | `GET /api/watchlist/list` | ⚠️ 后端查询缺少 `net_margin` |
| 财务趋势图表 | 组件已实现 | ⚠️ `FinancialTrendCard.vue` 未被引用 |
| 加载已保存规则 | `GET /api/screening/rules` → `loadRule()` | ⚠️ 前端 `Object.assign(ruleTree, ...)` 不替换 reactive 对象内容 |

---

## 4. 安全漏洞清单

### 4.1 已防护的攻击面

| 攻击面 | 防护措施 | 位置 |
|---|---|---|
| SQL 注入（stock_code） | 6 位数字白名单正则 | `codegen.py:84`, `stock_detail.py:607` |
| 路径遍历（PDF） | 白名单 + resolve 后目录校验 | `stock_detail.py:607-623` |
| 筛选 ORDER BY 注入 | 字段格式白名单 | `screening/engine.py` |
| DSL stock_code 注入 | 6 位数字验证 | `codegen.py:84` |
| 测试隔离 | 路径策略 + 环境变量 | `path_policy.py`, `conftest.py` |
| 覆写计算门禁 | 只使用 published 未撤销 | `calculator.py:240-247` |
| 快照原子发布 | staging + 事务 | `calculator.py:138-188` |
| 备份恢复两段确认 | plan_id + 15 分钟有效期 | `protocol.py`, `main.py:612` |
| 适配器配置校验 | 别名归一 + 未知拒绝 | `manager.py:62-67` |
| 熔断器 | 连续失败 5 次冷却 5 分钟 | `manager.py:131-173` |

### 4.2 未防护的攻击面

| 攻击面 | 风险 | 位置 | 严重度 |
|---|---|---|---|
| DSL validate 桩实现 | 用户创建无效表达式 | `dsl.py:87-109` | 🔴 高 |
| 归档 SQL f-string | 路径含引号导致 SQL 注入 | `main.py:647` | 🟡 中 |
| 筛选字段名直接拼 SQL | 白名单有效但防御层次不足 | `screening/engine.py` | 🟡 中 |
| DELETE 请求体 | 部分客户端不支持 | `watchlist.py:125` | 🟢 低 |
| 前端 `confirm()` | 可被浏览器设置阻止 | `DslIndicatorManager.vue:117` | 🟢 低 |

---

## 5. 错误处理缺陷

### 5.1 静默吞没异常的模式

| 位置 | 模式 | 影响 |
|---|---|---|
| `akshare_adapter.py:150, 163` | `except Exception: pass` | 数据解析错误被忽略 |
| `tdx_adapter.py:153, 164, 571, 626, 647` | `except Exception: pass` | 连接/解析错误被忽略 |
| `cninfo_adapter.py:455, 462, 469` | `except Exception: pass` | 公告解析错误被忽略 |
| `init.py:669` | `except Exception: pass` | 分红回填错误被忽略 |
| `schema.py:500, 504` | `except Exception: pass` | 迁移错误被忽略 |
| `main.py:650` | `except Exception: pass` | 归档导出失败被忽略 |
| `correction.py:185` | `except Exception: pass` | 更正处理错误被忽略 |

**建议：** 至少添加 `logger.warning` 或 `logger.debug` 记录。

### 5.2 前端错误处理

| 位置 | 模式 | 评估 |
|---|---|---|
| `WatchlistPage.vue:80-82` | `isAxiosError(e) ? e.response?.data?.detail : e instanceof Error ? e.message : '未知错误'` | ✅ 完善 |
| `ScreeningPage.vue:129-131` | 同上模式 | ✅ 完善 |
| `DslIndicatorManager.vue:73-74` | `catch (e) { message.error('加载指标列表失败') }` | ⚠️ 未展示具体错误 |
| `DataTraceability.vue:54-55` | `catch (e) { message.error('加载PDF列表失败') }` | ⚠️ 未展示具体错误 |

---

## 6. 类型安全缺陷

### 6.1 前端缺失的类型定义

| 文件 | 缺失类型 | 建议 |
|---|---|---|
| `WatchlistPage.vue` | `WatchlistItem`, `WatchlistGroup` | 定义接口替代 `any` |
| `DataStatusPage.vue` | `RetryItem`, `MissingItem`, `JobLog` | 定义接口替代 `any` |
| `DslIndicatorManager.vue` | `DslValidateResult` | 定义接口替代 `any` |
| `ScreeningPage.vue` | `SavedRule.rule_json` | 定义 `RuleJson` 接口 |

### 6.2 前后端类型不匹配

| 前端期望 | 后端实际 | 影响 |
|---|---|---|
| `WatchlistItem.net_margin` | 后端查询未选择该字段 | 列始终显示 `—` |
| `ScreeningResult._confidence` | 后端不返回该字段 | `strictOnly` 过滤无效 |
| `AuditFieldRow.fetch_time` | 后端返回 `fetch_time` | ✅ 匹配 |
| `IndicatorsResponse.freshness` | 后端返回 `freshness` | ✅ 匹配 |
| `DataQualityStatus` | 后端 `build_data_quality_status` | ✅ 精确匹配 |

---

## 7. PRD 功能缺口

### 7.1 已实现但功能不完整

| PRD 要求 | 当前实现 | 缺口 |
|---|---|---|
| §7.3 公告检查 | `_check_new_announcements()` 返回 `not_implemented` | 未接入任何披露源 |
| §11.5 DSL 预览 | Web API 是桩实现 | CLI 路径完整但 Web 未接入 |
| §14 SD4 财务趋势图表 | `FinancialTrendCard.vue` 已实现 | 未被 `StockDetailPage` 引用 |
| §12.4 strictOnly 过滤 | 前端检查 `_confidence` | 后端不返回该字段 |
| §13 自选 net_margin | 前端列出该列 | 后端查询未包含 |

### 7.2 PRD 明确不做但代码中存在的功能

| PRD 排除 | 代码状态 | 评估 |
|---|---|---|
| §3.4 不做历史筛选/回测 | 代码中无回测逻辑 | ✅ 合规 |
| §3.4 不做自动提醒 | 代码中无自动提醒 | ✅ 合规 |
| §3.4 不做新闻/市场总览 | 代码中无新闻/总览页面 | ✅ 合规 |
| §3.1 不做登录/权限 | 代码中无认证/授权 | ✅ 合规 |

---

## 8. 修复优先级

### 立即修复（本周）

1. **P0-2.1** DSL validate API 接入真实引擎
2. **P0-2.3** 将 `FinancialTrendCard.vue` 集成到 `StockDetailPage.vue`
3. **P1-2.10** 后端返回 `_confidence` 或前端改为基于 `warningCodes` 过滤
4. **P1-2.13** Watchlist API 查询添加 `s.net_margin`

### 短期修复（本月）

5. **P1-2.4** 筛选引擎增加字段名二次白名单验证
6. **P1-2.5** 归档 SQL 改用参数化
7. **P2-2.8** 为 `WatchlistPage`、`DataStatusPage` 定义类型接口
8. **P2-2.9** 将 `except Exception: pass` 改为 `logger.warning`

### 中期改进（下季度）

9. **P0-2.2** 接入公告检查（至少实现 CNINFO 公告时间对比）
10. **P1-2.6** Watchlist DELETE 改为路径参数
11. **P1-2.7** 替换 `confirm()` 为 Naive UI dialog
12. **P2-2.12** 为 DataStatus 响应定义完整类型

---

## 9. 与之前审查报告的对照

### 9.1 `docs/11_RED_TEAM_AUDIT_V2.md` (DQ-01 到 DQ-14) 修复状态

| ID | 代码修复 | 测试覆盖 | 数据重建 | 本次新发现 |
|---|---|---|---|---|
| DQ-01 | ✅ | ✅ `test_indicator_data_quality.py` | N/A | 无 |
| DQ-02 | ✅ | ✅ `test_snapshot_atomicity.py` | N/A | 无 |
| DQ-03 | ✅ 防护 | ✅ `test_indicator_data_quality.py` | ❌ | 无 |
| DQ-04 | ✅ | ✅ `test_adapter_configuration.py` | N/A | 无 |
| DQ-05 | ✅ 监测 | ✅ `test_data_quality_status.py` | ❌ | 无 |
| DQ-06 | ✅ 监测 | ✅ `test_data_quality_status.py` | ❌ | 无 |
| DQ-07 | ✅ 监测 | ✅ `test_data_quality_status.py` | ❌ | 无 |
| DQ-08 | ✅ | ✅ `test_collection_safety.py` | N/A | 无 |
| DQ-09 | ✅ | ✅ `test_period_semantics.py` | N/A | 无 |
| DQ-10 | ✅ 防护 | ✅ `test_storage_and_ingestion.py` | ❌ | 无 |
| DQ-11 | ✅ | ✅ `test_storage_and_ingestion.py` | ❌ | 无 |
| DQ-12 | ✅ 防护 | ✅ `test_manual_overrides.py` | ❌ | 无 |
| DQ-13 | ✅ | ✅ `test_storage_and_ingestion.py` | ❌ 迁移未执行 | 无 |
| DQ-14 | ✅ | ✅ `test_snapshot_atomicity.py` | N/A | 无 |

### 9.2 `docs/17_PRODUCTION_READINESS_AUDIT.md` 问题跟踪

上次审查中发现的前端问题大部分已修复：
- ✅ DataStatusPage 已展示 `warning_codes`
- ✅ StockDetailPage 已集成 `DataFreshnessCard`
- ✅ 筛选面板已实现质量门禁（保存/导出禁用）
- ⚠️ `strictOnly` 过滤仍然无效（新发现）
- ⚠️ Watchlist 缺少 `net_margin`（新发现）

---

## 10. 总结

### 代码完善度评分

| 维度 | 评分 | 说明 |
|---|---|---|
| 页面覆盖 | 5/5 | PRD §5 四个页面全部实现 |
| API 覆盖 | 4/5 | 33 个端点，1 个桩实现 |
| 类型安全 | 3/5 | 核心类型完善，Watchlist/DataStatus 缺失 |
| 错误处理 | 3/5 | 前端完善，后端多处静默吞没 |
| 安全防护 | 4/5 | 主要攻击面已防护，2 个中等风险 |
| 数据流完整性 | 4/5 | 16 条完整，5 条断裂 |
| PRD 合规 | 4/5 | 主要功能实现，2 个功能缺口 |

### 最终结论

**代码结构 CONDITIONAL PASS：** 前端四个页面和全部组件已实现，后端 33 个 API 端点已就位，核心数据流完整。主要缺口是 DSL validate 桩实现、FinancialTrendCard 未集成、strictOnly 过滤无效。

**数据层面继续 BLOCK：** 与之前审计报告结论一致，正式数据未重建，G22/G23 未通过。

**本次新发现 13 个问题：** 2 个 P0、5 个 P1、6 个 P2。建议优先修复 DSL validate API 和 FinancialTrendCard 集成。

---

**审查日期：** 2026-07-26
**审查方法：** 按图索骥——PRD → 前端 → API → 核心模块 → 存储，逐层追踪
**审查范围：** 全部前端源码（14 个 Vue/TS 文件）、后端 API（5 个路由文件）、核心模块（8 个 Python 文件）、CLI（1080 行）
**下次复审：** 完成 P0/P1 修复后
