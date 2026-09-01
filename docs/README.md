---
title: 文档地图（Documentation Map）
status: approved
category: decisions
created: 2026-07-31
last-reviewed: 2026-09-01
---

# 文档地图（Documentation Map）

本目录按 **docs-as-code 规范**（Diátaxis 四象限 + ADR + ISO/IEC 26511 生命周期）组织。
**阅读顺序：先读 `STATUS.md`，再按需进入各分类。** 历史文档一律不构成当前结论。

## 目录结构

| 路径 | 类型 | 内容 | 是否当前有效 |
|---|---|---|---|
| `STATUS.md` | 状态 | **当前状态唯一权威**（Verdict、剩余缺口、进行中工作） | ✅ 是 |
| `decisions/` | 决策 | PRD、技术约束、规划类（长期有效的合同与决策） | 01/02 ✅；03/04 historical |
| `reports/` | 报告 | 审计/验收/审查等事件型快照 | 当前结论以 `STATUS.md` 所列报告为准；其余仅为发现/修复事实 |
| `runbooks/` | 手册 | 运维/证据保全操作手册 | ✅ 是（首次使用指南 + O2 运维四册） |
| `contracts/` | 合同 | 正式签署的设计合同 | ✅ 是 |
| `evidence/` | 证据 | 机器生成的证据 JSON（evidence-*.json、evidence-s0/s1 事故证据） | 证据，非结论 |
| `archive/` | 归档 | 已废弃/完成的研究快照、执行计划（含 superpowers/） | ❌ 否（只读） |

## 文档生命周期（Lifecycle）

```
draft → approved → superseded → archived
```

| 状态 | 含义 | AI/读者处理方式 |
|---|---|---|
| `approved` | 当前有效结论 | 可作建议依据 |
| `superseded` | 已被更新文档取代（见 front-matter `superseded-by`） | 仅作追溯证据，**不得**作为当前结论 |
| `historical` | 已完成使命的历史决策（不再演进） | 同上 |
| `archived` | 已归档 | 不读（除非追溯历史） |

每个文档头部 YAML front-matter 示例：

```yaml
---
title: 数据重建最终报告
status: superseded        # approved | superseded | historical | archived
category: reports         # decisions | reports | runbooks | contracts | archive
created: 2026-07-31       # 可选
last-reviewed: 2026-07-31 # 必填：最后核验日期
supersedes: ...           # 可选：取代了谁
superseded-by: ...        # 可选：被谁取代（相对 docs/ 的路径）
---
```

## 当前有效的文档（Current Truth）

> **防漂移约定（2026-09-01）**：本文件不再逐条复制报告清单。报告级 Current Truth 的唯一入口是 `STATUS.md` 的「当前有效文档（Current Truth）」表；任何报告能否作为当前结论，以其 front-matter `status: approved` 且登记于该表为准。`superseded` 文档禁止作为当前结论引用。

| 类别 | 当前入口 |
|---|---|
| 状态唯一权威 | `STATUS.md` — Verdict / 剩余缺口 / 进行中工作 / 当前有效文档表 |
| 产品需求 | `decisions/01_PRODUCT_REQUIREMENTS_V1.md`（活文档） |
| 技术约束 | `decisions/02_TECH_CONSTRAINTS.md` |
| 报告（事件型） | `reports/` — **以 STATUS 表为准**；当前已登记至 102（2026-09-01 数据库治理执行状态），82–102 为 2026-08-25 以后的当前进展 |
| 运行手册 | `runbooks/` — user-first-use、s0-evidence-preservation、ops-backup-restore、ops-auto-update-retry、ops-data-rebuild、ops-build-release-s1、ops-duckdb-rebuild、ops-knowledge-base |
| 合同 | `contracts/path-isolation-contract.md` |

## 新文档流程（Checklist）

1. 判断类型：合同/PRD → `decisions/`；事件报告 → `reports/`；操作手册 → `runbooks/`；证据 → `evidence/`；其余 → 会话产物放 `.planning/`
2. 加 front-matter（status/category/last-reviewed，必要时 supersedes/superseded-by）
3. 若产生或取代结论 → 更新 `STATUS.md`
4. 若旧文档被取代 → 改其 `status: superseded` + `superseded-by`（永不删除）

## 编号对照（迁移说明）

2026-07-31 之前的历史文档原位于 `docs/` 根目录，编号 01–29：
01–04 → `decisions/`；05–29 → `reports/`；`evidence-*.json`、`evidence-s0/`、`evidence-s1/` → `evidence/`；`superpowers/` → `archive/superpowers/`。历史文档内部对旧路径的引用（如 `docs/11_RED_TEAM_AUDIT_V2.md`）为历史记录，未逐一改写，按 `docs/` 下新位置解释即可。
