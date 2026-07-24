# Value Dashboard OpenCode Skill

## 概述

Value Dashboard 是一个 A股价值投资研究与筛选工具。通过此 skill，OpenCode 可以调用 Value Dashboard 的全部核心功能。

## 使用方式

通过 CLI 子进程调用，所有命令输出 JSON 格式（含 schema_version）。

```bash
# 调用方式
python -m app.cli.main <command> [args] [--options]
```

## 核心命令

### 发现能力

```bash
vd discover schema          # 获取 JSON schema (含错误码/原因码)
vd discover capabilities    # 获取能力清单 (命令树)
vd discover examples        # 获取使用示例
```

### 复合指标管理 (DSL)

```bash
vd indicator create <name> <expression> --desc "中文描述" --dir higher_is_better
vd indicator validate <name> <version>
vd indicator preview_single <name> <version> <stock_code>
vd indicator preview_sample <name> <version> --limit 10
vd indicator publish <name> <version>
vd indicator list
```

### 筛选

```bash
vd screening run --rule '{"conditions":{"logic":"AND","rules":[{"field":"pe_ttm","op":">","value":0}]}}'
vd screening save_result <title> <results_file>
vd screening list
```

### 数据管理

```bash
vd data init                # 最小可用初始化
vd data update              # 增量更新
vd data status              # 数据覆盖状态
vd data compute_indicators  # 计算指标快照
```

### 人工覆写

```bash
vd override list_conflicts
vd override submit <stock_code> <field_name> <value> --reason "校正说明"
vd override revoke <override_id>
```

### 备份与归档

```bash
vd backup create            # 创建备份
vd backup restore <path>    # 恢复备份 (危险操作, 两段式确认)
vd backup list              # 列出备份
vd archive create           # 创建冷归档
vd archive verify <dir>     # 验证归档
```

### 危险操作确认

```bash
vd plan confirm <plan_id>   # 确认危险操作 (15分钟有效)
```

## JSON 协议

所有命令输出遵循统一 JSON 协议：

```json
{
  "schema_version": "1.0",
  "command": "indicator.create",
  "result": {
    "status": "ok",
    "data": { ... },
    "error_code": null,
    "error_message": null,
    "reason_code": null
  }
}
```

### 错误码

| 代码 | 含义 |
|---|---|
| E001 | 参数无效 |
| E002 | 表达式不存在 |
| E003 | 校验失败 |
| E004 | 数据库错误 |
| E005 | 适配器不可用 |
| E101 | plan_id 不存在 |
| E102 | plan_id 已过期 |
| E103 | plan_id 已执行 |

### 原因码

| 代码 | 含义 |
|---|---|
| R001 | 除零 |
| R002 | 字段缺失 |
| R003 | 历史数据不足 |
| R004 | 维度不匹配 |
| R005 | 循环依赖 |

### 危险操作两段式确认

危险操作（backup.restore, archive.clean, data.refetch）需要两步：

1. 执行命令 → 返回 `plan_id` + 计划摘要
2. 在15分钟内执行 `vd plan confirm <plan_id>` → 确认执行

## 约束

- CLI 支持非交互运行 (PRD §16.2 CL5)
- 不允许通过 CLI 直接修改数据库文件或应用代码 (PRD §16.2 CL9)
- 不提供内建 AI 或模型 API (PRD §16.4 CL12)
- 不提供自由代码执行 (PRD §16.4 CL13)
