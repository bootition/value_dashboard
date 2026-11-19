---
title: 多会话同仓并发事故复盘（2026-11-22 黑名单功能会话）
status: approved
category: reports
created: 2026-11-22
last-reviewed: 2026-11-22
---

# 多会话同仓并发事故复盘：6 类冲突、一个根因、三层解法

> **本报告是事件证据层，不是规则层。** 按 `AGENTS.md` §0 单一来源协议：
> AI 该怎么干活 → 已**合并进 `AGENTS.md` 的 W4 / W8**（不新增编号）；
> 命令级流程 → `docs/skills/multi-agent-safe-session/SKILL.md`；
> 机器级环境事实 → `docs/runbooks/ops-knowledge-base.md` §21。
> 本报告只负责：**发生了什么、真实读数、可复现的报错**。
>
> **与其他报告的关系**：`reports/140` §14.4/§14.5 由**另一个会话**独立记录了自己那一侧的
> 同一事故（它违反 W8 三次 + 发现 5 个历史提交 import 不通）。本报告从**被撞方视角**补充
> 时间线、进程与端口证据，并交叉引用，不重复其结论。

## 1. 事件背景

2026-11-22 主线任务：在 `fix/audit-remediation` 分支实现「股票黑名单」功能
（`app/core/blacklist.py` 等）。

- **13:40:36** 会话开工，`HEAD = b10e8d8`。
- 开工第一分钟 `git status --short` 显示 7 个未提交文件，内容是一个
  **`rank_mode`（条件级排名口径）**改动，不属于本会话。查 `git log` 确认 `reports/142`
  的排名口径功能已提交（`73bb498` 当时尚未出现），因此判断这是**另一个会话
  正在进行中的下一轮迭代**。

处理方式是「避开它，只改自己需要的文件」。方向对，**执行方式错**——继续在同一个工作树、
同一个分支上开工，等于把自己交给另一个写者的时序。

## 2. 实际踩到的 6 类冲突

### C1 改动被别人的提交吸走（最严重）

本会话的黑名单改动被**两次**带进对方的提交：

| 对方提交 | 时间 | 被带走的文件 | `--stat` 读数 |
|---|---|---|---|
| `73bb498`（排名口径方案 C） | 11-19 **13:53:16** | `app/core/screening/engine.py`（黑名单进筛选基础池） | +132 行（含 `RANK_MODES` 与本会话 hunk） |
| `6797fdd`（参数化字段收敛） | 11-19 **14:30:25** | `app/web/api/screening.py`、`docs/STATUS.md`、`docs/runbooks/ops-knowledge-base.md` §20 | +67 / +1 / 6 行 |
| `41181e1`（UI 审查整改） | 11-19 晚 | `docs/runbooks/ops-knowledge-base.md` §21（本报告配套的 KB 章节） | +19 行 |

> **第三次尤其有说服力**：§21 正是本报告在登记「多会话并发会互相吸走改动」这件事本身，
> 而它写完之后**又被吸走了一次**。规律很清楚——只要两个写者共享一个工作树，
> 这个模式就会一直重复，与双方是否守规矩无关。

可复现判据（两条都能当场验证）：

```
# 1) 谁引入了本会话的排除谓词
git log -S "exclusion_condition" --oneline -- app/core/screening/engine.py
#    → 73bb498（其父提交 b10e8d8 里 grep -c blacklist = 0）

# 2) 哪些文件被带进 6797fdd
git show 6797fdd --stat | grep -E "screening.py|STATUS.md|ops-knowledge-base.md"
```

后果：黑名单功能被**拆成两半**提交——数据层与文档进对方的
`feat(screening): 参数化字段收敛`，API/前端 18 个文件进本会话的 `79f15e4`（14:34:55）。
按 `AGENTS.md` Git §1「禁止跨主题打包提交」，这是实打实的违规，**且双方都不是故意的**。

### C2 提交边界不自洽 → 历史提交 `import` 不过（由 `reports/140` §14.5 发现）

`73bb498..3726e5f` **连续 5 个提交**里 `app/web/api/screening.py` 都
`import app.core.blacklist`，而 `app/core/blacklist.py` 直到本会话的 `79f15e4` 才入库
（本会话把它作为未跟踪新文件保留，准备和 API 一起提交）。

→ 把这 5 个提交 `git archive` 出来跑，直接 **`ModuleNotFoundError`**，新克隆与 `git bisect` 全断。

- 现状：`79f15e4` 之后 HEAD 自洽，缺陷只存在于那 5 个历史提交。
- **教训（本会话责任）**：新建模块必须**先进库再让任何人引用**；
  「未跟踪的新文件 + 已被别人 import」这个组合本身就是破窗。

### C3 对方的裸 pytest 让 S1 包装器反复拒绝

`scripts/s1-pytest.ps1` 的 preflight 有一道**机器级**判据：正式库抓取前后两次采样
「Python 进程集合」必须一致（`scripts/s1-path-preflight.ps1:329`
`throw "Python process state changed during formal capture"`）。

对方多次用**裸 pytest**（非包装器）跑回归，`Get-CimInstance Win32_Process` 实测：

| PID | 起始时间 | 命令行特征 |
|---|---|---|
| 425928 | 11-19 **13:51:10** | `C:\Python314\python.exe -m pytest --basetemp D:/vdtest-1795067470/pytest-tmp tests/regression/…` |
| 432252 | 11-19 **14:04:58** | `… --basetemp D:/vdtest-1795068298/…` |
| 449224 | 11-19 **14:20:13** | `… --basetemp D:/vdtest-1795069213/…` |

这些进程在包装器采样窗口内起止，直接触发上述 throw。同一轮还出现
`S1 execution error: Process(es) survived into After capture: 449224` ——
**存活的是对方的进程，不是本会话的**，但报错算在本会话账上。

> 这是**门禁正常工作**的结果，不是 bug（`AGENTS W6` 禁止裸跑全量的原因之一）。
> 但 W6 管不住「另一个会话」这个维度。

### C4 8765 端口被占 → `freeze S1`

```
S1 preflight Before failed: Value Dashboard service is listening on 8765 - freeze S1
```
判据在 `scripts/s1-path-preflight.ps1:362`：
```powershell
$listeners = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
```

### C5 对方中途写文件 → 假失败

同批次改动，第一次跑 6 个失败、重跑全绿，但两次的原因**不同**：

- **真实问题（本会话责任，已修）**：本会话把 `stock_detail.py` 拆成两个 router，
  打断了 6 个只 `include_router(stock_detail_router)` 的测试
  （`test_screening_api_regressions` 1 个、`test_stock_detail_periods` 3 个、
  `test_research_api` 2 个）→ 改用**聚合 router**（父 router `include_router` 两个子 router），
  外部单一 include 入口不变，6 个测试全恢复。
- **假失败（并发责任）**：`test_extended_screening_exposure` 断言
  `"EXTENDED_SCREENING_READY" in inspect.getsource(list_available_indicators)`，
  实测拿到的却是**另一个函数** `_dsl_indicator_label` 的源码——这是
  `app/web/api/screening.py` **在 import 与 `getsource` 之间被另一个会话改写**造成的。
  重跑单文件即 5 passed。前端 `vitest` 那 3 个 `rule-condition` 失败同理，单文件各自全绿。

### C6 构建产物互相覆盖

`npm run build` 同步到 `app/web/static/`（`frontend/scripts/sync-static.mjs`）。
本会话构建后 `app/web/static/index.html`（**该文件有意跟踪**，见 `AGENTS.md` 不要删清单）
变了；对方随后把它连同自己的构建一起提交。单次无害（下次构建覆盖），但它是「共享工作树」的
副作用样本。

## 3. 根因

> **共享可变状态没有归属。**

| 共享资源 | 能否被 `git worktree` 隔离 | 备注 |
|---|---|---|
| 工作树 / 文件 | ✅ 能 | `git worktree` 一开就隔离 |
| 分支 / 提交历史 | ✅ 能（配独立分支） | 必须各自独立分支 |
| Web 端口 8765 | ❌ 不能（机器级） | 需按 `web.port` 分配 |
| 正式库 `data/` | ❌ 不能（gitignore，不在 worktree 里） | 见 KB §21 |
| Python 进程表 | ❌ 不能（机器级） | S1 preflight 因此天然机器级串行 |

**结论：worktree 能消掉 C1 / C2 / C6，但消不掉 C3 / C4。**
C3 / C4 只能靠「端口分配 + S1 串行」，而这两条**无法靠提示词保证**，必须有机器级判据。

### 为什么"写进规则"不够

`AGENTS W8`（并行 agent 文件租约）在事故前**就已存在**，双方也都自认为遵守了
（本会话确实只改自己需要的文件、也按显式路径 `git add`）。仍然出事，因为：

1. W8 只约束「写哪些文件」，**不约束工作树与提交边界**；
2. W8 **没有执行器**——按 `AGENTS` 自己的判据「只改出口表现 = 延期付款」，它当时就是延期付款；
3. 本会话手上的 `create_worktree` 工具**没有用**。工具存在 ≠ 会用。

另有一条元观察：**两个会话各自独立地发现了同一问题并各写了一份文档**
（对方的 `reports/140` §14.4/§14.5、本报告的 §2）。这既是多会话的又一成本
（重复劳动 + 报告编号冲突风险），也说明这个坑足够显著。

## 4. 本次修复（三层，按性价比）

### L1 规则层 —— `AGENTS.md`（唯一注入面，按 §0 **合并进既有编号**）

- **W4 改写**为「并行会话的共享可变状态一律私有化，开工前先侦察」：把工作树、分支、端口、
  正式库、Python 进程表一次列全，写死「开工前先跑 preflight」与「全量 S1 只由主控串行跑」。
- **W8 补充**「提交只按显式路径，禁止 `git add -A` / `commit -a`」+「新模块必须先入库，
  再让别人 import」——直接反制 C1 与 C2。
- 两处都是**合并进既有编号**（§规则维护「合并」条），不新增编号，避免规则通胀。

### L2 流程层 —— `docs/skills/multi-agent-safe-session/SKILL.md`

命令固化：开局自检 → 建 worktree → 分配端口 → 定向测试 → 先入库新模块 → 按路径提交 → 主控串行合入。

### L3 执行器层 —— `scripts/session_preflight.py`

把「侦察」从自觉变成可执行判据，输出 `BLOCK` / `WARN` / `PASS` 与**具体下一步命令**：

| 检查项 | 触发条件 | 对应事故 |
|---|---|---|
| 他人未提交改动 | `git status --porcelain` 非空 | C1 |
| 其他 worktree | `git worktree list` 超过 1 条 | C1 / C6 |
| 其他会话在跑 pytest | 进程命令含 `pytest` 且 basetemp 不是本会话 | C3 |
| 8765 被占 | socket 探测 | C4 |

配套 `tests/regression/test_session_preflight.py`，守住「它会失败」——避免
`reports/125` 那种「守卫空转 24 小时」的假绿。

### 顺带补一条新门禁 RG10（针对同会话内的编码坑）

本会话还**复发**了 `AGENTS W1` 的编码事故：bash heredoc 传中文时，`·` 与 `—` 被控制台编码写成
`U+FFFD` 替换符，实见注释分隔线 `// ─── 黑名单操作 ───` 中间嵌了两个替换字符；另有一次把 `\n`
写成了真实换行。`W1` 当时**没有执行器**，靠事后全仓扫才发现。

现补 **RG10：仓库文件不得包含 `U+FFFD`**（`scripts/repo_integrity_guards.py`）。
零误报——源码里出现替换字符几乎必然是编码事故——且当场拦，不用等人工扫。
（本条报告自身最初就带着那个字面字符，被自己的 RG10 扫出来，已改写为文字描述。）

## 5. 与外部做法的对照

| 做法 | 代表 | 对应本仓库 |
|---|---|---|
| 一 agent 一 worktree，base checkout 只做集成点 | [Claude Code worktrees](https://code.claude.com/docs/en/worktrees)、[swarmgit](https://pypi.org/project/swarmgit/)（「Agents never run `git checkout`」） | W4 |
| 端口 / 数据库按 worktree 隔离 | [persistent-agent-runtime](https://raw.githubusercontent.com/shenjianan97/persistent-agent-runtime/refs/heads/main/docs/LOCAL_DEVELOPMENT.md)、[lanekeeper](https://pypi.org/project/lanekeeper/0.6.0/)（`database.strategy: per-agent`）、[portweave](https://www.npmjs.com/package/portweave) | W4（8765 必须按 `web.port` 分） |
| 单所有者文件 / 独占资源只由主控动 | [openbao AGENTS.md](https://github.com/rknightion/openbao-plugin-secrets-github/commit/e1b7430d6be7b0a094dee45dd8a5e05814a02db8) | W8 |
| 路径级租约 + 钩子拒绝越界编辑 | [raidlock](https://pypi.org/project/raidlock/0.1.0/)、[scopelease](https://pkg.go.dev/github.com/Beamfall/corvint@v0.8.1/internal/scopelease) | W8（目前只有规则，无执行器 → §6 T1） |
| git 操作与合并串行化 | [agent-queue merge-slot](https://github.com/ElectricJack/agent-queue/blob/32c4cbd4fc30f7e5c7e4624813235faa1b78c1f9/docs/specs/design/worktree-execution.md)（`Orchestrator._git_mutex`） | W4「全量 S1 只由主控串行跑」 |

> 差别：外部工具把隔离做成**基础设施**；本仓库目前做到「规则 + 开局 preflight」。
> 差距就是 §6 的 T1。

## 6. 未做 / 待办（诚实披露）

| # | 待办 | 为什么没做 | 触发条件 |
|---|---|---|---|
| T1 | W8 的**路径租约执行器**（`scripts/session_lease.py` + pre-commit 集成） | 单人项目当前 2 个会话，`session_preflight` 的 BLOCK 已能拦住多数场景；租约还需定义 TTL / 僵尸清理 / 跨机语义 | 出现第二次同类事故，或会话数 ≥3 |
| T2 | pytest basetemp 与端口**自动分配**（而非检测到冲突再改） | 依赖 T1 | T1 落地后一并做 |
| T3 | `s1-path-preflight.ps1:362` 的 8765 是**硬编码**，而 `config/default.yaml:6` 的 `web.port` 可配 | 这是**故意的保守冗余**（宁可误拦不可漏拦）；改成读配置会让「别人把服务起在 8766 读正式库」漏网 | **不动**，已登记 KB §21 防后人当 bug 修 |
| T4 | C2 的 5 个历史提交自洽性 | 修复需 rewrite history（force-push 共享分支，风险大于收益）；HEAD 已自洽 | 若要做 `git bisect` 考古时，从 `79f15e4` 起 |

## 7. 一句话结论

> **不要试图让两个 agent 更小心，要把共享可变状态变成私有。**
> 提示词与规则是概率性的，worktree / 端口分配 / preflight 判据是结构性的。
> 按本仓库「不要删清单」的判据（能指出它防的是哪次事故，就不算过度设计），上述三条都不算过度设计。
