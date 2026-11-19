---
title: 多会话安全开工（worktree / 端口 / 按路径提交）
status: approved
category: skills
created: 2026-11-22
last-reviewed: 2026-11-22
---

# SKILL：多会话安全开工

> **适用**：同一个仓库上可能有第二个 AI 会话（或你自己）同时在干活时——**任何一次会话开工之前**。
> 规则本体在 `AGENTS W4`（共享可变状态私有化）与 `AGENTS W8`（文件租约 + 按路径提交）；
> 事故证据在 `reports/144`。本文只给**能直接敲的命令**。

## 0. 先记住一条

> **不要试图让两个 agent 更小心，要把共享可变状态变成私有。**

提示词和规则是概率性的；worktree、端口分配、preflight 判据是结构性的。

## 1. 开工自检（30 秒，必做）

```bash
uv run --locked python scripts/session_preflight.py
```

三种结论：

| 结论 | 退出码 | 你该做什么 |
|---|---|---|
| `PASS` | 0 | 没有检测到第二方活动，直接在当前位置开工 |
| `WARN` | 0 | 可以开工，但先确认工作区那些改动**不是别人的** |
| `BLOCK` | 2 | **先处理再动手**，别在别人的活上叠 |

它查四件事，每条对应 `reports/144` 的一个事故：

| 检查 | 事故 |
|---|---|
| 工作区未提交改动 | C1 改动被别人的 `commit -a` 吸走 |
| 其他 `git worktree` | C1 / C6 构建产物互相覆盖 |
| 别人的 `pytest` 在跑 | C3 S1 preflight 反复 `Python process state changed` |
| 8765 被监听 | C4 `freeze S1` |

自己起过服务时：

```bash
uv run --locked python scripts/session_preflight.py --allow-port 8765   # BLOCK 降级为 WARN
```

## 2. WARN 时：判断归属

```bash
git status --short                 # 谁的文件？
git worktree list                  # 有没有别的会话
git log --oneline -3               # 那是不是我刚提交的？
```

**判据**：如果这些改动不是本次任务要做的，它们就是**别人的**。此时**不要**继续在当前工作树叠加，
走第 3 步。

## 3. 建私有工作树

```bash
# 方式 A（推荐，本环境工具已提供）：create_worktree
#   会新建分支 + 新检出目录，并把会话工作目录切过去；未提交文件留在原处

# 方式 B（手敲等价物）
git worktree add ../vd-<topic> -b <topic-branch> HEAD
git worktree lock ../vd-<topic> --reason "session <topic>"
cd ../vd-<topic>
```

**必须知道的四条环境事实**（详见 `docs/runbooks/ops-knowledge-base.md` §21）：

1. `data/` 是 gitignore 的，**不会出现在 worktree 里**。只读正式库要走**绝对路径**；
   要跑回归则走 `scripts/s1-pytest.ps1`（它本来就用 `VD_ENV=test` 的临时库，不受影响）。
2. **`.venv` 也不会跟过来，而 `s1-pytest.ps1` 硬编码 `<worktree>/.venv/Scripts/python.exe`**
   —— 2026-11-19 实测：worktree 里若被 `uv run` 留下一个**没有 pytest 的 stub venv**，
   全量回归会直接 `No module named pytest` 失败（看起来像代码坏了，其实是环境）。
   **做法：用目录联接复用主仓库的 venv，不要复制、也不要 `uv run` 重建**：
   ```powershell
   # PowerShell（Git Bash 的 mklink 参数容易被吃掉）
   Remove-Item -Recurse -Force .venv -ErrorAction SilentlyContinue
   New-Item -ItemType Junction -Path .venv -Target "D:\...\value-dashboard\.venv"
   ```
   ```bash
   # 之后一律用她，别写 uv run（uv run 会在 worktree 里新建 250MB 且缺 dev 依赖）
   ./.venv/Scripts/python.exe -m pytest ...      # cwd = worktree 根，import 的 app 就是这一份
   ```
3. **`frontend/node_modules` 同样不跟过来**，用同样的联接复用：
   ```powershell
   New-Item -ItemType Junction -Path frontend\node_modules -Target "D:\...\value-dashboard\frontend\node_modules"
   ```
   联接是**只读复用**：不要在这个 worktree 里 `npm install`（会写穿到主仓库，影响别的会话）。
4. 端口不会跟着 worktree 走。起服务前先改端口：

```bash
# config/default.yaml 的 web.port（默认 8765）；或环境变量覆盖（见 config.py）
# 原则：一个 worktree 一个端口，绝不两个会话抢 8765
```

收工清理：

```bash
git worktree unlock ../vd-<topic>
git worktree remove ../vd-<topic>
```

## 4. 干活期间的硬纪律

| 纪律 | 命令 | 为什么 |
|---|---|---|
| 提交只按显式路径 | `git add <file>…` / `git commit -- <file>…` | 禁止 `git add -A`、`git add .`、`git commit -a`：共享工作树下会把别人的半成品一起提交（`reports/144` C1） |
| 提交前核暂存面 | `git diff --cached --stat` | 一眼看出有没有跨主题文件混进来 |
| **新建模块先单独入库** | 先 `git add app/core/new_thing.py && git commit`，再让别处 import | 「未跟踪的新文件 + 已被别人引用」会让 5 个历史提交在新克隆上 `ModuleNotFoundError`（C2） |
| 全量 S1 串行 | 只有主控跑一次 `scripts/s1-pytest.ps1 tests/regression` | 端口与 Python 进程表是**机器级**的，worktree 隔不开 |
| 改文件用精确字面量 | 见 `AGENTS W1`；**不要**用 bash heredoc 拼中文 | heredoc 会把 `·` `—` 写成 `U+FFFD`；现由 **RG10** 兜底 |
| 编完立刻查编码 | `uv run --locked python scripts/repo_integrity_guards.py` | RG10 会扫全仓 `U+FFFD` |

## 5. 合入

`AGENTS W8`：**跨域修改走主控串行合入**。子会话只 push 自己的分支：

```bash
git push origin <topic-branch>
```

主控按主题逐个合、每次合完跑定向用例。不要两个分支同时往同一个文件上合。

## 6. 收尾清单

- [ ] `git status` 里**没有自己的**未提交改动
- [ ] 自己的分支已 `push`，并用 `git ls-remote origin <branch>` 核对远端 SHA 与本地一致
- [ ] 若提交时把别人的文件留在了工作区：**记录在会话总结里**，别假装干净
- [ ] 结论回写 `docs/`（`.planning/` 是 gitignore 的，放那里新克隆看不到）

## 7. 什么时候该升级到「路径租约」

`AGENTS W8` 目前只有规则 + 本 SKILL，**没有自动执行器**（`reports/144` §6 T1）。触发升级的条件：

- 出现**第二次**同类事故，或
- 并发会话数 **≥ 3**

届时按 `reports/144` §6 T1 做 `scripts/session_lease.py`（借 `app/core/storage/update_lock.py`
现成的 PID 校验 + 僵尸清理），并挂进 `.githooks/pre-commit` 当 RG11。
外部可参考 [raidlock](https://pypi.org/project/raidlock/0.1.0/)（路径级租约 + 钩子拒绝越界编辑）
与 [scopelease](https://pkg.go.dev/github.com/Beamfall/corvint@v0.8.1/internal/scopelease)（TTL + 僵尸锁）。
