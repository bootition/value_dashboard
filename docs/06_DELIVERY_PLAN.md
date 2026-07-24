# 交付与测试计划（开发后阶段）

> 编写日期: 2026-07-17
> 前提: M0-M10 全部里程碑通过验收（424/440 = 96.4%），开发阶段完成
> 目标: 从"开发完成"到"可交付使用"的完整执行计划
> 参考: 互联网大厂标准研发流水线，针对个人本地工具做适当裁剪

---

## 1. 当前状态

### 已完成

- 11 个里程碑（M0-M10）全部通过验收
- 核心功能全部实现：数据采集/指标计算/筛选引擎/DSL/CLI/备份恢复/PDF管理
- PRD §20 四条验收流程测试脚本已编写
- 性能验收夹具已编写（20条件）
- PyInstaller 打包配置已编写

### 未完成（必须做）

| 事项 | 原因 |
|---|---|
| 真实数据初始化 | 当前数据库只有几只测试股票，无法验证真实场景 |
| 真实数据性能验收 | 43ms 的成绩基于 1 只股票，无说服力 |
| 全链路验收测试 | 从未在真实数据上跑完 PRD §20 四条流程 |
| PyInstaller 实际打包 | spec 文件写了但从未执行，产物是否存在未知 |
| 打包产物验证 | exe 能否启动、CLI 能否运行均未知 |

### 未完成（可选做）

7 个低优先级遗留问题，不影响核心功能，排在交付之后。

---

## 2. 执行计划

### 阶段总览

```
阶段1: 真实数据初始化（1-2小时）
  ↓
阶段2: 集成测试（2-3小时）
  ↓
阶段3: 性能测试（30分钟）
  ↓
阶段4: 验收测试 UAT（1小时）
  ↓
阶段5: 打包发布（1小时）
  ↓
阶段6: 冒烟测试（30分钟）
  ↓
阶段7: 缺陷修复（按需）
  ↓
交付完成
```

**预估总工时: 6-8小时（不含数据初始化等待时间）**

---

### 阶段1: 真实数据初始化

**目标**: 在本地数据库中建立 5000+ 只股票的完整数据，为后续所有测试提供基础。

**前置条件**:
- Python 3.11+ 环境已安装
- 所有依赖已安装（`pip install -e ".[data-sources,windows,dev]"`）
- DuckDB/SQLite schema 已初始化（`vd init`）

**执行步骤**:

```bash
# 1. 确认环境
python -c "import akshare, easy_tdx, baostock, duckdb, lark, cryptography; print('OK')"

# 2. 初始化 schema（如果尚未初始化）
python -m app.cli.main init

# 3. 执行最小可用初始化（PRD §6.7）
#    这会拉取：股票全集 → 交易日历 → 申万行业 → 近5年价格 → 三大报表
#    预计耗时：1-2小时（取决于网络和限流）
python -m app.cli.main data init

# 4. 计算指标快照（PRD §10）
#    将三大报表数据计算为 33+ 个内建指标，物化到 indicator_snapshot 表
python -m app.cli.main data compute_indicators

# 5. 检查数据覆盖状态
python -m app.cli.main data status
```

**验收标准**:

| 检查项 | 期望值 | 容差 |
|---|---|---|
| 股票总数 | 5000+ | ±100 |
| Raw 价格覆盖 | 4000+ 只 | 10% 失败可接受 |
| Qfq 价格覆盖 | 4000+ 只 | 10% 失败可接受 |
| 资产负债表覆盖 | 4000+ 只 | 10% 失败可接受 |
| 利润表覆盖 | 4000+ 只 | 10% 失败可接受 |
| 现金流量表覆盖 | 4000+ 只 | 10% 失败可接受 |
| 指标快照数 | 4000+ 条 | 与财务覆盖一致 |
| 待重试任务 | <500 条 | 失败的正常现象 |
| 缺失字段 | <1000 条 | 部分字段缺失正常 |

**注意事项**:
- 初始化过程中如果中断，可以重新运行 `data init`，已抓取的数据不会重复抓取
- 如果 AKShare 被限流，可以等待后重试或使用 `vd data update --check-only` 检查状态
- 申万行业分类需要手动从 swsresearch.com 下载 CSV 放到 `config/sw_industry_cache.csv`（参见 M1 实证结论）
- `listing_date` 字段需要从 AKShare `stock_individual_info_em` 获取，5000 只股票逐个调用较慢

**如果数据不完整怎么办**:
- 不完整的股票仍可参与筛选（指标为 NULL，不满足数值条件不会出现在结果中）
- 不完整的股票可通过 `vd data status` 查看重试列表
- 可通过 `vd data update` 增量补充

---

### 阶段2: 集成测试

**目标**: 验证各模块在真实数据上的端到端功能，发现模块间的集成问题。

**测试范围**: 逐模块验证核心功能在真实数据上的正确性。

#### 2.1 数据层集成测试

```bash
# 验证 DuckDB 数据完整性
python -c "
from app.core.config import Config; Config.load()
from app.core.storage.duckdb_store import DuckDBStore
duck = DuckDBStore()

# 股票元数据
rows = duck.read_query('SELECT COUNT(*) as cnt, COUNT(DISTINCT exchange) as exch FROM stock_meta')
print(f'stock_meta: {rows[0][\"cnt\"]} stocks, {rows[0][\"exch\"]} exchanges')

# 价格数据
rows = duck.read_query('SELECT COUNT(DISTINCT stock_code) as cnt, MIN(trade_date) as earliest, MAX(trade_date) as latest FROM price_daily_raw')
print(f'price_daily_raw: {rows[0][\"cnt\"]} stocks, {rows[0][\"earliest\"]} to {rows[0][\"latest\"]}')

# 财务数据
rows = duck.read_query('SELECT COUNT(DISTINCT stock_code) as cnt, MIN(report_date) as earliest, MAX(report_date) as latest FROM balance_sheet')
print(f'balance_sheet: {rows[0][\"cnt\"]} stocks, {rows[0][\"earliest\"]} to {rows[0][\"latest\"]}')

# 指标快照
rows = duck.read_query('SELECT COUNT(*) as cnt, COUNT(pe_ttm) as pe_cnt, COUNT(roe) as roe_cnt FROM indicator_snapshot')
print(f'indicator_snapshot: {rows[0][\"cnt\"]} total, {rows[0][\"pe_cnt\"]} with PE, {rows[0][\"roe_cnt\"]} with ROE')
"
```

**验收标准**:
- stock_meta 有 5000+ 只股票，exchange 包含 SSE/SZSE/BSE
- price_daily_raw 最早日期不晚于 2021-01-01（5年前）
- balance_sheet 最早日期不晚于 2020-12-31
- indicator_snapshot 中 PE/ROE 非空比例 > 70%

#### 2.2 指标计算正确性测试

```bash
# 选3只有代表性的股票验证指标合理性
python -c "
from app.core.config import Config; Config.load()
from app.core.indicators.calculator import IndicatorCalculator
calc = IndicatorCalculator()

for code in ['600519', '000001', '000858']:  # 茅台、平安银行、五粮液
    ind = calc.compute_all_for_stock(code)
    print(f'{code}: PE={ind.get(\"pe_ttm\")}, PB={ind.get(\"pb_mrq\")}, ROE={ind.get(\"roe\")}, Debt={ind.get(\"debt_ratio\")}')
"
```

**验收标准**:
- PE-TTM 在 5-100 之间（合理范围）
- ROE 在 -0.5 到 0.5 之间
- 资产负债率在 0-1 之间
- 无 NaN/Infinity 值

#### 2.3 筛选引擎集成测试

```bash
# 运行一次筛选，验证结果合理性
python -c "
from app.core.config import Config; Config.load()
from app.core.screening.engine import ScreeningEngine
engine = ScreeningEngine()

rule = {
    'conditions': {'logic': 'AND', 'rules': [
        {'field': 'pe_ttm', 'op': '>', 'value': 0},
        {'field': 'pe_ttm', 'op': '<', 'value': 50},
        {'field': 'roe', 'op': '>', 'value': 0.1},
        {'field': 'debt_ratio', 'op': '<', 'value': 0.6},
    ]},
    'sort': [{'field': 'pe_ttm', 'direction': 'asc'}],
    'columns': ['stock_code', 'name', 'pe_ttm', 'pb_mrq', 'roe', 'debt_ratio'],
}
result = engine.run(rule)
print(f'结果: {result[\"total\"]} 条, 基础池: {result[\"base_pool_size\"]}, 耗时: {result[\"execution_time_ms\"]}ms')
for r in result['results'][:5]:
    print(f'  {r[\"stock_code\"]} {r[\"name\"]}: PE={r.get(\"pe_ttm\")}, ROE={r.get(\"roe\")}')
"
```

**验收标准**:
- 基础池 > 4000 只
- 结果数 > 50 条（4个宽松条件应该有足够结果）
- 耗时 < 500ms
- 结果按 PE 升序排列

#### 2.4 DSL 引擎集成测试

```bash
# 创建→校验→预览→发布一个复合指标
python -c "
from app.core.config import Config; Config.load()
from app.core.dsl.engine import DSLEngine
engine = DSLEngine()

# 创建
r = engine.create('test_roa', 'income.net_profit / balance.total_assets', 'ROA测试', 'higher_is_better')
print(f'Create: {r}')

# 校验
r = engine.validate('test_roa', 1)
print(f'Validate: valid={r.get(\"valid\")}, expanded={r.get(\"expanded_expression\")}')

# 单股预览
r = engine.preview_single('test_roa', 1, '600519')
print(f'Preview 600519: value={r.get(\"value\")}')

# 发布
r = engine.publish('test_roa', 1)
print(f'Publish: {r.get(\"status\")}')
"
```

**验收标准**:
- 简写展开正确（`net_profit → income.net_profit@TTM`）
- 校验通过
- 预览值在合理范围
- 发布成功

#### 2.5 CLI 集成测试

```bash
# 运行 M7 CLI 测试
python tests/test_m7_cli.py

# 运行 M8 测试
python tests/test_m8.py
```

**验收标准**: 所有测试场景 PASS

#### 2.6 Web API 集成测试

```bash
# 启动服务器
python -m uvicorn app.web.main:create_app --factory --host 127.0.0.1 --port 8765 &

# 验证各 API 端点
curl http://127.0.0.1:8765/api/health
curl http://127.0.0.1:8765/api/db/status
curl http://127.0.0.1:8765/api/data-status/summary
curl http://127.0.0.1:8765/api/screening/indicators
curl http://127.0.0.1:8765/api/stock/600519/info
curl http://127.0.0.1:8765/api/stock/600519/kline?adjust=raw&days=30
curl http://127.0.0.1:8765/api/stock/600519/indicators
curl http://127.0.0.1:8765/api/stock/600519/financial-trend?period=annual&years=5
```

**验收标准**: 所有 API 返回有效 JSON，无 500 错误

#### 2.7 备份恢复集成测试

```bash
# 创建加密备份
python -m app.cli.main backup create --password "test123" --target data/backup_test

# 列出备份
python -m app.cli.main backup list

# 恢复（两段式确认）
python -m app.cli.main backup restore data/backup_test/<backup_file>.zip --password "test123"
# → 返回 plan_id
python -m app.cli.main plan confirm <plan_id>
# → 确认
python -m app.cli.main backup restore_execute data/backup_test/<backup_file>.zip --password "test123"
# → 执行恢复
```

**验收标准**:
- 备份创建成功，生成 ZIP + recovery_key.txt
- 恢复成功，数据完整

---

### 阶段3: 性能测试

**目标**: 在真实 5000+ 股数据上验证 PRD §19.1 的 5 秒筛选性能目标。

**执行步骤**:

```bash
# 先修复 M10-问题1：性能夹具添加复合指标和行业排名
# 1. 创建并发布一个复合指标
python -m app.cli.main indicator create perf_test_indicator "income.revenue / balance.total_assets" --desc "资产周转率"
python -m app.cli.main indicator validate perf_test_indicator 1
python -m app.cli.main indicator preview_single perf_test_indicator 1 600519
python -m app.cli.main indicator publish perf_test_indicator 1

# 2. 运行性能测试
python tests/test_m10_performance.py
```

**PRD §19.1 验收条件**:

| 条件 | 要求 | 验证方法 |
|---|---|---|
| PF1 主机规格 | 已记录 | config/host_spec.yaml |
| PF2 本地热数据 | 不含抓取耗时 | 直接查 DuckDB |
| PF3 夹具 | 5000股×20条件×含复合指标×行业排名 | 修改 test_m10_performance.py 添加 |
| PF4 预热+10次 | 先1次预热再10次 | 脚本已实现 |
| PF5 9/10在5秒内 | 10次中至少9次<5秒 | 脚本已实现 |

**验收标准**:
- 基础池 > 4000 只
- 10 次中至少 9 次 < 5000ms
- 平均耗时 < 1000ms（预期）

**如果不通过**:
- 检查 indicator_snapshot 是否已计算（`vd data compute_indicators`）
- 检查 DuckDB 是否有索引（目前无索引，5000行扫描应该足够快）
- 如果慢于 1 秒，检查 SQL 执行计划

---

### 阶段4: 验收测试 UAT

**目标**: 在真实数据上执行 PRD §20 四条验收流程，确认产品满足需求。

**执行步骤**:

```bash
# 运行四条验收流程
python tests/test_m10_acceptance.py
```

**PRD §20 验收流程对照**:

#### §20.1 验收流程一：当前筛选

| 步骤 | 操作 | 预期结果 |
|---|---|---|
| 1 | 打开浏览器 http://127.0.0.1:8765 | 进入筛选页 |
| 2 | 查看基础股票池开关 | ST/停牌/上市年限开关可见 |
| 3 | 创建复合指标（DSL） | 草稿→校验→预览→发布 |
| 4 | 添加 10-20 个筛选条件 | 条件编辑器正常 |
| 5 | 点击"运行筛选" | 5秒内返回结果 |
| 6 | 查看结果表格 | 可配置列、排序、入选解释 |
| 7 | 保存结果 | 标题必填，保存成功 |
| 8 | 导出 CSV | 含数据日期/置信度/溯源信息 |
| 9 | 加入自选 | 成功加入自选列表 |

#### §20.2 验收流程二：单股研究

| 步骤 | 操作 | 预期结果 |
|---|---|---|
| 1 | 从筛选结果点击股票 | 进入个股详情页 |
| 2 | 查看头部信息 | 代码/名称/拼音/收盘价/价格日期 |
| 3 | 查看 K 线图 | 日K/成交量/均线/raw与qfq切换 |
| 4 | 查看指标摘要 | 估值/盈利/成长/安全/股东回报 5个Tab |
| 5 | 查看财务趋势 | 年度默认，可切季度/TTM |
| 6 | 切换年限 | 1/3/5/10年/全部 |
| 7 | 查看 current_only 标注 | "仅当前"标签可见 |
| 8 | 查看溯源信息 | 字段级+批次级溯源 |
| 9 | 打开 PDF | 如有已下载PDF可打开 |

#### §20.3 验收流程三：CLI 与 OpenCode

| 步骤 | 操作 | 预期结果 |
|---|---|---|
| 1 | `vd discover schema` | 返回 JSON schema（含 schema_version） |
| 2 | `vd discover capabilities` | 返回命令树 |
| 3 | `vd indicator create` + `validate` + `publish` | 完整生命周期 |
| 4 | `vd screening run` | 返回筛选结果（含 schema_version） |
| 5 | `vd backup restore` | 返回 plan_id（不立即执行） |
| 6 | `vd plan confirm <plan_id>` | 15分钟内确认成功 |
| 7 | 超时确认 | 被拒绝（E102） |

#### §20.4 验收流程四：初始化/更新/修复/归档/备份/恢复

| 步骤 | 操作 | 预期结果 |
|---|---|---|
| 1 | `vd data status` | 显示数据覆盖状态 |
| 2 | `vd data update --check-only` | 增量检查报告 |
| 3 | `vd data update` | 增量更新（如有新交易日） |
| 4 | `vd backup create --password xxx` | 创建加密备份+恢复密钥 |
| 5 | `vd archive create` | 创建冷归档 |
| 6 | `vd archive verify` | 验证归档完整性 |
| 7 | `vd backup restore` + `plan confirm` + `restore_execute` | 两段式确认恢复 |
| 8 | 恢复后打开 PDF | 浏览器可打开恢复的 PDF |

**验收标准**: 四条流程全部 PASS

---

### 阶段5: 打包发布

**目标**: 使用 PyInstaller 打包为可独立运行的 Windows 应用。

**前置条件**:
- 阶段1-4 全部通过
- 前端已构建（`cd frontend && npm run build`，产物在 `frontend/dist/`）
- 前端静态资源已复制到 `app/web/static/`

**执行步骤**:

```bash
# 1. 构建前端
cd frontend
npm run build
cd ..

# 2. 复制前端产物到 FastAPI 静态目录
#    确认 app/web/static/index.html 和 assets/ 存在

# 3. 执行 PyInstaller 打包
pyinstaller value-dashboard.spec

# 4. 验证打包产物
#    产物在 dist/value-dashboard/ 目录
dir dist\value-dashboard\

# 5. 测试启动
dist\value-dashboard\value-dashboard.exe

# 6. 测试 CLI 模式
dist\value-dashboard\value-dashboard.exe discover capabilities
```

**验收标准**:

| 检查项 | 期望 |
|---|---|
| dist/value-dashboard/ 目录存在 | ✅ |
| value-dashboard.exe 存在 | ✅ |
| exe 启动后 FastAPI 服务运行 | ✅ |
| 浏览器自动打开 http://127.0.0.1:8765 | ✅ |
| 前端页面正常显示 | ✅ |
| CLI 模式可用（exe discover capabilities） | ✅ |
| 打包体积 | < 500MB（可接受） |

**如果打包失败**:

| 问题 | 解决方案 |
|---|---|
| hiddenimports 遗漏 | 查看报错，添加缺失模块到 spec 文件 |
| datas 路径错误 | 检查 frontend/dist 是否复制到 app/web/static |
| duckdb native library 缺失 | 确保 duckdb wheel 包含 native binary |
| UPX 压缩问题 | 尝试 `upx=False` |

**打包后修改 start.bat**:

```bat
@echo off
cd /d "%~dp0"
value-dashboard.exe
pause
```

---

### 阶段6: 冒烟测试

**目标**: 在打包产物上执行最基本的流程，确认"双击可用"。

**执行步骤**:

1. 双击 `start.bat`（或 `value-dashboard.exe`）
2. 浏览器自动打开
3. 在筛选页运行一次筛选
4. 点击一只股票进入详情页
5. 查看 K 线和指标
6. 切换到数据状态页查看覆盖
7. 关闭服务

**验收标准**: 全流程无报错，页面正常显示

---

### 阶段7: 缺陷修复

**目标**: 修复在阶段1-6中发现的问题。

**缺陷分级**:

| 级别 | 定义 | 处理时限 |
|---|---|---|
| P0 阻断 | 核心功能无法使用（如筛选引擎崩溃、数据初始化失败） | 立即修复 |
| P1 严重 | 功能异常但有 workaround（如某些股票指标为空） | 交付前修复 |
| P2 一般 | 体验问题（如页面加载慢、排序不正确） | 下一版本 |
| P3 轻微 | 不影响功能（如文案错误、样式问题） | 按需修复 |

**已知需要在阶段7中修复的问题**:

| 问题 | 优先级 | 说明 |
|---|---|---|
| M10-问题1 | P1 | 性能夹具添加复合指标和行业排名 |
| M10-问题2 | P1 | start.bat 使用打包后的 exe |
| M10-问题3 | P0 | PyInstaller 实际打包验证 |
| M10-问题4 | P3 | CLI 输出统一 make_response |
| M4-问题5 | P2 | 溯源信息不完整 |
| M6-问题1 | P2 | 自选列表自定义列 |
| M8-问题3 | P3 | 校正模板状态列 |
| M9-问题1 | P2 | 增量备份（PRD可选） |

---

## 3. 执行顺序与依赖关系

```
阶段1: 真实数据初始化
  │
  ├─→ 阶段2: 集成测试（依赖真实数据）
  │     │
  │     ├─→ 阶段3: 性能测试（依赖集成测试通过）
  │     │
  │     └─→ 阶段4: 验收测试 UAT（依赖集成测试通过）
  │           │
  │           └─→ 阶段5: 打包发布（依赖验收通过）
  │                 │
  │                 └─→ 阶段6: 冒烟测试（依赖打包完成）
  │                       │
  │                       └─→ 阶段7: 缺陷修复（按测试结果）
  │
  └─→ (如果数据初始化失败 → 修复适配器 → 重新初始化)
```

**关键路径**: 阶段1 → 阶段2 → 阶段4 → 阶段5 → 阶段6

---

## 4. 回滚方案

如果打包发布后发现严重问题：

1. **数据回滚**: `vd backup restore <backup.zip> --password xxx` → `plan confirm` → `restore_execute`
2. **代码回滚**: 回到 git 中最后一个通过验收的版本
3. **前端回滚**: 重新构建前端 `cd frontend && npm run build`

---

## 5. 交付检查清单

完成以下全部检查后，视为交付完成：

- [ ] 阶段1: 真实数据初始化完成（5000+股票，指标快照已计算）
- [ ] 阶段2.1: 数据层集成测试通过
- [ ] 阶段2.2: 指标计算正确性测试通过
- [ ] 阶段2.3: 筛选引擎集成测试通过
- [ ] 阶段2.4: DSL 引擎集成测试通过
- [ ] 阶段2.5: CLI 集成测试通过
- [ ] 阶段2.6: Web API 集成测试通过
- [ ] 阶段2.7: 备份恢复集成测试通过
- [ ] 阶段3: 性能测试通过（10次中9次<5秒）
- [ ] 阶段4: PRD §20 四条验收流程全部 PASS
- [ ] 阶段5: PyInstaller 打包成功
- [ ] 阶段5: 打包产物可启动
- [ ] 阶段5: start.bat 使用 exe
- [ ] 阶段6: 冒烟测试通过（双击可用）
- [ ] 阶段7: P0/P1 缺陷全部修复

---

## 6. 后续迭代方向（交付后）

交付完成后，以下问题可在后续版本中逐步修复：

| 优先级 | 问题 | 说明 |
|---|---|---|
| P2 | M4-问题5 | 溯源信息补全（生效日期/数据版本/公式） |
| P2 | M6-问题1 | 自选列表自定义列 |
| P2 | M9-问题1 | 增量备份（PRD"可带"是可选的） |
| P2 | M9-问题3 | 恢复后 PDF 打开端到端验证 |
| P3 | M5-问题5 | CAGR n 参数解析测试补全 |
| P3 | M7-问题5 | CLI 输出统一使用 make_response |
| P3 | M8-问题3 | 校正模板状态列 |

**功能增强方向**（不在 V1 范围内，供参考）：
- 多维度筛选条件组合保存为模板
- 指标对比功能（多股票横向对比）
- 财务预警（自动检测异常指标）
- 数据更新调度（定时自动更新）
- 导出为 Excel 格式
