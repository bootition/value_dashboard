---
title: 红队 P2 风险修复报告（2026-08-09）
status: approved
category: reports
created: 2026-08-09
last-reviewed: 2026-08-09
supersedes: reports/63_SYSTEM_RED_TEAM_REVIEW_2026-08-09.md
---

# 红队 P2 风险修复报告

## 1. 修复范围

关闭 `reports/63` 记录的四项 P2 风险，不写入正式 `data/`。

| 风险 | 修复 | 回归保护 |
|---|---|---|
| 启动器仅按端口识别实例 | `start.bat` 请求 `/api/health`，仅收到 200 且 JSON `status=ok` 才复用实例 | 启动脚本契约测试 |
| SPA 未知资源路径返回首页 | 仅无扩展名客户端路由回退 `index.html`，请求带扩展名的缺失资源返回 404 | Web 组合根测试 |
| 自动更新轮询写放大 | `/auto-update` 直接以 SQLite 只读连接查询状态表；表尚未创建时返回安全的默认空闲状态 | Web 组合根测试确认端点不建表 |
| 单股价格重抓 raw/qfq 非原子 | 复用原子 pair 持久化事务；单股重抓仍保留完整字段审计 | 价格 lineage 回归测试确认失败回滚两表与批次记录 |

## 2. 验证

| 门禁 | 结果 |
|---|---|
| `uv run --locked ruff check app tests/regression` | PASS |
| 定向 S1（启动、Web、价格 lineage、自动更新） | PASS，55 passed |
| `scripts/s1-pytest.ps1 tests/regression` | PASS，481 passed |
| `frontend/npm run lint` | PASS |
| `frontend/npm run test` | PASS，55 Node + 20 Vitest tests |
| `frontend/npm run build` | PASS |

## 3. 裁决

`reports/63` 的 P2-1 至 P2-4 均已关闭。项目维持 **PASS，可正式研究**。

剩余非阻断风险仅为外部数据源不稳定、CNINFO 分红 `ex_date` 主适配器待评估和缺少浏览器级 E2E 门禁，详见 `STATUS.md` 已知缺口与 `reports/61`。
