# .githooks — 提交门禁（pre-commit）

**一句话**：`git commit` 时自动跑一次**秒级**静态门禁，把 AGENTS.md 里「靠人记得跑」的检查变成「不用记得」。

背景：本仓库长期**没有 CI、没有 git hook**，9 组静态门禁全靠自觉；`reports/140` 实测的后果是
`.gitignore` 一行正则把 `tests/regression/fixtures/` 吞了四个月没人发现（新克隆必红）。
自觉不可证伪，钩子可以。

---

## 1. 安装（一次性，1 行）

```bash
sh scripts/install-git-hooks.sh             # Git Bash / Linux / macOS
```

```powershell
pwsh -File scripts/install-git-hooks.ps1    # PowerShell（Windows）
```

两个脚本等价，都只做三件事，且**幂等**（重复跑不报错、不会重复设置）：

1. `git config --local core.hooksPath .githooks`
2. `chmod +x .githooks/pre-commit`（Windows 上不需要，ps1 会跳过）
3. 把 `.githooks/*` 的 CRLF 刷成 LF（见 §7 排障）

> **新克隆必须重跑一次安装**：`core.hooksPath` 写在 `.git/config` 里，不随仓库分发。

卸载：

```bash
sh scripts/install-git-hooks.sh --uninstall
pwsh -File scripts/install-git-hooks.ps1 -Uninstall
```

安装完的验证方式（脚本也会打印）：

```bash
git config --local --get core.hooksPath        # 应输出 .githooks
sh -n .githooks/pre-commit                     # 应无输出
bash .githooks/pre-commit; echo exit=$?        # 无暂存内容时应快速 exit=0
# 真实触发：随便改一个文件 → git add → git commit，看是否出现 [pre-commit] 输出
```

---

## 2. 装上之后挡什么

| 触发条件 | 跑什么 | 量级 |
| --- | --- | --- |
| 暂存区为空 | **什么都不跑**，静默 `exit 0` | ~0.2s |
| 任意暂存内容 | `uv run --locked python scripts/check_rework_guards.py`（RG1–RG9 九组） | ~3s |
| 暂存 `app/**`、`tests/**`、`conftest.py`、`_pytest_policy.py` 的 `.py` | `uv run --locked ruff check --output-format=concise <只这些文件>` | ~0.3–1s |
| 暂存 `docs/**/*.md` | **不额外跑**（编号/front-matter/supersede/真相源已由 RG6–RG9 覆盖），只打一行提示 | 0 |
| 暂存 `frontend/**` | **不跑** lint/test/build，只打一行提示 | 0 |
| 暂存 `scripts/**` 等其它 `.py` | 不 lint（存量债务，见 §8），只打一行提示 | 0 |

RG1–RG9 具体是什么，见 `scripts/check_rework_guards.py` 文件头 docstring 与 `AGENTS.md` 反返工段落。

---

## 3. 实测耗时（2026-11-19，Windows + Git Bash，5 个 agent 并行抢磁盘时）

| 场景 | 钩子内计时 | 用户感知（含 MSYS 进程启动开销） |
| --- | --- | --- |
| 空暂存区 | — | **0.23s** |
| 只改文档 / 前端 | 3.1s | **4.1s** |
| 改并暂存 1 个 `.py` | 3.6–3.7s | **4.9–5.0s** |

其中护栏本体约 3s（`uv run --locked python scripts/check_rework_guards.py` 单独跑实测 2.0–3.3s，
随机器负载波动）。**总预算 10s** —— 这就是钩子里只放快检查的原因。

---

## 4. 为什么钩子里不放慢检查

- 全量 `pytest`（S1 隔离回归）在本机是 **20–40 分钟**级别，前端 `npm run build` 是 1–3 分钟级别。
- 提交是**高频**动作。任何超过 10s 的钩子，几天内就会被 `git commit --no-verify` 永久绕过——
  那等于没装，比不装更糟（因为它给了"我们有门禁"的错觉）。
- 慢检查应该由 CI 或人工批次承担；钩子只挡「机械可判、秒级可判」的那部分。

---

## 5. 留给 CI 的清单（钩子**不**做，别往里加）

- 全量 pytest / S1 隔离回归（`scripts/s1-pytest.ps1 tests/regression`）
- 前端 `npm run lint` / `npm run test` / `npm run build`
- `uv lock --locked` 锁文件一致性
- 打包/构建（PyInstaller：`scripts/build-release.ps1`）
- 任何需要数据库（DuckDB/SQLite）、网络、外部数据源的检查
- 跨文件语义检查（"改了 schema 必须同步改 X" 之类）——太慢且容易假红
- 覆盖率、性能基线、`onboarding_metrics.py` 之类的度量

---

## 6. 绕过：可绕过，但留痕

1. **推荐**：`SKIP_GATES=1 git commit -m "..."` —— 会打醒目警告，并往 `$GIT_DIR/hook-skips.log` 记一行
   （时间 / 分支 / 暂存文件数 / 原因）。可审计。
2. `git commit --no-verify` —— git 原生行为，**钩子根本不执行**，因此无法留痕。

查看历史绕过：

```bash
wc -l .git/hook-skips.log && tail -20 .git/hook-skips.log
```

### 钩子不是 CI 的替代

- 绕过记录少（每月几次）→ 正常，紧急修复本来就会发生。
- 绕过记录频繁，或某些人几乎每次都绕 → 说明**门禁太吵**或**检查不对**：要么修钩子，
  要么补 CI（CI 上没有 `--no-verify`，也挡得住 `git commit -n`）。
- 注意 `--no-verify` 留不下任何痕迹，所以**真实绕过率 ≥ 日志计数**。想拿到真实值，只能靠 CI 兜底。

---

## 7. 环境变量

| 变量 | 作用 |
| --- | --- |
| `SKIP_GATES=1` | 跳过全部门禁（打警告 + 写 `hook-skips.log`） |
| `VD_HOOK_RUFF_ALL=1` | 把范围外的 `.py`（如 `scripts/*.py`）也送 ruff |
| `VD_HOOK_ASCII=1` | 强制英文输出（GBK 老终端兜底） |
| `VD_HOOK_DRY_RUN=1` | 只打印将要跑什么，不实际执行 |
| `VD_HOOK_NO_AUDIT=1` | 不写 `hook-skips.log` |

---

## 8. 排障

- **中文变乱码**：钩子已 `export PYTHONIOENCODING=utf-8`（`reports/138` §3.1 的 GBK 事故），
  并在真实 TTY 下尝试 `chcp 65001`（退出时自动还原原代码页）。仍然乱码就 `VD_HOOK_ASCII=1` 走纯英文。
- **"装了却没生效"**：先 `git config --local --get core.hooksPath`，必须是 `.githooks`。
  Git for Windows 多数情况能容忍 CRLF 的钩子（实测本钩子 CRLF 也能跑），但 macOS/Linux 的
  `#!/bin/sh\r` 会 `bad interpreter`，属于"装了却没生效"的经典事故 → 安装脚本会顺手刷成 LF
  （用 `od` 按字节判断，MSYS 的 grep/wc 会做文本模式转换、判不出来）。
- **提交明显变慢或卡住**：首次提交或 `uv.lock` 变更后，`uv run --locked` 会自行同步环境
  （实测全新克隆首次提交会创建 248MB `.venv`，约 6s；`.venv` 在 `.gitignore` 内）。
  建议先手动 `uv sync --extra dev` 再提交；钩子对超时有 30s/60s 兜底，超时会告警并放行。
- **护栏报 `ModuleNotFoundError`**：环境缺依赖 → 钩子判定为「环境问题」并 fail-open 放行（会告警）。
  `uv sync --extra dev` 后重试。
- **想看它到底要跑什么**：`VD_HOOK_DRY_RUN=1 bash .githooks/pre-commit`。

---

## 9. 已知取舍（写清楚，免得以后吵）

- **ruff 只覆盖 `app/ tests/ conftest.py _pytest_policy.py`**，也就是 AGENTS.md 的门禁范围。
  `scripts/` 当前有约 69 条 ruff 存量违规（实测 `ruff check .` = 69 errors，全在 scripts/build/_legacy），
  若对所有暂存 `.py` 硬跑 ruff，改一行脚本就会因旧债误红——那是逼人 `--no-verify` 的经典做法。
  范围外的 `.py` 只提示不拦截；要强制可用 `VD_HOOK_RUFF_ALL=1`，彻底解决应放到 CI 或单独清理批次。
- **钩子不做「工作区是否干净」检查**：本仓库常有多个 agent 并行改文件，那样会天天误报。
- **副作用边界**：钩子只读仓库文件 + 写系统临时目录 + 写 `$GIT_DIR/hook-skips.log`，
  **不修改工作区任何文件**。两个已知例外，都是 `uv run --locked` 自身的行为：
  - 触发 sync 时会写 `.venv/`（已在 `.gitignore` 内）；
  - 触发 sync 时 uv 会在仓库根留下 `uv-*.lock`（进程互斥锁，**未**被 `.gitignore` 覆盖）。
    ⇒ 建议 `.gitignore` 加一行 `uv-*.lock`，与下面 `.gitattributes` 那条一起补。
- 建议在 `.gitattributes` 里加一行 `.githooks/* text eol=lf`，从根本上避免 CRLF 事故。
