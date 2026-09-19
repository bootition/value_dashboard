"""Schema 定义与迁移机制

版本化 Schema 管理：通过 SQLite 中的 schema_migrations 表跟踪已应用的迁移。
每次启动时检查并执行未应用的迁移。

DuckDB 表：分析数据（价格、财务、指标快照、溯源审计）
SQLite 表：操作数据（DSL、规则、自选、覆写、计划、日志）
"""

from __future__ import annotations

import logging

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

# 当前 schema 版本（reports/79 方案 C 快速启动依据）：
# 任何迁移新增后必须递增对应常量，否则 skip_if_current 会错误跳过待应用迁移。
DUCKDB_SCHEMA_VERSION = 37
SQLITE_SCHEMA_VERSION = 17

# ─── DuckDB Schema (分析库) ───────────────────────────────────────────

DUCKDB_SCHEMA_V1 = """
-- 股票元数据
CREATE TABLE IF NOT EXISTS stock_meta (
    stock_code     VARCHAR PRIMARY KEY,
    name           VARCHAR NOT NULL,
    pinyin         VARCHAR,
    exchange       VARCHAR NOT NULL,   -- SSE / SZSE / BSE
    listing_date   DATE,
    is_listed      BOOLEAN DEFAULT TRUE,
    is_st          BOOLEAN,
    is_suspended   BOOLEAN,
    sw_level1      VARCHAR,            -- 申万一级（缺失为 NULL；已废弃，仅保留追溯）
    sw_level2      VARCHAR,            -- 申万二级（已废弃，仅保留追溯）
    sw_level1_code VARCHAR,
    sw_level2_code VARCHAR,
    csrc_l1        VARCHAR,            -- CSRC（证监会）一级门类（当前行业口径）
    csrc_l2        VARCHAR,            -- CSRC（证监会）二级大类
    total_shares   BIGINT,             -- 总股本（股）
    circ_shares    BIGINT,             -- 流通股本（股）
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
    -- 金融行业监管指标（不适用或来源不可得时保留 NULL，并在 missing_list 记录原因）
    core_tier1_capital_adequacy_ratio DOUBLE, -- 核心一级资本充足率(%)
    tier1_capital_adequacy_ratio      DOUBLE, -- 一级资本充足率(%)
    capital_adequacy_ratio            DOUBLE, -- 资本充足率(%)
    non_performing_loan_ratio         DOUBLE, -- 不良贷款率(%)
    provision_coverage_ratio          DOUBLE, -- 拨备覆盖率(%)
    risk_coverage_ratio               DOUBLE, -- 证券公司风险覆盖率(%)
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
    deducted_profit_cagr3 DOUBLE,
    deducted_profit_cagr5 DOUBLE,
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
    -- 分红融资比数据前置（2026-08-25，reports/82 后续指标）
    cumulative_dividend_amount DOUBLE,   -- A股累计现金分红金额（元，按A股流通股本折算，H股不计入）
    cumulative_financing_amount DOUBLE,  -- A股累计股权融资金额（元，IPO+增发+配股，优先募资额/净额）
    dividend_financing_ratio_pct DOUBLE, -- 分红融资比（%，A股口径）：广义分红/股权融资 × 100
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
    avg_volume       DOUBLE,
    period_return    DOUBLE,
    annualized_volatility DOUBLE,
    max_drawdown     DOUBLE,
    -- 国债基准与股息率利差（reports/68 P3：TTM已实施股息率与相对各期限利差）
    ttm_dividend_yield DOUBLE,          -- TTM已实施现金股息率(%)
    div_yield_spread_0p25y DOUBLE,      -- 相对0.25年期国债利差(%)
    div_yield_spread_0p5y DOUBLE,       -- 相对0.5年期国债利差(%)
    div_yield_spread_1y DOUBLE,         -- 相对1年期国债利差(%)
    div_yield_spread_2y DOUBLE,         -- 相对2年期国债利差(%)
    div_yield_spread_3y DOUBLE,         -- 相对3年期国债利差(%)
    div_yield_spread_5y DOUBLE,         -- 相对5年期国债利差(%)
    div_yield_spread_7y DOUBLE,         -- 相对7年期国债利差(%)
    div_yield_spread_10y DOUBLE,        -- 相对10年期国债利差(%)
    div_yield_spread_30y DOUBLE,        -- 相对30年期国债利差(%)
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
CREATE INDEX IF NOT EXISTS idx_source_audit_stock_date_field
    ON source_audit (stock_code, report_date, field_name);
CREATE INDEX IF NOT EXISTS idx_source_audit_fetch_batch
    ON source_audit (fetch_batch_id);
CREATE INDEX IF NOT EXISTS idx_source_audit_hash
    ON source_audit (raw_response_hash);

-- Legacy records removed from active research remain available as evidence.
CREATE TABLE IF NOT EXISTS source_audit_quarantine (
    id                BIGINT PRIMARY KEY,
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
    created_at        TIMESTAMP,
    effective_date    DATE,
    data_version      VARCHAR,
    formula           VARCHAR,
    quarantine_reason VARCHAR NOT NULL,
    quarantined_at    TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS dividends_quarantine (
    stock_code        VARCHAR NOT NULL,
    ex_date           DATE,
    announcement_date DATE,
    dividend_per_share DOUBLE,
    stock_dividend     DOUBLE,
    transfer_share     DOUBLE,
    rights_issue       DOUBLE,
    rights_issue_price DOUBLE,
    quarantine_reason  VARCHAR NOT NULL,
    quarantined_at     TIMESTAMP NOT NULL,
    PRIMARY KEY (stock_code, ex_date)
);

-- Immutable source material retained by content hash for traceability and repair.
CREATE TABLE IF NOT EXISTS raw_response_archive (
    raw_response_hash VARCHAR PRIMARY KEY,
    source            VARCHAR NOT NULL,
    fetch_time        TIMESTAMP NOT NULL,
    payload           BLOB,
    api_version       VARCHAR,
    integrity_verified BOOLEAN DEFAULT FALSE,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 个股业务概览（reports/67 独立低频域）──────────────────────────────────
-- 独立于 stock_meta / indicator_snapshot / readiness；失败保留旧值，
-- 不进筛选池，不阻断日常价格、财务与 A 股 readiness。
CREATE TABLE IF NOT EXISTS company_profile (
    stock_code     VARCHAR PRIMARY KEY,
    code           VARCHAR,
    name           VARCHAR,
    org_name       VARCHAR,
    profile        TEXT,               -- 公司简介（事实概览，confidence=approximate）
    scope          TEXT,               -- 经营范围
    employee_num   BIGINT,
    csrc_industry  VARCHAR,
    trade_market   VARCHAR,
    source         VARCHAR NOT NULL,   -- 来源（eastmoney_f10）
    fetch_time     TIMESTAMP NOT NULL, -- 抓取时间
    raw_hash       VARCHAR NOT NULL,   -- 原始响应 SHA-256
    confidence     VARCHAR NOT NULL,   -- approximate / missing
    batch_id       VARCHAR NOT NULL,   -- 批次
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS business_breakdown (
    stock_code   VARCHAR NOT NULL,
    report_date  DATE NOT NULL,        -- 主营构成报告期
    type         INTEGER NOT NULL,     -- 1=产品 2=行业 3=地区
    item_name    VARCHAR NOT NULL,
    amount       DOUBLE,               -- 主营收入金额
    ratio        DOUBLE,               -- 占比(%)
    rank         INTEGER,
    source       VARCHAR NOT NULL,
    fetch_time   TIMESTAMP NOT NULL,
    raw_hash     VARCHAR NOT NULL,
    confidence   VARCHAR NOT NULL,
    batch_id     VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date, type, item_name)
);
CREATE INDEX IF NOT EXISTS idx_business_breakdown_stock_report
    ON business_breakdown (stock_code, report_date);

-- 财政部-中国国债收益率曲线（reports/68 P3 独立低频基准域）
-- 每个点保存曲线日期、期限、收益率、来源、抓取时间、原始响应哈希和置信度；
-- 独立于 A 股 stock_meta / 价格 / 财报 / 筛选池 / readiness，失败不得阻塞股票研究。
CREATE TABLE IF NOT EXISTS treasury_yield_curve (
    curve_date   DATE NOT NULL,        -- 曲线日期（交易日）
    tenor_years  DOUBLE NOT NULL,      -- 期限（年），如 10.0 / 0.25
    yield_pct    DOUBLE NOT NULL,      -- 收益率（%）
    source       VARCHAR NOT NULL,     -- 来源（czb_mof）
    fetch_time   TIMESTAMP NOT NULL,   -- 抓取时间
    raw_hash     VARCHAR NOT NULL,     -- 原始响应 SHA-256
    confidence   VARCHAR NOT NULL,     -- strict / approximate / missing
    batch_id     VARCHAR NOT NULL,     -- 批次
    PRIMARY KEY (curve_date, tenor_years)
);
CREATE INDEX IF NOT EXISTS idx_treasury_yield_curve_date
    ON treasury_yield_curve (curve_date);

-- 历史总股本链（P4，reports/68 §3 主链：CNINFO p_stock2215）
-- 半年/年报期末锚点 + 变动事件共同构成历史骨架；verified 由东财 F10 近邻交叉核验。
-- 仅用于历史 PE/PB 研究序列，不替代 stock_meta 的当前股本；缺失日 fail-closed。
CREATE TABLE IF NOT EXISTS share_capital_history (
    stock_code     VARCHAR NOT NULL,
    effective_date DATE NOT NULL,     -- 生效日（变动日期或定期报告锚点日）
    total_shares   DOUBLE NOT NULL,   -- 总股本（股）
    change_reason  VARCHAR,           -- 变动原因（CN 变动原因简称，锚点为 NULL）
    is_anchor      BOOLEAN,           -- 是否定期报告期末锚点
    verified       BOOLEAN,           -- 是否经东财近邻交叉核验无冲突
    source         VARCHAR NOT NULL,  -- cninfo_capital
    raw_hash       VARCHAR NOT NULL,
    batch_id       VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, effective_date)
);
CREATE INDEX IF NOT EXISTS idx_share_capital_history_stock
    ON share_capital_history (stock_code, effective_date);

-- 历史研究统计域（P4，reports/68 §5/§6 独立只读域）
-- 每股票×序列×窗口×方法一行；staging→原子发布，版本+输入指纹；筛选只能 join 已发布域。
CREATE TABLE IF NOT EXISTS research_statistics (
    stock_code       VARCHAR NOT NULL,
    metric           VARCHAR NOT NULL,   -- pe_ttm / pb_mrq / ttm_dividend_yield / spread_10y
    window_years     INTEGER NOT NULL,   -- 1/3/5/10/99(全部)
    method           VARCHAR NOT NULL,   -- percentile / zscore
    value            DOUBLE,             -- 当前值的历史分位(0-100) 或 z-score
    samples          INTEGER,            -- 有效样本数
    coverage_pct     DOUBLE,             -- 有效日 / 有行情日（%）
    min_date         DATE,
    max_date         DATE,
    reason           VARCHAR,            -- 不可用时原因码
    version          INTEGER NOT NULL,
    input_fingerprint VARCHAR NOT NULL,
    published_at     TIMESTAMP NOT NULL,
    PRIMARY KEY (stock_code, metric, window_years, method, version)
);
CREATE INDEX IF NOT EXISTS idx_research_statistics_lookup
    ON research_statistics (metric, window_years, method, version);

-- 融资事件域（2026-08-25，数据补全：分红融资比指标的数据前置）
-- 覆盖 IPO 首发 / A 股增发 / 配股三类历史融资事件；
-- 募资额缺失绝不伪造：增发由 ISSUE_NUM×ISSUE_PRICE 推算时 derived=true 如实标注；
-- 独立于 stock_meta / 筛选池 / readiness，失败保留旧值并记录独立 retry/missing。
-- 注意：东财 F10 会把一次增发按发行对象拆成多条同 list_date 记录（如
-- 000008 2015-02-05 两条、600900 2016-04-15 两条，价同量不同），故不设
-- 复合主键，仅以 stock_code 索引 + 单股原子替换（DELETE→INSERT）保证无累积。
CREATE TABLE IF NOT EXISTS funding_events (
    stock_code      VARCHAR NOT NULL,
    event_type      VARCHAR NOT NULL,   -- ipo / a_placement(增发) / rights(配股)
    announce_date   DATE,               -- 发行公告日（IPO 招股公告 / 增发上市公告 / 配股上市公告）
    list_date       DATE,               -- 上市日 / 股份上市日
    issue_price     DOUBLE,             -- 发行价 / 增发价 / 配股价（元）
    issue_shares    DOUBLE,             -- 发行数量（股）
    raise_funds     DOUBLE,             -- 募资总额（元）；null 时由 price×shares 推算并记 derived=true
    raise_funds_net DOUBLE,             -- 募资净额（元，仅 CNINFO IPO 可得）
    derived         BOOLEAN DEFAULT FALSE, -- true=raise_funds 为 price×shares 推算值
    source          VARCHAR NOT NULL,   -- cninfo_funding / eastmoney_f10
    fetch_time      TIMESTAMP NOT NULL,
    raw_hash        VARCHAR NOT NULL,
    confidence      VARCHAR NOT NULL,   -- strict / approximate / missing
    batch_id        VARCHAR NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_funding_events_stock
    ON funding_events (stock_code);

-- 回购/注销事件域（2026-08-26，分红融资比“广义分红”数据补充）
-- 东财回购明细全市场单次低频拉取；金额为“已回购金额”（元），
-- 用于把回购注销纳入广义分红，弥补 funding_events 只覆盖融资侧的缺口。
CREATE TABLE IF NOT EXISTS buyback_events (
    stock_code      VARCHAR NOT NULL,
    start_date      DATE,
    announce_date   DATE,
    buyback_shares  DOUBLE,
    buyback_amount  DOUBLE,
    progress        VARCHAR,
    source          VARCHAR NOT NULL,
    fetch_time      TIMESTAMP NOT NULL,
    raw_hash        VARCHAR NOT NULL,
    confidence      VARCHAR NOT NULL,
    batch_id        VARCHAR NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_buyback_events_stock
    ON buyback_events (stock_code);

-- 港股分红域（2026-09-04，总市场分红融资比数据前置之一）
-- 仅覆盖 stock_zh_ah_spot() 能映射到 A 股 stock_meta 的 A+H 公司；
-- 独立于 A 股 dividends / stock_meta / indicator_snapshot / readiness。
-- stock_code 为 5 位港股代码（如 00941），A 股映射由
-- app/core/ah_hk_mapping.py 维护，不在本表内冗余。
CREATE TABLE IF NOT EXISTS hk_dividends (
    stock_code              VARCHAR NOT NULL,
    ex_date                 DATE,
    announcement_date       DATE,
    report_period           VARCHAR,
    plan_explain            VARCHAR,
    dividend_per_share_hkd  DOUBLE,
    dividend_per_share_cny  DOUBLE,
    transfer_end_date       VARCHAR,
    dividend_date           DATE,
    source                  VARCHAR NOT NULL,
    fetch_time              TIMESTAMP NOT NULL,
    raw_response_hash       VARCHAR NOT NULL,
    confidence              VARCHAR NOT NULL,
    batch_id                VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, ex_date, plan_explain)
);
CREATE INDEX IF NOT EXISTS idx_hk_dividends_stock
    ON hk_dividends (stock_code);

-- 指数估值域（2026-08-25，数据补全：沪深300 ERP 指标的数据前置）
-- 2026-09-05 v21：扩展为多指数四源——乐咕宽基月度 PE/PB（主）、申万一级行业
-- 日度 PE/PB（sws 主）、中证官网交叉、同花顺补充；pe_metric 记录口径，
-- extra 为 JSON 字符串（点位/静态PE/中位数等附加字段）。
CREATE TABLE IF NOT EXISTS index_valuation (
    index_code   VARCHAR NOT NULL,      -- 000300 / SW801010 等
    trade_date   DATE NOT NULL,
    pe_ttm       DOUBLE,                -- 市盈率（口径见 pe_metric）
    pe_metric    VARCHAR,               -- ttm / static / sws_daily / null=未披露
    pb           DOUBLE,                -- 市净率
    div_yield    DOUBLE,                -- 股息率(%)
    source       VARCHAR NOT NULL,      -- legulegu / sws / csindex / ths
    fetch_time   TIMESTAMP NOT NULL,
    raw_hash     VARCHAR NOT NULL,
    confidence   VARCHAR NOT NULL,      -- strict / approximate / missing
    batch_id     VARCHAR NOT NULL,
    extra        VARCHAR,               -- 附加字段 JSON 字符串
    PRIMARY KEY (index_code, trade_date, source)
);
CREATE INDEX IF NOT EXISTS idx_index_valuation_code
    ON index_valuation (index_code, trade_date);

-- ETF 日线行情域（2026-09-05，同花顺官方 Financial-API 主源）
-- 供 ETF 轮动工作台的网格价/持仓盈亏计算；源失败保留旧值，不阻断个股主链。
-- track_pe_ttm_five_year_percentile 为同花顺"跟踪指数 PE-TTM 五年分位"，
-- 用于无指数估值历史的 ETF（港股/中概等）兜底信号。
CREATE TABLE IF NOT EXISTS etf_daily (
    etf_code    VARCHAR NOT NULL,      -- 510300（无交易所后缀）
    trade_date  DATE NOT NULL,
    close_price DOUBLE,
    open_price  DOUBLE,
    high_price  DOUBLE,
    low_price   DOUBLE,
    volume      DOUBLE,
    turnover    DOUBLE,
    track_pe_ttm_five_year_percentile DOUBLE,
    source      VARCHAR NOT NULL,      -- ths
    fetch_time  TIMESTAMP NOT NULL,
    raw_hash    VARCHAR NOT NULL,
    confidence  VARCHAR NOT NULL,
    batch_id    VARCHAR NOT NULL,
    PRIMARY KEY (etf_code, trade_date, source)
);
CREATE INDEX IF NOT EXISTS idx_etf_daily_code
    ON etf_daily (etf_code, trade_date);

-- 间接法现金流量表（2026-09-19 v24，Phase B 数据层）
-- 来源：CSMAR C17 的 FS_Comscfi.dta（216,862 行，1997-06-30 ~ 2025-03-31），
-- 属上市公司原始披露科目（非二次计算值），按「三色灯」green 直接导入。
-- 域纪律：独立低频域，不进入 A 股 readiness；自由现金流、杠杆（EBITDA/EBIT）
-- 与折旧摊销类指标依赖本表；缺失时相关指标必须如实 NULL，不得回退估算。
-- 列名语义见 config/csmar_field_verdict.json（裁定表只含本项目判定，不含原文）。
CREATE TABLE IF NOT EXISTS cash_flow_indirect (
    stock_code                          VARCHAR NOT NULL,
    report_date                         DATE    NOT NULL,
    report_type                         VARCHAR,
    net_profit                          DOUBLE,
    credit_impairment_loss              DOUBLE,
    unconfirmed_investment_loss         DOUBLE,
    asset_impairment_provision          DOUBLE,
    fixed_asset_depreciation            DOUBLE,
    investment_property_depreciation    DOUBLE,
    right_of_use_asset_depreciation     DOUBLE,
    intangible_asset_amortization       DOUBLE,
    long_term_prepaid_amortization      DOUBLE,
    disposal_long_term_asset_loss       DOUBLE,
    fixed_asset_scrap_loss              DOUBLE,
    fair_value_change_loss              DOUBLE,
    financial_expense                   DOUBLE,
    investment_loss                     DOUBLE,
    deferred_tax_asset_decrease         DOUBLE,
    deferred_tax_liability_increase     DOUBLE,
    inventory_decrease                  DOUBLE,
    operating_receivable_decrease       DOUBLE,
    operating_payable_increase          DOUBLE,
    other_adjustment                    DOUBLE,
    cf_from_operating_indirect          DOUBLE,
    debt_to_capital                     DOUBLE,
    convertible_bond_due_within_1y      DOUBLE,
    finance_lease_fixed_assets          DOUBLE,
    cash_ending_balance                 DOUBLE,
    cash_beginning_balance              DOUBLE,
    cash_equivalent_ending              DOUBLE,
    cash_equivalent_beginning           DOUBLE,
    cash_equivalent_net_increase        DOUBLE,
    source                              VARCHAR NOT NULL,
    fetch_time                          TIMESTAMP NOT NULL,
    raw_response_hash                   VARCHAR NOT NULL,
    confidence                          VARCHAR NOT NULL,
    batch_id                            VARCHAR NOT NULL,
    raw_data                            VARCHAR,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_cash_flow_indirect_date
    ON cash_flow_indirect (report_date);

-- 财报公布日期域（2026-09-19 v24，Phase B「时点可见性」）
-- 来源：CSMAR C17 的 FAR_Finidx.Annodt（1990-2024 年报）。实测覆盖率 97.7%
-- （74,509/76,262 条有效；1,753 条缺失集中在 2014-2021 年），缺失如实登记不补齐。
-- 用途：本项目历史研究此前只能采用「最新重述回看」口径（用今天才知道的数字判断
-- 当年），PRD §8.1 明确标注「非当时可见、不用于回测」。本表提供每份年报的公开日，
-- 使「当时可见」口径成为可能（年度频率；季报/中报公布日 CSMAR 不提供）。
-- 纪律：本表只描述「何时公开」，不改变任何财务数值口径。
CREATE TABLE IF NOT EXISTS financial_report_dates (
    stock_code     VARCHAR NOT NULL,
    report_date    DATE    NOT NULL,
    announce_date  DATE    NOT NULL,
    source         VARCHAR NOT NULL,
    fetch_time     TIMESTAMP NOT NULL,
    batch_id       VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date, source)
);
CREATE INDEX IF NOT EXISTS idx_financial_report_dates_announce
    ON financial_report_dates (announce_date);
CREATE INDEX IF NOT EXISTS idx_financial_report_dates_stock
    ON financial_report_dates (stock_code, report_date);

-- 投资/筹资活动现金流量补充域（2026-09-19 v25，Phase B）
-- 来源：CSMAR C17 的 FS_Comscfd.dta（直接法现金流量表）中本项目此前未映射的
-- 投资/筹资活动科目。主要为自由现金流提供「资本支出」输入
-- （资本支出 = 购建固定资产、无形资产和其他长期资产支付的现金），
-- 同时可用于交叉核验分红总额（分配股利、利润或偿付利息支付的现金）
-- 与融资活动流水（对应 funding_events）。
-- 域纪律：独立低频域；不进入 A 股 readiness；不修改 cash_flow 主表。
CREATE TABLE IF NOT EXISTS cash_flow_activity (
    stock_code                    VARCHAR NOT NULL,
    report_date                   DATE    NOT NULL,
    report_type                   VARCHAR,
    capex                         DOUBLE,   -- 购建固定资产、无形资产和其他长期资产支付的现金
    investment_recovered          DOUBLE,   -- 收回投资收到的现金
    investment_income_cash        DOUBLE,   -- 取得投资收益收到的现金
    disposal_long_asset_cash      DOUBLE,   -- 处置固定资产、无形资产和其他长期资产收回的现金净额
    disposal_subsidiary_cash      DOUBLE,   -- 处置子公司及其他营业单位收到的现金净额
    other_investing_inflow        DOUBLE,   -- 收到的其他与投资活动有关的现金
    investing_inflow_total        DOUBLE,   -- 投资活动现金流入小计
    investment_paid               DOUBLE,   -- 投资支付的现金
    acquire_subsidiary_cash       DOUBLE,   -- 取得子公司及其他营业单位支付的现金净额
    other_investing_outflow       DOUBLE,   -- 支付其他与投资活动有关的现金
    investing_outflow_total       DOUBLE,   -- 投资活动现金流出小计
    equity_investment_received    DOUBLE,   -- 吸收权益性投资收到的现金
    borrow_received               DOUBLE,   -- 取得借款收到的现金
    bond_issued                   DOUBLE,   -- 发行债券收到的现金
    other_financing_inflow        DOUBLE,   -- 收到其他与筹资活动有关的现金
    financing_inflow_total        DOUBLE,   -- 筹资活动现金流入小计
    debt_repaid                   DOUBLE,   -- 偿还债务支付的现金
    dividend_interest_paid        DOUBLE,   -- 分配股利、利润或偿付利息支付的现金
    other_financing_outflow       DOUBLE,   -- 支付其他与筹资活动有关的现金
    financing_outflow_total       DOUBLE,   -- 筹资活动现金流出小计
    other_cash_effect             DOUBLE,   -- 其他对现金的影响
    source                        VARCHAR NOT NULL,
    fetch_time                    TIMESTAMP NOT NULL,
    raw_response_hash             VARCHAR NOT NULL,
    confidence                    VARCHAR NOT NULL,
    batch_id                      VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_cash_flow_activity_date
    ON cash_flow_activity (report_date);

-- 扩展指标域（2026-09-19 v25，Phase B「缺失指标族」）
-- 用途：承载本项目此前完全没有、且 CSMAR C17 也无法直接采用（三色灯 yellow）的
-- 指标族。按「先接间接法、再自算、CSMAR 值作核验」的裁定，本表数值全部由本项目
-- 依据 config/csmar_field_verdict.json 记录的口径自算，source='derived_calculator'。
-- 列族：
--   turnover_*   周转率族（口径：累计营业收入或营业成本 ÷ 期末余额，CSMAR FI_T4「A」式）
--   ebit/ebitda  息税前利润 / 息税折旧摊销前利润（CSMAR FI_T5 明确定义的公式）
--   leverage_*   财务/经营/综合杠杆（CSMAR FI_T7 明确定义的公式）
--   fcf_*        自由现金流（本项目口径：经营现金流净额 − 资本支出，已在列注释标注）
-- 依赖：turnover 仅依赖 balance_sheet/income_statement；ebitda/leverage/fcf 另需
-- cash_flow_indirect 与 cash_flow_activity，缺失时对应列为 NULL（如实缺失，不估算）。
-- 域纪律：独立低频域，不进入 indicator_snapshot 主链，不影响 A 股 readiness。
CREATE TABLE IF NOT EXISTS indicator_ext (
    stock_code                    VARCHAR NOT NULL,
    report_date                   DATE    NOT NULL,
    -- 周转率族（CSMAR FI_T4「A」式：累计损益 ÷ 期末余额）
    receivables_turnover          DOUBLE,   -- 应收账款周转率
    inventory_turnover            DOUBLE,   -- 存货周转率
    accounts_payable_turnover     DOUBLE,   -- 应付账款周转率
    current_asset_turnover        DOUBLE,   -- 流动资产周转率
    fixed_asset_turnover          DOUBLE,   -- 固定资产周转率
    total_asset_turnover          DOUBLE,   -- 总资产周转率
    equity_turnover               DOUBLE,   -- 股东权益周转率
    operating_cycle_days          DOUBLE,   -- 营业周期（天）= 应收周转天数 + 存货周转天数
    -- 现金流与自由现金流族
    depreciation_amortization     DOUBLE,   -- 折旧摊销（固定资产折旧+无形资产摊销+长期待摊费用摊销）
    capex                         DOUBLE,   -- 资本支出
    operating_cash_flow           DOUBLE,   -- 经营活动现金流量净额
    free_cash_flow                DOUBLE,   -- 自由现金流（本项目口径：经营现金流净额 − 资本支出）
    fcf_margin                    DOUBLE,   -- 自由现金流 / 营业收入
    -- 利润与杠杆族
    ebit                          DOUBLE,   -- 息税前利润 = 净利润+所得税费用+财务费用
    ebitda                        DOUBLE,   -- 息税折旧摊销前利润 = EBIT + 折旧摊销
    leverage_financial            DOUBLE,   -- 财务杠杆 = EBIT / 利润总额
    leverage_operating            DOUBLE,   -- 经营杠杆 = EBITDA / EBIT
    leverage_total                DOUBLE,   -- 综合杠杆 = EBITDA / 利润总额
    calculated_at                 TIMESTAMP NOT NULL,
    source                        VARCHAR NOT NULL,
    data_version                  INTEGER NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_indicator_ext_date
    ON indicator_ext (report_date);
-- 资产负债表补充域（2026-09-19 v27，"榨干数据包" R7）。
-- 为什么单独建表：这三项是主链 balance_sheet 未映射、但价值投资常用且**无法自算**的科目。
--   other_payables          其他应付款 —— 计算「大股东占款」（治理红旗）必需；
--                           本项目已有 other_receivables，缺的正是应付这一侧。
--   non_current_liab_due_1y 一年内到期的非流动负债 —— 有息负债完整口径必需
--                           （主链 interest_bearing_debt 目前仅 短借+长借+应付债券，低估）。
--   total_other_receivable  其他应收款合计 —— 与 other_payables 同源配对，
--                           避免主链 other_receivables（净额）与应付口径不匹配。
-- 来源：东方财富 F10 zcfzbAjaxNew（1 次请求/股票，5 个报告期），
-- 见 scripts/fetch_balance_sheet_ext.py。域纪律：独立低频域，不改主链 balance_sheet。
CREATE TABLE IF NOT EXISTS balance_sheet_ext (
    stock_code                VARCHAR NOT NULL,
    report_date               DATE    NOT NULL,
    report_type               VARCHAR,
    other_payables            DOUBLE,
    non_current_liab_due_1y   DOUBLE,
    total_other_receivable    DOUBLE,
    source                    VARCHAR NOT NULL,
    fetch_time                TIMESTAMP NOT NULL,
    raw_response_hash         VARCHAR NOT NULL,
    confidence                VARCHAR NOT NULL,
    batch_id                  VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_balance_sheet_ext_date
    ON balance_sheet_ext (report_date);

-- 员工人数历史域（2026-09-19 v28，"榨干数据包" R8）。
-- 用途：计算「人均创收 / 人均创利」——判断公司是真高效还是靠堆人。
-- 为什么需要历史序列：本项目 company_profile.employee_num 是**当前快照**，
-- 用它去除历史期收入会把「今天的员工数」套到「当年的收入」上（同类口径错误
-- 参见 ops-knowledge-base D23）。故历史期用 CSMAR FAR_Finidx.Nstaff 的
-- 逐年员工数，当前期回退到 company_profile.employee_num。
-- 来源：CSMAR C17 FAR_Finidx.Nstaff（1990-2024）+ company_profile（当前）。
CREATE TABLE IF NOT EXISTS company_employee_history (
    stock_code      VARCHAR NOT NULL,
    report_date     DATE    NOT NULL,
    employee_count  BIGINT,
    source          VARCHAR NOT NULL,
    fetch_time      TIMESTAMP NOT NULL,
    batch_id        VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date, source)
);
CREATE INDEX IF NOT EXISTS idx_company_employee_history_stock
    ON company_employee_history (stock_code, report_date);

-- 金融行业专用科目域（2026-09-19 v29，"榨干数据包" R9）。
-- 来源：CSMAR C17 的 FS_Combas / FS_Comins 中 A0b*(银行) / A0i*(保险) /
-- A0d*(证券) / A0f*(其他金融) 前缀科目，覆盖 78 只金融股（38 银行 + 36 券商 +
-- 1 保险 + 1 信托 + 2 金控），1990-2025Q1。
--
-- **为什么不进筛选界面**：银行/保险/证券的科目名称与含义只对该行业成立，
-- 对另外 5,464 只非金融股毫无意义。把它们放进全市场字段选择器，只会制造
-- 「选中后 98% 的股票都无数据」的伪条件 —— 与死条件同样有害。
-- 故本域定位为**行业研究域**：供个股详情/专项分析按行业取用，不做横截面筛选。
--
-- **已知不可得**：6 个监管比率（资本充足率/核心一级/一级/不良贷款率/
-- 拨备覆盖率/风险覆盖率）**CSMAR C17 不含**，东财 F10 的可及端点也未提供。
-- 主链 balance_sheet 的这 6 列保持 NULL 并如实披露，不用估算填充。
CREATE TABLE IF NOT EXISTS financial_sector_items (
    stock_code                     VARCHAR NOT NULL,
    report_date                    DATE    NOT NULL,
    report_type                    VARCHAR,
    -- 银行
    cash_and_cb_balance            DOUBLE,   -- 现金及存放中央银行款项
    due_from_banks                 DOUBLE,   -- 存放同业款项
    loans_and_advances             DOUBLE,   -- 发放贷款及垫款净额
    borrowing_from_cb              DOUBLE,   -- 向中央银行借款
    deposits_and_interbank         DOUBLE,   -- 吸收存款及同业存放
    interbank_deposits             DOUBLE,   -- 其中：同业及其他金融机构存放款项
    customer_deposits              DOUBLE,   -- 其中：吸收存款
    interest_income                DOUBLE,   -- 利息收入
    interest_expense               DOUBLE,   -- 利息支出
    net_interest_income            DOUBLE,   -- 利息净收入
    -- 保险
    premiums_receivable            DOUBLE,   -- 应收保费净额
    insurance_contract_reserve     DOUBLE,   -- 保险合同准备金
    policyholder_deposits          DOUBLE,   -- 保户储金及投资款
    earned_premiums                DOUBLE,   -- 已赚保费
    claim_payments_net             DOUBLE,   -- 赔付支出净额
    -- 证券
    settlement_reserve             DOUBLE,   -- 结算备付金
    customer_settlement_reserve    DOUBLE,   -- 其中：客户备付金
    margin_deposits_paid           DOUBLE,   -- 存出保证金
    client_securities_deposits     DOUBLE,   -- 代理买卖证券款
    underwriting_securities        DOUBLE,   -- 代理承销证券款
    fee_commission_income_net      DOUBLE,   -- 手续费及佣金净收入
    -- 其他金融（通用）
    interbank_lending              DOUBLE,   -- 拆出资金净额
    reverse_repo_assets            DOUBLE,   -- 买入返售金融资产净额
    interbank_borrowing            DOUBLE,   -- 拆入资金
    repo_liabilities               DOUBLE,   -- 卖出回购金融资产款
    source                         VARCHAR NOT NULL,
    fetch_time                     TIMESTAMP NOT NULL,
    raw_response_hash              VARCHAR NOT NULL,
    confidence                     VARCHAR NOT NULL,
    batch_id                       VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_financial_sector_items_stock
    ON financial_sector_items (stock_code, report_date);


-- CSMAR 每股指标历史域（2026-09-19 v31，"榨干数据包" R11）。
-- 来源：CSMAR C17 FI_T9.dta（每股指标，303,065 行，1990-2025Q1）。
--
-- 定位：**交叉核验源**。本项目已自算 bps / revenue_per_share / ocf_per_share /
-- retained_earnings_per_share（indicator_ext，覆盖最新报告期）；FI_T9 是
-- CSMAR 用其自有股本口径独立算出的一套，可用于验证我们自算值是否可靠，
-- 并提供我们未自算的每股明细（每股有形资产/负债/资本公积/盈余公积等）。
--
-- **不进筛选界面**：与 indicator_ext 的每股族语义重叠，且数据截止 2025Q1；
-- 放进字段表会让用户在同一概念上看到两套口径相近却不同的数（伪选择）。
-- 域纪律：独立历史域，只作核验与专项研究，不参与横截面筛选。
CREATE TABLE IF NOT EXISTS csmar_per_share_history (
    stock_code                     VARCHAR NOT NULL,
    report_date                    DATE    NOT NULL,
    bps                            DOUBLE,   -- F091001A 每股净资产1
    bps_parent                     DOUBLE,   -- F091701A 归属母公司每股净资产1
    revenue_per_share              DOUBLE,   -- F090501B 每股营业总收入1
    ocf_per_share                  DOUBLE,   -- F091801B 每股经营活动现金流量净额1
    operating_profit_per_share     DOUBLE,   -- F090901B 每股营业利润1
    ebit_per_share                 DOUBLE,   -- F090701B 息税前每股收益1
    tangible_asset_per_share       DOUBLE,   -- F091101A 每股有形资产1
    liability_per_share            DOUBLE,   -- F091201A 每股负债1
    capital_reserve_per_share      DOUBLE,   -- F091301A 每股资本公积1
    surplus_reserve_per_share      DOUBLE,   -- F091401A 每股盈余公积1
    undistributed_profit_per_share DOUBLE,   -- F091501A 每股未分配利润1
    retained_earnings_per_share    DOUBLE,   -- F091601A 每股留存收益1
    source                         VARCHAR NOT NULL,
    fetch_time                     TIMESTAMP NOT NULL,
    batch_id                       VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_csmar_per_share_history_stock
    ON csmar_per_share_history (stock_code, report_date);


-- CSMAR 披露指标域（2026-09-19 v33）。来源 FI_T2.dta（243,202 行）。
-- 为什么单独建：这些是**上市公司年报财务摘要里的官方披露值**（非 CSMAR 二次计算），
-- 与自算口径不同，尤其「加权平均ROE」是证监会/交易所标准披露口径
-- （本项目的 roe 是简单口径：TTM 归母净利 / 平均归母权益）。
--   non_recurring_gain_loss  非经常性损益
--   roe_weighted             加权平均净资产收益率（官方口径）
--   roe_weighted_deducted    扣非加权平均ROE
--   eps_deducted_basic       扣非基本每股收益
--   ocf_per_share_disclosed  每股经营活动现金流量净额
--   bps_parent_disclosed     归属母公司每股净资产
--   eps_basic / eps_diluted  基本/稀释每股收益
-- 域纪律：独立域；duplicates 自算指标时只作交叉核验，不替换主链。
CREATE TABLE IF NOT EXISTS csmar_disclosure_metrics (
    stock_code                  VARCHAR NOT NULL,
    report_date                 DATE    NOT NULL,
    non_recurring_gain_loss     DOUBLE,
    roe_weighted                DOUBLE,
    roe_weighted_deducted       DOUBLE,
    eps_deducted_basic          DOUBLE,
    ocf_per_share_disclosed     DOUBLE,
    bps_parent_disclosed        DOUBLE,
    eps_basic                   DOUBLE,
    eps_diluted                 DOUBLE,
    source                      VARCHAR NOT NULL,
    fetch_time                  TIMESTAMP NOT NULL,
    batch_id                    VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_csmar_disclosure_metrics_stock
    ON csmar_disclosure_metrics (stock_code, report_date);

-- CSMAR 风险与治理因子域（2026-09-19 v33）。来源 BDT_FinIndex.dta（67,193 行）。
-- 这批的核心价值是**需要多年序列才能自算**或**需要我们没有的原始科目**的因子：
--   profits_volatility_3y    盈利波动性（(EBIT/总资产) 三年滚动标准差）
--   cashflow_volatility_3y   现金流波动性（(现金流/总资产) 三年滚动标准差）
--   non_debt_tax_shield      非债务税盾（折旧/总资产）
--   tax_bearing              税负
--   bank_loan_ratio          银行借款比例
--   short_loan_dependence    短期借款依赖度
--   shareholders_occupy      大股东占款（与 indicator_ext.shareholder_occupation 交叉核验）
--   financial_liability      金融负债 / operating_liability 经营负债
--   working_capital_turnover 营运资金周转率 / cash_equivalents_turnover 现金及现金等价物周转率
--   tangible_asset_ratio     有形资产比率 / admin_expense_rate 管理费用率
--   book_to_market_bdt       账面市值比 / effective_tax_rate 实际税率
-- 域纪律：独立域，不进筛选界面（数据截止 2025Q1 且部分与自算指标重叠）。
CREATE TABLE IF NOT EXISTS csmar_risk_factors (
    stock_code                VARCHAR NOT NULL,
    report_date               DATE    NOT NULL,
    financial_liability       DOUBLE,
    operating_liability       DOUBLE,
    book_to_market_bdt        DOUBLE,
    admin_expense_rate        DOUBLE,
    tangible_asset_ratio      DOUBLE,
    working_capital_turnover  DOUBLE,
    cash_equivalents_turnover DOUBLE,
    revenue_growth_bdt        DOUBLE,
    non_debt_tax_shield       DOUBLE,
    effective_tax_rate        DOUBLE,
    profits_volatility_3y     DOUBLE,
    cashflow_volatility_3y    DOUBLE,
    interest_coverage_ratio   DOUBLE,
    tax_bearing               DOUBLE,
    bank_loan_ratio           DOUBLE,
    short_loan_dependence     DOUBLE,
    shareholders_occupy       DOUBLE,
    source                    VARCHAR NOT NULL,
    fetch_time                TIMESTAMP NOT NULL,
    batch_id                  VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_csmar_risk_factors_stock
    ON csmar_risk_factors (stock_code, report_date);


-- ── CSMAR 剩余指标表全量导入域（2026-09-19 v34）──────────────────────
-- 「榨干数据包」的完整性收口：把此前未接入的 11 张 CSMAR 指标表**整表导入**，
-- 共 425 个数据字段。列名保留 CSMAR 原始代码（F010101A 等），
-- 中文名与口径见 data package 内的字段字典（field_dictionary.csv）。
--
-- 为什么这批用代码列名而不逐个起可读名：
--   这批字段里大量是同一概念的 A/B/C/D/TTM 变体（如 ROE 有 4 种算法 × 4 个期间），
--   逐个起中文名既无必要也易出错；保留原始代码可保证与 CSMAR 文档一一对应，
--   且查询时可用字典表 join 出中文名。
--
-- 域纪律：**不进筛选界面**。数据截止 2025-03-31，且与自算指标大量重叠；
--   把它们放进全市场字段选择器会制造「同概念多套口径」的伪选择。
--   定位是：完整归档 + 交叉核验 + 专项研究。
-- FI_T1（302,959 行）；列名为 CSMAR 原始代码，中文名见字段字典
CREATE TABLE IF NOT EXISTS csmar_fi_t1 (
    stock_code VARCHAR NOT NULL,
    report_date DATE NOT NULL,
    "F010101A" DOUBLE,
    "F010201A" DOUBLE,
    "F010301A" DOUBLE,
    "F010401A" DOUBLE,
    "F010501A" DOUBLE,
    "F010601A" DOUBLE,
    "F010701B" DOUBLE,
    "F010702B" DOUBLE,
    "F010801B" DOUBLE,
    "F010901B" DOUBLE,
    "F011001B" DOUBLE,
    "F011201A" DOUBLE,
    "F011301A" DOUBLE,
    "F011401A" DOUBLE,
    "F011501A" DOUBLE,
    "F011601A" DOUBLE,
    "F011701A" DOUBLE,
    "F011801A" DOUBLE,
    "F011901A" DOUBLE,
    "F012001A" DOUBLE,
    "F012101A" DOUBLE,
    "F012201B" DOUBLE,
    "F012301B" DOUBLE,
    "F012401B" DOUBLE,
    "F012501B" DOUBLE,
    "F012601B" DOUBLE,
    "F012701B" DOUBLE,
    "F020502A" DOUBLE,
    source VARCHAR NOT NULL,
    fetch_time TIMESTAMP NOT NULL,
    batch_id VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_csmar_fi_t1_stock ON csmar_fi_t1 (stock_code, report_date);

-- FI_T8（302,716 行）；列名为 CSMAR 原始代码，中文名见字段字典
CREATE TABLE IF NOT EXISTS csmar_fi_t8 (
    stock_code VARCHAR NOT NULL,
    report_date DATE NOT NULL,
    "F080101A" DOUBLE,
    "F080102A" DOUBLE,
    "F080201A" DOUBLE,
    "F080301A" DOUBLE,
    "F080302A" DOUBLE,
    "F080401A" DOUBLE,
    "F080501A" DOUBLE,
    "F080502A" DOUBLE,
    "F080601A" DOUBLE,
    "F080602A" DOUBLE,
    "F080701B" DOUBLE,
    "F080702B" DOUBLE,
    "F080801B" DOUBLE,
    "F080802B" DOUBLE,
    "F080901B" DOUBLE,
    "F080902B" DOUBLE,
    "F081001B" DOUBLE,
    "F081002B" DOUBLE,
    "F081101B" DOUBLE,
    "F081102B" DOUBLE,
    "F081201B" DOUBLE,
    "F081202B" DOUBLE,
    "F081301B" DOUBLE,
    "F081401B" DOUBLE,
    "F081501B" DOUBLE,
    "F081601B" DOUBLE,
    "F081602C" DOUBLE,
    "F081701B" DOUBLE,
    "F081801B" DOUBLE,
    "F081901B" DOUBLE,
    "F082001B" DOUBLE,
    "F082101B" DOUBLE,
    "F082201B" DOUBLE,
    "F082202B" DOUBLE,
    "F082301B" DOUBLE,
    "F082302B" DOUBLE,
    "F082401B" DOUBLE,
    "F082402B" DOUBLE,
    "F082501B" DOUBLE,
    "F082502B" DOUBLE,
    "F082601B" DOUBLE,
    "F082701A" DOUBLE,
    "F082702A" DOUBLE,
    "F082801A" DOUBLE,
    "F082802A" DOUBLE,
    "F082703A" DOUBLE,
    "F082803A" DOUBLE,
    "F080703B" DOUBLE,
    "F081003B" DOUBLE,
    "F081603B" DOUBLE,
    "F080603A" DOUBLE,
    "F081103B" DOUBLE,
    "F080503A" DOUBLE,
    "F080803B" DOUBLE,
    "F081203B" DOUBLE,
    "F082602B" DOUBLE,
    "F082603B" DOUBLE,
    source VARCHAR NOT NULL,
    fetch_time TIMESTAMP NOT NULL,
    batch_id VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_csmar_fi_t8_stock ON csmar_fi_t8 (stock_code, report_date);

-- FI_T9（303,065 行）；列名为 CSMAR 原始代码，中文名见字段字典
CREATE TABLE IF NOT EXISTS csmar_fi_t9 (
    stock_code VARCHAR NOT NULL,
    report_date DATE NOT NULL,
    "F090101B" DOUBLE,
    "F090101C" DOUBLE,
    "F090102B" DOUBLE,
    "F090102C" DOUBLE,
    "F090103B" DOUBLE,
    "F090103C" DOUBLE,
    "F090104B" DOUBLE,
    "F090104C" DOUBLE,
    "F090201B" DOUBLE,
    "F090201C" DOUBLE,
    "F090202B" DOUBLE,
    "F090202C" DOUBLE,
    "F090301B" DOUBLE,
    "F090301C" DOUBLE,
    "F090401B" DOUBLE,
    "F090401C" DOUBLE,
    "F090501B" DOUBLE,
    "F090501C" DOUBLE,
    "F090601B" DOUBLE,
    "F090601C" DOUBLE,
    "F090701B" DOUBLE,
    "F090701C" DOUBLE,
    "F090801B" DOUBLE,
    "F090801C" DOUBLE,
    "F090901B" DOUBLE,
    "F090901C" DOUBLE,
    "F091001A" DOUBLE,
    "F091101A" DOUBLE,
    "F091201A" DOUBLE,
    "F091301A" DOUBLE,
    "F091401A" DOUBLE,
    "F091501A" DOUBLE,
    "F091601A" DOUBLE,
    "F091701A" DOUBLE,
    "F091801B" DOUBLE,
    "F091801C" DOUBLE,
    "F091901B" DOUBLE,
    "F091901C" DOUBLE,
    "F092001B" DOUBLE,
    "F092001C" DOUBLE,
    "F092101B" DOUBLE,
    "F092101C" DOUBLE,
    "F092201B" DOUBLE,
    "F092201C" DOUBLE,
    "F092301B" DOUBLE,
    "F092301C" DOUBLE,
    "F092401B" DOUBLE,
    "F092501B" DOUBLE,
    "F092601B" DOUBLE,
    "F092601C" DOUBLE,
    "F092602B" DOUBLE,
    "F092602C" DOUBLE,
    "F090302B" DOUBLE,
    "F090302C" DOUBLE,
    "F090402B" DOUBLE,
    "F090402C" DOUBLE,
    "F090502B" DOUBLE,
    "F090502C" DOUBLE,
    "F090602B" DOUBLE,
    "F090602C" DOUBLE,
    "F090702B" DOUBLE,
    "F090702C" DOUBLE,
    "F090802B" DOUBLE,
    "F090802C" DOUBLE,
    "F090902B" DOUBLE,
    "F090902C" DOUBLE,
    "F091002A" DOUBLE,
    "F091102A" DOUBLE,
    "F091202A" DOUBLE,
    "F091302A" DOUBLE,
    "F091402A" DOUBLE,
    "F091502A" DOUBLE,
    "F091602A" DOUBLE,
    "F091702A" DOUBLE,
    "F091802B" DOUBLE,
    "F091802C" DOUBLE,
    "F091902B" DOUBLE,
    "F091902C" DOUBLE,
    "F092002B" DOUBLE,
    "F092002C" DOUBLE,
    "F092102B" DOUBLE,
    "F092102C" DOUBLE,
    "F092202B" DOUBLE,
    "F092202C" DOUBLE,
    "F092302B" DOUBLE,
    "F092302C" DOUBLE,
    "F092103B" DOUBLE,
    "F092103C" DOUBLE,
    source VARCHAR NOT NULL,
    fetch_time TIMESTAMP NOT NULL,
    batch_id VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_csmar_fi_t9_stock ON csmar_fi_t9 (stock_code, report_date);

-- FI_T3（302,959 行）；列名为 CSMAR 原始代码，中文名见字段字典
CREATE TABLE IF NOT EXISTS csmar_fi_t3 (
    stock_code VARCHAR NOT NULL,
    report_date DATE NOT NULL,
    "F030101A" DOUBLE,
    "F030201A" DOUBLE,
    "F030301A" DOUBLE,
    "F030401A" DOUBLE,
    "F030501A" DOUBLE,
    "F030601A" DOUBLE,
    "F030701A" DOUBLE,
    "F030801A" DOUBLE,
    "F030901A" DOUBLE,
    "F031001A" DOUBLE,
    "F031101A" DOUBLE,
    "F031201A" DOUBLE,
    "F031301A" DOUBLE,
    "F031401A" DOUBLE,
    "F031501A" DOUBLE,
    "F031601A" DOUBLE,
    "F031701A" DOUBLE,
    "F031801A" DOUBLE,
    "F031901A" DOUBLE,
    "F032001A" DOUBLE,
    "F032101B" DOUBLE,
    "F032201B" DOUBLE,
    "F032301B" DOUBLE,
    "F032401B" DOUBLE,
    "F032501B" DOUBLE,
    "F032601B" DOUBLE,
    "F032701B" DOUBLE,
    "F032801B" DOUBLE,
    "F032901B" DOUBLE,
    "F033001B" DOUBLE,
    "F033101B" DOUBLE,
    "F033201B" DOUBLE,
    "F033301B" DOUBLE,
    "F033401B" DOUBLE,
    "F033501A" DOUBLE,
    source VARCHAR NOT NULL,
    fetch_time TIMESTAMP NOT NULL,
    batch_id VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_csmar_fi_t3_stock ON csmar_fi_t3 (stock_code, report_date);

-- FI_T6（301,294 行）；列名为 CSMAR 原始代码，中文名见字段字典
CREATE TABLE IF NOT EXISTS csmar_fi_t6 (
    stock_code VARCHAR NOT NULL,
    report_date DATE NOT NULL,
    "F060101B" DOUBLE,
    "F060101C" DOUBLE,
    "F060201B" DOUBLE,
    "F060201C" DOUBLE,
    "F060301B" DOUBLE,
    "F060301C" DOUBLE,
    "F060401B" DOUBLE,
    "F060401C" DOUBLE,
    "F060901B" DOUBLE,
    "F060901C" DOUBLE,
    "F061001B" DOUBLE,
    "F061001C" DOUBLE,
    "F061201B" DOUBLE,
    "F061201C" DOUBLE,
    "F061301B" DOUBLE,
    "F061302B" DOUBLE,
    "F061301C" DOUBLE,
    "F061302C" DOUBLE,
    "F061401B" DOUBLE,
    "F061402B" DOUBLE,
    "F061401C" DOUBLE,
    "F061402C" DOUBLE,
    "F061501B" DOUBLE,
    "F061601B" DOUBLE,
    "F061701B" DOUBLE,
    "F061801B" DOUBLE,
    "F061901B" DOUBLE,
    "F062001B" DOUBLE,
    "F062101B" DOUBLE,
    "F062201B" DOUBLE,
    "F062301B" DOUBLE,
    "F062401B" DOUBLE,
    source VARCHAR NOT NULL,
    fetch_time TIMESTAMP NOT NULL,
    batch_id VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_csmar_fi_t6_stock ON csmar_fi_t6 (stock_code, report_date);

-- FI_T5（305,273 行）；列名为 CSMAR 原始代码，中文名见字段字典
CREATE TABLE IF NOT EXISTS csmar_fi_t5 (
    stock_code VARCHAR NOT NULL,
    report_date DATE NOT NULL,
    "F050101B" DOUBLE,
    "F050102B" DOUBLE,
    "F050103B" DOUBLE,
    "F050104C" DOUBLE,
    "F050201B" DOUBLE,
    "F050202B" DOUBLE,
    "F050203B" DOUBLE,
    "F050204C" DOUBLE,
    "F050301B" DOUBLE,
    "F050302B" DOUBLE,
    "F050303B" DOUBLE,
    "F050304C" DOUBLE,
    "F050401B" DOUBLE,
    "F050402B" DOUBLE,
    "F050403B" DOUBLE,
    "F050404C" DOUBLE,
    "F050501B" DOUBLE,
    "F050502B" DOUBLE,
    "F050503B" DOUBLE,
    "F050504C" DOUBLE,
    "F050601B" DOUBLE,
    "F050601C" DOUBLE,
    "F050701B" DOUBLE,
    "F050801B" DOUBLE,
    "F050801C" DOUBLE,
    "F050901B" DOUBLE,
    "F051001B" DOUBLE,
    "F051101B" DOUBLE,
    "F051201B" DOUBLE,
    "F053201B" DOUBLE,
    "F053301B" DOUBLE,
    "F053301C" DOUBLE,
    "F051301B" DOUBLE,
    "F051301C" DOUBLE,
    "F051401B" DOUBLE,
    "F051401C" DOUBLE,
    "F051501B" DOUBLE,
    "F051501C" DOUBLE,
    "F051601B" DOUBLE,
    "F051601C" DOUBLE,
    "F051701B" DOUBLE,
    "F051701C" DOUBLE,
    "F051801B" DOUBLE,
    "F051801C" DOUBLE,
    "F051901B" DOUBLE,
    "F051901C" DOUBLE,
    "F053401B" DOUBLE,
    "F052001B" DOUBLE,
    "F052001C" DOUBLE,
    "F052101B" DOUBLE,
    "F052101C" DOUBLE,
    "F052201B" DOUBLE,
    "F052201C" DOUBLE,
    "F052301B" DOUBLE,
    "F052301C" DOUBLE,
    "F052401B" DOUBLE,
    "F052401C" DOUBLE,
    "F052901B" DOUBLE,
    "F052901C" DOUBLE,
    "F053001B" DOUBLE,
    "F053002B" DOUBLE,
    "F053003B" DOUBLE,
    "F053004C" DOUBLE,
    "F053101B" DOUBLE,
    "F053102B" DOUBLE,
    "F053103B" DOUBLE,
    "F053104C" DOUBLE,
    "F053202B" DOUBLE,
    source VARCHAR NOT NULL,
    fetch_time TIMESTAMP NOT NULL,
    batch_id VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_csmar_fi_t5_stock ON csmar_fi_t5 (stock_code, report_date);

-- FI_T10（264,772 行）；列名为 CSMAR 原始代码，中文名见字段字典
CREATE TABLE IF NOT EXISTS csmar_fi_t10 (
    stock_code VARCHAR NOT NULL,
    report_date DATE NOT NULL,
    "F100101B" DOUBLE,
    "F100102B" DOUBLE,
    "F100103C" DOUBLE,
    "F100201B" DOUBLE,
    "F100202B" DOUBLE,
    "F100203C" DOUBLE,
    "F100301B" DOUBLE,
    "F100302B" DOUBLE,
    "F100303C" DOUBLE,
    "F100401A" DOUBLE,
    "F100501A" DOUBLE,
    "F100601B" DOUBLE,
    "F100602B" DOUBLE,
    "F100603C" DOUBLE,
    "F100701A" DOUBLE,
    "F100801A" DOUBLE,
    "F100802A" DOUBLE,
    "F100901A" DOUBLE,
    "F100902A" DOUBLE,
    "F100903A" DOUBLE,
    "F100904A" DOUBLE,
    "F101001A" DOUBLE,
    "F101002A" DOUBLE,
    "F101101B" DOUBLE,
    "F101201B" DOUBLE,
    "F101202B" DOUBLE,
    "F101301B" DOUBLE,
    "F101302C" DOUBLE,
    source VARCHAR NOT NULL,
    fetch_time TIMESTAMP NOT NULL,
    batch_id VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_csmar_fi_t10_stock ON csmar_fi_t10 (stock_code, report_date);

-- FI_T4（304,505 行）；列名为 CSMAR 原始代码，中文名见字段字典
CREATE TABLE IF NOT EXISTS csmar_fi_t4 (
    stock_code VARCHAR NOT NULL,
    report_date DATE NOT NULL,
    "F040101B" DOUBLE,
    "F040201B" DOUBLE,
    "F040202B" DOUBLE,
    "F040203B" DOUBLE,
    "F040204B" DOUBLE,
    "F040205C" DOUBLE,
    "F040301B" DOUBLE,
    "F040302B" DOUBLE,
    "F040303B" DOUBLE,
    "F040304C" DOUBLE,
    "F040401B" DOUBLE,
    "F040501B" DOUBLE,
    "F040502B" DOUBLE,
    "F040503B" DOUBLE,
    "F040504B" DOUBLE,
    "F040505C" DOUBLE,
    "F040601B" DOUBLE,
    "F040602B" DOUBLE,
    "F040603B" DOUBLE,
    "F040604C" DOUBLE,
    "F040701B" DOUBLE,
    "F040702B" DOUBLE,
    "F040703B" DOUBLE,
    "F040704C" DOUBLE,
    "F040801B" DOUBLE,
    "F040802B" DOUBLE,
    "F040803B" DOUBLE,
    "F040804B" DOUBLE,
    "F040805C" DOUBLE,
    "F040901B" DOUBLE,
    "F040902B" DOUBLE,
    "F040903B" DOUBLE,
    "F040904B" DOUBLE,
    "F040905C" DOUBLE,
    "F041001B" DOUBLE,
    "F041002B" DOUBLE,
    "F041003B" DOUBLE,
    "F041004B" DOUBLE,
    "F041005C" DOUBLE,
    "F041101B" DOUBLE,
    "F041201B" DOUBLE,
    "F041202B" DOUBLE,
    "F041203B" DOUBLE,
    "F041204B" DOUBLE,
    "F041205C" DOUBLE,
    "F041301B" DOUBLE,
    "F041401B" DOUBLE,
    "F041402B" DOUBLE,
    "F041403B" DOUBLE,
    "F041404B" DOUBLE,
    "F041405C" DOUBLE,
    "F041501B" DOUBLE,
    "F041502B" DOUBLE,
    "F041503B" DOUBLE,
    "F041504B" DOUBLE,
    "F041505C" DOUBLE,
    "F041601B" DOUBLE,
    "F041701B" DOUBLE,
    "F041702B" DOUBLE,
    "F041703B" DOUBLE,
    "F041704B" DOUBLE,
    "F041705C" DOUBLE,
    "F041801B" DOUBLE,
    "F041802B" DOUBLE,
    "F041803B" DOUBLE,
    "F041804B" DOUBLE,
    "F041805C" DOUBLE,
    source VARCHAR NOT NULL,
    fetch_time TIMESTAMP NOT NULL,
    batch_id VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_csmar_fi_t4_stock ON csmar_fi_t4 (stock_code, report_date);

-- FI_T11（260,092 行）；列名为 CSMAR 原始代码，中文名见字段字典
CREATE TABLE IF NOT EXISTS csmar_fi_t11 (
    stock_code VARCHAR NOT NULL,
    report_date DATE NOT NULL,
    "F110101B" DOUBLE,
    "F110201B" DOUBLE,
    "F110301B" DOUBLE,
    "F110401B" DOUBLE,
    "F110501B" DOUBLE,
    "F110601B" DOUBLE,
    "F110701B" DOUBLE,
    "F110801B" DOUBLE,
    "F110302B" DOUBLE,
    "F110303B" DOUBLE,
    source VARCHAR NOT NULL,
    fetch_time TIMESTAMP NOT NULL,
    batch_id VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_csmar_fi_t11_stock ON csmar_fi_t11 (stock_code, report_date);

-- FAR_Finidx（76,262 行）；列名为 CSMAR 原始代码，中文名见字段字典
CREATE TABLE IF NOT EXISTS csmar_far_finidx (
    stock_code VARCHAR NOT NULL,
    report_date DATE NOT NULL,
    "A100000" DOUBLE,
    "A110601" DOUBLE,
    "A111201" DOUBLE,
    "A300000" DOUBLE,
    "B110101" DOUBLE,
    "B110303" DOUBLE,
    "B230403" DOUBLE,
    "D100000" DOUBLE,
    "T30100" DOUBLE,
    "T40100" DOUBLE,
    "T40401" DOUBLE,
    "T40402" DOUBLE,
    "T40403" DOUBLE,
    "T40700" DOUBLE,
    "T40801" DOUBLE,
    "T40802" DOUBLE,
    "T40803" DOUBLE,
    "T60200" DOUBLE,
    "T60300" DOUBLE,
    "Capexp" DOUBLE,
    "Etaxrt" DOUBLE,
    "Speitem" DOUBLE,
    "Nstaff" DOUBLE,
    source VARCHAR NOT NULL,
    fetch_time TIMESTAMP NOT NULL,
    batch_id VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_csmar_far_finidx_stock ON csmar_far_finidx (stock_code, report_date);


-- ── CSMAR 三表剩余科目 + 金融专用科目全量（2026-09-19 v35）─────────────
-- 「榨干」的最后一格：三张报表里本项目未映射的 100 个科目
-- （投资性房地产、开发支出、长期待摊费用、库存股、其他综合收益、专项储备、
--   其他权益工具、持有待售资产、长期应收款、债权投资、持续经营/终止经营净利润、
--   其他综合收益总额、资产处置收益…），以及金融专用科目全量 109 列。
-- 列名保留 CSMAR 代码，中文名见数据包内的字段字典。
-- 域纪律：归档与专项研究用，不进筛选界面（历史期 + 口径重叠）。
-- FS_Combas 剩余科目（51 列）；列名为 CSMAR 代码，中文名见字段字典
CREATE TABLE IF NOT EXISTS csmar_balance_items (
    stock_code VARCHAR NOT NULL,
    report_date DATE NOT NULL,
    "A001109000" DOUBLE,
    "A001127000" DOUBLE,
    "A001119000" DOUBLE,
    "A001120000" DOUBLE,
    "A001123101" DOUBLE,
    "A001129000" DOUBLE,
    "A001124000" DOUBLE,
    "A001125000" DOUBLE,
    "A001226000" DOUBLE,
    "A001202000" DOUBLE,
    "A001227000" DOUBLE,
    "A001203000" DOUBLE,
    "A001204000" DOUBLE,
    "A001228000" DOUBLE,
    "A001229000" DOUBLE,
    "A001206000" DOUBLE,
    "A001207000" DOUBLE,
    "A001211000" DOUBLE,
    "A001214000" DOUBLE,
    "A001215000" DOUBLE,
    "A001216000" DOUBLE,
    "A001217000" DOUBLE,
    "A001218201" DOUBLE,
    "A001219000" DOUBLE,
    "A001219101" DOUBLE,
    "A001221000" DOUBLE,
    "A001223000" DOUBLE,
    "A002105000" DOUBLE,
    "A002114000" DOUBLE,
    "A002115000" DOUBLE,
    "A002120000" DOUBLE,
    "A002129000" DOUBLE,
    "A002125000" DOUBLE,
    "A002126000" DOUBLE,
    "A002127000" DOUBLE,
    "A002204000" DOUBLE,
    "A002212000" DOUBLE,
    "A002205000" DOUBLE,
    "A002206000" DOUBLE,
    "A002207000" DOUBLE,
    "A002208000" DOUBLE,
    "A002209000" DOUBLE,
    "A002210000" DOUBLE,
    "A003112000" DOUBLE,
    "A003112101" DOUBLE,
    "A003112201" DOUBLE,
    "A003112301" DOUBLE,
    "A003102101" DOUBLE,
    "A003106000" DOUBLE,
    "A003107000" DOUBLE,
    "A003111000" DOUBLE,
    source VARCHAR NOT NULL,
    fetch_time TIMESTAMP NOT NULL,
    batch_id VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_csmar_balance_items_stock ON csmar_balance_items (stock_code, report_date);

-- FS_Comins 剩余科目（25 列）；列名为 CSMAR 代码，中文名见字段字典
CREATE TABLE IF NOT EXISTS csmar_income_items (
    stock_code VARCHAR NOT NULL,
    report_date DATE NOT NULL,
    "Bbd1102000" DOUBLE,
    "Bbd1102101" DOUBLE,
    "Bbd1102203" DOUBLE,
    "B001305000" DOUBLE,
    "B001302101" DOUBLE,
    "B001302201" DOUBLE,
    "B001303000" DOUBLE,
    "B001306000" DOUBLE,
    "B001308000" DOUBLE,
    "B001304000" DOUBLE,
    "B001400101" DOUBLE,
    "B001500101" DOUBLE,
    "B001500201" DOUBLE,
    "B002200000" DOUBLE,
    "B002300000" DOUBLE,
    "B002000401" DOUBLE,
    "B002000501" DOUBLE,
    "B002000301" DOUBLE,
    "B005000000" DOUBLE,
    "B005000101" DOUBLE,
    "B005000102" DOUBLE,
    "B006000000" DOUBLE,
    "B006000101" DOUBLE,
    "B006000103" DOUBLE,
    "B006000102" DOUBLE,
    source VARCHAR NOT NULL,
    fetch_time TIMESTAMP NOT NULL,
    batch_id VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_csmar_income_items_stock ON csmar_income_items (stock_code, report_date);

-- FS_Comscfd 剩余科目（24 列）；列名为 CSMAR 代码，中文名见字段字典
CREATE TABLE IF NOT EXISTS csmar_cashflow_items (
    stock_code VARCHAR NOT NULL,
    report_date DATE NOT NULL,
    "C002001000" DOUBLE,
    "C002002000" DOUBLE,
    "C002003000" DOUBLE,
    "C002004000" DOUBLE,
    "C002005000" DOUBLE,
    "C002100000" DOUBLE,
    "C002006000" DOUBLE,
    "C002007000" DOUBLE,
    "C002009000" DOUBLE,
    "C002010000" DOUBLE,
    "C002200000" DOUBLE,
    "C003008000" DOUBLE,
    "C003001000" DOUBLE,
    "C003001101" DOUBLE,
    "C003003000" DOUBLE,
    "C003002000" DOUBLE,
    "C003004000" DOUBLE,
    "C003100000" DOUBLE,
    "C003005000" DOUBLE,
    "C003006000" DOUBLE,
    "C003006101" DOUBLE,
    "C003007000" DOUBLE,
    "C003200000" DOUBLE,
    "C007000000" DOUBLE,
    source VARCHAR NOT NULL,
    fetch_time TIMESTAMP NOT NULL,
    batch_id VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_csmar_cashflow_items_stock ON csmar_cashflow_items (stock_code, report_date);

-- 金融行业专用科目全量（109 列，银行/保险/证券/其他金融前缀）
CREATE TABLE IF NOT EXISTS csmar_financial_items (
    stock_code VARCHAR NOT NULL,
    report_date DATE NOT NULL,
    "A0D2130000" DOUBLE,
    "A0F1132000" DOUBLE,
    "A0F1133000" DOUBLE,
    "A0F1224000" DOUBLE,
    "A0F1232000" DOUBLE,
    "A0F1233000" DOUBLE,
    "A0F2210000" DOUBLE,
    "A0F3108000" DOUBLE,
    "A0F3109000" DOUBLE,
    "A0b1103000" DOUBLE,
    "A0b1104000" DOUBLE,
    "A0b1105000" DOUBLE,
    "A0b1201000" DOUBLE,
    "A0b2102000" DOUBLE,
    "A0b2103000" DOUBLE,
    "A0b2103101" DOUBLE,
    "A0b2103201" DOUBLE,
    "A0d1101101" DOUBLE,
    "A0d1102000" DOUBLE,
    "A0d1102101" DOUBLE,
    "A0d1126000" DOUBLE,
    "A0d1218101" DOUBLE,
    "A0d2101101" DOUBLE,
    "A0d2122000" DOUBLE,
    "A0d2123000" DOUBLE,
    "A0d2202000" DOUBLE,
    "A0f1106000" DOUBLE,
    "A0f1108000" DOUBLE,
    "A0f1122000" DOUBLE,
    "A0f1300000" DOUBLE,
    "A0f2104000" DOUBLE,
    "A0f2106000" DOUBLE,
    "A0f2110000" DOUBLE,
    "A0f2300000" DOUBLE,
    "A0f3104000" DOUBLE,
    "A0i1113000" DOUBLE,
    "A0i1114000" DOUBLE,
    "A0i1115000" DOUBLE,
    "A0i1116000" DOUBLE,
    "A0i1116101" DOUBLE,
    "A0i1116201" DOUBLE,
    "A0i1116301" DOUBLE,
    "A0i1116401" DOUBLE,
    "A0i1209000" DOUBLE,
    "A0i1210000" DOUBLE,
    "A0i1224000" DOUBLE,
    "A0i1225000" DOUBLE,
    "A0i2111000" DOUBLE,
    "A0i2116000" DOUBLE,
    "A0i2117000" DOUBLE,
    "A0i2118000" DOUBLE,
    "A0i2119000" DOUBLE,
    "A0i2119101" DOUBLE,
    "A0i2119201" DOUBLE,
    "A0i2119301" DOUBLE,
    "A0i2119401" DOUBLE,
    "A0i2121000" DOUBLE,
    "A0i2124000" DOUBLE,
    "B0I1214000" DOUBLE,
    "B0d1104000" DOUBLE,
    "B0d1104101" DOUBLE,
    "B0d1104201" DOUBLE,
    "B0d1104301" DOUBLE,
    "B0d1104401" DOUBLE,
    "B0d1104501" DOUBLE,
    "B0f1105000" DOUBLE,
    "B0f1208000" DOUBLE,
    "B0f1213000" DOUBLE,
    "B0i1103000" DOUBLE,
    "B0i1103101" DOUBLE,
    "B0i1103111" DOUBLE,
    "B0i1103203" DOUBLE,
    "B0i1103303" DOUBLE,
    "B0i1202000" DOUBLE,
    "B0i1203000" DOUBLE,
    "B0i1203101" DOUBLE,
    "B0i1203203" DOUBLE,
    "B0i1204000" DOUBLE,
    "B0i1204101" DOUBLE,
    "B0i1204203" DOUBLE,
    "B0i1205000" DOUBLE,
    "B0i1206000" DOUBLE,
    "B0i1208103" DOUBLE,
    "C0F1023000" DOUBLE,
    "C0F1024000" DOUBLE,
    "C0F1025000" DOUBLE,
    "C0F1026000" DOUBLE,
    "C0F1027000" DOUBLE,
    "C0F1028000" DOUBLE,
    "C0F1029000" DOUBLE,
    "C0F1030000" DOUBLE,
    "C0F1031000" DOUBLE,
    "C0F1032000" DOUBLE,
    "C0b1002000" DOUBLE,
    "C0b1003000" DOUBLE,
    "C0b1004000" DOUBLE,
    "C0b1015000" DOUBLE,
    "C0b1016000" DOUBLE,
    "C0d1008000" DOUBLE,
    "C0d1010000" DOUBLE,
    "C0d1011000" DOUBLE,
    "C0f1009000" DOUBLE,
    "C0f1018000" DOUBLE,
    "C0i1005000" DOUBLE,
    "C0i1006000" DOUBLE,
    "C0i1007000" DOUBLE,
    "C0i1017000" DOUBLE,
    "C0i1019000" DOUBLE,
    "C0i2008000" DOUBLE,
    source VARCHAR NOT NULL,
    fetch_time TIMESTAMP NOT NULL,
    batch_id VARCHAR NOT NULL,
    PRIMARY KEY (stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_csmar_financial_items_stock ON csmar_financial_items (stock_code, report_date);


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
    status          TEXT NOT NULL DEFAULT 'draft',  -- draft/validated/single_previewed/previewed/published
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
    confirmed_at    TIMESTAMP,
    status          TEXT DEFAULT 'pending'  -- pending/executed/consumed/expired/cancelled
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
    # 迁移版本表必须先于 v12 探测存在，才能判断 v12 是否已执行。
    with store.transaction() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                description VARCHAR NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    # v12 迁移（2026-08-25）：funding_events 撤销复合主键
    # 东财 F10 把一次增发按发行对象拆成多条同 list_date 记录，旧主键
    # (stock_code, event_type, list_date) 会丢失同日期多批次数据。
    # 表为本次数据补全新引入、未发布，直接 DROP 重建（无用户数据可保留）。
    # 红队：DROP 重建只在 schema_migrations.version=12 未执行时进行，
    # 不能每次启动都检查/尝试 DROP（否则已重建表会在版本记录后再次被清）。
    try:
        v12_applied = bool(store.read_query(
            "SELECT 1 FROM schema_migrations WHERE version = 12"
        ))
    except Exception as error:  # noqa: BLE001
        logger.warning("funding_events v12 迁移版本检查失败(非致命): %s", error)
        v12_applied = True  # 无法确认时拒绝 DROP，避免误删已重建数据
    if not v12_applied:
        try:
            has_pk = store.read_query(
                """SELECT 1 FROM duckdb_constraints()
                   WHERE table_name = 'funding_events' AND constraint_type = 'PRIMARY KEY'"""
            )
            if has_pk:
                with store.transaction() as conn:
                    conn.execute("DROP TABLE IF EXISTS funding_events")
                store.execute_script(DUCKDB_SCHEMA_V1)
                logger.info("funding_events 主键约束已撤销并重建（v12 迁移）")
        except Exception as error:  # noqa: BLE001
            logger.warning("funding_events v12 迁移检查失败(非致命): %s", error)
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
        for column in (
            "avg_volume DOUBLE",
            "period_return DOUBLE",
            "annualized_volatility DOUBLE",
            "max_drawdown DOUBLE",
            "deducted_profit_cagr3 DOUBLE",
            "deducted_profit_cagr5 DOUBLE",
        ):
            connection.execute(f"ALTER TABLE indicator_snapshot ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "effective_date DATE",
            "data_version VARCHAR",
            "formula VARCHAR",
        ):
            connection.execute(f"ALTER TABLE source_audit ADD COLUMN IF NOT EXISTS {column}")
        try:
            connection.execute("ALTER TABLE stock_meta ALTER COLUMN is_st DROP DEFAULT")
        except Exception:
            logger.debug("stock_meta.is_st DROP DEFAULT skipped (may already be applied)")
        try:
            connection.execute("ALTER TABLE stock_meta ALTER COLUMN is_suspended DROP DEFAULT")
        except Exception:
            logger.debug("stock_meta.is_suspended DROP DEFAULT skipped (may already be applied)")
        connection.execute(
            "ALTER TABLE stock_meta ADD COLUMN IF NOT EXISTS total_shares BIGINT"
        )
        connection.execute(
            "ALTER TABLE stock_meta ADD COLUMN IF NOT EXISTS circ_shares BIGINT"
        )
        connection.execute(
            "ALTER TABLE stock_meta ADD COLUMN IF NOT EXISTS is_listed BOOLEAN"
        )
        connection.execute(
            "ALTER TABLE stock_meta ADD COLUMN IF NOT EXISTS csrc_l1 VARCHAR"
        )
        connection.execute(
            "ALTER TABLE stock_meta ADD COLUMN IF NOT EXISTS csrc_l2 VARCHAR"
        )
        for column in (
            "core_tier1_capital_adequacy_ratio DOUBLE",
            "tier1_capital_adequacy_ratio DOUBLE",
            "capital_adequacy_ratio DOUBLE",
            "non_performing_loan_ratio DOUBLE",
            "provision_coverage_ratio DOUBLE",
            "risk_coverage_ratio DOUBLE",
        ):
            connection.execute(f"ALTER TABLE balance_sheet ADD COLUMN IF NOT EXISTS {column}")
        connection.execute("UPDATE stock_meta SET is_listed = TRUE WHERE is_listed IS NULL")
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
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (4, 'Add total_shares and circ_shares to stock_meta')
            ON CONFLICT (version) DO NOTHING
            """
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (5, 'Track whether a stock is present in the current listed universe')
            ON CONFLICT (version) DO NOTHING
            """
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (6, 'Financial-sector regulatory screening fields')
            ON CONFLICT (version) DO NOTHING
            """
        )
        connection.execute(
            "ALTER TABLE raw_response_archive ADD COLUMN IF NOT EXISTS integrity_verified BOOLEAN DEFAULT FALSE"
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (7, 'Pre-computed archive integrity flag to avoid full-payload re-hashing')
            ON CONFLICT (version) DO NOTHING
            """
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (8, 'Independent low-frequency business overview tables')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v9: 财政部国债收益率曲线 + 快照股息率利差列（P3，reports/68）
        for column in (
            "ttm_dividend_yield DOUBLE",
            "div_yield_spread_0p25y DOUBLE",
            "div_yield_spread_0p5y DOUBLE",
            "div_yield_spread_1y DOUBLE",
            "div_yield_spread_2y DOUBLE",
            "div_yield_spread_3y DOUBLE",
            "div_yield_spread_5y DOUBLE",
            "div_yield_spread_7y DOUBLE",
            "div_yield_spread_10y DOUBLE",
            "div_yield_spread_30y DOUBLE",
        ):
            connection.execute(
                f"ALTER TABLE indicator_snapshot ADD COLUMN IF NOT EXISTS {column}"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_treasury_yield_curve_date "
            "ON treasury_yield_curve (curve_date)"
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (9, 'Treasury yield curve domain and dividend yield spread columns')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v10: 历史总股本链 + 历史研究统计域（P4，reports/68）
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_share_capital_history_stock "
            "ON share_capital_history (stock_code, effective_date)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_research_statistics_lookup "
            "ON research_statistics (metric, window_years, method, version)"
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (10, 'Share capital history chain and research statistics domain')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v11: 融资事件域 + 指数估值域（数据补全 2026-08-25，reports/82）
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_funding_events_stock "
            "ON funding_events (stock_code)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_index_valuation_code "
            "ON index_valuation (index_code, trade_date)"
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (11, 'Funding events and index valuation domains')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v12: funding_events 撤销复合主键（东财 F10 同 list_date 多批次，见上方迁移）
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (12, 'Drop funding_events composite primary key')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v13: 分红融资比数据前置（2026-08-25）
        connection.execute(
            "ALTER TABLE indicator_snapshot ADD COLUMN IF NOT EXISTS cumulative_dividend_amount DOUBLE"
        )
        connection.execute(
            "ALTER TABLE indicator_snapshot ADD COLUMN IF NOT EXISTS cumulative_financing_amount DOUBLE"
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (13, 'Dividend financing ratio snapshot inputs')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v14: 回购/注销事件域（2026-08-26，广义分红数据补充）
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_buyback_events_stock ON buyback_events (stock_code)"
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (14, 'Buyback events domain')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v15: 分红融资比百分数快照列（2026-08-26）
        connection.execute(
            "ALTER TABLE indicator_snapshot ADD COLUMN IF NOT EXISTS dividend_financing_ratio_pct DOUBLE"
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (15, 'Dividend financing ratio percent column')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v16: 原始响应归档冷热分层（2026-09-01）。
        # 生产库 raw_response_archive 已积累 26GB+ BLOB；该表任何新行提交
        # 都会让 DuckDB 在提交阶段扫描整表做主键校验（实测单行提交峰值
        # 24GB / 约 134s）。把既有归档改名为 history 后，新建一个小而空的
        # active 表承接新写入，并通过 raw_response_archive_all 视图合并读取。
        # 迁移只改 catalog 元数据，不复制 BLOB。
        history_exists = connection.execute(
            "SELECT 1 FROM duckdb_tables() WHERE table_name = 'raw_response_archive_history'"
        ).fetchone()
        active_exists = connection.execute(
            "SELECT 1 FROM duckdb_tables() WHERE table_name = 'raw_response_archive'"
        ).fetchone()
        if history_exists is None and active_exists is not None:
            connection.execute(
                "ALTER TABLE raw_response_archive RENAME TO raw_response_archive_history"
            )
        if history_exists is None:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS raw_response_archive_history (
                    raw_response_hash VARCHAR PRIMARY KEY,
                    source            VARCHAR NOT NULL,
                    fetch_time        TIMESTAMP NOT NULL,
                    payload           BLOB,
                    api_version       VARCHAR,
                    integrity_verified BOOLEAN DEFAULT FALSE,
                    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_response_archive (
                raw_response_hash VARCHAR PRIMARY KEY,
                source            VARCHAR NOT NULL,
                fetch_time        TIMESTAMP NOT NULL,
                payload           BLOB,
                api_version       VARCHAR,
                integrity_verified BOOLEAN DEFAULT FALSE,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE OR REPLACE VIEW raw_response_archive_all AS
            SELECT raw_response_hash, source, fetch_time, payload, api_version,
                   integrity_verified, created_at, 'history' AS storage
            FROM raw_response_archive_history
            UNION ALL
            SELECT raw_response_hash, source, fetch_time, payload, api_version,
                   integrity_verified, created_at, 'active' AS storage
            FROM raw_response_archive
            """
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (16, 'Split raw response archive into hot active and cold history tables')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v17: lineage 物化 hash 集合 + 归档分区登记表。
        # - valid_hash 让 4200 万行 source_audit 只连接小表，不在冷核对
        #   中触碰 26GB BLOB 视图；
        # - partitions 登记所有 raw_response_archive_* 表，轮转时用它重建
        #   合并视图；当前 active 行 closed_at 为 NULL。
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_response_archive_valid_hash (
                raw_response_hash VARCHAR PRIMARY KEY
            )
            """
        )
        connection.execute(
            """
            INSERT INTO raw_response_archive_valid_hash
            SELECT raw_response_hash FROM raw_response_archive_all
            WHERE payload IS NOT NULL AND OCTET_LENGTH(payload) > 0
            ON CONFLICT DO NOTHING
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_response_archive_partitions (
                partition_table VARCHAR PRIMARY KEY,
                created_at TIMESTAMP NOT NULL,
                closed_at TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO raw_response_archive_partitions
                (partition_table, created_at, closed_at)
            SELECT 'raw_response_archive_history',
                   COALESCE(MIN(created_at), CURRENT_TIMESTAMP),
                   COALESCE(MAX(created_at), CURRENT_TIMESTAMP)
            FROM raw_response_archive_history
            ON CONFLICT DO NOTHING
            """
        )
        connection.execute(
            """
            INSERT INTO raw_response_archive_partitions
                (partition_table, created_at, closed_at)
            SELECT 'raw_response_archive', CURRENT_TIMESTAMP, NULL
            ON CONFLICT DO NOTHING
            """
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (17, 'Materialized valid archive hash set for lineage checks')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v18: 分区登记表增加行数/字节统计，避免每次归档写入都扫描 BLOB
        # 计算 SUM(OCTET_LENGTH(payload))（2026-09-01 价格更新复现：该扫描
        # 把价格流水线拖慢到约 20 只/分）。
        connection.execute(
            "ALTER TABLE raw_response_archive_partitions "
            "ADD COLUMN IF NOT EXISTS row_count BIGINT DEFAULT 0"
        )
        connection.execute(
            "ALTER TABLE raw_response_archive_partitions "
            "ADD COLUMN IF NOT EXISTS estimated_bytes BIGINT DEFAULT 0"
        )
        connection.execute(
            """
            UPDATE raw_response_archive_partitions p
            SET row_count = a.c, estimated_bytes = a.b
            FROM (
                SELECT COUNT(*) AS c, COALESCE(SUM(OCTET_LENGTH(payload)), 0) AS b
                FROM raw_response_archive
            ) a
            WHERE p.partition_table = 'raw_response_archive'
            """
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (18, 'Track raw archive partition row/byte counters')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v19: source_audit 冷热分离（2026-09-03）。
        # source_audit 保留近期热审计行；老审计行由维护命令按 report_date
        # 批量迁入 source_audit_archive。日常 readiness/lineage 只扫描热表，
        # 排查历史问题时通过 source_audit_all 视图查询热+冷。
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS source_audit_archive (
                id                BIGINT PRIMARY KEY,
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
                created_at        TIMESTAMP,
                effective_date    DATE,
                data_version      VARCHAR,
                formula           VARCHAR,
                archived_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_source_audit_archive_stock_date_field
                ON source_audit_archive (stock_code, report_date, field_name)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_source_audit_archive_hash
                ON source_audit_archive (raw_response_hash)
            """
        )
        connection.execute(
            """
            CREATE OR REPLACE VIEW source_audit_all AS
            SELECT id, stock_code, field_name, report_date, value, source,
                   fetch_batch_id, fetch_time, raw_response_hash, confidence,
                   reason_code, api_version, is_override, override_id, created_at,
                   effective_date, data_version, formula
            FROM source_audit
            UNION ALL
            SELECT id, stock_code, field_name, report_date, value, source,
                   fetch_batch_id, fetch_time, raw_response_hash, confidence,
                   reason_code, api_version, is_override, override_id, created_at,
                   effective_date, data_version, formula
            FROM source_audit_archive
            """
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (19, 'Split source audit into hot and archive tables')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v20: 港股分红域（2026-09-04，总市场分红融资比的数据前置）。
        # 表 DDL 在 DUCKDB_SCHEMA_V1 中，这里补齐索引与迁移记录。
        # 域纪律：独立于 A 股 readiness；写路径走
        # app/core/hk_dividends.py + 单写者锁，不进入指标快照公式。
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_hk_dividends_stock "
            "ON hk_dividends (stock_code)"
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (20, 'Hong Kong dividend events for A+H dual-listed stocks')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v21: 指数估值域扩展（2026-09-05，多指数 ERP 与 ETF 轮动分位的地基）。
        # 新增 pe_metric（口径披露）与 extra（JSON 附加字段）；旧行保持 NULL，
        # 语义不变（legulegu 行即 TTM、csindex 行即 市盈率1=TTM）。
        connection.execute(
            "ALTER TABLE index_valuation ADD COLUMN IF NOT EXISTS pe_metric VARCHAR"
        )
        connection.execute(
            "ALTER TABLE index_valuation ADD COLUMN IF NOT EXISTS extra VARCHAR"
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (21, 'Index valuation multi-index extension: pe_metric and extra columns')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v22: ETF 日线行情域（2026-09-05，同花顺官方 Financial-API）。
        # 表 DDL 在 DUCKDB_SCHEMA_V1；此处补索引与迁移记录。
        # 域纪律：独立于 A 股 readiness；失败不阻断主链。
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_etf_daily_code "
            "ON etf_daily (etf_code, trade_date)"
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (22, 'ETF daily quotes for rotation strategy')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v23: etf_daily 增加同花顺跟踪指数 PE-TTM 五年分位列（2026-09-05）。
        connection.execute(
            "ALTER TABLE etf_daily ADD COLUMN IF NOT EXISTS "
            "track_pe_ttm_five_year_percentile DOUBLE"
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (23, 'ETF tracking-index PE-TTM five-year percentile column')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v24: CSMAR C17 数据层两域（2026-09-19，Phase B）。
        # ① cash_flow_indirect —— 间接法现金流量表 29 科目（FS_Comscfi）。
        #    为自由现金流族、杠杆（EBITDA/EBIT）、折旧摊销类指标提供输入；
        #    此前本项目只有直接法，导致 FCF 无法编制。
        # ② financial_report_dates —— 年报公布日（FAR_Finidx.Annodt，76,262 条）。
        #    支撑「当时可见」口径，解除 PRD §8.1「不用于回测」的标注限制。
        # 两表 DDL 在 DUCKDB_SCHEMA_V1 中，这里补齐索引与迁移记录。
        # 域纪律：均为独立低频域，不进入 A 股 readiness 门禁；写路径走
        # scripts/import_csmar_phase_b.py + 单写者锁。
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_cash_flow_indirect_date "
            "ON cash_flow_indirect (report_date)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_financial_report_dates_announce "
            "ON financial_report_dates (announce_date)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_financial_report_dates_stock "
            "ON financial_report_dates (stock_code, report_date)"
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (24, 'CSMAR C17: indirect cash flow statement + annual report publish dates')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v25: CSMAR C17 Phase B 指标层（2026-09-19）。
        # ① cash_flow_activity —— 直接法现金流量表的投资/筹资活动科目
        #    （为自由现金流提供资本支出输入，并提供分红总额与融资流水的核验源）；
        # ② indicator_ext —— 本项目此前完全缺失的三族指标：
        #    周转率族（CSMAR FI_T4，green 可自算）、杠杆族（FI_T7）、
        #    自由现金流族（FI_T6，yellow：公式透明但口径需自行定义）。
        # 两表 DDL 在 DUCKDB_SCHEMA_V1 中，这里补齐索引与迁移记录。
        # 域纪律：独立低频域，不进入 indicator_snapshot 主链与 readiness 门禁。
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_cash_flow_activity_date "
            "ON cash_flow_activity (report_date)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_indicator_ext_date "
            "ON indicator_ext (report_date)"
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (25, 'CSMAR C17: investing/financing cash flow + extended indicators (turnover/leverage/FCF)')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v26: indicator_ext 扩展第二批（2026-09-19，"榨干数据包" R6）。
        # 新增三族**可由本项目自算**的指标（不依赖 CSMAR，因此当期为最新）：
        #   ① 每股族：每股净资产/每股营业收入/每股经营现金流/每股留存收益
        #      —— 分母用 share_capital_history 的**当时股数**（effective_date<=报告期），
        #      不得用 stock_meta.total_shares（那是当前股本，会造成历史口径错误，
        #      参见 ops-knowledge-base D23）。
        #   ② 费用率族：销售/管理/研发/财务费用率
        #   ③ 结构族：流动资产占比、固定资产占比、权益乘数
        for column, kind in (
            ("bps", "DOUBLE"),
            ("revenue_per_share", "DOUBLE"),
            ("ocf_per_share", "DOUBLE"),
            ("retained_earnings_per_share", "DOUBLE"),
            ("selling_expense_ratio", "DOUBLE"),
            ("admin_expense_ratio", "DOUBLE"),
            ("rd_expense_ratio", "DOUBLE"),
            ("finance_expense_ratio", "DOUBLE"),
            ("current_asset_ratio", "DOUBLE"),
            ("fixed_asset_ratio", "DOUBLE"),
            ("equity_multiplier", "DOUBLE"),
        ):
            connection.execute(f"ALTER TABLE indicator_ext ADD COLUMN IF NOT EXISTS {column} {kind}")
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (26, 'indicator_ext: per-share / expense-ratio / structure families')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v27: 资产负债表补充域 + 大股东占款（2026-09-19）。
        # balance_sheet_ext 承载主链未映射的「其他应付款 / 一年内到期非流动负债 /
        # 其他应收款合计」；indicator_ext 新增 shareholder_occupation。
        # 表 DDL 在 DUCKDB_SCHEMA_V1 中，这里补索引与迁移记录。
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_balance_sheet_ext_date "
            "ON balance_sheet_ext (report_date)"
        )
        connection.execute(
            "ALTER TABLE indicator_ext ADD COLUMN IF NOT EXISTS shareholder_occupation DOUBLE"
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (27, 'balance_sheet_ext + shareholder occupation (governance red flag)')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v28: 员工人数历史域 + 人效指标（2026-09-19）。
        # company_employee_history 承载逐年员工数；indicator_ext 新增
        # employee_count / revenue_per_employee / profit_per_employee。
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_company_employee_history_stock "
            "ON company_employee_history (stock_code, report_date)"
        )
        for column in ("employee_count", "revenue_per_employee", "profit_per_employee"):
            connection.execute(
                f"ALTER TABLE indicator_ext ADD COLUMN IF NOT EXISTS {column} DOUBLE"
            )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (28, 'employee headcount history + per-employee productivity')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v29: 金融行业专用科目域（2026-09-19）。
        # 78 只金融股的行业专用科目；**不进筛选界面**（只对该行业成立，
        # 放进全市场字段表会制造「98% 股票无数据」的伪条件）。
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_financial_sector_items_stock "
            "ON financial_sector_items (stock_code, report_date)"
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (29, 'financial sector specific line items (banks/insurers/securities)')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v30: 报告期市值与估值衍生指标（2026-09-19）。
        # 解除 ops-knowledge-base D23「历史市值用当前股本」的阻塞：
        # 本域用【报告期当日原始收盘价 × 报告期时点股本】自算市值，而不是
        # stock_meta.total_shares。据此派生：
        #   tobin_q          托宾Q = (股权市值 + 总负债) / 总资产（CSMAR 市值A 口径）
        #   book_to_market   账面市值比 = 归母权益 / 股权市值
        #   ev_ebitda        企业价值倍数 = (股权市值 + 有息负债 − 货币资金) / EBITDA
        # **注意**：本域只服务于 indicator_ext；主链 indicator_snapshot 的历史市值
        # 仍受 D23 影响，未在本轮修复（需另立专项并评估对已发布研究结论的影响）。
        for column in ("report_date_close", "market_cap_at_report",
                       "tobin_q", "book_to_market", "ev_ebitda"):
            connection.execute(
                f"ALTER TABLE indicator_ext ADD COLUMN IF NOT EXISTS {column} DOUBLE"
            )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (30, 'point-in-time market cap + tobin Q / book-to-market / EV-EBITDA')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v31: CSMAR 每股指标历史域（2026-09-19）—— 自算每股族的交叉核验源。
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_csmar_per_share_history_stock "
            "ON csmar_per_share_history (stock_code, report_date)"
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (31, 'CSMAR per-share indicator history (cross-check source)')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v32: 修正「每股净资产」口径并补齐「归属母公司每股净资产」（2026-09-19）。
        # 交叉核验（scripts/import_per_share_history.py）发现：本项目的 bps 名为
        # 「每股净资产」却用了**归母权益**，与 CSMAR F091001A（股东权益合计口径）
        # 一致率仅 25%。改用股东权益合计后一致率 95.03%（1% 容差 96.67%）。
        # 两个概念都合法，故各自成列：
        #   bps        每股净资产         = 股东权益合计 / 股数（CSMAR F091001A）
        #   bps_parent 归属母公司每股净资产 = 归母权益   / 股数（CSMAR F091701A，95.04%）
        connection.execute(
            "ALTER TABLE indicator_ext ADD COLUMN IF NOT EXISTS bps_parent DOUBLE"
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (32, 'fix bps caliber (total equity) + add parent BPS')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v33: CSMAR 披露指标域 + 风险治理因子域（2026-09-19，"榨干" R12）。
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_csmar_disclosure_metrics_stock "
            "ON csmar_disclosure_metrics (stock_code, report_date)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_csmar_risk_factors_stock "
            "ON csmar_risk_factors (stock_code, report_date)"
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (33, 'CSMAR disclosure metrics (official weighted ROE) + risk/governance factors')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v34: CSMAR 剩余指标表全量导入（2026-09-19，{n} 张表 / {c} 列）。
        connection.execute("CREATE INDEX IF NOT EXISTS idx_csmar_fi_t1_stock ON csmar_fi_t1 (stock_code, report_date)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_csmar_fi_t8_stock ON csmar_fi_t8 (stock_code, report_date)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_csmar_fi_t9_stock ON csmar_fi_t9 (stock_code, report_date)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_csmar_fi_t3_stock ON csmar_fi_t3 (stock_code, report_date)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_csmar_fi_t6_stock ON csmar_fi_t6 (stock_code, report_date)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_csmar_fi_t5_stock ON csmar_fi_t5 (stock_code, report_date)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_csmar_fi_t10_stock ON csmar_fi_t10 (stock_code, report_date)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_csmar_fi_t4_stock ON csmar_fi_t4 (stock_code, report_date)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_csmar_fi_t11_stock ON csmar_fi_t11 (stock_code, report_date)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_csmar_far_finidx_stock ON csmar_far_finidx (stock_code, report_date)")
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (34, 'CSMAR remaining indicator tables (full import)')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v35: CSMAR 三表剩余科目 + 金融专用科目全量（2026-09-19）。
        connection.execute("CREATE INDEX IF NOT EXISTS idx_csmar_balance_items_stock ON csmar_balance_items (stock_code, report_date)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_csmar_income_items_stock ON csmar_income_items (stock_code, report_date)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_csmar_cashflow_items_stock ON csmar_cashflow_items (stock_code, report_date)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_csmar_financial_items_stock ON csmar_financial_items (stock_code, report_date)")
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (35, 'CSMAR statement remaining items + full financial sector items')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v36: AIQ 年度宽表 + 会计恒等式锚点（2026-09-19，"榨干"收尾）。
        # 注：csmar_aiq_annual 已于 v37 清理（54 列全部与 FI_T* 重叠），
        # 表与 DDL 均已移除，此处不再建索引。
        connection.execute(
            'ALTER TABLE csmar_balance_items ADD COLUMN IF NOT EXISTS "A004000000" DOUBLE'
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (36, 'CSMAR AIQ annual wide table + accounting identity anchor')
            ON CONFLICT (version) DO NOTHING
            """
        )
        # v37: 把 CSMAR 里**有价值且可自算**的新概念落地为当期可用指标（2026-09-19）。
        # 依据用户要求「有价值的要利用要上界面，没价值的不进库」：
        # 不导入 CSMAR 的过期数值，而是**用它的公式在本项目数据上自算**，
        # 从而覆盖最新报告期、可直接进筛选界面。
        #   现金比率 / 保守速动比率 / 产权比率 / 有形净值债务率 / 经营现金流对负债 /
        #   EBITDA对负债 / 营业收入现金含量 / 营业利润现金净含量 / 应计项目 /
        #   每股有形资产 / 每股负债 / 每股资本公积
        for column, kind in (
            ("cash_ratio", "DOUBLE"),
            ("conservative_quick_ratio", "DOUBLE"),
            ("debt_to_equity", "DOUBLE"),
            ("tangible_net_debt_ratio", "DOUBLE"),
            ("ocf_to_liabilities", "DOUBLE"),
            ("ebitda_to_liabilities", "DOUBLE"),
            ("cash_content_of_revenue", "DOUBLE"),
            ("ocf_to_operating_profit", "DOUBLE"),
            ("accruals", "DOUBLE"),
            ("tangible_asset_per_share", "DOUBLE"),
            ("liability_per_share", "DOUBLE"),
            ("capital_reserve_per_share", "DOUBLE"),
        ):
            connection.execute(
                f"ALTER TABLE indicator_ext ADD COLUMN IF NOT EXISTS {column} {kind}"
            )
        connection.execute(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (37, 'CSMAR-derived self-computed indicators (solvency/cash-quality/per-share)')
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

        row = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = 4"
        ).fetchone()
        if row is None:
            plan_columns = {
                column[1] for column in conn.execute("PRAGMA table_info(plans)").fetchall()
            }
            if "confirmed_at" not in plan_columns:
                conn.execute("ALTER TABLE plans ADD COLUMN confirmed_at TIMESTAMP")
            conn.execute(
                """
                INSERT INTO schema_migrations (version, description)
                VALUES (?, ?)
                """,
                (4, "危险操作计划添加 confirmed_at 列"),
            )
            logger.info("SQLite schema v4 已应用")

        row = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = 5"
        ).fetchone()
        if row is None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS screening_drafts (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    draft_json TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                (5, "筛选页单一草稿自动保存和恢复"),
            )
            logger.info("SQLite schema v5 已应用")

        row = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = 6"
        ).fetchone()
        if row is None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pdf_archive_manifest (
                    stock_code TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    archive_path TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (stock_code, filename)
                )
                """
            )
            conn.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                (6, "PDF 冷归档文件清单和 SHA-256 恢复校验"),
            )
            logger.info("SQLite schema v6 已应用")

        row = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = 7"
        ).fetchone()
        if row is None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS screening_runs (
                    run_id TEXT PRIMARY KEY,
                    rule_id INTEGER NOT NULL,
                    rule_version INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    columns_json TEXT NOT NULL,
                    sort_json TEXT NOT NULL,
                    data_date TEXT,
                    base_pool_config TEXT NOT NULL,
                    strict_only BOOLEAN NOT NULL,
                    confidence_summary TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (rule_id) REFERENCES screening_rules(id)
                )
                """
            )
            conn.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                (7, "服务端筛选运行记录，保存结果不可由浏览器伪造"),
            )
            logger.info("SQLite schema v7 已应用")

        row = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = 8"
        ).fetchone()
        if row is None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS announcement_registry (
                    announcement_id TEXT PRIMARY KEY,
                    stock_code TEXT NOT NULL,
                    announcement_time TEXT NOT NULL,
                    title TEXT,
                    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                (8, "CNINFO 公告差分和增量财务更新登记"),
            )
            logger.info("SQLite schema v8 已应用")

        row = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = 9"
        ).fetchone()
        if row is None:
            result_columns = {
                column[1] for column in conn.execute("PRAGMA table_info(screening_results)").fetchall()
            }
            if "base_pool_config" not in result_columns:
                conn.execute("ALTER TABLE screening_results ADD COLUMN base_pool_config TEXT")
            conn.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                (9, "保存筛选结果绑定运行时基础池和用户列配置"),
            )
            logger.info("SQLite schema v9 已应用")

        # Historical v1-v9 databases can claim a schema version while missing
        # columns introduced in later source files, so repair shape idempotently.
        retry_columns = {column[1] for column in conn.execute("PRAGMA table_info(retry_list)").fetchall()}
        if "max_retries" not in retry_columns:
            conn.execute("ALTER TABLE retry_list ADD COLUMN max_retries INTEGER DEFAULT 5")
        if "next_retry_at" not in retry_columns:
            conn.execute("ALTER TABLE retry_list ADD COLUMN next_retry_at TIMESTAMP")
        missing_columns = {column[1] for column in conn.execute("PRAGMA table_info(missing_list)").fetchall()}
        if "resolved_at" not in missing_columns:
            conn.execute("ALTER TABLE missing_list ADD COLUMN resolved_at TIMESTAMP")
        conn.execute("UPDATE retry_list SET extra_json = '{}' WHERE extra_json IS NULL")
        # 建唯一索引前先按 (stock_code, data_type, adapter, extra_json) 保留
        # 最新一条去重（参考 missing_list 去重写法），避免历史重复行让
        # CREATE UNIQUE INDEX 失败或把旧错误重新顶出来。
        conn.execute(
            "DELETE FROM retry_list WHERE id NOT IN ("
            "SELECT MAX(id) FROM retry_list "
            "GROUP BY stock_code, data_type, adapter, extra_json)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_retry_list_request "
            "ON retry_list(stock_code, data_type, adapter, extra_json)"
        )
        conn.execute(
            "INSERT INTO schema_migrations (version, description) VALUES (?, ?) "
            "ON CONFLICT(version) DO NOTHING",
            (10, "Repair retry and missing-list columns and enforce request uniqueness"),
        )
        # The calendar is a durable numerical-input dependency, not a
        # transient cache created only after a successful initialization.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS trading_dates (trade_date TEXT PRIMARY KEY)"
        )
        conn.execute(
            "INSERT INTO schema_migrations (version, description) VALUES (?, ?) "
            "ON CONFLICT(version) DO NOTHING",
            (11, "Persisted trading calendar required for technical indicators"),
        )
        duplicate_watchlist_rows = conn.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT stock_code, group_name FROM watchlist "
            "GROUP BY stock_code, group_name HAVING COUNT(*) > 1"
            ")"
        ).fetchone()[0]
        if duplicate_watchlist_rows:
            raise RuntimeError(
                "watchlist contains duplicate stock/group rows; reconcile them before schema migration"
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_watchlist_stock_group "
            "ON watchlist(stock_code, group_name)"
        )
        conn.execute(
            "INSERT INTO schema_migrations (version, description) VALUES (?, ?) "
            "ON CONFLICT(version) DO NOTHING",
            (12, "自选股票和分组唯一，防止重试重复写入"),
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS screening_drafts (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                draft_json TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        draft_columns = {
            column[1] for column in conn.execute("PRAGMA table_info(screening_drafts)").fetchall()
        }
        if "revision" not in draft_columns:
            conn.execute("ALTER TABLE screening_drafts ADD COLUMN revision INTEGER DEFAULT 0")
        conn.execute(
            "INSERT INTO schema_migrations (version, description) VALUES (?, ?) "
            "ON CONFLICT(version) DO NOTHING",
            (13, "筛选草稿 revision 版本号，支持并发冲突检测"),
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS data_refresh_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO schema_migrations (version, description) VALUES (?, ?) "
            "ON CONFLICT(version) DO NOTHING",
            (14, "数据域刷新状态（如 CSRC 行业低频刷新时间戳）"),
        )
        # 每只股票每个字段最多保留一条未解决缺失，去重后由数据到达时解决
        # （业务概览等独立低频域复用同一 missing_list 语义）。
        conn.execute(
            "DELETE FROM missing_list WHERE resolved_at IS NULL AND id NOT IN ("
            "SELECT MAX(id) FROM missing_list WHERE resolved_at IS NULL "
            "GROUP BY stock_code, field_name)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_missing_list_stock_field_open "
            "ON missing_list(stock_code, field_name) WHERE resolved_at IS NULL"
        )
        conn.execute(
            "INSERT INTO schema_migrations (version, description) VALUES (?, ?) "
            "ON CONFLICT(version) DO NOTHING",
            (15, "missing_list 未解决条目按股票+字段去重"),
        )

        # v16: ETF 轮动工作台操作域（2026-09-05）
        # 持仓/流水/预算均为用户录入数据，归属 SQLite 操作型存储；
        # ETF 行情在 DuckDB etf_daily（同花顺官方源）。
        row = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = 16"
        ).fetchone()
        if row is None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS etf_meta (
                    etf_code          TEXT PRIMARY KEY,
                    name              TEXT NOT NULL,
                    category          TEXT NOT NULL DEFAULT 'industry'
                                      CHECK (category IN ('industry', 'strategy', 'market')),
                    track_index_code  TEXT,              -- 跟踪指数代码（000300 / SW801010）
                    track_index_name  TEXT,
                    primary_metric    TEXT NOT NULL DEFAULT 'pe'
                                      CHECK (primary_metric IN ('pe', 'pb')),
                    industry_group    TEXT,              -- 行业分组（一指数一 ETF 视图）
                    budget            REAL NOT NULL DEFAULT 0,  -- 该 ETF 独立预算（元）
                    step_pct          REAL NOT NULL DEFAULT 5,  -- 网格间距（默认 5%）
                    enabled           INTEGER NOT NULL DEFAULT 1,
                    note              TEXT,
                    updated_at        TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS etf_trades (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    etf_code    TEXT NOT NULL,
                    trade_date  TEXT NOT NULL,
                    direction   TEXT NOT NULL CHECK (direction IN ('buy', 'sell')),
                    price       REAL NOT NULL,
                    shares      REAL NOT NULL,
                    amount      REAL NOT NULL,
                    fee         REAL NOT NULL DEFAULT 0,
                    note        TEXT,
                    created_at  TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_etf_trades_code ON etf_trades (etf_code, trade_date)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS etf_cash_flows (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    flow_date  TEXT NOT NULL,
                    direction  TEXT NOT NULL CHECK (direction IN ('in', 'out')),
                    amount     REAL NOT NULL,
                    note       TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS etf_sell_plans (
                    etf_code         TEXT PRIMARY KEY,
                    trigger_date     TEXT NOT NULL,   -- 触发 80% 分位的日期
                    trigger_price    REAL NOT NULL,   -- 触发价（首档锚点）
                    tranche_amount   REAL NOT NULL,   -- 单档金额 = 触发时持仓市值 ÷ 10
                    tranches_done    INTEGER NOT NULL DEFAULT 0,
                    updated_at       TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS etf_settings (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                (16, "ETF rotation workbench: meta/trades/cash-flows/sell-plans/settings"),
            )
            logger.info("SQLite schema v16 已应用")

        # v17: ETF 分层（2026-09-05 用户定稿）：
        # category ∈ industry（申万一级行业）/ strategy（策略因子）/
        # market（市场指数，含沪深300/中证500/中证1000/恒生科技等）。
        row = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = 17"
        ).fetchone()
        if row is None:
            meta_columns = {
                column[1] for column in conn.execute("PRAGMA table_info(etf_meta)").fetchall()
            }
            if "category" not in meta_columns:
                conn.execute(
                    "ALTER TABLE etf_meta ADD COLUMN category TEXT NOT NULL DEFAULT 'industry'"
                )
            conn.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                (17, "ETF meta category: industry/strategy/market"),
            )
            logger.info("SQLite schema v17 已应用")


def init_all_schema(
    duckdb_store: DuckDBStore | None = None,
    sqlite_store: SQLiteStore | None = None,
    *,
    paths: DatabasePathSet | None = None,
    skip_if_current: bool = False,
) -> None:
    """Initialize both schemas through an explicit validated boundary.

    If no arguments are provided, this function will attempt to create
    paths from environment variables (VD_ENV, VD_DUCKDB_PATH, VD_SQLITE_PATH).
    This is a convenience for CLI commands that run after _ensure_formal_env_vars().

    skip_if_current (reports/79 方案 C): 当两个库的 schema_migrations 已到
    最新版本时跳过全部 DDL——正式库上这段幂等 DDL 实测约 5s（10GB DuckDB
    上逐条 CREATE/ALTER 检查目录），是启动 8~12s 的主要成分。跳过后的启动
    路径约 3~4s。迁移版本必须随每次 schema 变更递增（DUCKDB_SCHEMA_VERSION /
    SQLITE_SCHEMA_VERSION），否则此快速路径会错误跳过待应用迁移。
    """
    if paths is None and duckdb_store is None and sqlite_store is None:
        from app.core.storage.path_policy import resolve_and_validate_paths
        paths = resolve_and_validate_paths()
    if paths is None and (duckdb_store is None or sqlite_store is None):
        raise PathIsolationError("init_all_schema requires both stores or validated paths")
    if paths is not None:
        validated = paths.validate()
        duckdb_store = duckdb_store or DuckDBStore(paths=validated)
        sqlite_store = sqlite_store or SQLiteStore(paths=validated)
        if duckdb_store.db_path != validated.duckdb_path:
            raise PathIsolationError("DuckDB store does not match injected paths")
        if sqlite_store.db_path != validated.sqlite_path:
            raise PathIsolationError("SQLite store does not match injected paths")

    assert duckdb_store is not None and sqlite_store is not None

    if skip_if_current and _schemas_at_current_version(duckdb_store, sqlite_store):
        logger.info("数据库 schema 已是最新版本，跳过初始化（快速启动）")
        return

    init_duckdb_schema(duckdb_store)
    init_sqlite_schema(sqlite_store)

    logger.info("所有数据库 schema 初始化完成")


def _schemas_at_current_version(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> bool:
    """Cheap version probe for the fast-start path; any failure falls back
    to the full idempotent init."""
    try:
        rows = sqlite_store.query("SELECT MAX(version) AS v FROM schema_migrations")
        sqlite_version = rows[0].get("v") if rows else None
        if sqlite_version != SQLITE_SCHEMA_VERSION:
            return False
        rows = duckdb_store.read_query("SELECT MAX(version) AS v FROM schema_migrations")
        duckdb_version = rows[0].get("v") if rows else None
        return duckdb_version == DUCKDB_SCHEMA_VERSION
    except Exception:
        return False
