"""已动用资本回报率 ROCE 域（books: 《我从达尔文那里学到的投资知识》第 2 章）。

口径来源（用户裁决，2026-11-16，`reports/130`）
------------------------------------------------
原书定义：**非金融企业 ROCE = 息税前利润 ÷（净营运资本 + 净固定资产）**，
且强调用「**历史**」ROCE（过去 5–10 年甚至更长时间），筛选门槛 20%。

落地到本项目的数据映射（口径 C，用户 2026-11-16 拍板）：

    已动用资本 CE(t) = 总资产(t) − 流动负债(t)
                     = 净营运资本 + 全部长期资产

两个关键裁定与依据：

1. **「净固定资产」取「全部长期资产（非流动资产）」而不是 `balance_sheet.fixed_assets`**。
   原书说的 *net fixed assets* 在经典资产负债表里是「Fixed Assets」整段（对 Current
   Assets），含在建工程、无形资产、长期股权投资、使用权资产等。本项目实测只取
   `fixed_assets`（PPE）会系统性高估重资产公司：
   中国海油非流动资产 8,003 亿、其中 `fixed_assets` 只有 85 亿（油气资产无对应字段），
   只算 PPE 会得出 119% ROCE，比贵州茅台还高——排序完全失真。
   `净营运资本 + 非流动资产 ≡ 总资产 − 流动负债` 是会计恒等式，两者同值。

2. **不剔除超额现金**。原书倾向剔除（「若将现金计入投入资本，会不必要地拉低 ROCE」），
   但同时写明「**如果你对这种处理方式存疑，也可以将部分现金计入投入资本**」。
   本项目实测：A 股白酒/家电的现金占流动资产 70–90%，按 `现金 − 债务` 全额剔除会把
   分母掏空——格力剔现金后已动用资本从 −204 亿爬到 +26 亿，ROCE 因此从 −81% 跳到
   4,405%；五粮液 2024 年分母 −54 亿，ROCE 直接变成 −7,194%（符号翻转）。
   剔除现金版全市场 30.9% 的股票「超过 20%」，与书中「这类企业虽罕见」矛盾；
   不剔现金版只有 5.9%，量级吻合。故**主口径不剔现金**。

期间口径
--------
- 只用**年报**（12-31）：ROCE 是年度经营回报，季报累计值直接除年平均资本会系统性低估。
- `EBIT = 利润总额 + 财务费用`（与 `indicator_ext.ebit`、CSMAR FI_T5 同源）。
- 单年 ROCE = 当年 EBIT ÷ 平均已动用资本（期初 = 上一年报 CE，期末 = 当年报 CE）。
- 快照值 = **最近 5 年 / 10 年有效年报 ROCE 的中位数**（书中用法），
  外加最新一年年报 ROCE 与各窗口的**有效年数**，让人一眼看出这是几年的中位数。

不设任何比例护栏
----------------
用户 2026-11-16 裁决：「有极值是正常的。我们只要验证这个数据是真实的、是对的就行了，
不需要剔除极值。」因此本域**不做任何 |ROCE| 上限、不做市值/ST/亏损过滤**。
唯一的 NULL 来自 math：资本占用 ≤ 0 时比值无定义（除法不成立），
此时 `roce_history.status` 如实写 `INVALID_DENOMINATOR`，CE 原值照常保留可复核。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.update_lock import exclusive_update

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注
    from app.core.storage.path_policy import DatabasePathSet

logger = logging.getLogger(__name__)

DATA_VERSION = 1
SOURCE = "derived_roce"

#: 5 年 / 10 年窗口（按每股最近 N 个**有效年报**取值）
ROCE_WINDOWS: tuple[int, ...] = (5, 10)

#: 有效分母（资本占用 > 0）的状态码
STATUS_OK = "OK"
STATUS_INVALID_DENOMINATOR = "INVALID_DENOMINATOR"
STATUS_MISSING_EBIT = "MISSING_EBIT"
STATUS_NO_PRIOR_PERIOD = "NO_PRIOR_PERIOD"   # 序列首年：没有上年报表，算不出平均资本

#: 年报期过滤（本域只吃年报，理由见模块 docstring「期间口径」）
_ANNUAL = "strftime(report_date, '%m-%d') = '12-31'"

HISTORY_SELECT_SQL = f"""
    INSERT INTO roce_history BY NAME
    SELECT
        stock_code,
        report_date,
        ce,
        ce_prev,
        ebit,
        CASE WHEN ce > 0 AND ce_prev > 0 AND ebit IS NOT NULL
             THEN ebit / ((ce + ce_prev) / 2.0) END AS roce,
        CASE
            WHEN ce IS NULL OR ce_prev IS NULL      THEN '{STATUS_NO_PRIOR_PERIOD}'
            WHEN ebit IS NULL                       THEN '{STATUS_MISSING_EBIT}'
            WHEN ce <= 0 OR ce_prev <= 0            THEN '{STATUS_INVALID_DENOMINATOR}'
            ELSE '{STATUS_OK}'
        END AS status,
        ? AS calculated_at,
        ? AS source,
        ? AS data_version
    FROM (
        SELECT
            b.stock_code,
            b.report_date,
            (b.total_assets - b.total_current_liabilities) AS ce,
            LAG(b.total_assets - b.total_current_liabilities)
                OVER (PARTITION BY b.stock_code ORDER BY b.report_date) AS ce_prev,
            i.ebit
        FROM (
            SELECT stock_code, report_date, total_assets, total_current_liabilities
            FROM balance_sheet
            WHERE {_ANNUAL}
              AND total_assets IS NOT NULL
              AND total_current_liabilities IS NOT NULL
        ) b
        LEFT JOIN (
            SELECT stock_code, report_date,
                   (total_profit + financial_expenses) AS ebit
            FROM income_statement
            WHERE {_ANNUAL}
              AND total_profit IS NOT NULL
              AND financial_expenses IS NOT NULL
        ) i USING (stock_code, report_date)
    ) src
"""

SNAPSHOT_UPDATE_SQL = """
    UPDATE indicator_snapshot AS s
    SET roce_latest       = agg.latest,
        roce_5y_median    = agg.med5,
        roce_5y_years     = agg.n5,
        roce_10y_median   = agg.med10,
        roce_10y_years    = agg.n10
    FROM (
        SELECT stock_code,
               MAX(CASE WHEN rn = 1 THEN roce END)                 AS latest,
               MEDIAN(roce) FILTER (WHERE rn <= 5)                 AS med5,
               COUNT(roce) FILTER (WHERE rn <= 5)                  AS n5,
               MEDIAN(roce) FILTER (WHERE rn <= 10)                AS med10,
               COUNT(roce) FILTER (WHERE rn <= 10)                 AS n10
        FROM (
            SELECT stock_code, report_date, roce,
                   ROW_NUMBER() OVER (PARTITION BY stock_code
                                      ORDER BY report_date DESC) AS rn
            FROM roce_history
            WHERE roce IS NOT NULL
        ) ranked
        GROUP BY stock_code
    ) agg
    WHERE s.stock_code = agg.stock_code
"""


class RoceBuilder:
    """构建 roce_history 并用它刷新 indicator_snapshot 的五列。

    设计意图（`reports/130`）：
    - `roce_history` 是**全历史年报序列**（可回溯、可画图、可审计）；
    - `indicator_snapshot.roce_*` 是**筛选入口**（横截面用）；
    - 两者同源同事务，绝不各算一套。
    """

    def __init__(self, duck: DuckDBStore, paths: DatabasePathSet | None = None) -> None:
        self._duck = duck
        self._paths = paths

    def _lock_path(self):
        return self._paths.duckdb_path if self._paths is not None else self._duck.db_path

    def build(self) -> dict[str, Any]:
        """全量重建 ROCE 域（历史序列 + 快照汇总，同一事务）。

        为什么全量而不是增量：本域的数据源是**年报**（每股每年一行），
        全库 `roce_history` 目前规模在十万行量级，一条 INSERT…SELECT
        的耗时远小于维护增量水位 + 补偿逻辑的复杂度（reports/119 D28 的
        「派生域没有重建触发就会冻结」教训，这里用最笨但不会错的方式规避）。
        """
        calculated_at = datetime.now(UTC).replace(tzinfo=None)
        with exclusive_update(self._lock_path()), self._duck.write_connection() as conn:
            conn.execute("DELETE FROM roce_history")
            conn.execute(HISTORY_SELECT_SQL, [calculated_at, SOURCE, DATA_VERSION])
            conn.execute(SNAPSHOT_UPDATE_SQL)
            rows = conn.execute(
                """SELECT COUNT(*) AS n,
                          COUNT(roce) AS ok,
                          COUNT(*) FILTER (WHERE status = 'INVALID_DENOMINATOR') AS bad_denom,
                          COUNT(*) FILTER (WHERE status = 'MISSING_EBIT') AS no_ebit,
                          COUNT(*) FILTER (WHERE status = 'NO_PRIOR_PERIOD') AS no_prior,
                          MIN(report_date) AS d0, MAX(report_date) AS d1
                   FROM roce_history"""
            ).fetchone()
            snap = conn.execute(
                """SELECT COUNT(*) FILTER (WHERE roce_5y_median IS NOT NULL) AS med5,
                          COUNT(*) FILTER (WHERE roce_10y_median IS NOT NULL) AS med10,
                          COUNT(*) FILTER (WHERE roce_latest IS NOT NULL) AS latest,
                          COUNT(*) FILTER (WHERE roce_5y_median >= 0.20) AS ge20,
                          COUNT(*) AS total
                   FROM indicator_snapshot"""
            ).fetchone()
        report = {
            "status": "success",
            "scope": "all",
            "history_rows": int(rows[0]),
            "history_roce_ok": int(rows[1]),
            "history_invalid_denominator": int(rows[2]),
            "history_missing_ebit": int(rows[3]),
            "history_no_prior_period": int(rows[4]),
            "report_date_range": [str(rows[5]), str(rows[6])],
            "snapshot": {
                "roce_latest": int(snap[2]),
                "roce_5y_median": int(snap[0]),
                "roce_10y_median": int(snap[1]),
                "roce_5y_ge_20pct": int(snap[3]),
                "total": int(snap[4]),
            },
            "data_version": DATA_VERSION,
        }
        logger.info("ROCE 域构建完成: %s", report)
        return report
