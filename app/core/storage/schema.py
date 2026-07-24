"""Schema 定义与迁移机制

版本化 Schema 管理：通过 SQLite 中的 schema_migrations 表跟踪已应用的迁移。
每次启动时检查并执行未应用的迁移。

DuckDB 表：分析数据（价格、财务、指标快照、溯源审计）
SQLite 表：操作数据（DSL、规则、自选、覆写、计划、日志）
"""

from __future__ import annotations

import logging

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

# ─── DuckDB Schema (分析库) ───────────────────────────────────────────

DUCKDB_SCHEMA_V1 = """
-- 股票元数据
CREATE TABLE IF NOT EXISTS stock_meta (
    stock_code     VARCHAR PRIMARY KEY,
    name           VARCHAR NOT NULL,
    pinyin         VARCHAR,
    exchange       VARCHAR NOT NULL,   -- SSE / SZSE / BSE
    listing_date   DATE,
    is_st          BOOLEAN,
    is_suspended   BOOLEAN,
    sw_level1      VARCHAR,            -- 申万一级（缺失为 NULL）
    sw_level2      VARCHAR,            -- 申万二级
    sw_level1_code VARCHAR,
    sw_level2_code VARCHAR,
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 原始日线
CREATE TABLE IF NOT EXISTS price_daily_raw (
    stock_code    VARCHAR NOT NULL,
    trade_date    DATE NOT NULL,
    open          DOUBLE,
    high          DOUBLE,
    low           DOUBLE,
    close         DOUBLE,
    volume        DOUBLE,
    turnover      DOUBLE,            -- 成交额
    turnover_rate DOUBLE,            -- 换手率(%)
    PRIMARY KEY (stock_code, trade_date)
);

-- 前复权日线
CREATE TABLE IF NOT EXISTS price_daily_qfq (
    stock_code       VARCHAR NOT NULL,
    trade_date       DATE NOT NULL,
    open             DOUBLE,
    high             DOUBLE,
    low              DOUBLE,
    close            DOUBLE,
    volume           DOUBLE,
    turnover         DOUBLE,
    turnover_rate    DOUBLE,            -- P2修复: 与raw表一致
    PRIMARY KEY (stock_code, trade_date)
);

-- 资产负债表
CREATE TABLE IF NOT EXISTS balance_sheet (
    stock_code   VARCHAR NOT NULL,
    report_date  DATE NOT NULL,
    report_type  VARCHAR,              -- annual / quarterly / semi_annual
    -- 流动资产
    monetary_funds             DOUBLE,   -- 货币资金
    trading_financial_assets   DOUBLE,   -- 交易性金融资产
    notes_receivable           DOUBLE,   -- 应收票据
    accounts_receivable        DOUBLE,   -- 应收账款
    prepayments                DOUBLE,   -- 预付款项
    other_receivables          DOUBLE,   -- 其他应收款
    inventory                  DOUBLE,   -- 存货
    contract_assets            DOUBLE,   -- 合同资产
    total_current_assets       DOUBLE,   -- 流动资产合计
    -- 非流动资产
    long_term_equity_investment DOUBLE,  -- 长期股权投资
    fixed_assets               DOUBLE,   -- 固定资产
    construction_in_progress   DOUBLE,   -- 在建工程
    right_of_use_assets        DOUBLE,   -- 使用权资产
    intangible_assets          DOUBLE,   -- 无形资产
    goodwill                   DOUBLE,   -- 商誉
    deferred_tax_assets        DOUBLE,   -- 递延所得税资产
    total_non_current_assets   DOUBLE,   -- 非流动资产合计
    total_assets               DOUBLE,   -- 资产总计
    -- 流动负债
    short_term_loans           DOUBLE,   -- 短期借款
    notes_payable              DOUBLE,   -- 应付票据
    accounts_payable           DOUBLE,   -- 应付账款
    prepayments_received       DOUBLE,   -- 预收款项
    contract_liabilities       DOUBLE,   -- 合同负债
    employee_benefits_payable  DOUBLE,   -- 应付职工薪酬
    taxes_payable              DOUBLE,   -- 应交税费
    total_current_liabilities  DOUBLE,   -- 流动负债合计
    -- 非流动负债
    long_term_loans            DOUBLE,   -- 长期借款
    bonds_payable              DOUBLE,   -- 应付债券
    lease_liabilities          DOUBLE,   -- 租赁负债
    total_non_current_liabilities DOUBLE, -- 非流动负债合计
    total_liabilities          DOUBLE,   -- 负债合计
    -- 所有者权益
    paid_in_capital            DOUBLE,   -- 实收资本(股本)
    capital_reserve            DOUBLE,   -- 资本公积
    surplus_reserve            DOUBLE,   -- 盈余公积
    undistributed_profit       DOUBLE,   -- 未分配利润
    minority_interest          DOUBLE,   -- 少数股东权益
    total_equity               DOUBLE,   -- 所有者权益合计
    total_equity_parent        DOUBLE,   -- 归属于母公司所有者权益
    -- 完整原始数据（JSON列存储Eastmoney/TDX返回的全部500+字段）
    raw_data                   JSON,
    PRIMARY KEY (stock_code, report_date)
);

-- 利润表
CREATE TABLE IF NOT EXISTS income_statement (
    stock_code       VARCHAR NOT NULL,
    report_date      DATE NOT NULL,
    report_type      VARCHAR,
    total_operating_revenue DOUBLE,        -- 营业总收入
    revenue                   DOUBLE,        -- 营业收入
    total_operating_cost     DOUBLE,        -- 营业总成本
    cost_of_revenue          DOUBLE,        -- 营业成本
    -- 税金及附加
    taxes_and_surcharges     DOUBLE,
    selling_expenses         DOUBLE,        -- 销售费用
    administrative_expenses  DOUBLE,        -- 管理费用
    rd_expenses              DOUBLE,        -- 研发费用
    financial_expenses       DOUBLE,        -- 财务费用
    -- 其中: 利息费用/利息收入
    interest_expense         DOUBLE,        -- 利息费用
    interest_income          DOUBLE,        -- 利息收入
    asset_impairment_loss    DOUBLE,        -- 资产减值损失
    credit_impairment_loss   DOUBLE,        -- 信用减值损失
    exchange_gain            DOUBLE,        -- 公允价值变动收益
    investment_income        DOUBLE,        -- 投资收益
    -- 其中: 对联营/合营企业投资收益
    operating_profit         DOUBLE,        -- 营业利润
    non_operating_income     DOUBLE,        -- 营业外收入
    non_operating_expenses   DOUBLE,        -- 营业外支出
    total_profit             DOUBLE,        -- 利润总额
    income_tax               DOUBLE,        -- 所得税费用
    net_profit               DOUBLE,        -- 净利润
    parent_net_profit        DOUBLE,        -- 归属于母公司所有者的净利润
    minority_shareholder_profit DOUBLE,     -- 少数股东损益
    deducted_net_profit      DOUBLE,        -- 扣除非经常性损益后的净利润
    basic_eps                DOUBLE,        -- 基本每股收益
    diluted_eps              DOUBLE,        -- 稀释每股收益
    -- 完整原始数据
    raw_data                  JSON,
    PRIMARY KEY (stock_code, report_date)
);

-- 现金流量表
CREATE TABLE IF NOT EXISTS cash_flow (
    stock_code            VARCHAR NOT NULL,
    report_date           DATE NOT NULL,
    report_type           VARCHAR,
    -- 经营活动
    cash_received_sales   DOUBLE,            -- 销售商品提供劳务收到的现金
    taxes_refunded        DOUBLE,            -- 收到的税费返还
    other_operating_cf_in DOUBLE,            -- 收到其他与经营活动有关的现金
    total_operating_cf_in DOUBLE,            -- 经营活动现金流入小计
    cash_paid_goods       DOUBLE,            -- 购买商品接受劳务支付的现金
    cash_paid_employees   DOUBLE,            -- 支付给职工以及为职工支付的现金
    cash_paid_taxes       DOUBLE,            -- 支付的各项税费
    other_operating_cf_out DOUBLE,           -- 支付其他与经营活动有关的现金
    total_operating_cf_out DOUBLE,           -- 经营活动现金流出小计
    cf_from_operating     DOUBLE,            -- 经营活动产生的现金流量净额
    -- 投资活动
    cf_from_investing     DOUBLE,            -- 投资活动产生的现金流量净额
    -- 筹资活动
    cf_from_financing     DOUBLE,            -- 筹资活动产生的现金流量净额
    -- 汇率变动影响
    exchange_rate_effect  DOUBLE,            -- 汇率变动对现金的影响
    cf_net                DOUBLE,            -- 现金及现金等价物净增加额
    -- 期初/期末现金余额
    cash_beginning        DOUBLE,            -- 期初现金及现金等价物余额
    cash_ending           DOUBLE,            -- 期末现金及现金等价物余额
    -- 完整原始数据
    raw_data              JSON,
    PRIMARY KEY (stock_code, report_date)
);

-- 分红记录
CREATE TABLE IF NOT EXISTS dividends (
    stock_code        VARCHAR NOT NULL,
    ex_date           DATE,
    announcement_date DATE,
    dividend_per_share DOUBLE,          -- 每股股息(税前)
    stock_dividend     DOUBLE,          -- 每股送股
    transfer_share     DOUBLE,          -- 每股转增
    rights_issue       DOUBLE,          -- 每股配股
    rights_issue_price DOUBLE,          -- 配股价
    PRIMARY KEY (stock_code, ex_date)
);

-- 除权除息记录
CREATE TABLE IF NOT EXISTS xdxr (
    stock_code   VARCHAR NOT NULL,
    event_date   DATE NOT NULL,
    category     INTEGER,               -- 1=除权除息, 2-10=股本变动, etc.
    fenhong      DOUBLE,
    songzhuangu  DOUBLE,
    peigu        DOUBLE,
    peigujia     DOUBLE,
    PRIMARY KEY (stock_code, event_date, category)
);

-- 指标快照（预计算，筛选核心表）
CREATE TABLE IF NOT EXISTS indicator_snapshot (
    stock_code   VARCHAR NOT NULL,
    report_date  DATE NOT NULL,
    -- 估值
    pe_ttm       DOUBLE,
    pb_mrq       DOUBLE,
    ps_ttm       DOUBLE,
    pcf_ttm      DOUBLE,
    dividend_yield DOUBLE,
    total_market_cap DOUBLE,
    circ_market_cap  DOUBLE,
    -- 盈利
    roe          DOUBLE,
    roa          DOUBLE,
    gross_margin DOUBLE,
    net_margin   DOUBLE,
    roic         DOUBLE,
    cf_to_net_profit DOUBLE,
    -- 成长
    revenue_yoy       DOUBLE,
    net_profit_yoy    DOUBLE,
    deducted_profit_yoy DOUBLE,
    revenue_cagr3     DOUBLE,
    revenue_cagr5     DOUBLE,
    net_profit_cagr3  DOUBLE,
    net_profit_cagr5  DOUBLE,
    -- 安全
    debt_ratio       DOUBLE,
    current_ratio    DOUBLE,
    quick_ratio      DOUBLE,
    interest_bearing_debt DOUBLE,
    interest_coverage DOUBLE,
    goodwill_ratio   DOUBLE,
    -- 股东回报
    payout_ratio     DOUBLE,
    dps              DOUBLE,            -- 每股股息
    consecutive_div_years INTEGER,
    -- 行情
    ma5             DOUBLE,
    ma10            DOUBLE,
    ma20            DOUBLE,
    ma60            DOUBLE,
    ma120           DOUBLE,
    ma250            DOUBLE,
    latest_close     DOUBLE,
    latest_price_date DATE,
    turnover_rate    DOUBLE,            -- 换手率(最近20日平均%)
    calculated_at    TIMESTAMP,
    data_version     VARCHAR,
    PRIMARY KEY (stock_code, report_date)
);

-- 批次级溯源（每次抓取一条记录）
CREATE SEQUENCE IF NOT EXISTS fetch_batch_id_seq;
CREATE TABLE IF NOT EXISTS fetch_batch (
    id              BIGINT PRIMARY KEY DEFAULT nextval('fetch_batch_id_seq'),
    batch_id        VARCHAR NOT NULL,
    data_type       VARCHAR NOT NULL,
    source          VARCHAR NOT NULL,
    adapter_version VARCHAR NOT NULL,
    fetch_time      TIMESTAMP NOT NULL,
    raw_response_hash VARCHAR NOT NULL,
    row_count       INTEGER NOT NULL,
    report_date_range VARCHAR,
    confidence      VARCHAR NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 关键字段级逐值溯源（仅 PRD §14 要求的字段）
CREATE SEQUENCE IF NOT EXISTS source_audit_id_seq;
CREATE TABLE IF NOT EXISTS source_audit (
    id                BIGINT PRIMARY KEY DEFAULT nextval('source_audit_id_seq'),
    stock_code        VARCHAR NOT NULL,
    field_name        VARCHAR NOT NULL,
    report_date       DATE,
    value             DOUBLE,
    source            VARCHAR NOT NULL,
    fetch_batch_id    VARCHAR NOT NULL,
    fetch_time        TIMESTAMP NOT NULL,
    raw_response_hash VARCHAR NOT NULL,
    confidence        VARCHAR NOT NULL,
    reason_code       VARCHAR,
    api_version       VARCHAR,
    is_override       BOOLEAN DEFAULT FALSE,
    override_id       BIGINT,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# ─── SQLite Schema (操作库) ───────────────────────────────────────────

SQLITE_SCHEMA_V1 = """
-- Schema 版本追踪
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- DSL 表达式注册表（版本化，不可变）
CREATE TABLE IF NOT EXISTS dsl_expressions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    version         INTEGER NOT NULL,
    expression_text TEXT NOT NULL,
    ast_json        TEXT,
    status          TEXT NOT NULL DEFAULT 'draft',  -- draft/validated/previewed/published
    description     TEXT,
    direction       TEXT,                             -- higher_is_better / lower_is_better / none
    historical_capable BOOLEAN,
    content_hash    TEXT,                              -- P1-23修复: 独立列存储内容哈希
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(name, version)
);

-- 表达式依赖关系
CREATE TABLE IF NOT EXISTS dsl_dependencies (
    expression_id     INTEGER NOT NULL,
    depends_on_id     INTEGER NOT NULL,
    depends_on_version INTEGER NOT NULL,
    PRIMARY KEY (expression_id, depends_on_id),
    FOREIGN KEY (expression_id) REFERENCES dsl_expressions(id),
    FOREIGN KEY (depends_on_id) REFERENCES dsl_expressions(id)
);

-- 筛选规则
CREATE TABLE IF NOT EXISTS screening_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    version         INTEGER NOT NULL,
    rule_json       TEXT NOT NULL,
    locked_indicators TEXT NOT NULL,    -- JSON: 指标名+版本快照
    status          TEXT NOT NULL DEFAULT 'draft',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(name, version)
);

-- 保存的筛选结果
CREATE TABLE IF NOT EXISTS screening_results (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    title               TEXT NOT NULL,
    note                TEXT,
    rule_id             INTEGER,
    rule_version        INTEGER,
    data_date           TIMESTAMP NOT NULL,
    result_json         TEXT NOT NULL,
    columns_json        TEXT NOT NULL,
    sort_json           TEXT,
    confidence_summary  TEXT,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (rule_id) REFERENCES screening_rules(id)
);

-- 自选列表
CREATE TABLE IF NOT EXISTS watchlist (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code      TEXT NOT NULL,
    group_name      TEXT DEFAULT 'default',
    source_rule_id  INTEGER,
    source_result_id INTEGER,
    added_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (source_rule_id) REFERENCES screening_rules(id),
    FOREIGN KEY (source_result_id) REFERENCES screening_results(id)
);

-- 人工覆写
CREATE TABLE IF NOT EXISTS manual_overrides (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code          TEXT NOT NULL,
    field_name          TEXT NOT NULL,
    report_date         DATE,
    original_value      REAL,
    override_value      REAL NOT NULL,
    reason              TEXT NOT NULL,
    correction_template TEXT,           -- JSON
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    rolled_back_at      TIMESTAMP,
    rolled_back_to      INTEGER,
    status              TEXT DEFAULT 'active'  -- M8-3修复: 专用状态列 (active/rolled_back/published)
);

-- 危险操作计划（两段式确认）
CREATE TABLE IF NOT EXISTS plans (
    plan_id         TEXT PRIMARY KEY,
    operation       TEXT NOT NULL,
    plan_summary    TEXT NOT NULL,      -- JSON
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at      TIMESTAMP NOT NULL,
    status          TEXT DEFAULT 'pending'  -- pending/executed/expired/cancelled
);

-- 任务日志
CREATE TABLE IF NOT EXISTS job_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type    TEXT NOT NULL,
    status      TEXT NOT NULL,           -- running/success/failed
    started_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    details_json TEXT
);

-- 重试列表
CREATE TABLE IF NOT EXISTS retry_list (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code    TEXT NOT NULL,
    data_type     TEXT NOT NULL,
    adapter       TEXT NOT NULL,
    error         TEXT,
    retry_count   INTEGER DEFAULT 0,
    max_retries   INTEGER DEFAULT 5,    -- P2修复: 最大重试次数
    next_retry_at TIMESTAMP,             -- P2修复: 下次重试时间
    last_attempt  TIMESTAMP,
    extra_json    TEXT                    -- JSON: 请求补充参数，如 adjust=qfq
);

-- 缺失列表
CREATE TABLE IF NOT EXISTS missing_list (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code    TEXT NOT NULL,
    field_name    TEXT NOT NULL,
    reason_code   TEXT NOT NULL,
    resolved_at   TIMESTAMP,             -- P2修复: 解决时间
    detected_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- PDF 解析失败任务
CREATE TABLE IF NOT EXISTS pdf_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code      TEXT NOT NULL,
    announcement_id TEXT,
    pdf_hash        TEXT,
    page            INTEGER,
    error           TEXT,
    status          TEXT DEFAULT 'pending'
);

-- 备份记录
CREATE TABLE IF NOT EXISTS backup_registry (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL,           -- full / incremental
    path        TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    encrypted   BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 用户配置
CREATE TABLE IF NOT EXISTS config (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def init_duckdb_schema(store: DuckDBStore) -> None:
    """初始化 DuckDB 分析库 schema"""
    logger.info("初始化 DuckDB schema...")
    store.execute_script(DUCKDB_SCHEMA_V1)
    with store.transaction() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                description VARCHAR NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            "ALTER TABLE price_daily_qfq ADD COLUMN IF NOT EXISTS turnover_rate DOUBLE"
        )
        connection.execute(
            "ALTER TABLE indicator_snapshot ADD COLUMN IF NOT EXISTS calculated_at TIMESTAMP"
        )
        connection.execute(
            "ALTER TABLE indicator_snapshot ADD COLUMN IF NOT EXISTS data_version VARCHAR"
        )
        connection.execute("ALTER TABLE stock_meta ALTER COLUMN is_st DROP DEFAULT")
        connection.execute("ALTER TABLE stock_meta ALTER COLUMN is_suspended DROP DEFAULT")
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (2, 'QFQ turnover_rate and nullable stock status')
            ON CONFLICT (version) DO NOTHING
            """
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (3, 'Indicator calculation timestamp and data version')
            ON CONFLICT (version) DO NOTHING
            """
        )
    logger.info("DuckDB schema 初始化完成")


def init_sqlite_schema(store: SQLiteStore) -> None:
    """初始化 SQLite 操作库 schema + 记录迁移版本"""
    logger.info("初始化 SQLite schema...")

    with store.transaction() as conn:
        # 先创建 schema_migrations 表（如果不存在）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 检查 v1 是否已应用
        row = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = 1"
        ).fetchone()

        if row is None:
            # 执行 v1 迁移：使用 executescript 一次性执行全部 SQL
            # 所有语句都是 CREATE TABLE IF NOT EXISTS，幂等安全
            conn.executescript(SQLITE_SCHEMA_V1)

            conn.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                (1, "初始 schema：DSL/规则/自选/覆写/计划/日志/重试/缺失/PDF/备份/配置"),
            )
            logger.info("SQLite schema v1 已应用")
        else:
            logger.info("SQLite schema v1 已存在，跳过")

        row = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = 2"
        ).fetchone()
        if row is None:
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_manual_overrides_published
                ON manual_overrides (
                    stock_code,
                    field_name,
                    IFNULL(report_date, '')
                )
                WHERE status = 'published' AND rolled_back_at IS NULL
                """
            )
            conn.execute(
                """
                INSERT INTO schema_migrations (version, description)
                VALUES (?, ?)
                """,
                (2, "同一股票、字段和报告期只允许一个当前 published 覆写"),
            )
            logger.info("SQLite schema v2 已应用")

        row = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = 3"
        ).fetchone()
        if row is None:
            retry_columns = {
                column[1] for column in conn.execute("PRAGMA table_info(retry_list)").fetchall()
            }
            if "extra_json" not in retry_columns:
                conn.execute("ALTER TABLE retry_list ADD COLUMN extra_json TEXT")
            conn.execute(
                """
                INSERT INTO schema_migrations (version, description)
                VALUES (?, ?)
                """,
                (3, "重试任务保存标准化请求补充参数"),
            )
            logger.info("SQLite schema v3 已应用")


def init_all_schema() -> None:
    """初始化所有数据库 schema（启动时调用）"""
    duckdb_store = DuckDBStore()
    sqlite_store = SQLiteStore()

    init_duckdb_schema(duckdb_store)
    init_sqlite_schema(sqlite_store)

    logger.info("所有数据库 schema 初始化完成")
