---
title: 构建、发布与 S1 门禁运行手册
status: approved
category: runbooks
created: 2026-08-03
last-reviewed: 2026-08-03
---

# 构建、发布与 S1 门禁运行手册

> O2（`reports/41` B1）。发行包构建（build-release）、S1 隔离回归
> （s1-pytest）、前端门禁与锁文件的日常用法。**发布前必须跑 S1 且
> 确认正式库哈希不变。**

## 1. 日常门禁

```bash
# S1 隔离回归（正式库只读保护 + 逐字节哈希比对；406+ 项）
scripts/s1-pytest.ps1 tests/regression

# 静态检查
uv run --locked ruff check app tests/regression

# 前端三连（lint / 52 node 合约 + 10 vitest / 构建）
cd frontend
npm run lint
npm run test
npm run build
```

- S1 wrapper 禁止正式库写操作（`VD_FORMAL_ACK` 被拒）；前后哈希不一致
  会以退出码 99 失败并留下 `delta-report.json`。
- 证据写入 `docs/evidence/evidence-s1/<run-id>/`（hash-evidence.json）。

## 2. 锁文件

```bash
uv lock --locked     # Python 依赖锁定
# 前端锁文件 frontend/package-lock.json 由 npm 维护
```

## 3. 构建发行包

```bash
scripts/build-release.ps1            # 默认输出 dist/
scripts/build-release.ps1 -OutputDirectory D:\release\vd-20260803
```

- 前置：`uv.lock` 与 `frontend/package-lock.json` 必须存在且已提交；
- 构建会自动跑前端构建与 smoke（真实 exe `/api/health`）；
- 禁止将 `data/` 正式库打包进发行目录（脚本强制检查）。

## 4. 启动方式

| 环境 | 入口 |
|---|---|
| 发行版 | 发行目录 `value-dashboard.exe`（或同目录 `start.bat`） |
| 开发版 | `start.bat`（回退 `python -m app.web.main`）或 `python -m app.cli.main server` |

## 5. 发布清单（Red Team 纪律）

1. `git status` 干净（data/、构建产物不提交）；
2. S1 411 全绿 + ruff + 前端三连全绿；
3. 正式库 SHA-256 前后一致（见最新 evidence-s1）；
4. `uv lock --locked` 通过；
5. 真实发行包 smoke 通过（build-release 已含）；
6. 文档：STATUS.md 同步、报告留档、`git push` 并确认远程。

## 6. 参考

- `docs/reports/40`（正式启用独立复审 PASS 的完整门禁清单）
- `docs/runbooks/s0-evidence-preservation.md`（证据保全）