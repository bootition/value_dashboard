---
title: 视觉基线回归与按需构建启动报告（2026-08-06）
status: approved
category: reports
created: 2026-08-06
last-reviewed: 2026-08-06
supersedes: reports/51_LAUNCH_PATH_AND_LIVE_STATUS_RECOVERY_2026-08-06.md
---

# 视觉基线回归与按需构建启动报告（2026-08-06）

本报告修正 `reports/50`、`reports/51` 中对筛选页视觉语言和启动策略的结论，并承接
其中仍然有效的连接修复、旧 dist 遮蔽修复与状态一致性事实。

## 1. 用户反馈的三个根因

1. **启动慢约十几秒**：`start.bat` 每次开发入口都无条件执行一次完整 `npm run build`
   （约 10–20 秒），这是把“避免旧前端遮蔽”做成了过度修复。
2. **视觉与信息架构混淆**：把“不应使用丑陋卡片模式”错误落实成“全面取消圆角、
   阴影并使用宋体”，导致筛选页与其他页面字体、圆角割裂；真正的信息架构问题是
   独立「01 / UNIVERSE 研究范围」区块与条件区割裂，且 ST/停牌使用左右滑动开关。
3. **提交尾巴**：`app/web/static/index.html` 是受 Git 跟踪的发布入口（历史提交一直随
   `npm run build` 的 `sync-static` 同步），但不属于 `.gitignore` 覆盖范围。上一会话
   以“生成文件”为由留了它未提交，交代不实。

## 2. 修复

### 2.1 筛选信息架构：范围作为常驻条件

- 删除独立「01/UNIVERSE / 先确定研究范围」区块，把范围并入「筛选条件」章节顶部。
- ST、停牌、最低上市年限改为三条“常驻范围条件”，明确标注「固定参与基础股票池计算 / 始终并且」，与动态条件的全部/任一逻辑并列但语义独立。
- ST、停牌处理方式由左右滑动开关改为下拉选择（排除 / 包含），不再翻转换词。
- 后端 JSON 契约不变：仍通过 `include_st / include_suspended / min_listing_years` 传递，
  `base_pool` 草稿键不变，`conditions/sort/columns` 不进范围。

### 2.2 视觉基线回归

- 筛选编辑器卡片、结果面板恢复全局 `16px` 圆角与轻阴影；控件回到 6–9px 圆角。
- 标题、指标名等移除 `Songti SC/STSong` 宋体，恢复全局 `system-ui/微软雅黑` 体系。
- `ScreeningRuleEditor`、`RuleConditionRow` 同步圆角与字体，卡片层级、逻辑切换、连接词语义保留。

### 2.3 启动按需构建

- 新增 `frontend/scripts/fe-fingerprint.cjs`（纯 Node、无依赖）：对 `frontend/src`、入口、
  配置与 lockfile 计算 SHA-256 指纹；支持 `--check`（指纹与 stamp 一致且服务资源完整）
  与 `--stamp`（写 stamp）。
- `start.bat` 开发分支：先 npm 门禁与缺失依赖安装；仅当
  `app/web/static/index.html` 缺失、stamp 缺失、指纹变化或入口引用的 hash 资源缺失时
  才跑 `npm run build`；否则一步跳到 `python -m app.web.main`。二次启动通常只做一次
  毫秒级 Node 检查。
- stamp 放在仓库根 `.planning/.vd-fe-stamp.txt`（已被 `.gitignore` 忽略），不进入前端
  构建产物、不会被 `sync-static` 发布到 `app/web/static`，也不产生 Git 噪音。
- 构建入口 `app/web/static/index.html` 作为受跟踪的发布入口，随每次 build 同步提交。

## 3. 验证

- 真实浏览器（Chrome DevTools Protocol）实际渲染断言：常驻范围条件、ST/停牌/年限、
  「始终并且」均存在；旧「先确定研究范围」不存在；编辑器/运行面板计算样式为 16px 圆角
  与阴影；无宋体；常驻区无滑动开关。
- 前端：ESLint 通过、52 Node 测试通过、19 组件测试通过（新增
  `screening-standing-conditions.test.ts`）、生产 build 通过。
- 后端：`test_release_entrypoint.py` 16 passed（新增按需构建契约、指纹脚本契约）；
  Ruff 通过。
- 指纹行为实测：stamp 一致→`--check` 0；改动源码→1；恢复→0。

## 4. 披露

- 本轮未再触碰正式库连接层（`reports/51` 已修复）；正式自动更新 PID 早前已轮换，
  连接修复在下次 start.bat 启动自然生效。
- `app/web/static/index.html` 本轮随最终构建同步提交；hash 资源 `app/web/static/assets/`
  依然保持 ignored（不可提交构建产物）的既定边界。