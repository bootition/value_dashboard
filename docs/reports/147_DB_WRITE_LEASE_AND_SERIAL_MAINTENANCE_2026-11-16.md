---
title: 跨会话正式库写租约 + 串行维护执行器 + 一次数据丢失事故复盘（reports/147）
status: approved
category: reports
created: 2026-11-16
last-reviewed: 2026-11-16
---

# 跨会话写租约、串行维护，以及我造成的一次数据丢失

> 用户指令：「需要串行跑的，你看一下怎么解决嘛。现在是多个对话同时推进，但是**没有什么主控的对话**。
> 你可以去排查一下，有没有对话在动数据库；然后跨对话交流，比如预约一下时间什么的。
> 你查一查，联网搜索一下人家都是怎么做的。」

---

## 1. 现场排查：当时到底有没有人在写库

| 检查 | 结果 |
|---|---|
| `git worktree list` | 4 个工作树：主检出 + `finish-refactor-backlog` + `fix-quarterly-coverage-fallback` + `fix-data-defects-ebitda`（我） |
| 分支提交时间 | `finish-refactor-backlog` 15:39 刚提交（**另一个会话活着**） |
| Python 进程 | 2 个，都是 `finish-refactor-backlog` 的**全量 pytest**（`--basetemp` 指向临时目录） |
| 8765 端口 | 空闲（没有 Web 服务） |
| 正式库锁文件 / WAL | 不存在 |
| 正式库 `mtime` | **88 分钟前**（14:15），静默 |

**结论**：另一个会话在跑测试，但**用的是临时库**（`VD_ENV=test` + 临时路径），
**没有人在写正式库**。可以做串行维护。

## 2. 业界做法（联网查证）

| 做法 | 出处 | 我采纳的部分 |
|---|---|---|
| **`O_EXCL` 原子创建**是唯一的抢锁手段；写临时文件再 rename 不是原子抢占 | [py-filelock SoftFileLock](https://py-filelock.readthedocs.io/en/latest/_modules/filelock/_soft.html#SoftFileLock.break_lock) | `os.open(path, O_CREAT\|O_EXCL)` |
| 锁文件里记 **PID + hostname**；同机 PID 不通即可回收。**年龄判定不能单独依赖**（可能与受保护操作重叠） | 同上；[locks.py](https://raw.githubusercontent.com/reda-baqechame/self-learning-24.7-agent/e3813c1eaf3ba3354255e48fe75d0b3ddedaf74f/locks.py#L1) | PID 优先、年龄兜底（宽限 2×TTL） |
| **TTL + 续租**，renew 间隔 ≤ TTL/3，避免抖动误判 | [zeph-memory entity_lock](https://docs.rs/zeph-memory/latest/zeph_memory/graph/entity_lock/index.html)、[InferHub](https://github.com/dev-art-solutions/inferhub) | 默认 TTL 1800s，长任务每 TTL/3 续租 |
| 过期锁**在下一次尝试时回收**，不靠定时清扫 | zeph-memory | `acquire` 时判定 + 回收并留审计记录 |
| 并发要按**资源与依赖**建模，不要因为"调用是并行的"就当它们独立 | [parallel tool call 可靠性](https://ask.atlascloud.ai/how-parallel-tool-calls-change-coding-agent-reliability) | 三步维护**固定串行**，顺序不可乱 |

## 3. 落地：三个脚本

### 3.1 `scripts/session_lease.py`（租约 + 排队）

```bash
python scripts/session_lease.py status                    # 谁在写库 + 队列
python scripts/session_lease.py acquire --owner <会话> --purpose "<要做什么>"
python scripts/session_lease.py renew   --owner <会话>
python scripts/session_lease.py release --owner <会话>
python scripts/session_lease.py enqueue --owner <会话> --purpose "<预约>"   # 抢不到就排队，不忙等
```

租约文件：`<data>/.locks/db-write.lease.json`（在 `data/` 下 —— 机器级共享状态且已 gitignore）。

**状态判定**（`_lease_state`）：

| 状态 | 判据 | 可否抢占 |
|---|---|---|
| `active` | 同机 PID 存活 且 未过期 | ❌ |
| `dead_owner` | 同机 PID 已退出 | ✅（回收并打印审计） |
| `expired` | 过期超过 `TTL × (2−1)` 宽限 | ✅ |
| `corrupt` | JSON 损坏 / `expires_at` 不可解析 | ❌（要求人工确认后删） |

### 3.2 `scripts/run_db_maintenance.py`（串行执行器）

抢租约 → 逐步执行 → **每步前续租** → `finally` 释放。三步顺序固定：

1. `nonstandard_dates`（清理 01-01 错记）
2. `employee_periods`（员工数报告期改回年度期末）
3. `declare_date_backfill`（回填公告日）

抢不到租约**直接退出**并提示 `enqueue`，不做忙等。

### 3.3 `scripts/session_preflight.py` 接入租约检查

开工自检新增第 5 项：**别的会话持有写租约 → BLOCK**，并指向 `enqueue`。
（写正式库是互斥操作，不是"注意一下"就行的。）

## 4. 打通最后一公里：`path_policy` 允许工作树共用正式库

原本 `DatabasePathSet.validate()` 要求 `run_root` 必须等于**当前检出**的 `data/`。
而 `data/` 是 gitignore 的，链接工作树里根本没有 —— 导致
**worktree 会话永远无法执行任何维护脚本**，而"多会话并行、没有主控"正是常态。

改法：额外接受 `shared_data_roots()` —— 用
`git rev-parse --path-format=absolute --git-common-dir` 拿到**主检出**的 `.git`，
其父目录的 `data/` 即共享根。

安全性由四层保证：`duckdb`/`sqlite` 文件名必须完全匹配、`VD_FORMAL_ACK=confirmed`、
**必须持有写租约**（preflight 检查）、写库走 `exclusive_update()` 单写者锁。

> 踩坑：`subprocess.run(text=True)` 在 Windows 按 cp936 解码，中文路径变成
> 「鎺屾帶缁忔祹」→ 路径比对永远不相等。必须显式 `encoding="utf-8"`。

## 5. 维护执行结果（正式库实测）

| 表 | 清理前非标准期 | 清理后 | 总行数变化 |
|---|---|---|---|
| `cash_flow_indirect` | 62,369 | **0** | 244,755 → 186,733 |
| `cash_flow_activity` | 62,370 | **0** | 367,041 → 308,954 |
| `financial_sector_items` | 1,014 | **0** | 5,674 → 4,730 |
| `report_metadata` | 71,860 | **0** | 356,728 → 289,678 |
| （冷归档视图 `csmar_*_items`） | 251,475 | **不改** | 只读 parquet 视图，原始证据 |

其它两项：

| 项 | 前 | 后 |
|---|---|---|
| `company_employee_history` 2025-12-31 期 | **缺失** | **5,565 行** |
| `report_metadata.declare_date` 覆盖 | 10.15% | **96.94%** |

### 5.1 踩到的两个技术坑

1. **DuckDB 不支持自引用 DELETE**：`DELETE ... WHERE EXISTS (SELECT ... FROM 同一张表)`
   报 `Can only delete from base table`。改为注册待删主键表 + `DELETE ... USING`。
2. **`csmar_*_items` 是视图**：`read_parquet('D:/vd-cold-archive-*/…')` 的只读视图，
   既不能删也不该删（那是上游原始证据）。脚本加了 `_is_base_table()` 防护。

## 6. ⚠ 事故：我的补采删掉了 2025 年报和 2026 半年报

### 6.1 经过

补 `2025-06-30` 折旧摊销断档时，我调：

```python
refresh(CASHFLOW_SUPPLEMENT, periods=["2025-06-30"], force_codes=全部 5,580 只)
```

`delete_scope='all'` 的删除条件**只按 `stock_code` 匹配**（注释里写着"清掉窗口外的
旧本源行"），于是把同一批股票在 `2025-09-30 / 2025-12-31 / 2026-03-31 / 2026-06-30 /
2026-09-30` 的同源行**全部删掉**，只插回了 2025-06-30 那一期。

**损失**：`cash_flow_indirect` 2025-12-31 从 5,579 行 → **4 行**；2026-06-30 从
5,579 → **4 行**。`cash_flow_activity` 同样。`indicator_ext` 未重算，**下游派生域没有扩散**。

### 6.2 恢复

立即用 6 期一起全量重采（`periods=[2026-09-30 … 2025-06-30]`, `force_codes` 全部）：

```
targeted 5,580 / updated 5,580 / failed 0 / 耗时 174s
```

恢复后（比事故前更好）：

| 报告期 | 事故前 | 恢复后 |
|---|---|---|
| 2025-06-30 | **5** | **5,585（有折旧 5,571）** ← 断档已补齐 |
| 2025-12-31 | 5,579 | 5,584（有折旧 5,576） |
| 2026-06-30 | 5,579 | 5,584（有折旧 5,558） |

### 6.3 根因修复

`delete_scope='all'` 的删除范围加一条：

```sql
AND t.report_date IN (SELECT DISTINCT report_date FROM _supp_rows)
```

即「窗口外旧行清理」限制在**本轮真正请求的期次**内。

### 6.4 测试

| 文件 | 用例 | 钉死的契约 |
|---|---|---|
| `test_cashflow_row_replacement.py` | 新增 `test_partial_period_backfill_keeps_other_periods` | 只补一期时，其他期次的同源行必须保留 |
| `test_supplement_field_maps.py` | 重写 `test_refresh_keeps_out_of_window_source_rows` | 旧用例把「窗口外旧行一并消失」当**可接受代价**写进了断言 —— 正是这次事故的思想根源，按新契约改写 |

**门禁**：相关 8 个文件 **129 passed**、`ruff` 全绿。

## 7. 留给其他会话的协议（已写入 AGENTS 与知识库）

1. 开工先跑 `python scripts/session_preflight.py` —— 它会告诉你**有没有人在写库**；
2. 要写正式库：`session_lease.py acquire` → 干活（长任务 `renew`）→ `release`；
3. 抢不到就 `enqueue` 登记，**不要忙等、不要强拆**；
4. 只补部分期次时，注意「窗口外旧行」的删除语义（本次已修，但别的域可能还有类似口径）；
5. **补采前后都要核对行数**：`SELECT report_date, count(*) … GROUP BY 1 ORDER BY 1 DESC`。
