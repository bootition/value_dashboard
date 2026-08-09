---
title: 系统红队复审报告（2026-08-09）
status: approved
category: reports
created: 2026-08-09
last-reviewed: 2026-08-09
---

# 系统红队复审报告

## 1. 范围与方法

独立复核普通用户关键路径：启动、健康检查、筛选、个股搜索与详情、自选、数据状态、自动更新、数据质量门禁、CLI 危险操作、导出和发布静态资源。全程不写正式 `data/`。

当前状态依据为 `STATUS.md`（2026-08-08）和 `reports/62`（2026-08-08）；`reports/60` 的 BLOCK 裁决已被 `reports/62` 取代，不作为本次当前状态依据。

## 2. 门禁结果

| 门禁 | 结果 |
|---|---|
| `uv run --locked ruff check app tests/regression` | PASS |
| `scripts/s1-pytest.ps1 tests/regression` | PASS，478 passed |
| `frontend/npm run lint` | PASS |
| `frontend/npm run test` | PASS，55 Node + 20 Vitest tests |
| `frontend/npm run build` | PASS |
| 工作区与空白差异检查 | PASS |

本机审查期间未启动服务，`127.0.0.1:8765/api/health` 连接被拒绝；因此未将它作为在线服务失败，也未重复启动会写入正式库的普通用户路径。已有 S1 Web 组合回归和当前构建产物验证覆盖 API/静态服务联动。

## 3. 发现

未发现 P0 或 P1，正常研究使用不被阻断。

### P2-1 启动脚本按端口误认已有实例

- 位置：`start.bat:14-18`
- 条件：任意非本项目进程监听 8765。
- 影响：启动器直接打开该端口，用户可能看到非本项目页面，当前项目不会启动。
- 建议：探测 `/api/health` 的预期响应，或核验监听进程归属。

### P2-2 SPA 未知非资源路径返回首页

- 位置：`app/web/main.py:363-372`
- 条件：发布产物入口存在但某个懒加载 chunk 缺失时访问该 chunk 的非 `/assets/` 路径。
- 影响：服务返回 200 HTML 而非 404，浏览器表现为 MIME 错误或白屏，诊断性不足。
- 现状：当前入口和懒加载 chunk 均完整；不影响当前使用。

### P2-3 自动更新状态轮询存在 SQLite 写放大

- 位置：`app/web/api/data_status.py:521-531`、`app/core/auto_update.py:92-105`
- 条件：自动更新中前端按 4 秒轮询状态。
- 影响：每次构造控制器执行幂等建表，与进度写入竞争 SQLite 写锁，可能轻微降低更新吞吐；不会产生错误数据。

### P2-4 单股价格重抓的 raw/qfq 写入不是一个事务

- 位置：`app/core/update.py:1598-1615`
- 条件：`price_daily` 重抓在 raw 已持久化后 qfq 抓取或写入失败。
- 影响：两表暂时错位，质量门禁会 fail-closed 拒绝筛选，后续重试自愈；不存在静默放行。

## 4. 残余风险

- CNINFO 分红 `ex_date` 主适配器死代码仍依赖可用回退源；当前没有数据缺口，详见 `reports/61`。
- 数据源截断在退市门禁阈值以下时主要依赖后续质量披露发现。
- 未配置浏览器级 E2E 为前端门禁；本次由构建、组件测试和 S1 Web 回归覆盖。

## 5. 裁决

**PASS，可正常使用于正式研究。** 本轮无阻断项；P2 项应在后续可靠性迭代中关闭，但不足以推翻正式库 `ready=true`、`warning_codes=[]` 的当前状态。
