---
title: Git 考古与可安全移除的历史包袱（2026-11-17）
status: approved
category: reports
created: 2026-11-17
last-reviewed: 2026-11-17
---

# Git 考古：哪些是有意设计，哪些是历史包袱

> **一句话结论**：这个仓库的"设计密度"其实很高——**大部分看起来奇怪的结构都能对应到一次
> 真实事故**（`_path_policy`、`update_lock`、`error_kind`、`merge_step_status`、
> 门禁阈值、S1 包装器）。真正的历史包袱只有 4 类、共 **12 项**，其中 6 项可以零风险删除；
> 另外有 1 项（`_domestic_direct`）经实测**从未真正生效**——它解决的是一个这台机器上不存在、
> 而且用它的方式也解决不了的问题。
>
> 读这份报告的正确姿势：**要删东西之前先看 §7「不要删」**，那里的每一项删掉都会重演一次事故。

---

## 1. 方法与判据

### 1.1 先说一个必须知道的事实：这个仓库有**两个根提交**

```
f4517d2  2026-07-24  chore: capture incident source baseline before S1 remediation   （42 文件 / 12,436 行）
40ba9ff  2026-08-28  chore: establish repository baseline snapshot                   （69 文件 / 30,183 行）
        └── 两者于 2026-08-31 由 f3747f5 合并：
            "merge: reconcile local audit-remediation history with remote red-team history"
```

后果：
- `git log --follow` 对 08-28 之前的文件会"断头"；`git blame` 可能把整行归给 baseline 快照；
- 任何"这个模块是谁在什么时候引入的"结论，**在 08-28 之前只能以报告编号为线索**，不能只信 commit；
- 两次 `chore: ... baseline` 都是"把当时的工作区一股脑入库"，因此**首次引入时间不等于设计时间**。

### 1.2 判定标准

| 判据 | 怎么测 |
|---|---|
| **可达性** | 从 `app.web.main` / `app.cli.main` / `core.auto_update` 出发的 import 闭包（88 模块 / 60,130 行）；闭包外的 17 模块 / 3,055 行 = 维护面 |
| **替代实现** | 同一能力是否已有第二处（RFC：`_to_float` ×7、两份 `default.yaml`） |
| **意图证据** | commit body / 报告编号 / 代码注释是否写明"防的是什么事故" |
| **问题是否仍存在** | 用当前环境实测（例：§5.1 的 `getproxies()` 实验） |

复现：`git log --max-parents=0`、`python scripts/onboarding_metrics.py`（见 `reports/134`）。

---

## 2. 架构演变：六个时代

| 时代 | 时间 | 触发事件 | 引入的结构（今天还在） | 当时要解决的问题今天还在吗 |
|---|---|---|---|---|
| **E1 止血 + 文档治理** | 07-24 ~ 07-31 | 07-22 测试污染正式库（`incident-2026-07-22`） | `storage/path_policy.py`（`VdEnv`/`DatabasePathSet`）、`s1-pytest.ps1`、证据保全 runbook、Diátaxis + front-matter 生命周期 | ✅ 仍在（每个测试都靠它隔离） |
| **E2 红队整改 + 功能扩张** | 08-01 ~ 08-14 | 发布级红队 P0/P1（`reports/30/34/80/81`） | `update_lock.py`（跨进程互斥）、`job_logs` + 进度模型、国债曲线、历史股本链、研究统计、ruff 规则集扩展 | ✅ 仍在 |
| **E3 数据完整性与吞吐** | 08-25 ~ 09-05 | 内存 OOM、raw archive 轮转失控、冷归档需求 | raw/archive 拆分、DuckDB 内存上限、`cold_archive` 视图、统计域多进程、`screening_readiness_cache` | ✅ 大部分仍在（缓存见 §6.5） |
| **E4 CSMAR 数据包** | 09-17 ~ 09-20 | 商业数据包入库（462 万行）、字段口径陷阱 | schema v24–v25、三色灯裁定（`csmar_formulas.py`）、`csmar_*` 表 | ⚠️ 入库已完成；**裁定器只在"再导入"时需要**（§5.3） |
| **E5 自动更新可靠性** | 10-20 ~ 11-13 | 两轮 110/153 分钟、指标 14 天未发布（`reports/117/118`） | `ErrorKind` 七分类、熔断桶 `(adapter,data_type)`、门禁分级 `max(50,1%)`、`merge_step_status`、qfq 待重算名单、价格攒批 | ✅ 仍在（且是本项目最贵的教训） |
| **E6 数据资产 + 指标扩张** | 11-14 ~ 11-17 | 扩展指标冻结、ROCE 四次返工、告警淹没（`reports/119~133`） | `asset_health` severity/tier、`null_reason` 六义、`DerivedSnapshotDomain` 注册表、RG1–RG4 门禁、复合指标 Skill | ✅ 仍在 |

**演变的一句话总结**：从"单体脚本能跑就行"→"用契约和证据把事故挡住"。
结构复杂度是**事故数量**的函数，不是"过度设计"。这也是为什么 §7 的"不要删"清单比 §4 的"可删"清单更重要。

---

## 3. Git 工作流本身：三个值得记录的偏差

| 事实 | 证据 | 影响 |
|---|---|---|
| **没有 PR**：单分支（`fix/audit-remediation`）+ 报告编号批次 | 328 commit / 1 merge / 0 PR；每批改动在 `docs/reports/NNN` 里描述 | 报告承担了 PR 描述 + 评审记录 + 事故复盘的三种职责，所以报告膨胀到 132 份 |
| **一次 revert / reapply**：08-04 桌面筛选 UI | `9518851` Revert → `8baa5bb` Reapply（间隔 32 分钟） | 唯一一次回滚，原因是合并冲突而非功能问题；说明"回滚成本"在本项目几乎没有被演练过 |
| **一个月静默期**：最后一次提交 09-20，下一次 10-20 | `git log --since=2026-09-10 --until=2026-10-21` 只有 09-17/19/20 与 10-20 四天 | E5 的事故（10-20 慢更新、10-30 崩溃）都发生在**静默期之后**——回归是在"没人动它"的时期积累的 |

> 顺带：`.gitignore` 里还留着 `/docs/evidence-s1$RUNID/` 这条**字面量**规则（PowerShell 变量没展开的产物）。
> 无害，但它是"我们的 git 规则也曾被脚本化生成"的化石。

---

## 4. 有意为之：12 项"看起来怪，但别删"

| # | 结构 | 表面上看像 | 实际意图（证据） | 删了会怎样 |
|---|---|---|---|---|
| 1 | `storage/path_policy.py` + `scripts/s1-pytest.ps1` | 为了一个本地工具搞路径契约，过度 | 07-22 测试写坏正式库（tag `incident-2026-07-22`、contract 898 行） | 测试再次污染 `data/` |
| 2 | `storage/update_lock.py` + `exclusive_update` | 单人工具不需要锁 | 08-02 红队：跨进程互斥（`reports/30`） | 自动更新 + CLI 并发写库 → DuckDB 损坏 |
| 3 | `adapters/base.py::ErrorKind` 七分类 | 错误分类这么细？ | `reports/117` P0-4：5 只股票让 3 个源同时熔断 | 熔断放大、假 `retry_exhausted`、永久 missing |
| 4 | `job_status.merge_step_status` | 一个 dict 合并而已 | `reports/117` P0-2：success 覆盖 rejected，14 天假绿 | 对外假成功 |
| 5 | `data_quality.gate_hard_block_threshold`（`max(50,1%)` + 1/3 占比） | 魔法阈值 | `reports/117`/`118`：整批拒绝 vs 逐股隔离 | 26 只新股再次拖死全市场发布 |
| 6 | `screening/field_units.py` + 前端镜像 | 单位表重复 | `reports/80` 簇 A/B/C：100 倍误差 | 显示/筛选口径分叉 |
| 7 | `core/dsl/`（2,404 行） | 单人工具要什么 DSL | `reports/131` 42 个大师指标需要表达式化 | 每个指标都要改 Python |
| 8 | `cli/protocol.py` 的 plan→confirm | 单人工具要什么审批令牌 | 破坏性操作两段式（backup restore / archive 必须 `--plan-id`） | 一条命令就能抹掉归档表 |
| 9 | `quality/asset_health.py` 的 `severity`/`tier` | 规则为什么要分级 | `reports/122` J9/J10：32 条永远红的告警淹没真问题 | 告警疲劳，真问题没人看 |
| 10 | `null_reasons` 六义 + 稀疏表 | 一个 NULL 还要六种原因 | `reports/120`："能选中但筛不出结果"无法解释 | 用户分不清"没有"和"坏了" |
| 11 | `DerivedSnapshotDomain` 注册表 + RG1–RG4 | 新的概念税 | `reports/133`：ROCE 六列被快照重发清空 | 派生列静默变 NULL |
| 12 | `cold_archive` 17 张表转 Parquet 视图 | 为什么不留在主库 | 主库 19GB、DuckDB 单文件压力（`reports/109/123`） | 主库继续膨胀、重建变慢 |

> 判据：**能指出"防的是哪次事故"的结构，就不算过度设计**；指不出来的才进 §5/§6。

---

## 5. 为解决"已不存在的问题"而引入（含实测）

### 5.1 `_domestic_direct()`：一个从未生效的代理绕过器（7 个适配器 + 1 个新模块）

**它想解决的问题**：国内数据源不应走 GitHub 代理（`127.0.0.1:10808`），所以进入请求前
`pop` 掉 `HTTP_PROXY/HTTPS_PROXY`，退出时恢复。

**实测结论：它做不到它想做的事。**

```python
import os, urllib.request as u
u.getproxies()
# → {'http': 'http://127.0.0.1:10808', ...}          ← 代理来自 Windows 注册表
for k in ("HTTP_PROXY","HTTPS_PROXY","http_proxy","https_proxy"): os.environ.pop(k, None)
u.getproxies()
# → {'http': 'http://127.0.0.1:10808', ...}          ← 清空 env 后仍然有代理
```

三条事实：
1. 本机 **没有任何** proxy 环境变量（进程 / 用户 / 机器级都查过）；
2. 真正的代理来自 **Windows 系统代理注册表**（`ProxyEnable=1`、`127.0.0.1:10808`）；
3. `requests`（akshare 用的）在 env 为空时会**回退读注册表**，所以 `os.environ.pop()` 对
   4 个 akshare 型适配器（`cninfo_funding` / `eastmoney_repurchase` / `hk_dividend` / `ah_hk_mapping`）**完全无效**；
   对已经写了 `trust_env=False` 的 `index_valuation` / `ths` 也无效；只剩 `hk_price`（httpx 默认配置）会受影响，
   而当前没有 env 变量 → 也是 no-op。

**结论**：这是"解决已不存在问题 + 方法无效"的双重包袱，而且它**修改进程级环境**（多线程下会让
别的请求失去代理）。7 份副本已在 `reports/134` 合并成 1 份，接下来应当整体删除。

**移除步骤（低风险）**
```bash
# 1) 给仍用默认 trust_env 的 httpx client 显式加 trust_env=False（按源逐个确认）
grep -n "httpx.Client(" app/core/adapters/*.py
# 2) 删掉 20 处 with _domestic_direct():（保留内部缩进块）
# 3) 删除 app/core/adapters/_net.py
# 4) 验证
uv run --locked ruff check app && \
VD_ENV=test ... uv run --locked python -m pytest tests/regression/test_adapter_*.py \
  tests/regression/test_funding_and_index_valuation_domain.py -q
```
**影响面**：只在"未来有人设置 proxy 环境变量"时行为不同；当前环境零变化（已实测）。

### 5.2 `/design-preview` 路由 + `ResearchWorkbenchPreviewPage.vue`（123 行）

- 该页面是**硬编码假数据**的 UI 原型（内联 `600519`、`五粮液`、47/83 只候选池…）；
- 只在 `frontend/src/main.ts:58` 注册，**没有任何导航入口**，只能手输 URL 进入；
- 它是 E2 时代"四页桌面 UI 定稿"（`reports/47/48/52`）留下的样稿。

**风险**：假数据会被打进 bundle 并随 `npm run build` 进入生产静态目录；用户误入会看到"看起来像真的"的研究结论。
**移除**：删路由 + 删组件（或改为 `import.meta.env.DEV` 条件注册）。**零业务影响**。

### 5.3 `app/core/csmar_formulas.py`（198 行）：只在测试里被 import

- CSMAR 数据包已入库（`csmar_*` 表 + schema v24–v25），三色灯裁定的结论已写进
  `reports/131` / `.planning`；运行时代码**不引用**它；
- 唯一引用：`tests/regression/test_csmar_field_verdict.py`。

**处置**：移入 `tests/support/csmar_formulas.py`（保留裁定测试），或删除后把裁定表落成一份 CSV 证据。
**不要**直接删测试——那 2 个真实口径陷阱（`FI_T10` 市值含负债、`F300000` 实为归母权益）值得留。

### 5.4 `docs/runbooks/s0-evidence-preservation.md`（822 行，`status: approved`，last-reviewed 07-26）

- S0 证据保全流程已被 S1 包装器取代（`scripts/s1-pytest.ps1` + `conftest.py` 的指纹捕获）；
- 但它仍标 `approved`，而 AGENTS.md 说 `approved` = "可作建议依据"。

**处置**：front-matter 改 `status: historical`，并在开头加一行"已被 S1 取代，见 `runbooks/ops-build-release-s1.md`"。
**不要删**：里面记录了 07-22 事故的证据口径，是审计资产。

---

## 6. 冗余与漂移（不是死代码，是"两套都在跑"）

| # | 冗余 | 证据 | 风险 | 建议 |
|---|---|---|---|---|
| 6.1 | **两份 `config/default.yaml`**：仓库 `config/` + 包内 `app/resources/config/` | 当前仅差 2 行注释；`config.py` 源码跑读前者、打包跑读后者；**HEAD 上没有同步校验** | 下一次改 key 只改一份 → 打包版行为不同 | 生成制：构建时从 `config/default.yaml` 复制；或加一条"两份必须逐字节相同"的测试（`reports/134` 的 RG 风格） |
| 6.2 | `masked_fields_for_codes()` / `masked_indicator_fields()` | 后者是前者的纯别名（`data_quality.py:1341`） | grep 失效、新人不知道改哪个 | 删别名，改调用点（9 处） |
| 6.3 | `_to_float` ×7、`_to_date` ×6 | 同名不同义：有的拒 `±Inf`、有的用 pandas NaN、有的裸 `float()` | 同一种源数据在不同适配器里得到不同结果 | 统一 `core/numbers.py::to_float(*, allow_inf=False)`，逐处确认语义 |
| 6.4 | `data_quality.py`（2,017 行）与 `quality/`（4,275 行） | 两个"质量"概念并行；`reports/117` 里 `missing_count` 出现过 11,823 / 162 两个数 | 改判据要改两处 | 收口为 `quality/gates.py` + `quality/trust.py`（`reports/134` S4） |
| 6.5 | `screening_readiness_cache`（08-30 引入，最后改动 09-08） | 为绕开"冷启动 54s 阻塞"而缓存到 SQLite + 启动预热 | **可能已不需要**（后续有索引/统计优化），但**未实测** | 先测：干净库冷启动跑一次 readiness，若 <2s 则删缓存+预热；否则保留 |
| 6.6 | 磁盘上的 `build/lib/app/...`（4.4MB）与 `_legacy/`（583KB） | 未跟踪，但**会被 grep / AST 扫描命中** | 分析代码时读到旧副本（本次考古就撞到） | 本地删除即可（`git clean` 无关，属工作区卫生） |
| 6.7 | `add_to_watchlist` 两处定义（`screening.py` / `watchlist.py`） | 不同路由、都可用 | 名字相同、语义不同（一个带可筛选校验） | 改名 `add_from_screening`，或至少加注释互指 |
| 6.9 | **63 份 `status: approved` 但 `last-reviewed` 早于 2026-09-17** 的文档 | 含 `contracts/path-isolation-contract.md`（898 行，08-02）、`decisions/02_TECH_CONSTRAINTS.md`、`runbooks/s0-evidence-preservation.md`（822 行）、`runbooks/ops-*.md` | AGENTS.md 说 `approved` = 可作建议依据 → 新人会把两个月前的结论当现状 | 批量改 `historical` 或补 `last-reviewed`（一次性脚本可做，但需逐份判断） |
| 6.8 | `dividend_yield` 废弃列 + `DEPRECATED_SCALE=100` 换算 shim | `reports/124` 实测两列恒差 100 倍 | 旧筛选草稿仍可能引用 | 保留 shim 直到 DSL 草稿清零（可查 SQLite 里 `screening_draft` 是否还有该字段） |

---

## 7. 可安全移除清单（按风险排序，可直接执行）

| 级别 | 项 | 动作 | 验证 |
|---|---|---|---|
| **L0 零风险** | `/design-preview` 路由 + 预览页（§5.2） | 删 2 处代码 | `npm run lint && npm run build` |
| **L0** | `masked_indicator_fields()` 别名（§6.2） | 删函数 + 改 9 处调用 | `ruff` + 定向 pytest |
| **L0** | `csmar_formulas.py`（§5.3） | 移入 `tests/support/` | `pytest test_csmar_field_verdict` |
| **L0** | S0 runbook 状态（§5.4） | front-matter 改 `historical` | 文档检查 |
| **L0** | 磁盘 `build/`、`_legacy/`（§6.6） | 本地删除（不入 git） | `git status` 不变 |
| **L1 低风险** | `_domestic_direct` 全套（§5.1） | 见 §5.1 四步 | 适配器测试 231 passed + 一次真实抓取 |
| **L1** | `app/web/static/index.html` 取消跟踪 | `git rm --cached`，由 `npm run build` 生成 | `start.bat` 首次会构建（已支持 `NEED_BUILD`） |
| **L1** | `scripts/legacy_dangerous/`（1 脚本 + README） | 移入 `docs/archive/` 或删除（git 历史已保留） | `verify_no_dangerous_maintenance` 仍通过 |
| **L2 中风险** | 两份 `default.yaml` 合并（§6.1） | 构建期复制 + 同步测试 | 打包版 `vd --version` + 配置读取 smoke |
| **L2** | `null_reason_sweeper.py`（1,279 行）搬出 `app/core` | 移到 `scripts/lib/` 或接入自动更新链 | `scripts/classify_null_reasons.py` 仍可跑 |
| **L2** | schema 迁移折叠（63 条 → baseline + 少量 ALTER） | 保留 v≥40 的 ALTER，其余并进 DDL | 旧库升级测试 + `SCHEMA_DDL_DRIFT`=0 |
| **L3 需实测** | `screening_readiness_cache`（§6.5） | 先测冷启动耗时再决定 | 见 §6.5 |

**总量估算**（按行数实测）：L0+L1 ≈ **−600 行**
（`_domestic_direct` 全套 −65：`_net.py` 45 行 + 20 处 `with`/缩进；预览页 −123；
`csmar_formulas` 迁出 app 计 −198；`legacy_dangerous` −248；别名 −8；`index.html` 取消跟踪 −1 文件），
L2 ≈ 再 −1,300 行（sweeper 迁出 1,279 + 迁移折叠）。
**注意**：`csmar_formulas` 与 `sweeper` 是"迁出 app/"，不是"删除"——总代码量不减少，减少的是**运行时阅读面**。
**收益不是行数**，而是"新人不会再问'这个函数为什么在这里/为什么有两份'"。

---

## 8. 不要删（每一条都对应一次已发生的事故）

| 项 | 看起来可删的理由 | 为什么不能删 |
|---|---|---|
| `path_policy` / S1 包装器 / 证据捕获 | "本地工具，路径校验太重" | 07-22 事故的唯一结构性防线 |
| `update_lock` | "单人用，不会并发" | 自动更新子进程 + CLI + Web 三方写库是常态 |
| `ErrorKind` 七分类 | "错误就是错误" | `reports/117` 熔断放大 |
| `merge_step_status` | "合并 dict 而已" | 14 天假绿 |
| 门禁阈值 `max(50,1%)` | "为什么不能全放行/全拦" | 26 只新股拖死全市场 vs 系统性单位错乱 |
| `field_units` 双份 | "前端不用管单位" | 100 倍误差 |
| `DerivedSnapshotDomain` | "又一个概念" | ROCE 六列被清空（且增量路径仍有坑） |
| `cold_archive` 视图 | "为什么不放回主库" | 19GB 主库 + 重建时间 |
| `cli/protocol` plan 机制 | "单人工具要什么审批" | `backup restore` / 冷库归档是**不可逆**操作 |
| `dividend_yield` 废弃 shim | "直接删列" | 旧筛选草稿会静默变成"全市场命中" |

---

## 9. 建议的执行顺序

1. **本周**（各 30 分钟）：L0 五项 → 一次性提交，`reports/134` 的度量脚本对比 `app_loc` 与 `duplicate_helper_names`。
2. **下周**：L1 `_domestic_direct` 删除（唯一一项需要真机抓取验证的）；顺便给 `hk_price` 加 `trust_env=False`。
3. **下一轮重构窗口**：L2 两份配置合并 + sweeper 搬迁（与 `reports/134` S4/S6 合并做，避免重复改同一批文件）。
4. **永久**：把 §8 的十条写进 `AGENTS.md` 的"不要删"清单（本次已在 `reports/133` §6 有相邻规则，可合并）。

---

## 10. 如何重跑本分析

```bash
# 双根历史 & 结构提交
git log --max-parents=0 --format='%h %ad %s' --date=short
git log --format='%h|%ad|%s' --date=short | while IFS='|' read h ad s; do \
  n=$(git show --name-only --format='' $h | grep -c .); [ "$n" -gt 20 ] && echo "$ad $h $n $s"; done

# 代理绕过是否有效（§5.1）
python -c "import urllib.request as u; print(u.getproxies())"

# 可达性 / 重复 / 文档新鲜度
python scripts/onboarding_metrics.py
```
