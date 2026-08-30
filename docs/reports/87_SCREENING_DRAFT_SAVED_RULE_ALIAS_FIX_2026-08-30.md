---
title: 筛选草稿污染已保存规则引用修复报告
status: approved
category: reports
created: 2026-08-30
last-reviewed: 2026-08-30
---

# 筛选草稿污染已保存规则引用修复报告（2026-08-30）

## 裁决

**修复完成。** 用户选中已保存规则后修改条件值，界面显示新值，但实际仍按旧规则版本运行的根因已定位并修复：`applyLoadedRule` 用浅拷贝把已保存规则中的 `conditions.rules` 数组共享给了编辑区，编辑区一改值，内存中的“已保存版本”也被同步修改，`isDraftDirty()` 永远判断“没有修改”。

## 根因

`frontend/src/views/ScreeningPage.vue` 原实现：

```ts
if (rule.rule_json.conditions) {
  Object.assign(ruleTree, rule.rule_json.conditions)
}
sortRules.value = [...rule.rule_json.sort]
```

`Object.assign` 复制了 `conditions.rules` 的**数组引用**。编辑器中 `RuleConditionRow` 直接原地修改条件对象，导致：

- 屏幕上：总市值 300 亿 → 100 亿；
- `savedRules` 中 rule 42 v1 的条件也变成 100 亿；
- `isDraftDirty()` 比较草稿与“已保存版本”发现完全相同 → false；
- 运行按钮继续提交 `rule_id=42, rule_version=1`，所以结果仍是 162（30 亿口径）。

这与数据库记录一致：草稿已是 10,000,000,000，但最近运行仍为 rule 42 v1。

## 修复

- `frontend/src/types/screening.ts`
  - 新增 `cloneScreeningRule` 深拷贝工具。
- `frontend/src/views/ScreeningPage.vue`
  - `applyLoadedRule` 对 conditions 和 sort 均深拷贝后再载入编辑区，编辑器修改不再污染 `savedRules`。
- `frontend/tests/component/screening-rule-clone.test.ts`
  - 新增深拷贝回归测试：修改克隆条件/排序不会污染原始规则。

## 验证

- 真实 Chrome 复现并验证：
  - 修复前：选中规则 42 后把总市值改为 100 亿，点击运行只发出 `rule_id=42, rule_version=1`；
  - 修复后：同一操作先发出 `/api/screening/rules/save`（`total_market_cap=10000000000`，新版本 v2），再以 v2 运行，结果从 162 变为 323。
- 前端门禁：lint 通过；Vitest 12 个文件 58 tests 全部通过；`npm run build` 成功。
- 后端定向回归（CSRC/数据质量相关）53 passed 不受影响。

## 用户操作

刷新浏览器（`Ctrl + Shift + R`）后重新选择规则、修改条件再运行即可。结果区会显示“执行规则 v2”。
