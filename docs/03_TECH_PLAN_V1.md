# 技术规划文档 V1

> 基于 PRODUCT_REQUIREMENTS_V1.md (已冻结PRD) + 可行性研究 (findings.md, phase4_feasibility.md, phase5_coverage_matrix.md) + 技术约束清单 (tech_constraints.md) + Oracle 架构推荐。
> 日期: 2026-07-17

---

## 第1章 技术栈选型

### 1.1 后端语言与框架

| 决策项 | 选择 | 理由 |
|---|---|---|
| 语言 | **Python 3.11+** | 4个数据源适配器(AKShare/easy_tdx/BaoStock/CNINFO)均为Python库，后端必须用Python |
| Web框架 | **FastAPI** | 异步支持(并发数据抓取)、自动OpenAPI文档、性能优于Flask、Pydantic v2原生集成 |
| 数据验证 | **Pydantic v2** | JSON协议校验、适配器请求/响应模型、CLI协议类型安全 |
| HTTP客户端 | **httpx** | 异步HTTP，用于CNINFO/AKShare适配器 |
| CLI框架 | **Typer** (基于Click) | 类型安全的CLI参数解析，支持非交互运行(PRD CL5) |
| DSL解析器 | **lark** | Earley解析器，支持自定义DSL语法，可生成AST |
| 加密 | **cryptography** | AES-256-GCM加密个性化数据备份(PRD AR9) |
| PDF处理 | 不内建生成 | PRD不要求内建PDF生成；仅下载和打开CNINFO原始PDF |

### 1.2 数据库

| 决策项 | 选择 | 理由 |
|---|---|---|
| 分析数据库 | **DuckDB** (嵌入式) | 列式存储，适合筛选扫描；窗口函数支持排名/百分位；嵌入式无需安装服务器；可读写Parquet冷归档；处理1875万行价格数据轻松 |
| 操作数据库 | **SQLite** (WAL模式) | 适合小事务(配置/规则/自选/日志)；WAL模式支持并发读+单写；嵌入式无需安装 |
| 冷归档 | **Parquet** | DuckDB原生支持读写；列式压缩；适合历史快照长期存储 |
| 是否需要PostgreSQL | **否** | 需要独立服务器安装，违反一键启动约束 |

**数据量评估**:
- 日线价格: 5000股 × 250交易日/年 × 15年 ≈ 1875万行 (每行含raw+qfq的OHLCV+成交量)
- 财务报表: 5000股 × 4期/年 × 15年 = 30万报告行 × 500+字段
- 指标快照: 5000股 × 最新期 = 5000行 × ~50指标列 (筛选核心表，极小)
- PDF文件: ~30万份 (冷归档，文件系统存储)

**性能预期**: DuckDB在5000行×50列的指标快照表上执行20条件筛选+窗口函数排名，耗时 <100ms。5秒预算绰绰有余。

### 1.3 前端

| 决策项 | 选择 | 理由 |
|---|---|---|
| 框架 | **Vue 3 + TypeScript** | 4页面轻量SPA；中文社区强；学习曲线低；bundle小 |
| 构建 | **Vite** | 极速HMR；构建产物为静态资源，由FastAPI托管 |
| UI组件库 | **Naive UI** | TypeScript原生；轻量；适合4页面应用 |
| K线图表 | **KLineCharts** | 专为股票K线设计；支持十字光标/缩放/raw与qfq切换/均线叠加/成交量 |
| 状态管理 | **Pinia** | Vue 3官方推荐；轻量；TypeScript友好 |
| HTTP请求 | **axios** | 拦截器/取消请求/并发控制 |

### 1.4 打包与启动

| 决策项 | 选择 | 理由 |
|---|---|---|
| Python打包 | **PyInstaller (`--onedir`模式)** | 将Python运行时+所有依赖+前端静态资源打包为一个目录。不用`--onefile`：DuckDB+pyarrow+pandas打包体积约300-500MB，onefile每次启动需解压到临时目录，冷启动可达10秒级；onedir安装时解压一次，之后秒启(PRD E6只要求一键启动，不要求单文件) |
| 启动方式 | `value-dashboard.exe` (无参数) → 启动FastAPI → 打开浏览器 | 一键启动(PRD E6) |
| CLI方式 | `value-dashboard.exe cli <command> --json` | 同一可执行文件，CLI模式(PRD CL5) |
| OpenCode调用 | 子进程调用CLI，JSON输入输出 | PRD CL2 |

### 1.5 完整依赖清单

**Python后端依赖**:
```
fastapi >= 0.110
uvicorn[standard]
pydantic >= 2.0
httpx
duckdb
typer
lark
cryptography
akshare
easy-tdx
baostock
pyarrow  # Parquet读写
PyYAML
pandas >= 2.0  # akshare传递依赖,显式pin防版本漂移破坏兼容性
pywin32  # Windows DPAPI凭据保护(PRD AR12)
pypinyin  # 股票名称拼音本地生成(PRD SD1,无免费源提供拼音)
```

**前端依赖**:
```
vue@3
vue-router@4
pinia
naive-ui
klinecharts    # K线图专用
echarts        # 通用图表(财务趋势/数据状态页图表)
axios
typescript
vite
```

---

## 第2章 系统架构

### 2.1 架构总览

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Value Dashboard (本地单机)                        │
│                                                                     │
│  ┌────────────┐         ┌────────────────────────────────────────┐  │
│  │  浏览器     │  HTTP   │           Python 进程                   │  │
│  │ Chrome/Edge│◄──────►│                                        │  │
│  │            │         │  ┌───────────┐   ┌──────────────────┐  │  │
│  │ Vue 3 SPA  │         │  │  FastAPI  │   │   核心库           │  │  │
│  │ ·筛选页    │         │  │  Web服务   │──►│  ┌────────────┐  │  │  │
│  │ ·自选页    │         │  └───────────┘   │  │ DSL引擎     │  │  │  │
│  │ ·个股页    │         │                  │  │(parser+     │  │  │  │
│  │ ·状态页    │         │  ┌───────────┐   │  │ codegen)    │  │  │  │
│  │            │         │  │   CLI     │──►│  ├────────────┤  │  │  │
│  │ KLineCharts│         │  │ (JSON I/O)│   │  │ 筛选引擎    │  │  │  │
│  │ RuleEditor │         │  └───────────┘   │  ├────────────┤  │  │  │
│  └────────────┘         │                  │  │ 适配器层    │  │  │  │
│                         │                  │  │ (4数据源)   │  │  │  │
│                         │                  │  ├────────────┤  │  │  │
│                         │                  │  │ 指标引擎    │  │  │  │
│                         │                  │  ├────────────┤  │  │  │
│                         │                  │  │ 备份/加密   │  │  │  │
│                         │                  │  └────────────┘  │  │  │
│                         │                  └────────┬─────────┘  │  │
│                         │                           │            │  │
│                         │  ┌────────────────────────┴─────────┐  │  │
│                         │  │          存储层                    │  │  │
│                         │  │  ┌─────────┐    ┌──────────────┐  │  │  │
│                         │  │  │ DuckDB  │    │   SQLite     │  │  │  │
│                         │  │  │(分析数据)│    │  (操作数据)   │  │  │  │
│                         │  │  └─────────┘    └──────────────┘  │  │  │
│                         │  │  ┌─────────┐    ┌──────────────┐  │  │  │
│                         │  │  │ Parquet │    │ PDF/备份/日志 │  │  │  │
│                         │  │  │(冷归档)  │    │  (文件系统)   │  │  │  │
│                         │  │  └─────────┘    └──────────────┘  │  │  │
│                         │  └────────────────────────────────────┘  │  │
│                         └────────────────────────────────────────┘  │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────────┐│
│  │                    数据源适配器                                   ││
│  │  CNINFO(HTTP)   AKShare(HTTP)   easy_tdx(TCP)   BaoStock(Socket)││
│  │  真值层          主适配器          备用适配器       价格补充      ││
│  └─────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 三入口共享核心库

```
                    ┌─────────────────────┐
                    │     核心库           │
                    │  (valuedashboard.    │
                    │      core)           │
                    │                     │
                    │  · 适配器层          │
                    │  · DSL引擎           │
                    │  · 筛选引擎          │
                    │  · 指标引擎          │
                    │  · 存储层            │
                    │  · 备份/加密         │
                    │  · PDF管理           │
                    └──────┬──────┬───────┘
                           │      │
              ┌────────────┘      └────────────┐
              │                                │
     ┌────────┴────────┐          ┌───────────┴────────┐
     │   Web入口        │          │   CLI入口           │
     │ (FastAPI)        │          │ (Typer + JSON)     │
     │                  │          │                    │
     │ · 浏览器UI服务    │          │ · OpenCode调用      │
     │ · 读操作为主      │          │ · 全部写操作        │
     │ · 部分写操作      │          │ · 非交互运行        │
     │   (两段式确认)    │          │ · 危险操作两段确认   │
     └──────────────────┘          └────────────────────┘
              │
     ┌────────┴────────┐
     │   启动入口        │
     │ (value-dashboard │
     │  .exe 无参数)    │
     │                  │
     │ · 启动FastAPI    │
     │ · 打开浏览器      │
     │ · 增量检查        │
     └──────────────────┘
```

**关键设计**: CLI和Web共享同一个核心库和同一个数据库文件。CLI是独立进程，可被OpenCode通过子进程调用。Web服务是主进程，启动时执行增量检查(PRD L2)。

**DuckDB 多进程并发模型** (审查问题1修订):

DuckDB 的进程模型限制：单进程读写，或多进程只读，二者互斥。Web服务常驻进程持有DuckDB写句柄期间，CLI的写操作(data update/retry/refetch/backup restore/快照重算)无法获得写权限。SQLite侧无此问题(WAL模式天然支持跨进程多读单写)。

采用以下三层协调机制：

1. **Web进程对DuckDB采用 open-per-query 模式**: 嵌入式DuckDB打开开销极小(~1ms级)。Web服务不在进程内持有长期DuckDB连接，而是每个查询请求时打开连接、执行、关闭。这样Web进程不长期占据DuckDB文件锁，CLI写操作有窗口获得写权限。

2. **应用级写锁文件** (`data/.duckdb.write.lock`): 所有向DuckDB写入的操作(CLI和Web均适用)先抢锁文件。抢不到锁时：
   - CLI: 明确提示"当前有其他写操作正在进行，请稍后重试"或"请先关闭Web服务"（针对backup restore等独占操作）
   - Web: 返回HTTP 503 + Retry-After，前端展示"数据更新中，请稍候"
   - 锁文件包含: 持有者PID、操作类型、开始时间、预计时长

3. **独占操作前置条件**: `backup restore` 明确为独占操作，前置条件是Web进程退出。PRD AR5规定恢复只能走CLI，与此设计吻合。CLI执行restore时检测Web进程是否在运行，如在运行则拒绝并提示用户先关闭。

**并发场景矩阵**:

| 场景 | Web读 | CLI写 | CLI读 | Web写(快照重算) |
|---|---|---|---|---|
| Web正常服务 | open-per-query读 | — | — | — |
| CLI增量更新 | open-per-query读(可能读到旧值，可接受) | 抢锁→写→释放 | 并行读 | 阻塞，提示等待 |
| CLI backup restore | **拒绝，提示先关闭Web** | 独占写 | 拒绝 | 拒绝 |
| 快照重算(由CLI触发) | open-per-query读(可能读到旧快照) | 抢锁→写→释放 | 并行读 | — |

**数据一致性保证**: 增量更新期间Web可能读到新旧混合数据。由于PRD L4要求"保留旧值不以空值覆盖"，且 DuckDB 事务是ACID的，单个写事务内的更新要么全部可见要么全部不可见。Web的open-per-query每次读到的是一个一致的快照。可接受的短暂不一致窗口在更新完成后自动消失。

### 2.3 核心模块规格

#### 2.3.1 适配器层 (adapters)

**接口契约**:
```python
from typing import Protocol, Literal
from pydantic import BaseModel
from datetime import datetime

class FetchRequest(BaseModel):
    data_type: str  # "price_daily" | "balance_sheet" | "income_statement" | "cash_flow" | "dividends" | "xdxr" | "listing_info" | "announcements"
    stock_codes: list[str]
    start_date: str | None = None
    end_date: str | None = None
    fields: list[str] | None = None  # None = all standard fields
    adjust: Literal["raw", "qfq", "hfq"] = "raw"  # for price data only

class SourceMetadata(BaseModel):
    source: str                    # "cninfo" | "akshare_eastmoney" | "tdx" | "baostock"
    fetch_time: datetime
    raw_response_hash: str         # SHA256 of raw response
    confidence: Literal["strict", "approximate", "missing"]
    api_version: str | None = None

class FetchResult(BaseModel):
    data: list[dict]               # standardized records
    metadata: SourceMetadata
    raw_response: bytes | None = None  # for archival

class DataAdapter(Protocol):
    name: str
    protocol: Literal["http", "tcp", "socket"]
    supported_data_types: set[str]
    rate_limit_interval: float     # seconds between requests

    async def fetch(self, request: FetchRequest) -> FetchResult: ...
    def normalize(self, raw_data: Any, request: FetchRequest) -> list[dict]: ...
```

**适配器管理器**:
- 按data_type路由到对应适配器
- 主适配器失败时自动切换到备用适配器
- 每个适配器独立的限流控制
- 记录每次抓取的SourceMetadata
- 跨源验证：当多个适配器覆盖同一data_type时，定期比对结果并标记差异

**字段映射**: 每个适配器有一个YAML配置文件，映射源特定字段名到标准化字段名:
```yaml
# config/field_mapping/akshare.yaml
balance_sheet:
  货币资金: monetary_funds
  应收账款: accounts_receivable
  存货: inventory
  流动资产合计: total_current_assets
  资产总计: total_assets
  # ... (500+ mappings)
```

**适配器优先级矩阵**:

| data_type | 主适配器 | 备用适配器 | 补充 |
|---|---|---|---|
| balance_sheet | AKShare (Eastmoney) | easy_tdx (TDX .dat) | — |
| income_statement | AKShare (Eastmoney) | easy_tdx (TDX .dat) | — |
| cash_flow | AKShare (Eastmoney) | easy_tdx (TDX .dat) | — |
| price_daily (raw) | AKShare (Eastmoney) | BaoStock (socket) | easy_tdx (TDX) |
| price_daily (qfq) | AKShare (Eastmoney) | BaoStock (socket) | — |
| dividends | CNINFO (truth) + AKShare | easy_tdx (XDXR) | — |
| xdxr | easy_tdx (TDX) | AKShare (CNINFO) | — |
| announcements | CNINFO | SZSE direct API | — |
| listing_info | AKShare | BaoStock | — |
| sw_industry | 本地缓存 (SWS手动下载) | 无 (缺失按PRD §12.4置null + 原因码) | — |

> **申万行业分类说明** (审查问题2修订): 证监会行业分类(CSRC)与申万行业分类是两套不同taxonomy，不可混用。将CSRC值写入`sw_level1/sw_level2`列会导致行业排名在错误分类法上静默计算，违反PRD §6.3/§9.2/§12.4。因此申万行业无备用适配器——缺失时按PRD §12.4返回null + 原因码，全市场排名仍可用。M1里程碑中增加实证任务：验证`stock_industry_category_cninfo` API是否支持申万标准（若支持则可恢复为合法fallback，若仅CSRC则维持"无备用"）。

#### 2.3.2 DSL引擎 (dsl)

**架构流程**:
```
DSL表达式文本
    │
    ▼
┌───────────┐     ┌───────────┐     ┌───────────┐     ┌───────────┐
│  Parser    │────►│ Validator │────►│ CodeGen   │────►│ Execution │
│ (lark)     │     │(维度校验)  │     │(SQL生成)   │     │ (DuckDB)  │
│ AST生成    │     │(依赖解析)  │     │            │     │           │
│            │     │(历史能力推导)│    │            │     │           │
└───────────┘     └───────────┘     └───────────┘     └───────────┘
                        │
                        ▼
                 ┌───────────┐
                 │ Registry  │
                 │(版本注册)  │
                 │(SQLite)   │
                 └───────────┘
```

**DSL语法** (lark grammar 核心规则):
```
expression: or_expr
or_expr: and_expr ("OR" and_expr)*
and_expr: comparison ("AND" comparison)*
comparison: additive (COMP_OP additive)?
additive: multiplicative (("+" | "-") multiplicative)*
multiplicative: unary (("*" | "/") unary)*
unary: "-"? primary
primary: NUMBER | field_ref | indicator_ref | func_call | "(" expression ")"

field_ref: TABLE "." FIELD_NAME ("@" period_spec)?
period_spec: "TTM" | "YoY" | "QoQ" | "MRQ" | "LATEST" | "CAGR" NUMBER

func_call: FUNC_NAME "(" arg_list? ")"
FUNC_NAME: "TTM" | "YoY" | "QoQ" | "CAGR" | "rank" | "rank_industry"
         | "percentile" | "zscore" | "normalize" | "rolling_avg"
         | "rolling_max" | "rolling_min" | "lag" | "avg" | "max" | "min"
```

**AST节点示例**:
```python
@dataclass
class FieldRef:
    table: str          # "balance" | "income" | "cashflow"
    field: str          # "total_assets"
    period: str         # "TTM" | "YoY" | "LATEST" | etc.
    # 维度元数据
    unit: str           # "CNY" | "ratio" | "percent" | "count"
    period_type: str    # "cumulative" | "single_quarter" | "ttm" | "point_in_time"
    historical_capable: bool

@dataclass
class CrossSectionalOp:
    op: str             # "rank" | "percentile" | "zscore" | "normalize"
    child: ASTNode
    scope: str          # "market" | "sw_level1" | "sw_level2"
    # 横截面函数自动标记为 current_only
    historical_capable: bool = False  # always False
```

**维度校验规则**:
- 加减法: 双方unit必须相同，period_type必须相同(或自动提升cumulative→TTM)
- 除法: 结果unit由操作数推导(CNY/CNY=ratio, CNY/count=CNY_per_share)
- 比较运算: 双方unit必须相同
- TTM(): 仅适用于cumulative流量字段
- rank/percentile/zscore/normalize: 结果恒为current_only
- 空值传播: 任一操作数为null时结果为null，返回原因码

**版本化与依赖锁定**:
- 每个已发布表达式获得版本号(v1, v2, ...)
- 表达式+依赖树哈希存储，不可变
- 修改已发布表达式 → 生成新版本
- 依赖图构建 → 拓扑排序 → 检测循环 → 拒绝循环依赖
- 筛选规则引用指标时锁定版本

**生命周期**: 草稿 → 校验 → 单股预览 → 小样本预览 → 发布

#### 2.3.3 筛选引擎 (screening)

**两阶段执行策略** (审查问题3修订):

> **关键语义**: 股票池成员资格由 `stock_meta` 决定（PRD §12.3 SC9: 只由ST/停牌/上市年限预设决定）；指标值取**每只股票各自**的最新快照，而非全局最大报告期。增量更新部分失败时（PRD §7.4 L4场景），重试列表中股票的快照日期可能落后于全局最大值——使用全局`MAX(report_date)`会把这些股票从基础池中静默删除，违反PRD §7.4(保留旧值)和§12.3(成员资格只由预设决定)。结果中必须携带每股各自的`data_date`供溯源展示(PRD §12.5)。

```sql
-- 阶段1: 基础股票池 (从stock_meta出发, 基于ST/停牌/上市年限开关过滤)
-- + per-stock 最新指标快照 (每只股票取各自最新的report_date)
WITH base_pool AS (
    SELECT m.stock_code, m.name, m.sw_level1, m.sw_level2,
           m.is_st, m.is_suspended, m.listing_date,
           s.*  -- 每股各自最新快照的指标值
    FROM stock_meta m
    LEFT JOIN LATERAL (
        SELECT *
        FROM indicator_snapshot s
        WHERE s.stock_code = m.stock_code
        ORDER BY s.report_date DESC
        LIMIT 1
    ) s ON true
    WHERE (:include_st OR m.is_st = false)
      AND (:include_suspended OR m.is_suspended = false)
      AND m.listing_date <= CURRENT_DATE - INTERVAL ':min_years' YEAR
),

-- 阶段2: 横截面排名计算 (DuckDB窗口函数, 作用于过滤后的基础池)
ranked AS (
    SELECT *,
        RANK() OVER (ORDER BY pe_ttm) AS pe_ttm_market_rank,
        PERCENT_RANK() OVER (ORDER BY pe_ttm) AS pe_ttm_market_percentile,
        RANK() OVER (PARTITION BY sw_level1 ORDER BY pe_ttm) AS pe_ttm_industry_rank,
        PERCENT_RANK() OVER (PARTITION BY sw_level1 ORDER BY pe_ttm) AS pe_ttm_industry_percentile
        -- ... (所有需要的排名列)
    FROM base_pool
)

-- 阶段3: 应用用户条件 (由DSL CodeGen生成)
-- 结果包含每股各自的 report_date 作为 data_date 供溯源展示
SELECT * FROM ranked
WHERE <user_condition_1>
  AND <user_condition_2>
  OR <user_condition_3>
  -- ... (最多20个条件, 3层嵌套)
ORDER BY <user_sort_columns>
```

> **注**: `LEFT JOIN LATERAL` 确保即使某只股票的指标快照完全缺失（如新股或全部抓取失败），该股票仍保留在基础池中（指标值为null，不满足任何数值条件故不会出现在结果中，但不会被静默删除）。DuckDB支持`LATERAL`子查询。替代写法可用窗口函数`QUALIFY row_number() OVER (PARTITION BY stock_code ORDER BY report_date DESC) = 1`。

**性能保证**:
- indicator_snapshot表: ~5000行 × ~50列 → DuckDB扫描 <100ms
- 窗口函数排名: 5000行 → <50ms
- 条件过滤: 5000行 → <10ms
- 总计: <200ms (5秒预算的4%)
- 剩余预算用于: 请求解析、SQL生成、结果序列化、网络传输、前端渲染

**指标快照预计算**: 在数据更新任务中(非筛选时)，为每只股票计算所有内建指标和已发布复合指标，物化到indicator_snapshot表。这是筛选性能的关键前提。

#### 2.3.4 存储层 (storage)

**DuckDB 并发访问策略** (审查问题1修订):

DuckDB进程模型为单进程读写或多进程只读（互斥）。Web服务常驻进程不能长期持有DuckDB写句柄，否则CLI写操作无法执行。采用以下策略：

- **Web进程**: 对DuckDB采用 open-per-query 模式（嵌入式打开开销~1ms），不持有长期连接。读操作每次打开→查询→关闭，不长期占据文件锁。
- **CLI写操作**: 通过应用级锁文件 `data/.duckdb.write.lock` 协调。抢到锁后打开DuckDB写连接，执行完毕释放锁。抢不到锁时明确提示用户。
- **独占操作** (backup restore): 前置条件为Web进程退出。CLI检测Web进程是否运行，如在运行则拒绝执行。
- **SQLite侧**: WAL模式天然支持跨进程多读单写，无需额外协调。

详见 §2.2「DuckDB 多进程并发模型」的并发场景矩阵。

**DuckDB分析库** (data/valuedashboard.duckdb):

| 表名 | 用途 | 预估行数 | 关键列 |
|---|---|---|---|
| stock_meta | 股票元数据 | ~5000 | stock_code, name, pinyin, exchange, listing_date, is_st, is_suspended, sw_level1, sw_level2 |
| price_daily_raw | 原始日线 | ~1875万 | stock_code, trade_date, open, high, low, close, volume, turnover |
| price_daily_qfq | 前复权日线 | ~1875万 | stock_code, trade_date, open, high, low, close, volume, turnover |
| balance_sheet | 资产负债表 | ~30万 | stock_code, report_date, monetary_funds, accounts_receivable, ..., total_assets, total_liabilities, total_equity (500+字段) |
| income_statement | 利润表 | ~30万 | stock_code, report_date, revenue, cost_of_revenue, ..., net_profit (500+字段) |
| cash_flow | 现金流量表 | ~30万 | stock_code, report_date, cf_from_operating, ..., cf_net (500+字段) |
| dividends | 分红记录 | ~10万 | stock_code, ex_date, dividend_per_share, stock_dividend, rights_issue |
| xdxr | 除权除息记录 | ~10万 | stock_code, event_date, category, fenhong, songzhuangu, peigu, peigujia |
| indicator_snapshot | 指标快照(预计算) | ~5000 | stock_code, report_date, pe_ttm, pb_mrq, roe_ttm, ..., (所有内建+已发布指标) |
| source_audit | 溯源审计 | 随抓取增长 | stock_code, field_name, source, fetch_time, raw_hash, confidence, value |

**SQLite操作库** (data/valuedashboard.sqlite):

| 表名 | 用途 | 关键列 |
|---|---|---|
| dsl_expressions | DSL表达式注册表 | id, name, version, expression_text, ast_json, status, description, direction, created_at |
| dsl_dependencies | 表达式依赖关系 | expression_id, depends_on_id, depends_on_version |
| screening_rules | 筛选规则 | id, name, version, rule_json, locked_indicators, status, created_at |
| screening_results | 保存的筛选结果 | id, title, note, rule_id, rule_version, data_date, result_json, columns_json, sort_json, confidence_summary |
| watchlist | 自选列表 | id, stock_code, group_name, source_rule_id, source_result_id, added_at |
| manual_overrides | 人工覆写 | id, stock_code, field_name, original_value, override_value, reason, created_at, rollback_of |
| plans | 危险操作计划 | plan_id, operation, plan_summary, created_at, expires_at, status |
| job_logs | 任务日志 | id, job_type, status, started_at, finished_at, details_json |
| retry_list | 重试列表 | id, stock_code, data_type, adapter, error, retry_count, last_attempt |
| missing_list | 缺失列表 | id, stock_code, field_name, reason_code, detected_at |
| pdf_tasks | PDF解析失败任务 | id, stock_code, announcement_id, pdf_hash, page, error, status |
| backup_registry | 备份记录 | id, type(full/incremental), path, checksum, encrypted, created_at |
| config | 用户配置 | key, value, updated_at |

#### 2.3.5 备份与加密 (backup)

**冷热分层**:
- 热数据: DuckDB数据库文件 (当前结构化数据，直接查询)
- 冷归档: Parquet快照 (历史数据导出) + PDF文件 + 备份包

**备份策略**:
- 全量备份: DuckDB导出为Parquet + SQLite dump + PDF目录 + 配置
- 增量备份: 自上次备份以来变更的数据
- 保留最近3套全量备份(PRD AR10)
- 个性化数据(规则/自选/覆写/配置)用用户口令AES-256-GCM加密(PRD AR9)
- 公共数据(价格/财务/PDF)不加密(PRD AR8)
- 凭据用Windows凭据保护机制(DPAPI)存储，不进入备份文件(PRD AR12)

**恢复流程**:
1. CLI接收恢复命令 → 生成plan_id
2. 用户确认plan_id → 解密个性化数据 → 恢复DuckDB/SQLite/PDF
3. 恢复PDF后用户可在浏览器打开(PRD AR7)

#### 2.3.6 CLI与JSON协议 (cli)

**协议结构**:
```json
{
  "schema_version": "1.0",
  "command": "screening.run",
  "params": { ... },
  "result": {
    "status": "ok" | "error",
    "data": { ... },
    "error_code": null | "E001",
    "error_message": null | "...",
    "reason_code": null | "R001"
  }
}
```

**CLI命令树**:
```
vd discover schema              # 获取JSON schema
vd discover capabilities        # 获取能力清单
vd discover examples            # 获取示例
vd discover fields              # 发现可用字段
vd discover indicators          # 发现内建指标
vd discover functions           # 发现DSL函数
vd discover reason_codes        # 发现原因码

vd indicator create             # 创建指标草稿
vd indicator validate           # 校验指标
vd indicator preview_single     # 单股预览
vd indicator preview_sample     # 小样本预览
vd indicator publish            # 发布指标版本

vd screening create             # 创建筛选规则
vd screening version            # 规则版本化
vd screening run                # 手动运行筛选
vd screening save_result        # 保存结果
vd screening export_csv         # 导出CSV
vd screening add_to_watchlist   # 加入自选

vd data init                    # 初始化(一键启动)
vd data update                  # 增量更新
vd data diagnose                # 诊断
vd data retry                   # 重试失败任务
vd data switch_source           # 切换数据源
vd data refetch                 # 指定范围重抓

vd override list_conflicts      # 查看冲突
vd override submit              # 提交人工校正
vd override revoke              # 撤销人工覆写

vd archive create               # 创建冷归档
vd archive verify               # 验证归档
vd archive clean                # 清理计划(需确认)
vd backup create                # 创建备份
vd backup restore               # 恢复备份(需确认)
vd backup list                  # 列出备份

vd plan confirm <plan_id>       # 确认危险操作
```

**两段式确认流程**:
```
Step 1: vd backup restore --target /path/to/backup
  → 返回: { "plan_id": "abc123", "plan_summary": {...}, "expires_at": "2026-07-17T15:30:00Z" }

Step 2 (15分钟内): vd plan confirm abc123
  → 执行恢复 → 返回结果

Step 2 (超时): vd plan confirm abc123
  → 返回错误: { "error_code": "E401", "error_message": "plan_id已过期" }
```

---

## 第3章 数据模型设计

### 3.1 实体关系图 (核心表)

```
┌─────────────┐     ┌─────────────────┐     ┌──────────────────┐
│ stock_meta  │1───►│ price_daily_raw │     │ price_daily_qfq  │
│             │1───►│                 │     │                  │
│ stock_code  │1───►└─────────────────┘     └──────────────────┘
│ name        │1───►┌─────────────────┐     ┌──────────────────┐
│ exchange    │     │ balance_sheet   │     │ income_statement │
│ listing_date│1───►│                 │     │                  │
│ is_st       │1───►└─────────────────┘     └──────────────────┘
│ sw_level1   │1───►┌─────────────────┐     ┌──────────────────┐
│ sw_level2   │     │ cash_flow       │     │ dividends        │
└──────┬──────┘1───►│                 │     │                  │
       │            └─────────────────┘     └──────────────────┘
       │1───►┌─────────────────┐
       │     │ xdxr            │
       │1───►│                 │
       │     └─────────────────┘
       │1───►┌─────────────────────────────┐
       │     │ indicator_snapshot           │
       │     │ (预计算: pe_ttm, pb, roe...) │
       │     └─────────────────────────────┘
       │1───►┌─────────────────────────────┐
       │     │ source_audit                 │
       │     │ (溯源: source, time, hash)   │
       │     └─────────────────────────────┘
       │
       │     ┌─────────────────┐     ┌──────────────────┐
       └───►│ watchlist       │     │ manual_overrides │
             └─────────────────┘     └──────────────────┘

┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│ dsl_expressions │◄───►│ dsl_dependencies│     │ screening_rules │
│ (版本化,不可变)  │     │ (依赖图)         │     │ (引用指标版本)   │
└─────────────────┘     └─────────────────┘     └────────┬────────┘
                                                         │
                                                ┌────────┴────────┐
                                                │ screening_results│
                                                │ (保存的结果)      │
                                                └─────────────────┘
```

### 3.2 置信度与溯源模型

> **审计粒度界定** (审查问题5修订): 逐值审计在500字段×~30万报告期行下可达亿级行，写入开销与维护复杂度不必要。PRD §14只要求**关键字段**溯源。采用两级溯源：

**两级溯源模型**:

1. **批次级 lineage (全量覆盖)**: 每次抓取操作记录一条批次元数据，覆盖该次抓取的全部数据。

```sql
CREATE TABLE fetch_batch (
    id BIGINT PRIMARY KEY,
    batch_id VARCHAR NOT NULL,            -- UUID批次标识
    data_type VARCHAR NOT NULL,           -- "balance_sheet" | "price_daily" | etc.
    source VARCHAR NOT NULL,              -- "cninfo" | "akshare_eastmoney" | "tdx" | "baostock"
    adapter_version VARCHAR NOT NULL,     -- 适配器版本
    fetch_time TIMESTAMP NOT NULL,        -- 抓取时间
    raw_response_hash VARCHAR NOT NULL,   -- SHA256(raw_response_archive)
    row_count INTEGER NOT NULL,           -- 本次抓取行数
    stock_codes VARCHAR[],                -- 涉及的股票代码列表
    report_date_range VARCHAR,            -- 涉及的报告期范围
    confidence VARCHAR NOT NULL,          -- 批次默认置信度
    created_at TIMESTAMP DEFAULT NOW()
);
```

2. **关键字段级逐值溯源 (仅PRD §14要求的字段)**: 仅对个股详情页溯源展示涉及的指标字段（估值、盈利、成长、安全、股东回报摘要涉及的字段）做逐值记录，约20-30个字段×5000股=~15万行，体量可控。

```sql
CREATE TABLE source_audit (
    id BIGINT PRIMARY KEY,
    stock_code VARCHAR NOT NULL,
    field_name VARCHAR NOT NULL,         -- 标准化字段名 (仅关键字段)
    report_date DATE,                    -- 报告期(财务数据)或交易日期(价格)
    value DOUBLE,                        -- 实际值
    source VARCHAR NOT NULL,             -- "cninfo" | "akshare_eastmoney" | "tdx" | "baostock"
    fetch_batch_id VARCHAR NOT NULL,     -- 关联fetch_batch.batch_id
    fetch_time TIMESTAMP NOT NULL,       -- 抓取时间
    raw_response_hash VARCHAR NOT NULL,  -- SHA256(raw_response)
    confidence VARCHAR NOT NULL,         -- "strict" | "approximate" | "missing"
    reason_code VARCHAR,                 -- null或原因码(如"R001: source_unavailable")
    api_version VARCHAR,                 -- 适配器API版本
    is_override BOOLEAN DEFAULT FALSE,   -- 是否被人工覆写
    override_id BIGINT,                  -- 关联的人工覆写ID
    created_at TIMESTAMP DEFAULT NOW()
);
```

**溯源字段清单** (仅以下字段进入source_audit逐值表):
- 估值: PE-TTM, PB-MRQ, PS-TTM, PCF-TTM, 股息率, 总市值, 流通市值
- 盈利: ROE, ROA, 毛利率, 净利率, 投入资本回报率, 经营现金流/净利润
- 成长: 营收YoY, 归母净利YoY, 扣非归母净利YoY, 营收CAGR(3/5年), 归母净利CAGR(3/5年)
- 安全: 资产负债率, 流动比率, 速动比率, 有息负债, 利息保障倍数, 商誉占比
- 股东回报: 分红率, 每股股息, 连续分红年数
- 最近收盘价 + 价格日期

非关键字段（如原始报表中的 货币资金/应收账款/存货 等明细科目）仅通过 `fetch_batch` 表实现批次级溯源，不逐值记录。

**置信度赋值规则**:
- `strict`: 来自CNINFO(法定披露)的值，或多个独立来源交叉验证一致的值
- `approximate`: 来自单一第三方来源(Eastmoney/TDX/BaoStock)的值
- `missing`: 无法获取的值，返回null + 原因码

### 3.3 人工覆写模型

```sql
CREATE TABLE manual_overrides (
    id BIGINT PRIMARY KEY,
    stock_code VARCHAR NOT NULL,
    field_name VARCHAR NOT NULL,
    report_date DATE,
    original_value DOUBLE,               -- 原始值(从source_audit获取)
    override_value DOUBLE NOT NULL,      -- 覆写值
    reason TEXT NOT NULL,                -- 覆写原因
    correction_template JSON,            -- 受控JSON校正模板(PRD PDF2)
    created_at TIMESTAMP DEFAULT NOW(),
    rolled_back_at TIMESTAMP,            -- 撤销时间(null=未撤销)
    rolled_back_to BIGINT                -- 撤销后恢复的覆写ID
);
```

---

## 第4章 目录结构

```
value-dashboard/
├── app/                              # 应用源码
│   ├── __init__.py
│   ├── core/                         # 核心库 (CLI和Web共享)
│   │   ├── __init__.py
│   │   ├── adapters/                 # 数据源适配器
│   │   │   ├── __init__.py
│   │   │   ├── base.py              # DataAdapter协议, FetchRequest, FetchResult
│   │   │   ├── akshare_adapter.py   # AKShare/Eastmoney适配器
│   │   │   ├── tdx_adapter.py       # easy_tdx/TDX适配器
│   │   │   ├── baostock_adapter.py  # BaoStock适配器
│   │   │   ├── cninfo_adapter.py    # CNINFO适配器
│   │   │   └── manager.py           # 适配器管理器(路由/备用/限流/跨源验证)
│   │   ├── dsl/                      # DSL引擎
│   │   │   ├── __init__.py
│   │   │   ├── grammar.lark         # DSL语法定义
│   │   │   ├── parser.py            # AST解析器
│   │   │   ├── ast_nodes.py         # AST节点定义(含维度元数据)
│   │   │   ├── validator.py         # 维度校验/依赖检测/历史能力推导
│   │   │   ├── codegen.py           # DuckDB SQL代码生成
│   │   │   └── registry.py          # 版本化表达式注册表(SQLite)
│   │   ├── screening/                # 筛选引擎
│   │   │   ├── __init__.py
│   │   │   ├── engine.py            # 筛选执行(构建SQL, DuckDB执行)
│   │   │   ├── snapshot.py          # 指标快照预计算
│   │   │   └── ranking.py           # 横截面排名计算
│   │   ├── indicators/               # 内建指标计算
│   │   │   ├── __init__.py
│   │   │   ├── valuation.py         # PE/PB/PS/PCF/股息率
│   │   │   ├── profitability.py     # ROE/ROA/毛利率/净利率/投入资本回报率
│   │   │   ├── growth.py            # YoY/CAGR
│   │   │   ├── safety.py            # 资产负债率/流动比率/速动比率/有息负债/利息保障倍数/商誉占比
│   │   │   ├── shareholder.py       # 分红率/每股股息/连续分红年数
│   │   │   └── technical.py         # MA/收益率/波动率/最大回撤/成交量/换手率
│   │   ├── storage/                  # 存储层
│   │   │   ├── __init__.py
│   │   │   ├── duckdb_store.py      # DuckDB连接管理(分析数据)
│   │   │   ├── sqlite_store.py      # SQLite连接管理(操作数据, WAL模式)
│   │   │   ├── schema.py            # Schema定义与迁移
│   │   │   └── parquet_archive.py   # Parquet冷归档读写
│   │   ├── backup/                   # 备份与加密
│   │   │   ├── __init__.py
│   │   │   ├── encryptor.py         # AES-256-GCM加密
│   │   │   ├── backup_manager.py    # 全量+增量备份
│   │   │   └── restore_manager.py   # 恢复管理
│   │   ├── pdf/                      # PDF管理
│   │   │   ├── __init__.py
│   │   │   ├── downloader.py        # CNINFO PDF下载
│   │   │   ├── archive.py           # PDF归档到冷存储
│   │   │   └── viewer.py            # PDF浏览器打开能力
│   │   └── config.py                # 配置加载器
│   ├── cli/                          # CLI入口
│   │   ├── __init__.py
│   │   ├── main.py                   # CLI参数解析, JSON I/O
│   │   ├── protocol.py               # JSON协议处理(schema_version)
│   │   ├── commands/                 # CLI命令实现
│   │   │   ├── discover.py           # schema/capabilities/examples/fields/indicators
│   │   │   ├── indicator.py          # create/validate/preview/publish
│   │   │   ├── screening.py          # create/version/run/save/export
│   │   │   ├── data.py               # init/update/diagnose/retry/switch_source
│   │   │   ├── override.py           # list_conflicts/submit/revoke
│   │   │   ├── backup.py             # archive/backup/restore
│   │   │   └── plan.py               # 两段式确认(plan_id)
│   │   └── opencode_skill.md         # OpenCode skill说明
│   └── web/                          # FastAPI Web服务
│       ├── __init__.py
│       ├── main.py                   # 服务入口 + 浏览器启动 + 增量检查
│       ├── api/                      # REST API
│       │   ├── screening.py          # 筛选页API
│       │   ├── watchlist.py          # 自选列表API
│       │   ├── stock_detail.py       # 个股详情API
│       │   ├── data_status.py        # 数据状态页API
│       │   └── kline.py              # K线数据API
│       └── static/                   # 预构建前端资源 (Vue 3)
│           ├── index.html
│           └── assets/
├── frontend/                         # Vue 3前端源码 (开发时)
│   ├── src/
│   │   ├── App.vue
│   │   ├── main.ts
│   │   ├── router/
│   │   │   └── index.ts
│   │   ├── views/                    # 4个页面
│   │   │   ├── ScreeningPage.vue     # 筛选页(默认首页)
│   │   │   ├── WatchlistPage.vue     # 自选列表页
│   │   │   ├── StockDetailPage.vue   # 个股详情页
│   │   │   └── DataStatusPage.vue    # 只读数据状态页
│   │   ├── components/               # 可复用组件
│   │   │   ├── KLineChart.vue        # K线图(KLineCharts)
│   │   │   ├── RuleEditor.vue        # 可视化AND/OR规则编辑器
│   │   │   ├── RuleNode.vue          # 规则节点(递归, 最多3层)
│   │   │   ├── IndicatorPicker.vue   # 指标/字段选择器
│   │   │   ├── ResultTable.vue       # 筛选结果表格
│   │   │   ├── ConfidenceBadge.vue   # strict/approximate/missing徽章
│   │   │   ├── SourceBadge.vue       # 来源+溯源徽章
│   │   │   ├── FinancialTrend.vue    # 财务趋势图
│   │   │   └── DataStatusPanel.vue   # 数据状态面板
│   │   ├── composables/              # Vue组合式函数
│   │   │   ├── useScreening.ts
│   │   │   ├── useKLine.ts
│   │   │   ├── useRuleEditor.ts
│   │   │   └── useConfidence.ts
│   │   ├── stores/                   # Pinia状态管理
│   │   │   ├── screening.ts
│   │   │   ├── watchlist.ts
│   │   │   └── stockDetail.ts
│   │   └── types/
│   │       └── protocol.ts           # JSON协议TypeScript类型
│   ├── package.json
│   ├── vite.config.ts
│   └── tsconfig.json
├── data/                             # 运行时数据 (gitignore)
│   ├── valuedashboard.duckdb         # DuckDB分析库
│   ├── valuedashboard.sqlite         # SQLite操作库
│   ├── parquet/                      # Parquet冷归档
│   ├── pdf/                          # PDF文件
│   ├── backup/                       # 加密备份包
│   ├── cache/                        # 适配器响应缓存
│   └── logs/                         # 日志
├── config/                           # 配置
│   ├── default.yaml                  # 默认配置
│   └── field_mapping/                # 适配器字段映射
│       ├── akshare.yaml
│       ├── tdx.yaml
│       ├── baostock.yaml
│       └── cninfo.yaml
├── tests/                            # 测试
│   ├── unit/
│   ├── integration/
│   └── fixtures/                     # 筛选性能验收夹具(PRD PF3)
├── start.bat                         # Windows一键启动
├── pyproject.toml                    # Python项目配置
├── value-dashboard.spec              # PyInstaller打包配置
└── README.md
```

---

## 第5章 实现路线图

### 5.1 里程碑划分

| 里程碑 | 目标 | 验收对应 | 预估周期 |
|---|---|---|---|
| M0 | 项目骨架 + 存储层 + 配置 | — | 1周 |
| M1 | 适配器层 + 最小可用初始化 | PRD §6.7, §20.4步骤1-2 | 3周 |
| M2 | 指标引擎 + 指标快照预计算 | PRD §10 | 2周 |
| M3 | 筛选页 + 筛选引擎 + 性能验收 | PRD §12, §19.1, §20.1 | 3周 |
| M4 | 个股详情页 + K线 + 财务趋势 | PRD §14, §20.2 | 3周 |
| M5 | DSL引擎 + 复合指标全流程 | PRD §11, §20.1步骤3-4 | 3周 |
| M6 | 自选列表 + 数据状态页 | PRD §13, §15 | 1周 |
| M7 | CLI + JSON协议 + OpenCode skill | PRD §16, §20.3 | 2周 |
| M8 | PDF管理 + 人工覆写 + 校正模板 | PRD §9.5, §17, §20.4步骤4-5 | 2周 |
| M9 | 备份/归档/恢复/加密 | PRD §18, §20.4步骤6-8 | 2周 |
| M10 | PyInstaller打包 + 一键启动 + 全量验收 | PRD §19, §20 | 2周 |

### 5.2 里程碑详情

#### M0: 项目骨架 (1周)
- 初始化pyproject.toml, Python虚拟环境, 前端Vue 3项目
- 实现DuckDB/SQLite连接管理器
- 实现Schema定义与迁移机制
- 实现配置加载器
- 搭建FastAPI最小服务 + Vue 3最小页面
- **记录目标Windows主机规格** (PRD §19.1 PF1): 记录CPU型号/内存/磁盘类型(SSD/HDD)，存入 `config/host_spec.yaml`，作为5秒筛选性能验收环境的组成部分
- **产出**: 可启动的空壳项目 + 主机规格记录

#### M1: 适配器层 + 最小可用初始化 (3周)
- 实现4个适配器(CNINFO/AKShare/easy_tdx/BaoStock)
- 实现适配器管理器(路由/备用/限流)
- 实现字段映射配置
- 实现最小可用初始化流程(PRD §6.7):
  1. 当前上市股票全集 + ST/停牌/上市日期
  2. 每股最近收盘价 + 价格日期
  3. 每股近5年raw+qfq日线
  4. 每股最小核心财务集
  5. 当前申万一级/二级归属
- 实现增量检查(新交易日/新公告/待重试)
- 实现失败处理(保留旧值/重试列表/缺失列表)
- **实证任务**: 验证 `stock_industry_category_cninfo` API的分类标准选项——是否支持申万标准(若是则可恢复为sw_industry的合法fallback，若仅CSRC则维持"无备用+missing处理")
- **验收**: PRD §20.4步骤1-2

#### M2: 指标引擎 (2周)
- 实现估值指标(PE-TTM/PB-MRQ/PS-TTM/PCF-TTM/股息率/总市值/流通市值)
- 实现盈利能力指标(ROE/ROA/毛利率/净利率/投入资本回报率/经营现金流比净利润)
- 实现成长指标(YoY/CAGR 3年/5年)
- 实现安全性指标(资产负债率/流动比率/速动比率/有息负债/利息保障倍数/商誉占比)
- 实现股东回报指标(分红率/每股股息/连续分红年数)
- 实现行情指标(MA5-250/区间收益率/年化波动率/最大回撤/平均成交量/换手率)
- 实现指标快照预计算→物化到indicator_snapshot表
- **验收**: PRD §10全部指标可计算

#### M3: 筛选页 + 筛选引擎 (3周)
- 实现筛选引擎(两阶段执行: 基础池→排名→条件过滤)
- 实现可视化AND/OR规则编辑器(最多3层嵌套)
- 实现规则版本化(锁定指标版本)
- 实现基础股票池开关(ST/停牌/上市年限)
- 实现结果展示(列配置/多字段排序/入选解释)
- 实现结果保存(标题必填/备注可选/数据日期/规则版本/指标版本/置信度)
- 实现CSV导出(含完整溯源信息)
- 实现结果加入自选
- **性能验收**: PRD §19.1 (5000股×20条件≤5秒, 10次中9次通过)
- **验收**: PRD §20.1 (内建指标子集，步骤1-2/5-9)；完整§20.1(含步骤3-4复合指标)于M10全量验收

#### M4: 个股详情页 (3周)
- 实现K线图(KLineCharts: 日K/成交量/均线叠加/raw与qfq切换/缩放/十字光标)
- 实现估值/盈利/成长/安全/股东回报摘要
- 实现财务趋势预设视图(年度默认, 可切季度/TTM)
- 实现historical_capable指标5年默认+1/3/5/10年/全部切换
- 实现current_only指标明确标注
- 实现自定义数值指标视图
- 实现关键字段溯源(报告期/生效日期/数据版本/来源/价格日期/置信度/公式)
- 实现PDF浏览器打开
- **验收**: PRD §20.2

#### M5: DSL引擎 (3周)
- 实现lark语法定义
- 实现AST解析器
- 实现维度校验(百分比/绝对值/累计值/单季度值/单位兼容性)
- 实现历史能力自动推导(historical_capable/current_only)
- 实现空值传播与原因码
- 实现简写展开(TTM/最新报告期默认)
- 实现依赖图构建与循环依赖检测
- 实现版本化注册表(不可变/新版本/依赖锁定)
- 实现DuckDB SQL代码生成
- 实现生命周期(草稿→校验→单股预览→小样本预览→发布)
- **验收**: PRD §11全部要求

#### M6: 自选列表 + 数据状态页 (1周)
- 实现自选列表(分组/排序/自定义列/手动保留/移除/来源记录)
- 实现只读数据状态页(更新时间/覆盖状态/回填状态/重试摘要/缺失摘要/PDF失败摘要/备份摘要)
- **验收**: PRD §13, §15

#### M7: CLI + JSON协议 (2周)
- 实现CLI入口(Typer, 非交互运行)
- 实现JSON协议(schema_version/命令/参数/结果/错误码/原因码)
- 实现全部CLI命令(discover/indicator/screening/data/override/backup/plan)
- 实现两段式确认(plan_id, 15分钟有效期)
- 实现OpenCode skill说明
- 实现schema/capabilities/examples机器可读输出
- **验收**: PRD §20.3

#### M8: PDF管理 + 人工覆写 (2周)
- 实现CNINFO PDF下载与本地存储
- 实现PDF归档(冷存储)与恢复
- 实现PDF浏览器打开
- 实现人工覆写(与原始值分离/可审计/可回滚/可显示原因/可预览影响面)
- 实现受控JSON校正模板(公告标识/PDF哈希/页码/报告期/单位/校正原因/字段与数值)
- 实现校正流程(草稿→校验→影响预览→确认发布)
- 实现PDF解析失败任务生成
- **验收**: PRD §9.5, §17, §20.4步骤4-5

#### M9: 备份/归档/恢复/加密 (2周)
- 实现冷归档(Parquet快照导出 + PDF + 备份包)
- 实现归档验证与本地清理
- 实现全量+增量备份(保留最近3套)
- 实现个性化数据加密(AES-256-GCM, 用户口令)
- 实现离线恢复密钥生成
- 实现Windows凭据保护(DPAPI)存储凭据
- 实现恢复流程(CLI, 两段式确认)
- 实现网页PDF在冷归档中时显示归档位置与恢复指引
- **验收**: PRD §18, §20.4步骤6-8

#### M10: 打包 + 一键启动 + 全量验收 (2周)
- PyInstaller打包(`--onedir`模式, Python+依赖+前端静态资源→一个目录)
- 实现一键启动(exe→FastAPI→浏览器打开)
- 实现启动时增量检查
- 构建筛选性能验收夹具(5000股×20条件×含复合指标+行业排名)
- 执行PRD §20四条验收流程全量测试
- **验收**: PRD §19, §20全部

### 5.3 总周期

**预估总周期: ~24周 (约6个月)**

关键路径: M0 → M1 → M2 → M3 (筛选可用) → M5 (DSL) → M7 (CLI) → M10 (打包验收)

M4/M6/M8/M9可与M3/M5/M7部分并行(不同模块，低耦合)。

### 5.4 优先交付顺序 (与PRD §7.2一致)

1. **最小可用**: M0+M1 → 股票全集可见 + 最新核心财务 + 近5年价格
2. **筛选可用**: M2+M3 → 当前筛选可用
3. **研究可用**: M4 → 单股研究可用
4. **高级筛选**: M5 → 复合指标 + DSL
5. **完整工具**: M6+M7+M8+M9+M10 → CLI/PDF/覆写/备份/打包
