---
title: 技术债补全与个股研究细节体验修复（2026-09-02）
status: approved
category: reports
created: 2026-09-02
last-reviewed: 2026-09-02
---

# 技术债补全与个股研究细节体验修复（2026-09-02）

## 1. 结论

- 知识库 T1-T9 除用户操作项外全部完成评估或代码补全；新增 4 条 CLI 能力。
- 磁盘清理完成：重建回滚快照（50GB）、7 月旧备份（12GB）、accept/uat/s2 归档（1.16GB）、
  重复原始资料与 UAT 归档（6.3GB）、dist/build 构建产物均按观察结果删除，释放约 70GB。
- 个股详情 K 线空白根因定位并修复：klinecharts 10 未设置 symbol 前不会触发数据加载。
- 个股研究页统一为一张历史研究统计卡；国债比较并入“股息率-国债10年利差”序列。
- 历史统计分位线改为逐线配色、空值断笔，并支持鼠标悬浮查看点位。

## 2. 技术债补全

| # | 处理 |
|---|---|
| T1 | DuckDB 镜像最新仍为 1.5.5，无升级空间；维持分事务索引重建 workaround |
| T2 | 新增 `vd archive restore` / `archive restore_execute`（验证 manifest + verified 后恢复全部热表） |
| T3 | 前次已关闭（config/user.yaml） |
| T4 | 新增 `vd data quarantine-dividends-audit`：8457 行扫描结果 0 可恢复 / 14 重复 / 7050 无候选 / 787 歧义 / 18 冲突 / 588 跨行冲突 |
| T5 | 新增 `vd data probe-eastmoney-push2` 单请求探测；2026-09-02 实测 HTTP 200，push2 已解封，恢复后仍限速 ≤2 req/s |
| T6 | 新增 `vd screening audit-legacy-unit-rules`；当前正式库命中 0 条旧规则 |
| T7 | 死锁判定测试重试/线程等待已就位；定向通过 |
| T8 | 国债快照测试动态日期对齐、PDF 路径隔离测试均通过 |
| T9 | 复核确认 `vd backup` 已默认 5000 行分块处理 raw_response_archive_history |

## 3. UI 修复

1. 自选列表新增上市日期、总市值、流通市值三列（均可排序）。
2. 复合指标管理显示中文名+英文字段名；筛选条件保留英文表达式名。
3. K 线：初始化后显式 setSymbol，修复只显示坐标轴/空白的问题。
4. 历史研究统计：P10/P20/P50/P80/最大（及 σ 线）使用不同颜色；序列遇 null 断笔，
   不再跨缺失区间拉出长直线；SVG 增加 mousemove 悬浮提示（日期+数值）。
5. 个股详情只保留一张历史研究统计卡，国债比较不再单独发请求。
6. 目录修复滚动容器监听（原 window.scrollY 恒为 0），高亮随正文滚动；
   桌面宽度下手动固定视口顶部并右移贴边。

## 4. 清理依据

- 重建回滚观察：2026-09-01 job #124 成功完成（22:52 本地），满足报告 101/102 的
  “至少一个完整更新周期”回滚窗口；9-01 两份硬链接快照与 sqlite 副本删除。
- 7 月备份/归档：当前新库（41GB）健康，9-01/9-02 多轮更新继续推进，旧备份已无回滚价值。
- `_legacy/raw_source_data` 与 `额外资料/` 文件大小逐一相同，为重复数据。
- 构建产物按需重建，前端 build 门禁已再次通过。

## 5. 门禁

- Ruff：app + tests/regression 0 violation。
- 后端回归：完整 S1 681 passed（含 start.bat packaged 分支的 live-service 隔离 shim）。
- 后端定向回归：93 passed（archive/treasury/capital/screening/watchlist/data-status）。
- 前端：lint 通过、vitest 57 passed、production build 通过。
