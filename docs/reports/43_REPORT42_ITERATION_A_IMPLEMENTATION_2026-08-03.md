---
title: 报告42 迭代A实施报告（G1 操作指南 + L0-1~L0-7，2026-08-03）
status: approved
category: reports
created: 2026-08-03
last-reviewed: 2026-08-03
---

# 报告42 迭代A实施报告（G1 操作指南 + L0-1~L0-7，2026-08-03）

> 按 `reports/42` §6 推荐顺序完成**迭代 A（L0，必须）**：
> G1 操作指南 + L0-1 至 L0-7 全部落地。门禁全绿（S1 411 passed、ruff、
> 前端 lint/52 node + 10 组件测试/build），正式数据库只读且 SHA-256 不变。
> L1/L2 不在本次范围，按 `reports/42` 迭代顺序留待后续。

## 1. G1 首次使用与日常操作指南

- 新增 `docs/runbooks/user-first-use.md`（9 章）：启动与停止、数据状态、
  第一次筛选、可信度与截断、保存/导出/自选、个股研究、日常维护、
  备份与恢复、已知边界。
- README.md 增加「5 分钟上手」摘要并链接该手册；`docs/README.md` 文档地图同步。

## 2. L0-1~L0-7 实施明细

| # | 任务 | 实现 | 验收对照 |
|---|---|---|---|
| L0-1 | 首次筛选引导 | `ScreeningPage.vue` 空态从单一 `n-empty` 改为「三步开始」卡片（命名→保存→运行），数据质量失败时附数据状态页链接 | 新用户不经口头指导可完成首筛 |
| L0-2 | 单位/口径统一 | 新增 `utils/screening-format.ts`：pct/ratio/price/plain 四类字段口径（与详情页 fmtPct/fmt 一致）；筛选结果表与自选表共用 `formatFieldValue`，表头带单位（如 ROE(%)） | 同一 ROE/负债率在筛选/自选/详情同义显示 |
| L0-3 | strict-only 一致性 | 移除结果面板的客户端二次过滤（服务端为权威）；`ScreeningPage` 监听 strict-only 切换，有结果时 400ms 防抖重跑服务端 | 屏幕/保存记录/CSV 三者结果数一致（同一 run_id） |
| L0-4 | 草稿冲突恢复 | 409 不再永久停用自动保存：弹出「草稿冲突」对话框，提供「加载服务器草稿 / 保留本地副本（覆盖服务器）/ 刷新页面」三选 | 双窗口冲突后不丢编辑、不猜测 |
| L0-5 | 人性化错误与下一步 | 新增 `helpers/api-error.ts`：reason code/英文 detail → 中文原因 + 下一步；应用于筛选运行/保存、结果保存/导出/加自选、自选增删、个股详情各加载 | 常见 400/409/网络错误均有可执行指引 |
| L0-6 | 股票代码校验与自选移除保护 | 前端 6 位数字校验+提示；后端 `watchlist add` 强制 `^\d{6}$` 且须存在于 `stock_meta`（400 拒绝）；移除前 `useDialog` 确认。新增 `test_watchlist_code_validation.py` 3 个测试 | 不可添加垃圾代码；误移除有确认 |
| L0-7 | 无效个股结果态 | `StockDetailPage` 404（信息/指标）时呈现 `NResult`「股票不存在或暂无数据」+ 回到筛选/自选按钮，不再渲染一页横线 | 无效 URL 有明确回退路径 |

## 3. 门禁（2026-08-03 重跑）

| 门禁 | 结果 |
|---|---|
| `scripts/s1-pytest.ps1 tests/regression` | **411 passed**（169.99s；较上轮 408 多 3 个 watchlist 校验测试） |
| `uv run --locked ruff check app tests/regression` | All checks passed |
| 前端 lint / node / vitest / build | 通过 / 52 passed / 10 passed / 成功 |
| 正式库 SHA-256（前后一致） | DuckDB `741C75BE...`、SQLite `3D41498F...` |

组件测试注：`watchlist-flow.test.ts` 挂载补上 `NDialogProvider`（与 App.vue 一致，
L0-6 移除确认所需），断言不变。

## 4. 结论与后续

- **迭代 A（G1 + L0-1~L0-7）完成**，`reports/42` §2 的 L0 验收标准
  「10 分钟内完成 启动→确认数据日期→新建并运行规则→保存→导出」现已由
  手册+界面引导双重支撑。
- 后续按 `reports/42` §6：迭代 B（L1-1~L1-7）与迭代 C（L2）未纳入本次范围；
  并行运维项 O1/O2/O4 亦未动，见 STATUS.md 剩余缺口。

## 5. 证据

- `docs/evidence/evidence-report42-iterationA-gates-20260803.json`
- `docs/evidence/evidence-s1/<最新 run>/hash-evidence.json`（正式库逐字节比对）
