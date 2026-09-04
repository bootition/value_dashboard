"""A+H 上市映射（2026-09-04 快照 + 人工覆写）

映射构建顺序：
1. 以 `ak.stock_zh_ah_spot()` 返回的港股代码/名称为输入；
2. 名称做 NFKC/去空白归一后与 DuckDB stock_meta（当前上市池）精确匹配；
3. 精确匹配不到的 H 股名称由 MANUAL_OVERRIDES 按官方简称/更名关系覆写；
4. 任一港股代码只保留一个 A 股代码，绝不使用模糊后缀剥离猜映射。

本快照不是临时探测脚本：它是持久化的代码数据，未来“总市场分红融资比”
需要把 hk_dividends（港股代码）join 回 A 股代码时直接使用。
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import unicodedata
from typing import Any

from app.core.storage.duckdb_store import DuckDBStore

logger = logging.getLogger(__name__)

__all__ = [
    "AH_SPOT_NAME_SNAPSHOT",
    "MANUAL_OVERRIDES",
    "build_ah_hk_mapping",
    "normalize_company_name",
    "refresh_ah_spot_snapshot",
]

AH_SPOT_SNAPSHOT_CAPTURED_AT = "2026-09-04"
AH_SPOT_NAME_SNAPSHOT: dict[str, str] = {
    "03678": "弘业期货",
    "01108": "凯盛新能",
    "02238": "广汽集团",
    "02068": "中铝国际",
    "01375": "中州证券",
    "00323": "马鞍山钢铁股份",
    "00895": "东江环保",
    "01618": "中国中冶",
    "00338": "上海石油化工股份",
    "00347": "鞍钢股份",
    "01528": "红星美凯龙",
    "03996": "中国能源建设",
    "02866": "中远海发",
    "06196": "郑州银行",
    "01053": "重庆钢铁股份",
    "00588": "北京北辰实业股份",
    "02880": "辽港股份",
    "01033": "中石化油服",
    "02009": "金隅集团",
    "01812": "晨鸣纸业",
    "03308": "中际旭创",
    "03750": "宁德时代",
    "03986": "兆易创新",
    "09630": "芯碁微装",
    "01377": "鼎泰高科",
    "06809": "澜起科技",
    "06160": "百济神州",
    "02476": "胜宏科技",
    "02359": "药明康德",
    "06869": "长飞光纤光缆",
    "02676": "纳芯微",
    "00668": "安克创新",
    "06821": "凯莱英",
    "01304": "峰岹科技",
    "01989": "广合科技",
    "06951": "三环集团",
    "01347": "华虹宏力",
    "03200": "大族数控",
    "09980": "东鹏饮料",
    "00300": "美的集团",
    "03223": "君正股份",
    "06166": "剑桥科技",
    "01211": "比亚迪股份",
    "03661": "圣邦股份",
    "09995": "荣昌生物",
    "00941": "中国移动",
    "03296": "华勤技术",
    "00501": "豪威集团",
    "00981": "中芯国际",
    "02648": "安井食品",
    "02631": "天岳先进",
    "02315": "百奥赛图-B",
    "02475": "立讯精密",
    "02318": "中国平安",
    "03606": "福耀玻璃",
    "03968": "招商银行",
    "01336": "新华保险",
    "01880": "中国中免",
    "01276": "恒瑞医药",
    "01088": "中国神华",
    "06693": "赤峰黄金",
    "00168": "青岛啤酒股份",
    "03347": "泰格医药",
    "02692": "兆威机电",
    "02714": "牧原股份",
    "09927": "赛力斯",
    "02768": "国恩科技",
    "01772": "赣锋锂业",
    "00358": "江西铜业股份",
    "02899": "紫金矿业",
    "09696": "天齐锂业",
    "01187": "可孚医疗",
    "02338": "潍柴动力",
    "02601": "中国太保",
    "06936": "顺丰控股",
    "02628": "中国人寿",
    "03898": "时代电气",
    "03759": "康龙化成",
    "06185": "康希诺生物",
    "00470": "先导智能",
    "01081": "大金重工",
    "03288": "海天味业",
    "02249": "晶合集成",
    "06030": "中信证券",
    "01787": "山东黄金",
    "01385": "上海复旦",
    "02579": "中伟新材",
    "00883": "中国海洋石油",
    "02050": "三花智控",
    "06613": "蓝思科技",
    "00763": "中兴通讯",
    "06127": "昭衍新药",
    "00921": "海信家电",
    "09611": "龙旗科技",
    "03908": "中金公司",
    "02218": "安德利果汁",
    "00537": "普源精电",
    "06690": "海尔智家",
    "02493": "迈威生物-B",
    "01513": "丽珠医药",
    "06031": "三一重工",
    "01072": "东方电气",
    "03268": "美格智能",
    "01776": "广发证券",
    "01877": "君实生物",
    "06886": "华泰证券",
    "01138": "中远海能",
    "06655": "华新建材",
    "06680": "金力永磁",
    "00914": "海螺水泥",
    "06826": "昊海生物科技",
    "03993": "洛阳钼业",
    "02196": "复星医药",
    "02715": "埃斯顿",
    "01919": "中远海控",
    "00995": "安徽皖通高速公路",
    "06099": "招商证券",
    "09969": "诺诚健华",
    "02611": "国泰海通",
    "02865": "钧达股份",
    "00874": "白云山",
    "00317": "中船防务",
    "01171": "兖矿能源",
    "02603": "吉宏股份",
    "00564": "中创智领",
    "06066": "中信建投证券",
    "02402": "亿华通",
    "00699": "均胜电子",
    "02607": "上海医药",
    "01898": "中煤能源",
    "00857": "中国石油股份",
    "00177": "江苏宁沪高速公路",
    "09981": "沃尔核材",
    "00939": "建设银行",
    "02465": "龙蟠科技",
    "00038": "第一拖拉机股份",
    "02208": "金风科技",
    "02701": "国民技术",
    "02600": "中国铝业",
    "01963": "重庆银行",
    "00998": "中信银行",
    "02039": "中集集团",
    "01858": "春立医疗",
    "06881": "中国银河",
    "03328": "交通银行",
    "00811": "新华文轩",
    "02333": "长城汽车",
    "06178": "光大证券",
    "00638": "广和通",
    "01398": "工商银行",
    "06067": "星源材质",
    "06198": "青岛港",
    "02883": "中海油田服务",
    "02691": "南华期货股份",
    "06865": "福莱特玻璃",
    "00548": "深圳高速公路股份",
    "03618": "重庆农村商业银行",
    "01688": "领益智造",
    "01288": "农业银行",
    "01339": "中国人民保险集团",
    "03988": "中国银行",
    "03958": "东方证券",
    "01330": "绿色动力环保",
    "01157": "中联重科",
    "00719": "山东新华制药股份",
    "00902": "华能国际电力股份",
    "01658": "邮储银行",
    "03866": "青岛银行",
    "01766": "中国中车",
    "00916": "龙源电力",
    "00598": "中国外运",
    "00107": "四川成渝高速公路",
    "00386": "中国石油化工股份",
    "00568": "山东墨龙",
    "00728": "中国电信",
    "01186": "中国铁建",
    "09989": "海普瑞",
    "01057": "浙江世宝",
    "01456": "国联民生",
    "01071": "华电国际电力股份",
    "00753": "中国国航",
    "01065": "天津创业环保股份",
    "01988": "民生银行",
    "01800": "中国交通建设",
    "00390": "中国中铁",
    "00553": "南京熊猫电子股份",
    "00956": "新天绿色能源",
    "03969": "中国通号",
    "01816": "中广核电力",
    "06745": "滨化股份",
    "01055": "中国南方航空股份",
    "02727": "上海电气",
    "06818": "中国光大银行",
    "00187": "京城机电股份",
    "00670": "中国东方航空股份",
    "06806": "申万宏源",
    "00991": "大唐发电",
    "01349": "复旦张江",
    "02202": "万科企业",
    "02016": "浙商银行",
    "00525": "广深铁路股份",
    "03369": "秦港股份",
    "01635": "大众公用",
}

MANUAL_OVERRIDES: dict[str, dict[str, str]] = {
    "01375": {
        "a_code": "601375",
        "hk_name": "中州证券",
        "a_name": "中原证券",
        "reason": "H 股历史简称与 A 股现名不同",
    },
    "00323": {
        "a_code": "600808",
        "hk_name": "马鞍山钢铁股份",
        "a_name": "马钢股份",
        "reason": "H 股全称 vs A 股简称",
    },
    "00338": {
        "a_code": "600688",
        "hk_name": "上海石油化工股份",
        "a_name": "上海石化",
        "reason": "H 股全称 vs A 股简称",
    },
    "01528": {
        "a_code": "601828",
        "hk_name": "红星美凯龙",
        "a_name": "美凯龙",
        "reason": "A 股更名",
    },
    "03996": {
        "a_code": "601868",
        "hk_name": "中国能源建设",
        "a_name": "中国能建",
        "reason": "H 股全称 vs A 股简称",
    },
    "01053": {
        "a_code": "601005",
        "hk_name": "重庆钢铁股份",
        "a_name": "重庆钢铁",
        "reason": "H 股全称 vs A 股简称",
    },
    "00588": {
        "a_code": "601588",
        "hk_name": "北京北辰实业股份",
        "a_name": "北辰实业",
        "reason": "H 股全称 vs A 股简称",
    },
    "01033": {
        "a_code": "600871",
        "hk_name": "中石化油服",
        "a_name": "石化油服",
        "reason": "H 股与 A 股官方简称不同",
    },
    "01812": {
        "a_code": "000488",
        "hk_name": "晨鸣纸业",
        "a_name": "ST晨鸣",
        "reason": "A 股现名含 ST 前缀",
    },
    "06869": {
        "a_code": "601869",
        "hk_name": "长飞光纤光缆",
        "a_name": "长飞光纤",
        "reason": "H 股全称 vs A 股简称",
    },
    "01211": {
        "a_code": "002594",
        "hk_name": "比亚迪股份",
        "a_name": "比亚迪",
        "reason": "H 股全称 vs A 股简称",
    },
    "02315": {
        "a_code": "688796",
        "hk_name": "百奥赛图-B",
        "a_name": "百奥赛图",
        "reason": "H 股 -B 后缀",
    },
    "00168": {
        "a_code": "600600",
        "hk_name": "青岛啤酒股份",
        "a_name": "青岛啤酒",
        "reason": "H 股全称 vs A 股简称",
    },
    "02768": {
        "a_code": "002768",
        "hk_name": "国恩科技",
        "a_name": "国恩股份",
        "reason": "H 股与 A 股官方简称不同",
    },
    "00358": {
        "a_code": "600362",
        "hk_name": "江西铜业股份",
        "a_name": "江西铜业",
        "reason": "H 股全称 vs A 股简称",
    },
    "06185": {
        "a_code": "688185",
        "hk_name": "康希诺生物",
        "a_name": "康希诺",
        "reason": "H 股全称 vs A 股简称",
    },
    "01385": {
        "a_code": "688385",
        "hk_name": "上海复旦",
        "a_name": "复旦微电",
        "reason": "A 股更名",
    },
    "00883": {
        "a_code": "600938",
        "hk_name": "中国海洋石油",
        "a_name": "中国海油",
        "reason": "H 股全称 vs A 股简称",
    },
    "02218": {
        "a_code": "605198",
        "hk_name": "安德利果汁",
        "a_name": "安德利",
        "reason": "H 股全称 vs A 股简称",
    },
    "02493": {
        "a_code": "688062",
        "hk_name": "迈威生物-B",
        "a_name": "迈威生物",
        "reason": "H 股 -B 后缀",
    },
    "01513": {
        "a_code": "000513",
        "hk_name": "丽珠医药",
        "a_name": "丽珠集团",
        "reason": "H 股与 A 股官方简称不同",
    },
    "06826": {
        "a_code": "688366",
        "hk_name": "昊海生物科技",
        "a_name": "昊海生科",
        "reason": "H 股全称 vs A 股简称",
    },
    "00995": {
        "a_code": "600012",
        "hk_name": "安徽皖通高速公路",
        "a_name": "皖通高速",
        "reason": "H 股全称 vs A 股简称",
    },
    "06066": {
        "a_code": "601066",
        "hk_name": "中信建投证券",
        "a_name": "中信建投",
        "reason": "H 股全称 vs A 股简称",
    },
    "00857": {
        "a_code": "601857",
        "hk_name": "中国石油股份",
        "a_name": "中国石油",
        "reason": "H 股全称 vs A 股简称",
    },
    "00177": {
        "a_code": "600377",
        "hk_name": "江苏宁沪高速公路",
        "a_name": "宁沪高速",
        "reason": "H 股全称 vs A 股简称",
    },
    "00038": {
        "a_code": "601038",
        "hk_name": "第一拖拉机股份",
        "a_name": "一拖股份",
        "reason": "H 股全称 vs A 股简称",
    },
    "02883": {
        "a_code": "601808",
        "hk_name": "中海油田服务",
        "a_name": "中海油服",
        "reason": "H 股全称 vs A 股简称",
    },
    "02691": {
        "a_code": "603093",
        "hk_name": "南华期货股份",
        "a_name": "南华期货",
        "reason": "H 股全称 vs A 股简称",
    },
    "06865": {
        "a_code": "601865",
        "hk_name": "福莱特玻璃",
        "a_name": "福莱特",
        "reason": "H 股全称 vs A 股简称",
    },
    "00548": {
        "a_code": "600548",
        "hk_name": "深圳高速公路股份",
        "a_name": "深高速",
        "reason": "H 股全称 vs A 股简称",
    },
    "03618": {
        "a_code": "601077",
        "hk_name": "重庆农村商业银行",
        "a_name": "渝农商行",
        "reason": "H 股全称 vs A 股简称",
    },
    "01339": {
        "a_code": "601319",
        "hk_name": "中国人民保险集团",
        "a_name": "中国人保",
        "reason": "H 股全称 vs A 股简称",
    },
    "01330": {
        "a_code": "601330",
        "hk_name": "绿色动力环保",
        "a_name": "绿色动力",
        "reason": "H 股全称 vs A 股简称",
    },
    "00719": {
        "a_code": "000756",
        "hk_name": "山东新华制药股份",
        "a_name": "新华制药",
        "reason": "H 股全称 vs A 股简称",
    },
    "00902": {
        "a_code": "600011",
        "hk_name": "华能国际电力股份",
        "a_name": "华能国际",
        "reason": "H 股全称 vs A 股简称",
    },
    "00107": {
        "a_code": "601107",
        "hk_name": "四川成渝高速公路",
        "a_name": "四川成渝",
        "reason": "H 股全称 vs A 股简称",
    },
    "00386": {
        "a_code": "600028",
        "hk_name": "中国石油化工股份",
        "a_name": "中国石化",
        "reason": "H 股全称 vs A 股简称",
    },
    "01071": {
        "a_code": "600027",
        "hk_name": "华电国际电力股份",
        "a_name": "华电国际",
        "reason": "H 股全称 vs A 股简称",
    },
    "01065": {
        "a_code": "600874",
        "hk_name": "天津创业环保股份",
        "a_name": "创业环保",
        "reason": "H 股全称 vs A 股简称",
    },
    "01800": {
        "a_code": "601800",
        "hk_name": "中国交通建设",
        "a_name": "中国交建",
        "reason": "H 股全称 vs A 股简称",
    },
    "00553": {
        "a_code": "600775",
        "hk_name": "南京熊猫电子股份",
        "a_name": "南京熊猫",
        "reason": "H 股全称 vs A 股简称",
    },
    "00956": {
        "a_code": "600956",
        "hk_name": "新天绿色能源",
        "a_name": "新天绿能",
        "reason": "H 股全称 vs A 股简称",
    },
    "01816": {
        "a_code": "003816",
        "hk_name": "中广核电力",
        "a_name": "中国广核",
        "reason": "H 股全称 vs A 股简称",
    },
    "01055": {
        "a_code": "600029",
        "hk_name": "中国南方航空股份",
        "a_name": "南方航空",
        "reason": "H 股全称 vs A 股简称",
    },
    "06818": {
        "a_code": "601818",
        "hk_name": "中国光大银行",
        "a_name": "光大银行",
        "reason": "H 股全称 vs A 股简称",
    },
    "00187": {
        "a_code": "600860",
        "hk_name": "京城机电股份",
        "a_name": "京城股份",
        "reason": "H 股全称 vs A 股简称",
    },
    "00670": {
        "a_code": "600115",
        "hk_name": "中国东方航空股份",
        "a_name": "中国东航",
        "reason": "H 股全称 vs A 股简称",
    },
    "02202": {
        "a_code": "000002",
        "hk_name": "万科企业",
        "a_name": "万科Ａ",
        "reason": "H 股全称 vs A 股全称",
    },
    "00525": {
        "a_code": "601333",
        "hk_name": "广深铁路股份",
        "a_name": "广深铁路",
        "reason": "H 股全称 vs A 股简称",
    },
}


def normalize_company_name(name: Any) -> str:
    """Normalize Chinese company names for exact-name matching.

    Only removes display noise (full-width forms, whitespace); it does
    NOT strip 股份/银行/证券/科技 suffixes because that would create
    unsafe collisions such as 招商银行 vs 招商证券.
    """
    text = "" if name is None else str(name)
    for full, half in (("Ａ", "A"), ("Ｂ", "B"), ("Ｈ", "H")):
        text = text.replace(full, half)
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"[\s　]+", "", text)


def _dedupe_spot_rows(rows: Any) -> list[dict[str, str]]:
    """Normalize ak.stock_zh_ah_spot()/dicts into unique hk_code rows."""
    if rows is None:
        return []
    out: dict[str, dict[str, str]] = {}
    iterable = rows.to_dict(orient="records") if hasattr(rows, "to_dict") else rows
    for raw in iterable:
        row = (
            raw
            if isinstance(raw, dict)
            else {str(k): v for k, v in raw.items()}  # type: ignore[union-attr]
        )
        hk_code = str(row.get("代码", row.get("hk_code", row.get("code", "")))).strip()
        name = str(row.get("名称", row.get("hk_name", row.get("name", "")))).strip()
        if not re.fullmatch(r"[0-9]{4,5}", hk_code) or not name:
            continue
        normalized_code = hk_code.zfill(5)
        out.setdefault(normalized_code, {"hk_code": normalized_code, "hk_name": name})
    return list(out.values())


def _stock_meta_name_index(duck: DuckDBStore) -> dict[str, str]:
    """Return normalized current-listed A-share name → stock_code."""
    rows = duck.read_query(
        "SELECT stock_code, name FROM stock_meta "
        "WHERE is_listed IS TRUE ORDER BY stock_code"
    )
    index: dict[str, str] = {}
    collisions: dict[str, list[str]] = {}
    for row in rows:
        normalized = normalize_company_name(row["name"])
        previous = index.get(normalized)
        if previous is not None and previous != row["stock_code"]:
            collisions.setdefault(normalized, [previous]).append(row["stock_code"])
        index[normalized] = row["stock_code"]
    for normalized in collisions:
        index.pop(normalized, None)
        logger.warning("stock_meta 名称归一化后重名，跳过自动匹配: %s", normalized)
    return index


@contextlib.contextmanager
def _domestic_direct() -> Any:
    saved = {
        key: os.environ.get(key)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    }
    for key in saved:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def refresh_ah_spot_snapshot() -> list[dict[str, str]]:
    """Fetch current `stock_zh_ah_spot()` rows (单次行情页请求).

    Failure raises so callers can fall back to AH_SPOT_NAME_SNAPSHOT;
    this function never fabricates a mapping on partial data.
    """
    import akshare as ak

    with _domestic_direct():
        df = ak.stock_zh_ah_spot()
    return _dedupe_spot_rows(df)


def build_ah_hk_mapping(
    duck: DuckDBStore,
    *,
    ah_spot_rows: list[dict[str, Any]] | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Build A-share code → HK code/name mapping with exact-name matching.

    - refresh=True: live stock_zh_ah_spot() first, snapshot on failure;
    - refresh=False: persisted AH_SPOT_NAME_SNAPSHOT first;
    - MANUAL_OVERRIDES are always applied after exact matches.
    """
    name_index = _stock_meta_name_index(duck)
    meta_names: dict[str, str] = {}
    for row in duck.read_query(
        "SELECT stock_code, name FROM stock_meta WHERE is_listed IS TRUE"
    ):
        meta_names[row["stock_code"]] = row["name"]

    spot_rows: list[dict[str, Any]] = list(ah_spot_rows or [])
    source = "injected" if ah_spot_rows is not None else "snapshot"
    warning: str | None = None
    if refresh and ah_spot_rows is None:
        try:
            spot_rows = refresh_ah_spot_snapshot()
            source = "live"
        except Exception as exc:  # noqa: BLE001
            warning = f"AH spot 刷新失败，回退内置快照: {type(exc).__name__}: {exc}"
            logger.warning(warning)
    if not spot_rows:
        spot_rows = [
            {"hk_code": code, "hk_name": name}
            for code, name in AH_SPOT_NAME_SNAPSHOT.items()
        ]
        source = "snapshot"

    unique_rows = _dedupe_spot_rows(spot_rows)
    mapping: dict[str, dict[str, str]] = {}
    unmatched: list[dict[str, str]] = []
    exact_matches = 0
    manual_matches = 0
    used_overrides: list[str] = []

    for spot in unique_rows:
        hk_code = str(spot["hk_code"]).zfill(5)
        hk_name = str(spot["hk_name"])
        override = MANUAL_OVERRIDES.get(hk_code)
        if override is None:
            a_code = name_index.get(normalize_company_name(hk_name))
            match_type = "exact_name"
        else:
            a_code = override.get("a_code")
            match_type = "manual_override"
        if a_code is None or a_code not in meta_names:
            unmatched.append({"hk_code": hk_code, "hk_name": hk_name})
            continue
        mapping[a_code] = {
            "hk_code": hk_code,
            "hk_name": hk_name,
            "a_name": meta_names[a_code],
            "match_type": match_type,
        }
        if match_type == "exact_name":
            exact_matches += 1
        else:
            manual_matches += 1
            used_overrides.append(hk_code)

    return {
        "status": "ok",
        "source": source,
        "warning": warning,
        "total_ah_spot_rows": len(unique_rows),
        "mapped_stocks": len(mapping),
        "exact_matches": exact_matches,
        "manual_overrides_used": manual_matches,
        "used_overrides": used_overrides,
        "unmatched_hk_codes": unmatched,
        "mapping": mapping,
    }
