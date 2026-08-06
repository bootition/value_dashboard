---
title: 用户启动路径与实时状态恢复报告（2026-08-06）
status: superseded
category: reports
created: 2026-08-06
last-reviewed: 2026-08-06
supersedes: reports/50_FINAL_SCREENING_UI_AND_AUTO_UPDATE_RECOVERY_2026-08-05.md
superseded-by: reports/52_VISUAL_BASELINE_AND_LAUNCH_BUILD_STRATEGY_2026-08-06.md
---

# 用户启动路径与实时状态恢复报告（2026-08-06）

本报告承接 `reports/50` 已验证的筛选 UI、死亡锁恢复、逐股原子续传、增量快照以及 2026-08-05 正式库 `ready=true`、`warning_codes=[]` 等事实，并更新用户启动路径与更新期间实时状态结论。

## 1. 用户入口根因

仓库根 `start.bat` 曾在发现 `dist/value-dashboard/value-dashboard.exe` 后优先启动旧发行包，导致源码和前端已经升级，但用户双击后仍看到旧界面。旧 exe 是实际遮蔽源，不是单纯浏览器缓存。

修复后：

- 仓库开发入口不再引用 `dist`；只有与 `start.bat` 同目录的 `value-dashboard.exe` 才进入打包模式。
- 开发模式启动前执行锁定依赖安装（仅缺少 `node_modules` 时）和当前前端 build，构建失败即停止。
- 8765 端口占用时 fail-closed，避免新进程绑定失败后浏览器仍连接旧服务。
- SPA `index.html` 返回 `Cache-Control: no-store`，带 hash 的静态资源仍可正常缓存。

## 2. 真实浏览器验收

通过真实 `start.bat` 启动后，服务进程为源码 `python.exe`，HTTP 入口与筛选 chunk 均来自当前构建。Chrome DevTools Protocol 的实际渲染检查确认：

- 新版“把投资判断写成一句规则”“全部成立 / 任一成立”和研究底稿文案存在。
- 旧 `且 (AND)` 控件和旧帮助文案不存在。
- 规则区、编辑器、运行面板和逻辑开关的 computed style 均为 `border-radius: 0px`、`box-shadow: none`。

## 3. 后台更新期间状态 503

真实启动还暴露出第二个问题：后台自动更新持有默认读写 DuckDB 连接时，Web 状态接口另开 `read_only=True` 连接。DuckDB 不允许同一进程对同一文件同时使用不同连接配置，因此 `/api/data-status/summary` 在每个写事务期间返回 503，事务间隙偶尔恢复，侧栏显示“状态读取失败”。

修复方式：

- open-per-query 读取连接改用与写事务一致的默认配置，仍不持有长期连接。
- `read_query()` 使用 DuckDB 解析器强制只接受单条 SELECT，避免统一底层配置削弱只读调用边界。
- 新增存储级并发回归和 API 级回归：同进程写事务连接存活时，读取查询与 `/api/data-status/summary` 必须正常返回；CREATE 等写语句必须被拒绝。

## 4. 验证与运行披露

- 定向后端：24 passed；Ruff 全绿。
- 完整隔离回归：441 passed，1 deselected。唯一未执行项是 `test_collect_only_does_not_modify_production_databases`：正式自动更新仍由 PID 77932 持有 DuckDB 文件，Windows 拒绝测试读取正式库计算 SHA-256；未终止正式更新，也未绕过保护。
- 前端：ESLint、52 个 Node 单测、16 个组件测试和生产 build 全通过。
- 当前正式自动更新 job `31b22d80-fc52-4393-a395-d70470e8eb35` 仍在按逐股断点追赶；连接修复将在该任务自然结束并安全重启后进入实际服务进程。

结论：旧发行包遮蔽和后台更新期间状态接口 503 的代码根因均已关闭；正式价格覆盖仍是持续更新项，不据此宣称全市场数据完整。
