# 文档地图（Documentation Map）

本目录按 **docs-as-code 规范**（Diátaxis 四象限 + ADR + ISO/IEC 26511 生命周期）组织。
**阅读顺序：先读 `STATUS.md`，再按需进入各分类。** 历史文档一律不构成当前结论。

## 目录结构

| 路径 | 类型 | 内容 | 是否当前有效 |
|---|---|---|---|
| `STATUS.md` | 状态 | **当前状态唯一权威**（Verdict、剩余缺口、进行中工作） | ✅ 是 |
| `decisions/` | 决策 | PRD、技术约束、规划类（长期有效的合同与决策） | 01/02 ✅；03/04 historical |
| `reports/` | 报告 | 审计/验收/审查等事件型快照（05–34） | 仅 29/34 ✅；30–33 修复事实有效但整体结论已被 34 取代；其余 superseded |
| `runbooks/` | 手册 | 运维/证据保全操作手册 | ✅ 是 |
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

- `STATUS.md` — 当前状态唯一权威
- `decisions/01_PRODUCT_REQUIREMENTS_V1.md` — 产品需求规格（验收合同，活文档）
- `decisions/02_TECH_CONSTRAINTS.md` — 技术约束
- `reports/29_DATA_REBUILD_REPORT_2026-07-31.md` — 最新数据重建结论
- `reports/34_SYSTEM_RED_TEAM_REVIEW_2026-08-02.md` — **当前发布裁决**：第五轮全项目系统性红队独立复核（BLOCK + 4 项 P1 + 退出条件）
- `reports/30`–`reports/33` — 前四轮修复/验收事实（整体"全部关闭"结论已被 34 取代，仅作追溯）
- `runbooks/s0-evidence-preservation.md` — 证据保全手册
- `contracts/path-isolation-contract.md` — 路径隔离合同

## 新文档流程（Checklist）

1. 判断类型：合同/PRD → `decisions/`；事件报告 → `reports/`；操作手册 → `runbooks/`；证据 → `evidence/`；其余 → 会话产物放 `.planning/`
2. 加 front-matter（status/category/last-reviewed，必要时 supersedes/superseded-by）
3. 若产生或取代结论 → 更新 `STATUS.md`
4. 若旧文档被取代 → 改其 `status: superseded` + `superseded-by`（永不删除）

## 编号对照（迁移说明）

2026-07-31 之前的历史文档原位于 `docs/` 根目录，编号 01–29：
01–04 → `decisions/`；05–29 → `reports/`；`evidence-*.json`、`evidence-s0/`、`evidence-s1/` → `evidence/`；`superpowers/` → `archive/superpowers/`。历史文档内部对旧路径的引用（如 `docs/11_RED_TEAM_AUDIT_V2.md`）为历史记录，未逐一改写，按 `docs/` 下新位置解释即可。
