"""SQLite 种子工厂 `tests/support/seed_sqlite.py` 的常驻契约测试。

工厂存在的**主要理由**与 DuckDB 版同源（`reports/140` §17.7）：把「拼错列名 →
静默插进一行全 NULL / 缺 `extra_json` 的种子 → 用例继续绿着但什么都没覆盖」
变成**当场红**。SQLite 侧还多一种事故形态：`retry_list` 的去重键**包含
`extra_json`**（`uq_retry_list_request`），而唯一索引把 NULL 当作互不相等，
于是"漏写 `extra_json`"不会报错，只会让同一条重试静默变成两条。本文件把
两个库**共有的 8 条不变式**在 SQLite 实现上重新钉一遍，再加上 SQLite 独有的
约束分类（唯一索引 vs NOT NULL）。

覆盖的不变式（每条都对应一类具体事故）：

1. **未知列名必须抛 `UnknownSqliteColumn`，且一行都不许写** —— 拼错列名静默变 NULL。
2. **有唯一键的表重复播种必须抛 `DuplicateSqliteSeed`** —— "我改了参数"静默无效
   （库里还是上一次的值，断言却对着新参数写）。
3. **`overwrite=True` 必须真的覆盖** —— 并钉住 SQLite 的**覆盖粒度**：SQLite 的
   `INSERT OR REPLACE` 是"删旧行再插新行"，本次没提供的列回到 NULL；这与 DuckDB
   的 upsert（只改本次提供的列，见 `test_seed_factory.py` 不变式 3）**不是**同一件事，
   正是两个工厂不能合并成"跨库万能口"的第一条理由。
4. **没有自然唯一键的流式表可以重复追加**：`job_logs`（主键只有自增 `id`，
   同一 `job_type` 本来就该有多条 running→终态历史）；
   `manual_overrides` 只对 `status='published'` 的行去重（**部分**唯一索引
   `uq_manual_overrides_published`），`active` 历史行本来就允许共存。
5. **未传的业务列必须是 NULL** —— 工厂偷偷补默认值会让「故意缺失」那组用例假绿。
6. **`auto_update_state` 是单行表**：`CHECK (id = 1)` → `id` 固定、重复必抛、
   `overwrite=True` 后仍然只有一行。
7. **默认值形状照生产口径** —— `status` 在枚举内、`retry_count=0` /
   `max_retries=5` / `error_kind='unknown'` / `extra_json='{}'`（合法 JSON）、
   时间戳列形状同 `DEFAULT CURRENT_TIMESTAMP`（`YYYY-MM-DD HH:MM:SS`）。
   形状错的默认值不会报错，只会让下游"这条数据是否齐备"的判据静默失效。
8. **确定性** —— 同一 (表, 键) 播种两次必须得到同一份时间戳字面量，
   `now()` 会让"同一份种子"每次不同，测试变成随机变量。

附加：
* **NOT NULL / CHECK 失败不得冒充"主键冲突"**（`:class:`SqliteSeedConstraint``）——
  把 NOT NULL 报成重复会逼人去改主键，白查一轮。
"""

from __future__ import annotations

import json
import re

import pytest

from app.core.auto_update import STATE_TABLE, VALID_STATES, AutoUpdateController
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.core.update_parts.retry import NON_RETRYABLE_ERROR_KINDS
from tests.support.seed_sqlite import (
    DEFAULT_SEED_TIME,
    DuplicateSqliteSeed,
    SeedSqlite,
    SqliteSeedConstraint,
    SqliteSeedError,
    UnknownSqliteColumn,
)

STOCK = "600519"
REPORT_DATE = "2024-12-31"

#: 生产口径的时间戳形状：SQLite 的 ``DEFAULT CURRENT_TIMESTAMP`` 是
#: ``YYYY-MM-DD HH:MM:SS``；ISO-8601（``datetime.now(UTC).isoformat()``）也允许，
#: 两种分隔符都收（T 或空格）。
TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")

#: `manual_overrides.status` 的枚举（`app/core/storage/schema.py` 的列注释
#: "M8-3修复: 专用状态列 (active/rolled_back/published)"）。
OVERRIDE_STATUSES = frozenset({"active", "rolled_back", "published"})
#: `retry_list.error_kind`：schema 默认 `'unknown'` + 契约 §5 的两类"非故障"
#: （`app/core/update_parts/retry.py::NON_RETRYABLE_ERROR_KINDS`，不抄第二份清单）。
ERROR_KINDS = frozenset({"unknown"}) | NON_RETRYABLE_ERROR_KINDS


@pytest.fixture
def seed(duckdb_store: DuckDBStore, sqlite_store: SQLiteStore) -> SeedSqlite:
    """建好 `auto_update_state` 再返回播种器。

    这张表**不在** `init_sqlite_schema()` 里：它由
    `AutoUpdateController._ensure_state_table()` 懒建（DDL 只存在于生产代码）。
    这里就让生产代码去建，测试不抄第二份列清单 —— 否则 schema 一改，
    抄件继续绿。
    """
    AutoUpdateController(duck=duckdb_store, sqlite=sqlite_store)
    return SeedSqlite(sqlite_store)


def _count(store: SQLiteStore, table: str) -> int:
    return store.query(f"SELECT COUNT(*) AS cnt FROM {table}")[0]["cnt"]


def _one(store: SQLiteStore, table: str) -> dict:
    rows = store.query(f"SELECT * FROM {table}")
    assert len(rows) == 1, f"{table} 期望恰好 1 行，实际 {len(rows)} 行"
    return rows[0]


# ── 不变式 1：未知列名当场红，且不落库 ───────────────────────────────
@pytest.mark.parametrize(
    ("call", "table", "typo"),
    [
        pytest.param(
            lambda seed: seed.job_log("incremental_update", job_typ="x"),
            "job_logs",
            "job_typ",
            id="job_log:job_typ",
        ),
        pytest.param(
            # `last_attempt` 抄成 `last_atempt`：重试队列里最常见的拼错之一，
            # 插进去只会让"上次尝试时间"永远 NULL（重试间隔判据静默失效）。
            lambda seed: seed.retry(STOCK, "price_daily", adapter="manager", last_atempt="x"),
            "retry_list",
            "last_atempt",
            id="retry:last_atempt",
        ),
        pytest.param(
            # `group_name` 是唯一索引 `uq_watchlist_stock_group` 的一半。
            lambda seed: seed.watchlist(STOCK, grup_name="current"),
            "watchlist",
            "grup_name",
            id="watchlist:grup_name",
        ),
        pytest.param(
            lambda seed: seed.trading_dates(["2026-08-06"], trade_dte="2026-08-06"),
            "trading_dates",
            "trade_dte",
            id="trading_dates:trade_dte",
        ),
        pytest.param(
            lambda seed: seed.override(STOCK, "revenue", 1.0, reason="r", reprot_date=REPORT_DATE),
            "manual_overrides",
            "reprot_date",
            id="override:reprot_date",
        ),
        pytest.param(
            # 批量 rows 也要逐行进同一套列校验：任一行拼错 → 整批一行都不落
            # （`_prepare` 在写之前就把所有行校验完，不会先写一半再撞拼错）。
            lambda seed: seed.override(
                rows=[
                    {
                        "stock_code": STOCK, "field_name": "revenue",
                        "override_value": 1.0, "reason": "r",
                    },
                    {
                        "stock_code": STOCK, "field_name": "total_assets",
                        "override_value": 2.0, "reason": "r", "staus": "published",
                    },
                ],
            ),
            "manual_overrides",
            "staus",
            id="override_rows:staus",
        ),
        pytest.param(
            lambda seed: seed.refresh_state("csrc_industry_last_refresh", "{}", updatd_at="x"),
            "data_refresh_state",
            "updatd_at",
            id="refresh_state:updatd_at",
        ),
        pytest.param(
            lambda seed: seed.auto_update_state(curent_stage="idle"),
            STATE_TABLE,
            "curent_stage",
            id="auto_update_state:curent_stage",
        ),
        pytest.param(
            # `screening_runs` 的"只跑严格池"列叫 `strict_only`（不是 `strict`）：
            # 它是结果可复现性的一部分，拼错后这一列成 NULL → 存档语义失真。
            # 注：外键 `rule_id → screening_rules(id)` 在**列校验之后**才会碰到，
            # 所以这里不需要先建父行（一行都没写，父行也不该出现）。
            lambda seed: seed.screening_run(
                "run-typo", 1, 1, result_json="[]", columns_json="[]", sort_json="[]",
                base_pool_config="{}", strict_only=False, confidence_summary="{}",
                strict="only",
            ),
            "screening_runs",
            "strict",
            id="screening_run:strict",
        ),
    ],
)
def test_misspelled_column_raises_unknown_sqlite_column_and_writes_no_row(
    seed: SeedSqlite, sqlite_store: SQLiteStore, call, table: str, typo: str
) -> None:
    """拼错列名 MUST 抛 `UnknownSqliteColumn`，且事务里一行都不许落。

    "一行都不许落"不是洁癖：半行种子会让下一条断言对着一个**部分为 NULL
    的行**跑，看起来有数据，实际业务列全缺 —— 比直接报错难查得多。
    """
    with pytest.raises(UnknownSqliteColumn) as excinfo:
        call(seed)

    # 报错必须点名拼错的列并列出可用列，否则拼错的人只能靠猜。
    assert typo in str(excinfo.value)
    assert "可用列" in str(excinfo.value)
    assert _count(sqlite_store, table) == 0


# ── 不变式 2：有唯一键的表重复播种必须抛 ─────────────────────────────
@pytest.mark.parametrize(
    ("call", "table"),
    [
        pytest.param(
            lambda seed: seed.retry(STOCK, "price_daily", adapter="manager", error="temporary"),
            "retry_list",
            id="retry",
        ),
        pytest.param(
            lambda seed: seed.watchlist(STOCK),
            "watchlist",
            id="watchlist",
        ),
        pytest.param(
            lambda seed: seed.trading_dates(["2026-08-06"]),
            "trading_dates",
            id="trading_dates",
        ),
        pytest.param(
            lambda seed: seed.refresh_state("csrc_industry_last_refresh", "{}"),
            "data_refresh_state",
            id="refresh_state",
        ),
        pytest.param(
            lambda seed: seed.auto_update_state(),
            STATE_TABLE,
            id="auto_update_state",
        ),
        pytest.param(
            # `published` 才落进部分唯一索引 `uq_manual_overrides_published`。
            lambda seed: seed.override(
                STOCK, "revenue", 1000.0, reason="人工修正", status="published"
            ),
            "manual_overrides",
            id="override_published",
        ),
        pytest.param(
            # 批量入口同一把尺子：同一批 published 行第二次播种同样抛。
            lambda seed: seed.override(
                rows=[
                    {
                        "stock_code": STOCK, "field_name": "revenue",
                        "override_value": 1000.0, "reason": "人工修正",
                        "status": "published",
                    },
                ],
            ),
            "manual_overrides",
            id="override_rows_published",
        ),
    ],
)
def test_duplicate_seed_raises_and_keeps_first_value(
    seed: SeedSqlite, sqlite_store: SQLiteStore, call, table: str
) -> None:
    """重复播种 MUST 抛 `DuplicateSqliteSeed` —— 不许静默保留上一次的值。

    静默保留会让"我改了参数"变成空操作：调用点写 `error='boom'`，库里还是
    `'temporary'`，断言却按新参数写，红得莫名其妙（或者更糟：恰好绿）。
    """
    call(seed)

    with pytest.raises(DuplicateSqliteSeed, match=table):
        call(seed)

    assert _count(sqlite_store, table) == 1


# ── 不变式 3：overwrite=True 真的覆盖（并钉住 SQLite 的粒度）─────────
def test_overwrite_true_replaces_the_existing_row_instead_of_ignoring_it(
    seed: SeedSqlite, sqlite_store: SQLiteStore
) -> None:
    """`overwrite=True` 必须换掉值，且不留下第二行。

    这条同时钉住 SQLite 与 DuckDB 的**粒度差异**（本模块 docstring 第 1 条理由）：
    SQLite 的 `INSERT OR REPLACE` 是"删掉冲突行、再插一行新的"，所以本次没提供的
    `last_attempt` **回到 NULL**；而 DuckDB 的 `INSERT OR REPLACE` 是 upsert，
    会保留上一次的 `selling_expenses`（见 `test_seed_factory.py` 不变式 3 的断言）。
    一个"跨库 overwrite"做不到两边都对，这就是两个工厂必须分开的理由。
    需要整行干净重来时要意识到：SQLite 侧**已经**是整行重来了。
    """
    seed.retry(
        "000001", "price_daily", adapter="manager", error="temporary",
        last_attempt="2026-08-04 10:33:17",
    )
    seed.retry("000001", "price_daily", adapter="manager", error="boom", overwrite=True)

    rows = sqlite_store.query(
        "SELECT error, last_attempt, retry_count FROM retry_list"
    )
    assert rows == [{"error": "boom", "last_attempt": None, "retry_count": 0}]

    seed.watchlist(STOCK, group_name="default")
    seed.watchlist(STOCK, group_name="default", added_at="2026-08-01 09:00:00", overwrite=True)
    assert sqlite_store.query("SELECT stock_code, added_at FROM watchlist") == [
        {"stock_code": STOCK, "added_at": "2026-08-01 09:00:00"}
    ]


# ── 不变式 4：流式表可重复追加；部分唯一索引只认 published ───────────
def test_keyless_job_log_stream_accepts_repeated_seeding(
    seed: SeedSqlite, sqlite_store: SQLiteStore
) -> None:
    """`job_logs` 主键只有自增 `id` —— 同一行的重复播种必须都留下。

    这是表的真实语义，不是工厂疏忽：同一 `job_type` 的每次执行都是一条独立历史
    （`_start_job` 先写 running，`_finish_job` 再 UPDATE 成终态），
    "最近一次更新"就是靠这个流按 `finished_at` 排序读出来的。
    """
    seed.job_log("incremental_update")
    seed.job_log("incremental_update")
    seed.job_log("full_init")

    assert _count(sqlite_store, "job_logs") == 3
    assert [row["id"] for row in sqlite_store.query("SELECT id FROM job_logs ORDER BY id")] == [
        1, 2, 3,
    ]


def test_active_overrides_coexist_while_published_is_unique(
    seed: SeedSqlite, sqlite_store: SQLiteStore
) -> None:
    """`manual_overrides` 只对 `published` 行去重（`WHERE status='published' AND
    rolled_back_at IS NULL` 的**部分**唯一索引，schema v2）。

    所以"active 行可以重复"不是工厂漏了检查，而是索引没覆盖它们；而第二行
    `published` 必须抛 —— 同股票同字段同报告期只能有一个当前生效的覆写，
    否则下游"读哪一行"取决于 sqlite 的扫描顺序。
    """
    seed.override(STOCK, "revenue", 1000.0, reason="第一次")
    seed.override(STOCK, "revenue", 2000.0, reason="第二次")
    assert _count(sqlite_store, "manual_overrides") == 2

    seed.override(STOCK, "revenue", 3000.0, reason="生效", status="published")
    with pytest.raises(DuplicateSqliteSeed, match="manual_overrides"):
        seed.override(STOCK, "revenue", 4000.0, reason="又生效", status="published")

    # 已回滚的 published 行不再占用唯一槽位（索引条件含 rolled_back_at IS NULL）。
    seed.override(
        STOCK, "revenue", 5000.0, reason="回滚后重登",
        status="published", rolled_back_at="2026-08-01 00:00:00",
    )
    assert _count(sqlite_store, "manual_overrides") == 4

    # 批量 rows（一次 executemany + 一次事务）：两条 active 同键共存是**真实
    # 语义**（同一股票同一字段的多次人工修正历史），不是工厂漏检。
    seed.override(rows=[
        {
            "stock_code": STOCK, "field_name": "total_assets",
            "override_value": 1.0, "reason": "批量一",
        },
        {
            "stock_code": STOCK, "field_name": "total_assets",
            "override_value": 2.0, "reason": "批量二",
        },
    ])
    assert _count(sqlite_store, "manual_overrides") == 6

    # 同一批里两条 `published`（同股票/字段/报告期）→ 第二条撞部分唯一索引 →
    # **整批回滚**：不是"第一条留下、第二条报错"，而是一行都不落。
    with pytest.raises(DuplicateSqliteSeed, match="manual_overrides"):
        seed.override(rows=[
            {
                "stock_code": "000001", "field_name": "revenue",
                "report_date": REPORT_DATE, "override_value": 1.0,
                "reason": "批次头", "status": "published",
            },
            {
                "stock_code": "000001", "field_name": "revenue",
                "report_date": REPORT_DATE, "override_value": 2.0,
                "reason": "批次尾", "status": "published",
            },
        ])
    assert _count(sqlite_store, "manual_overrides") == 6

    # 已回滚的 published 不占唯一槽位：两条一起播也可以（索引条件含
    # `rolled_back_at IS NULL`）—— 这正是"回滚后重新发布"要的批量形状。
    seed.override(rows=[
        {
            "stock_code": "000001", "field_name": "revenue",
            "report_date": REPORT_DATE, "override_value": 1.0,
            "reason": "回滚一", "status": "published",
            "rolled_back_at": "2026-08-01 00:00:00",
        },
        {
            "stock_code": "000001", "field_name": "revenue",
            "report_date": REPORT_DATE, "override_value": 2.0,
            "reason": "回滚二", "status": "published",
            "rolled_back_at": "2026-08-02 00:00:00",
        },
    ])
    assert _count(sqlite_store, "manual_overrides") == 8


# ── 不变式 5：未传的业务列必须是 NULL ────────────────────────────────
def test_business_columns_not_passed_stay_null(
    seed: SeedSqlite, sqlite_store: SQLiteStore
) -> None:
    """工厂不给业务列设默认值 —— 不写就是 NULL。

    「缺 error → 不当成失败」「缺 report_date → 不当成当期」这类用例的全部前提，
    就是种子里那几个列**真的**是 NULL。工厂一旦补个 `''` 或 `0`，它们会变成
    "有值但是空"的另一件事，而且仍然绿。
    """
    seed.job_log("incremental_update")
    seed.retry(STOCK, "price_daily", adapter="manager")
    seed.watchlist(STOCK)
    seed.override(STOCK, "revenue", 1000.0, reason="r")
    seed.auto_update_state()

    job = _one(sqlite_store, "job_logs")
    assert job["finished_at"] is None

    retry = _one(sqlite_store, "retry_list")
    assert retry["error"] is None
    assert retry["last_attempt"] is None
    assert retry["next_retry_at"] is None

    watch = _one(sqlite_store, "watchlist")
    assert watch["source_rule_id"] is None
    assert watch["source_result_id"] is None

    override = _one(sqlite_store, "manual_overrides")
    assert override["report_date"] is None
    assert override["original_value"] is None
    assert override["correction_template"] is None
    assert override["rolled_back_at"] is None
    assert override["rolled_back_to"] is None

    # 批量 rows 走同一把尺子：未传的业务列仍是 NULL（不是批量路径补的默认值），
    # 可选口径列落单行默认值（status='active'、created_at=固定字面量）。
    seed.override(rows=[
        {
            "stock_code": STOCK, "field_name": "batch_blank",
            "override_value": 7.0, "reason": "批量",
        },
    ])
    batch = sqlite_store.query(
        "SELECT report_date, original_value, correction_template, rolled_back_at, "
        "rolled_back_to, status, created_at FROM manual_overrides "
        "WHERE field_name = 'batch_blank'"
    )
    assert batch == [{
        "report_date": None,
        "original_value": None,
        "correction_template": None,
        "rolled_back_at": None,
        "rolled_back_to": None,
        "status": "active",
        "created_at": DEFAULT_SEED_TIME,
    }]

    state = _one(sqlite_store, STATE_TABLE)
    assert state["progress_json"] is None
    assert state["last_error"] is None
    assert state["last_success_at"] is None


# ── 不变式 6：单行表 auto_update_state 的 id 口径 ────────────────────
def test_auto_update_state_is_a_single_row_and_overwrites_in_place(
    seed: SeedSqlite, sqlite_store: SQLiteStore
) -> None:
    """`CHECK (id = 1)` 的单行表：`id` 是表口径（不可传），重复必抛，覆盖后仍一行。

    状态页与调度器都按 `WHERE id = 1` 读，所以"第二行"不是数据，是**看不见的**
    数据：它不会让任何查询变红，只会让 `seed` 写得像生效了其实没生效。
    """
    seed.auto_update_state()
    assert _one(sqlite_store, STATE_TABLE)["id"] == 1

    with pytest.raises(DuplicateSqliteSeed, match=STATE_TABLE):
        seed.auto_update_state(state="paused")

    seed.auto_update_state(state="paused", paused=1, current_stage="prices", overwrite=True)
    state = _one(sqlite_store, STATE_TABLE)
    assert (state["state"], state["paused"], state["current_stage"]) == ("paused", 1, "prices")


# ── 不变式 7：默认值形状照生产口径 ───────────────────────────────────
def test_defaults_match_the_production_shapes(
    seed: SeedSqlite, sqlite_store: SQLiteStore
) -> None:
    """默认值不只是"有值"，还要与生产写入的**形状**一致。

    `extra_json` 必须是合法 JSON（生产 `json.loads` 直接读它）、`paused` 必须是
    INTEGER 布尔、时间戳必须能被 `CURRENT_TIMESTAMP` 的读者解析。
    形状错的默认值不会报错，只会让下游判据（长度/正则/枚举/JSON 解析）静默失效。
    """
    seed.job_log("incremental_update")
    seed.retry(STOCK, "price_daily", adapter="manager")
    seed.watchlist(STOCK)
    seed.override(STOCK, "revenue", 1000.0, reason="r")
    seed.refresh_state("csrc_industry_last_refresh", "{}")
    seed.auto_update_state()
    seed.trading_dates([REPORT_DATE])

    job = _one(sqlite_store, "job_logs")
    assert job["status"] == "running"
    assert TIMESTAMP_RE.fullmatch(str(job["started_at"]))
    assert json.loads(job["details_json"]) == {}

    retry = _one(sqlite_store, "retry_list")
    assert retry["retry_count"] == 0
    assert retry["max_retries"] == 5
    assert retry["error_kind"] in ERROR_KINDS
    assert retry["error_kind"] == "unknown"
    assert isinstance(json.loads(retry["extra_json"]), dict)

    watch = _one(sqlite_store, "watchlist")
    assert watch["group_name"] == "default"
    assert TIMESTAMP_RE.fullmatch(str(watch["added_at"]))

    override = _one(sqlite_store, "manual_overrides")
    assert override["status"] in OVERRIDE_STATUSES
    assert override["status"] == "active"
    assert TIMESTAMP_RE.fullmatch(str(override["created_at"]))

    refresh = _one(sqlite_store, "data_refresh_state")
    assert TIMESTAMP_RE.fullmatch(str(refresh["updated_at"]))

    state = _one(sqlite_store, STATE_TABLE)
    assert state["state"] in VALID_STATES
    assert state["state"] == "enabled"
    assert state["paused"] == 0
    assert state["current_stage"] == "idle"
    assert TIMESTAMP_RE.fullmatch(str(state["updated_at"]))

    assert _one(sqlite_store, "trading_dates")["trade_date"] == REPORT_DATE


# ── 不变式 8：确定性 ─────────────────────────────────────────────────
def test_default_values_are_deterministic_for_the_same_row(
    seed: SeedSqlite, sqlite_store: SQLiteStore
) -> None:
    """同一 (表, 键) 两次播种得到同一时间戳字面量 —— 不是 `now()`。

    `now()` 会让同一份种子每次不同：断言里一旦出现"这条比那条新"或者
    `WHERE updated_at > ?` 的判据，测试就变成随机变量。
    """
    seed.watchlist(STOCK)
    first = _one(sqlite_store, "watchlist")

    sqlite_store.execute("DELETE FROM watchlist")
    # 换一个**新实例**再播一遍：时间戳不能是构造时生成、实例内固定的值。
    SeedSqlite(sqlite_store).watchlist(STOCK)
    second = _one(sqlite_store, "watchlist")

    assert first["added_at"] == second["added_at"]
    # 也不能"刚好等于现在"：这里直接钉住工厂的固定字面量（不是再抄一份常量）。
    assert first["added_at"] == DEFAULT_SEED_TIME
    assert TIMESTAMP_RE.fullmatch(DEFAULT_SEED_TIME)


# ── 附加：NOT NULL / CHECK 失败不得冒充"主键冲突" ────────────────────
def test_not_null_violation_is_not_reported_as_a_duplicate(
    seed: SeedSqlite, sqlite_store: SQLiteStore
) -> None:
    """非唯一性的约束失败 MUST 抛 `SqliteSeedConstraint`，不是 `DuplicateSqliteSeed`。

    `data_refresh_state.value` 是 NOT NULL。把这种失败报成"主键冲突"会把人引到
    主键上去查（还会顺手让人以为 `overwrite=True` 能解决），白查一轮。
    """
    with pytest.raises(SqliteSeedConstraint, match="NOT NULL") as excinfo:
        seed.refresh_state("csrc_industry_last_refresh", None)  # type: ignore[arg-type]

    assert not isinstance(excinfo.value, DuplicateSqliteSeed)
    assert _count(sqlite_store, "data_refresh_state") == 0


# ── A：`join(conn)` —— 调用点已开事务时，工厂不许自己再开/提交 ───────────
class _NoTransactionStore:
    """把"工厂又去开事务/读连接"变成**立刻失败**的假 store。

    SQLite 侧不会像 DuckDB 那样死等文件锁，但"工厂自己 commit"同样致命：
    调用点的原子块会被**提前落库**，后半段抛错时前半段收不回来。这个假 store
    让那次多余的 `transaction()` 立刻炸掉，并把它记进 `calls` 供断言。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def transaction(self) -> None:
        self.calls.append("transaction")
        raise AssertionError("join(conn) 之后不许再开事务（提交权归调用方）")

    def connection(self) -> None:
        self.calls.append("connection")
        raise AssertionError("join(conn) 之后不许另开读连接（PRAGMA 用绑定连接）")

    def execute(self, *args: object, **kwargs: object) -> None:
        self.calls.append("execute")
        raise AssertionError("join(conn) 之后不许走 store.execute()")


def test_joined_sqlite_seed_never_opens_its_own_transaction(sqlite_store: SQLiteStore) -> None:
    """`join(conn)` 绑定后 MUST 一次 `store.transaction()` / `connection()` 都不开。

    这是缺口 A 在 SQLite 侧的全部意义：`with sqlite_store.transaction() as conn:`
    块里的播种原先只能写内联 SQL，就是因为工厂自带事务会把块拆成两次提交。
    """
    no_tx = _NoTransactionStore()
    with sqlite_store.transaction() as conn:
        SeedSqlite(no_tx).join(conn).missing_list("000001", "pe_ttm", "source_incomplete")  # type: ignore[arg-type]
        SeedSqlite(no_tx).join(conn).watchlist(STOCK)  # type: ignore[arg-type]

    assert no_tx.calls == []
    assert _count(sqlite_store, "missing_list") == 1
    assert _count(sqlite_store, "watchlist") == 1


def test_joined_sqlite_seed_defers_commit_to_the_caller(sqlite_store: SQLiteStore) -> None:
    """绑定的 façade **不提交**：调用方回滚，种子行必须一起消失。"""

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom), sqlite_store.transaction() as conn:
        seed = SeedSqlite(sqlite_store).join(conn)
        seed.missing_list("000001", "pe_ttm", "source_incomplete")
        seed.job_log("incremental_update")
        raise _Boom

    assert _count(sqlite_store, "missing_list") == 0
    assert _count(sqlite_store, "job_logs") == 0


def test_joined_sqlite_seed_keeps_column_validation_and_duplicate_classification(
    sqlite_store: SQLiteStore,
) -> None:
    """绑定后口径不变：拼错列名 `UnknownSqliteColumn`、重复唯一键 `DuplicateSqliteSeed`。"""
    with sqlite_store.transaction() as conn:
        seed = SeedSqlite(sqlite_store).join(conn)

        with pytest.raises(UnknownSqliteColumn, match="reason_cod"):
            seed.missing_list("000001", "pe_ttm", "source_incomplete", reason_cod="x")

        seed.missing_list("000001", "pe_ttm", "source_incomplete")
        with pytest.raises(DuplicateSqliteSeed, match="missing_list"):
            seed.missing_list("000001", "pe_ttm", "source_empty")

    assert _count(sqlite_store, "missing_list") == 1


# ── B：`dsl_expression` / `screening_rule` / `missing_list` ──────────────
def test_dsl_expression_versions_coexist_while_same_version_is_rejected(
    seed: SeedSqlite, sqlite_store: SQLiteStore
) -> None:
    """`dsl_expressions` 的唯一键是 `(name, version)`：多版本共存，同版本必抛。

    同一表达式发两个版本是**真实语义**（`version=1` 是线上锁定的那版，
    `version=2` 是草稿）；把它们按"名字"去重就会让发布流程无法表达。
    """
    seed.dsl_expression(
        "value_score", 1, "pe_ttm / pb_mrq",
        status="published", description="Value score", direction="higher_is_better",
        historical_capable=False, content_hash="locked-hash",
    )
    seed.dsl_expression("value_score", 2, "pe_ttm / pb_mrq * 2", content_hash="draft-hash")

    rows = sqlite_store.query(
        "SELECT name, version, status, content_hash, direction FROM dsl_expressions "
        "ORDER BY version"
    )
    assert [(row["version"], row["status"], row["content_hash"]) for row in rows] == [
        (1, "published", "locked-hash"),
        (2, "draft", "draft-hash"),
    ]
    assert rows[0]["direction"] == "higher_is_better"

    with pytest.raises(DuplicateSqliteSeed, match="dsl_expressions"):
        seed.dsl_expression("value_score", 1, "pe_ttm / pb_mrq", status="published")


def test_dsl_expression_defaults_leave_the_lock_fingerprint_null(
    seed: SeedSqlite, sqlite_store: SQLiteStore
) -> None:
    """`content_hash` 等业务列**没有默认值**：不写就是 NULL，`status` 才是 schema 默认。

    给 `content_hash` 编一个"像 sha256"的默认值会让"指纹缺失/陈旧"那组用例
    对着一个工厂编的假指纹跑 —— 它们断言的正是"锁里那一版与库里这一版不同"。
    """
    seed.dsl_expression("plain_score", 1, "pe_ttm")

    row = _one(sqlite_store, "dsl_expressions")
    assert row["status"] == "draft"
    assert row["content_hash"] is None
    assert row["description"] is None
    assert row["direction"] is None
    assert row["historical_capable"] is None
    assert row["ast_json"] is None
    assert TIMESTAMP_RE.fullmatch(str(row["created_at"]))


def test_dsl_expression_unknown_column_raises_and_writes_nothing(
    seed: SeedSqlite, sqlite_store: SQLiteStore
) -> None:
    """拼错列名照样当场红（`histroical_capable` → `UnknownSqliteColumn`），且一行不落。"""
    with pytest.raises(UnknownSqliteColumn, match="histroical_capable"):
        seed.dsl_expression("x", 1, "pe_ttm", histroical_capable=True)

    assert _count(sqlite_store, "dsl_expressions") == 0


def test_screening_rule_locks_indicators_and_rejects_same_version(
    seed: SeedSqlite, sqlite_store: SQLiteStore
) -> None:
    """`screening_rules` 三列 NOT NULL；`locked_indicators` 默认 `'{}'`（合法 JSON）。

    真实锁定内容必须逐字来自调用点（`{"score": {"version": 1, ...}}` 才是被断言的
    对象），工厂只给"没锁任何指标"的最小合法值。
    """
    seed.screening_rule("plain rule", 1, '{"conditions": {"logic": "AND", "rules": []}}')
    seed.screening_rule(
        "locked rule", 1, '{"conditions": {"logic": "AND", "rules": []}}',
        locked_indicators='{"score": {"version": 1, "content_hash": "locked-hash"}}',
        status="saved",
    )

    rows = sqlite_store.query(
        "SELECT name, locked_indicators, status FROM screening_rules ORDER BY id"
    )
    assert json.loads(rows[0]["locked_indicators"]) == {}
    assert json.loads(rows[1]["locked_indicators"]) == {
        "score": {"version": 1, "content_hash": "locked-hash"}
    }
    assert (rows[0]["status"], rows[1]["status"]) == ("draft", "saved")

    with pytest.raises(DuplicateSqliteSeed, match="screening_rules"):
        seed.screening_rule("plain rule", 1, '{"conditions": {}}')


def test_screening_run_duplicate_run_id_raises_and_optional_columns_stay_null(
    seed: SeedSqlite, sqlite_store: SQLiteStore
) -> None:
    """`screening_runs`：主键 `run_id` 重复必须抛；`data_date` 不写就是 NULL。

    为什么这条**单独**一个函数、没挂进不变式 2 的参数表：`screening_runs` 的
    `rule_id` 有外键 `REFERENCES screening_rules(id)`，造一行合法种子必须先建
    父行（两步），而那张参数表的契约是"一个 lambda = 一行种子"（父行若写在
    lambda 里，第二遍会先撞父行重复，报成 `screening_rules` 冲突）。父行只用
    :meth:`SeedSqlite.screening_rule` 建，不碰内联 SQL。

    `data_date` 是这张表唯一可空的业务列（生产在无 `as_of` 时留空），
    `created_at` 交给 schema 的 `DEFAULT CURRENT_TIMESTAMP` —— 工厂不替它编时间。
    """
    seed.screening_rule("run parent", 1, '{"conditions": {"logic": "AND", "rules": []}}')
    rule_id = sqlite_store.query(
        "SELECT id FROM screening_rules WHERE name = ? AND version = 1", ["run parent"]
    )[0]["id"]

    seed.screening_run(
        "run-1",
        rule_id,
        1,
        result_json='[{"stock_code":"000001"}]',
        columns_json='["stock_code"]',
        sort_json='[]',
        base_pool_config='{}',
        strict_only=True,
        confidence_summary='{"strict_only":true}',
    )

    row = _one(sqlite_store, "screening_runs")
    assert row["rule_id"] == rule_id
    assert row["rule_version"] == 1
    assert row["strict_only"] == 1
    assert row["data_date"] is None
    assert row["created_at"] is not None

    with pytest.raises(DuplicateSqliteSeed, match="screening_runs"):
        seed.screening_run(
            "run-1", rule_id, 1, result_json="[]", columns_json="[]", sort_json="[]",
            base_pool_config="{}", strict_only=False, confidence_summary="{}",
        )
    assert _count(sqlite_store, "screening_runs") == 1

    # `overwrite=True` 才是"改成我的值"（SQLite 的 REPLACE 是删旧行插新行：
    # 本次没提供的列回到 NULL，而不是保留旧值 —— 与 DuckDB 的 upsert 不同）。
    seed.screening_run(
        "run-1", rule_id, 1, result_json='[]', columns_json='[]', sort_json='[]',
        base_pool_config='{"pool":"all"}', strict_only=False,
        confidence_summary='{}', data_date="2025-12-31", overwrite=True,
    )
    replaced = _one(sqlite_store, "screening_runs")
    assert replaced["strict_only"] == 0
    assert replaced["data_date"] == "2025-12-31"
    assert replaced["base_pool_config"] == '{"pool":"all"}'

    # `join(conn)`：调用点已开事务时方法不自开、不提交（提交权归调用方）。
    with sqlite_store.transaction() as conn:
        SeedSqlite(sqlite_store).join(conn).screening_run(
            "joined-run", rule_id, 1, result_json='[]', columns_json='[]',
            sort_json='[]', base_pool_config='{}', strict_only=False,
            confidence_summary='{}',
        )
    assert _count(sqlite_store, "screening_runs") == 2


def test_missing_list_open_rows_are_unique_while_resolved_history_coexists(
    seed: SeedSqlite, sqlite_store: SQLiteStore
) -> None:
    """部分唯一索引 `uq_missing_list_stock_field_open`：未解决去重、已解决可共存。

    这是 GC 与"可行动缺失计数"两组用例的前提：同股票同字段可以有一条打开的行
    **加**多条已解决的历史行，但打开的行只能有一条（否则计数翻倍）。
    """
    seed.missing_list("000001", "pe_ttm", "source_incomplete")
    with pytest.raises(DuplicateSqliteSeed, match="missing_list"):
        seed.missing_list("000001", "pe_ttm", "source_empty")

    # 已解决的历史行不占槽位。
    seed.missing_list("000001", "pe_ttm", "source_incomplete", resolved_at="2026-01-01 00:00:00")
    seed.missing_list("000002", "price.close", "source_empty")
    assert _count(sqlite_store, "missing_list") == 3

    open_rows = sqlite_store.query(
        "SELECT stock_code FROM missing_list WHERE resolved_at IS NULL ORDER BY stock_code"
    )
    assert open_rows == [{"stock_code": "000001"}, {"stock_code": "000002"}]


def test_missing_list_detected_at_falls_back_to_the_database_clock(
    seed: SeedSqlite, sqlite_store: SQLiteStore
) -> None:
    """`detected_at` 不传就走 schema 的 `DEFAULT CURRENT_TIMESTAMP`（"库里发现的时刻"）。

    工厂不把这一列显式写 NULL，也不编一个字面量：留空让 schema 默认生效，
    所以它必须落在库侧"现在 ±1 分钟"里。
    """
    seed.missing_list("000001", "pe_ttm", "source_incomplete")

    row = _one(sqlite_store, "missing_list")
    assert row["resolved_at"] is None
    assert sqlite_store.query(
        "SELECT COUNT(*) AS c FROM missing_list "
        "WHERE detected_at BETWEEN datetime('now', '-1 minute') AND datetime('now', '+1 minute')"
    )[0]["c"] == 1


def test_missing_list_rejects_the_nonexistent_detail_column(
    seed: SeedSqlite, sqlite_store: SQLiteStore
) -> None:
    """任务清单里提过的 `detail` 列在当前 schema **不存在** → 必须当场红。

    工厂不"顺手加个默认列"：`detail=` 若被静默吞掉，调用点会以为记下了原因细节，
    而库里什么都没有（诊断信息静默丢失比报错难查得多）。
    """
    with pytest.raises(UnknownSqliteColumn, match="detail"):
        seed.missing_list("000001", "pe_ttm", "source_incomplete", detail="解析失败")

    assert _count(sqlite_store, "missing_list") == 0


# ── E：`refresh_state(updated_now=True)` —— "刚刷新过" ─────────────────
def test_refresh_state_updated_now_is_fresh_by_the_database_clock(
    seed: SeedSqlite, sqlite_store: SQLiteStore
) -> None:
    """`updated_now=True` 必须产生**真的**"刚刷新过"，默认值仍是固定字面量。

    7 处 `data_refresh_state` 用例的前提是"刚刷新过 → 日内节流生效"；工厂默认的
    固定字面量会把它们**静默**变成"很久没刷过"，于是被测逻辑走"该刷新"分支，
    断言要么红得莫名其妙、要么对着错误路径绿。这条同时钉住：
    `updated_at` 落在库侧 `datetime('now')` 的 ±1 分钟内，且**不是**那个固定字面量。
    """
    seed.refresh_state("csrc_industry_last_refresh", "2026-09-19", updated_now=True)

    row = _one(sqlite_store, "data_refresh_state")
    assert row["updated_at"] != DEFAULT_SEED_TIME
    assert TIMESTAMP_RE.fullmatch(str(row["updated_at"]))
    assert sqlite_store.query(
        "SELECT COUNT(*) AS c FROM data_refresh_state "
        "WHERE updated_at BETWEEN datetime('now', '-1 minute') AND datetime('now', '+1 minute')"
    )[0]["c"] == 1

    # 默认路径不受影响：仍是确定性字面量（"同一份种子"不该是随机变量）。
    sqlite_store.execute("DELETE FROM data_refresh_state")
    SeedSqlite(sqlite_store).refresh_state("csrc_industry_last_refresh", "2026-09-19")
    assert _one(sqlite_store, "data_refresh_state")["updated_at"] == DEFAULT_SEED_TIME


def test_refresh_state_rejects_updated_now_together_with_an_explicit_timestamp(
    seed: SeedSqlite, sqlite_store: SQLiteStore
) -> None:
    """`updated_now=True` 与显式 `updated_at=` 同时给 = 调用点没说清要哪个 → 抛。

    "两个都要"只可能有一个生效：静默让其中一个赢，正是"我改了参数却毫无效果"
    那类事故的入口。
    """
    with pytest.raises(SqliteSeedError, match="updated_now"):
        seed.refresh_state(
            "csrc_industry_last_refresh", "2026-09-19",
            updated_now=True, updated_at="2026-01-01 00:00:00",
        )

    assert _count(sqlite_store, "data_refresh_state") == 0
