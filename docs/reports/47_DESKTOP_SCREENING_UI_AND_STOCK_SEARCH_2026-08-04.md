---
title: 桌面筛选界面与个股搜索入口实施报告（2026-08-04）
status: approved
category: reports
created: 2026-08-04
last-reviewed: 2026-08-04
---

# 桌面筛选界面与个股搜索入口实施报告（2026-08-04）

## 1. 用户确认的界面边界

- V1 保持四页：筛选（默认）、自选列表、个股详情、只读数据状态；不新增研究首页、市场总览或资讯流。
- 桌面壳层采用浅色侧栏、圆角工作面与浅淡绿色强调色。
- 指标展示中文优先并保留必要缩写，例如“市盈率（PE-TTM）”与“净资产收益率（ROE）”。
- 个股详情模块先进入搜索状态；搜索仅存在于该模块，支持代码或名称模糊匹配；选择结果后进入详情。

## 2. 实施

- `frontend/src/App.vue`：正式应用切换为四模块桌面侧栏；具体股票详情仍归属“个股详情”导航状态。
- `frontend/src/views/ScreeningPage.vue`：将已确认的桌面筛选工作区接入真实规则草稿、规则版本、基础池、条件、排序、严格可信开关和服务端运行逻辑。
- `frontend/src/views/StockSearchPage.vue`：新增个股详情的搜索状态；搜索结果点击进入既有 `/stock/:code` 详情页。
- `app/web/api/stock_detail.py`：新增只读 `/api/stock/search`，对当前上市股票提供代码或名称部分匹配，最多返回 20 项。
- `frontend/src/utils/screening-format.ts`：提供用户界面字段中文名映射；API、规则 JSON 与机器导出字段保持稳定。
- `docs/decisions/01_PRODUCT_REQUIREMENTS_V1.md` 与 `frontend/DESIGN.md`：同步页面边界、个股搜索流程、中文指标规则及桌面壳层。

## 3. 验证

| 门禁 | 结果 |
|---|---|
| 个股搜索隔离 API 回归 | 9 passed |
| `scripts/s1-pytest.ps1 tests/regression` | 424 passed（179.24s） |
| `uv run --locked ruff check app tests/regression` | All checks passed |
| `frontend/npm run lint` | 通过 |
| `frontend/npm run test` | 52 Node 契约测试 + 11 Vitest 测试通过 |
| `frontend/npm run build` | 通过 |

## 4. 结论

用户确认的筛选页视觉方向已接入正式路径，且原有筛选、保存、导出、加入自选与质量防护契约均由门禁覆盖。个股详情的模块内搜索入口已可用；自选、具体个股详情、数据状态的同一视觉语言深化留待后续逐页确认。
