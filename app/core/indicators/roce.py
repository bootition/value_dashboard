"""已动用资本回报率 ROCE 域（《我从达尔文那里学到的投资知识》第 2 章）。

原书定义
--------
> 「非金融企业的 ROCE 可定义为：**息税前利润 ÷（净营运资本 + 净固定资产）**」
> 「我们倾向于剔除超额现金（即现金减去债务后的余额，如果现金远多于债务的话）」
> 「如果你对这种处理方式存疑，也可以将部分现金计入投入资本。」

最终口径（2026-11-16 定稿，`reports/132`）
------------------------------------------
    已动用资本 CE(t) = 总资产(t) − 流动负债(t)
                     = 净营运资本 + 全部长期资产
                     ------------------------------------
    EBIT(t)          = 利润总额(t) + 财务费用(t)
    单年 ROCE(t)     = EBIT(t) ÷ ((CE(t−1) + CE(t)) / 2)
    主指标            = 最近 10 个有效年报 ROCE 的中位数（5 年值做趋势辅助）
    **现金计入分母，不剔除。**

裁定一：「净固定资产」= 全部长期资产（非流动资产），不是 `balance_sheet.fixed_assets`
--------------------------------------------------------------------------------
原书 *net fixed assets* 对应经典资产负债表的「Fixed Assets」整段（对 Current Assets），
含在建工程、无形资产、长期股权投资、使用权资产、油气资产。
若只取 `fixed_assets`（PPE）：
- 中国海油非流动资产 8,003 亿、`fixed_assets` 仅 85 亿（油气资产无对应列）→ 算出 119%；
- 海天味业 90.5%、公牛集团 80.6%、天孚通信 83.1%（分母只剩几十亿）；
- 白酒/软件类公司普遍飞出 100%，与书中标杆（蒂芙尼 16%、好市多 22%、
  纳兰达组合中位 42%、Havell 52%）完全脱节。
`净营运资本 + 非流动资产 ≡ 总资产 − 流动负债` 是会计恒等式，取后者即取全部长期资产。

裁定二：**不剔除超额现金**——这是本域最容易被"按书直译"做坏的地方
------------------------------------------------------------------
书里那句「剔除超额现金」是**向上修正**（别让闲置现金压低好公司的 ROCE），
前提是「现金远多于债务」。A 股现金占资产的比例远高于书中的印度样本，
机械执行会**把分母削到零**，制造出比原意更离谱的数字。实测（2025 年报）：

| 公司 | 现金/总资产 | 剔现金后已动用资本 | 剔现金 ROCE | 不剔现金 ROCE |
|---|---|---|---|---|
| 同花顺 | 89% | 2 亿 | **444%** | 24.1% |
| 重庆啤酒 | 10%（营运资本为负） | −0.4 亿 | **分母为负** | 68.4% |
| 海天味业 | 82% | 38 亿 | **90.5%** | 34.2% |
| 公牛集团 | 72% | 26 亿 | **80.6%** | 34.4% |
| 贵州茅台 | 17% | 2,011 亿 | 51.0% | 45.2% |

同花顺剔完现金只剩 2 亿资本、EBIT 35 亿 → 444%。任何"给 100 元赚 444 元"的
比率都不是投资结论，是分母被削掉的算术产物；它市值 1,500 亿，按市价买回报率只有 2.3%。

**判据：用书里的标杆校验量级。** 不剔现金时全市场（市值≥100亿、非金融）
中位 10.3%、p90 19.7%、最高 68.4%、≥20% 仅 117 只（9.2%）——与书中
「好市多 22%、蒂芙尼 16%、组合中位 42%、20% 以上虽罕见但确实存在」吻合；
剔现金时有 30% 的公司超过 20%，且上限飞出 400%+。
书中也留了授权：「**也可以将部分现金计入投入资本**」。故取"计入"。

裁定三：不设任何比例护栏
------------------------
「有极值是正常的。只要验证数据是真实的、是对的，不需要剔除极值。」
因此不做 |ROCE| 上限、不做截断。唯一的 NULL 来自数学：CE ≤ 0 时比值无定义，
此时 `roce_history.status` 如实标注 `INVALID_DENOMINATOR`，CE 原值照常保留可复核。
同理，ROE 域里的 `abs(roe) <= 1.0` 静默置空属于同一类错误，应删除（`reports/132` §5）。

期间口径
--------
- 只用**年报**（12-31）：季报累计值直接除年平均资本会系统性低估。
- 单年 ROCE 用**平均资本**（期初 = 上一年报 CE，期末 = 当年报 CE）。

筛选口径（`roce_eligible` 快照标志，2026-11-16 用户定稿）
--------------------------------------------------------
1. **非金融**：银行/保险/券商没有流动-非流动划分、利润表无「财务费用」行
   （实测 121 只金融股中 100 只缺 `total_current_assets`、101 只缺 `financial_expenses`），
   ROCE 在会计上不适用；书里也明写「**非金融企业**的 ROCE」。
2. **总市值 ≥ 100 亿**：剔除微盘分母噪音。
3. **有效 ROCE 年数 ≥ 8**：书里「初步筛选出的约 150 家公司，都在过去 5 到 10 年
   甚至更长时间里实现了超过 20% 的 ROCE」。用"有效年数"而不是"上市满 N 年"，
   因为算单年 ROCE 需要上一年的资本，"上市满 5 年"实际只够算出 4 个值。

⚠️ 提醒：10 年中位数会掩盖「历史好、现在崩」的公司（智飞生物 10 年 36.9%、
最新 −66.4%；卓胜微 44.1% / −2.6%）。因此三者必须并列展示：
`roce_10y_median`（历史成色）+ `roce_5y_median`（趋势）+ `roce_latest`（现况）。
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

#: 筛选口径（module docstring「筛选口径」）：有效年数下限与市值下限
ELIGIBLE_MIN_YEARS = 8
ELIGIBLE_MIN_MARKET_CAP = 1e10

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
    SET roce_latest     = agg.latest,
        roce_5y_median  = agg.med5,
        roce_5y_years   = agg.n5,
        roce_10y_median = agg.med10,
        roce_10y_years  = agg.n10,
        roce_eligible   = agg.eligible
    FROM (
        SELECT r.stock_code, r.latest, r.med5, r.n5, r.med10, r.n10,
               (r.n10 >= ? AND r.med10 IS NOT NULL
                AND COALESCE(snap.total_market_cap, 0) >= ?
                AND COALESCE(meta.sw_level1, '') NOT IN ('银行', '非银金融')) AS eligible
        FROM (
            SELECT stock_code,
                   MAX(CASE WHEN rn = 1 THEN roce END)   AS latest,
                   MEDIAN(roce) FILTER (WHERE rn <= 5)   AS med5,
                   COUNT(roce) FILTER (WHERE rn <= 5)    AS n5,
                   MEDIAN(roce) FILTER (WHERE rn <= 10)  AS med10,
                   COUNT(roce) FILTER (WHERE rn <= 10)   AS n10
            FROM (
                SELECT stock_code, report_date, roce,
                       ROW_NUMBER() OVER (PARTITION BY stock_code
                                          ORDER BY report_date DESC) AS rn
                FROM roce_history
                WHERE roce IS NOT NULL
            ) ranked
            GROUP BY stock_code
        ) r
        LEFT JOIN stock_meta meta USING (stock_code)
        LEFT JOIN indicator_snapshot snap USING (stock_code)
    ) agg
    WHERE s.stock_code = agg.stock_code
"""


# 住在 `indicator_snapshot` 里、由本域（而不是指标计算器 staging）生产的列。
# 登记到 `app/core/indicators/calculator.py::derived_snapshot_domains()` 后，
# 两条发布路径都会先保全、再调用 `rebuild_snapshot_columns()`、最后校验没有列
# 被清空（`reports/133` R3；事故来源 `reports/131` K5/D28）。
SNAPSHOT_DERIVED_COLUMNS: tuple[str, ...] = (
    "roce_latest",
    "roce_5y_median",
    "roce_5y_years",
    "roce_10y_median",
    "roce_10y_years",
    "roce_eligible",
)


def rebuild_snapshot_columns(connection: Any) -> None:
    """用 `roce_history` 重算 `indicator_snapshot` 的 ROCE 六列（幂等，毫秒级）。"""
    connection.execute(SNAPSHOT_UPDATE_SQL, [ELIGIBLE_MIN_YEARS, ELIGIBLE_MIN_MARKET_CAP])


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
            conn.execute(
                SNAPSHOT_UPDATE_SQL,
                [ELIGIBLE_MIN_YEARS, ELIGIBLE_MIN_MARKET_CAP],
            )
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
