---
title: 备份与恢复运行手册
status: approved
category: runbooks
created: 2026-08-03
last-reviewed: 2026-08-03
---

# 备份与恢复运行手册

> O2（`reports/41` B1）：补充 `docs/runbooks/` 中缺失的日常运维手册。
> 对应 PRD §18.1/§18.2/§18.3：备份（AR9-10，可选口令加密）、恢复（AR5，仅 CLI、
> 含 plan confirm 二次确认）、Windows Credential Manager 凭据（AR12）。

## 1. 何时备份

- 重要研究结果/自选/规则产生后；
- 执行任何会改写数据库的操作前（数据重建、quarantine、restore 等）；
- 周期性（如每周）作基线备份。

## 2. 创建备份

```bash
# 不带加密（简单明文 ZIP，本地单用户场景）
vd backup create

# 带口令加密（推荐；备份含规则/自选/结果等个性化数据时使用）
vd backup create --prompt-password

# 指定备份目录（默认 data/backup）
vd backup create --target D:\va-dash-backup

# 查看已用备份/凭据
vd backup list
```

备份为 ZIP，内含正式库快照；加密口令用于生成「恢复密钥」（recovery key）。

## 3. 恢复

> **风险：恢复会覆盖当前正式数据库**（股票列表、自选、保存结果、草稿全部被替换）。
> 恢复前务必先建一份新备份。

```bash
# 1) 规划恢复，生成确认 plan（不改变任何记录）
vd backup restore <backup.zip> [--password] [--recovery-key]

# 2) 确认后真正执行（plan confirm 之后才动手）
vd backup restore_execute
```

- 忘了口令：使用 backup create 时给出的 `--recovery-key`（见 `vd backup create` 输出提示）。
- 只读检查：恢复前可用 `vd data:status` 与 `vd status` 核对当前状态，确认后再覆盖。

## 4. 常见问题

| 问题 | 处理 |
|---|---|
| 提示凭据不存在 | 未 `store_credential`；用 `--password`/`--recovery-key` 显式提供 |
| 忘记口令 | 需 recovery key，否则该备份无法解密（设计如此，防泄露） |
| 恢复后悔 | 无内置撤销；恢复前备份旧库可回滚 |

## 5. 参考

- `docs/reports/29`（重建后数据的备份基线与 30 股抽样）
- 正式库写纪律：单写者串行，数据写操作必须经 CLI/维护脚本。