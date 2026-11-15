"""分红「权益分派实施公告」PDF 解析测试（2026-11-14 建，2026-11-15 扩 v2，reports/121）。

用**真实公告文本**做 fixture，不依赖 pypdf、不联网——只测纯文本解析逻辑。
全部片段来自正式库抽样时真实下载的 PDF（2026-11-15，pypdf 提取原文）：

- 贵州茅台 2025 年度 `2026-06-22/1225379934.PDF`（表格版，Ａ 股行）
- 平安银行 2026 半年度 `2026-09-17/1225568476.PDF`（深市行文版，含小标题陷阱）
- 宁德时代 2026 中期 `2026-08-04/1225456175.PDF`（"除权除息日及现金红利发放日"链式写法）
- 宁德时代 2025 年度 `2026-04-15/1225107879.PDF`（"除息日"变体 + 链式写法）
- 宁德时代 2025 中期 `2025-08-13/1224474223.PDF`
- 德科立 2024 年度 `2025-05-23/1223632800.PDF`（科创板无股份类别标签的表格）
- 西部证券 2024 年度 `2025-06-13/1223859688.PDF`（标签在行尾、日期折到下一行）
- 柳工 2004 年度 `2005-07-13/15619180.html`（旧公告只有 HTML，GBK 编码）
- 洪都航空 2010 年度 `2011-07-13/59668095.PDF`（沪市「标签行 + 日期」竖排，除权（除息）日）

v1 在这 5 份里只有 1 份解析正确，v2 是 5/5；失败形态见各用例文档字符串。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.core.pdf.dividend_parser import (
    extract_html_text,
    parse_dividend_amounts,
    parse_dividend_dates,
)

MOUTAI_TEXT = """
贵州茅台酒股份有限公司 2025 年年度权益分派实施公告
一、通过分配方案的股东会届次和日期
本次利润分配方案经公司 2026 年 6 月 11 日的 2025 年度股东会审议通过。
二、分配方案
（一）发放年度：2025年年度
（三）分配方案：
本次利润分配以方案实施前的公司总股本扣除回购专用账户内的股份 1,250,081,601 股为基数，
每股派发现金红利 28.02423 元（含税），共计派发现金红利 35,032,574,305.19 元。
三、相关日期
股份类别 股权登记日 最后交易日
除权（息）
日
现金红利发放日
Ａ股 2026/6/25 － 2026/6/26 2026/6/26
四、分配实施办法
"""


def test_parse_moutai_table_layout() -> None:
    """表格版式：`A股 登记日 - 除权日 派发日`（含全角 Ａ）。"""
    parsed = parse_dividend_dates(MOUTAI_TEXT)
    assert parsed is not None
    assert parsed.record_date == date(2026, 6, 25)
    assert parsed.ex_date == date(2026, 6, 26)
    assert parsed.pay_date == date(2026, 6, 26)
    assert parsed.complete is True


def test_parse_inline_layout() -> None:
    """行文版式：分句给出「股权登记日为 …」「除权（息）日为 …」。"""
    text = (
        "本次权益分派股权登记日为 2025-07-10，除权（息）日为 2025-07-11，"
        "现金红利发放日为 2025-07-11。"
    )
    parsed = parse_dividend_dates(text)
    assert parsed is not None
    assert parsed.ex_date == date(2025, 7, 11)
    assert parsed.record_date == date(2025, 7, 10)
    assert parsed.pay_date == date(2025, 7, 11)


def test_parse_rejects_when_ex_date_before_record_date() -> None:
    """除权日早于登记日 = 取错列，必须放弃而不是硬写。"""
    text = "A股 2026/6/26 － 2026/6/25 2026/6/26"
    assert parse_dividend_dates(text) is None


def test_parse_returns_none_without_dates() -> None:
    assert parse_dividend_dates("") is None
    assert parse_dividend_dates("本公司董事会及全体董事保证本公告内容真实准确完整。") is None


def test_parse_ignores_far_away_dates() -> None:
    """关键词后 60 字内没有日期时不能串到后面的段落。"""
    text = "股权登记日 详见下表。" + "（此处省略若干说明）" * 20 + "2020-01-01 另一次分派"
    assert parse_dividend_dates(text) is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("A股 2024/5/20 - 2024/5/21 2024/5/21", date(2024, 5, 21)),
        ("Ａ股 2023/12/07 2023/12/08 2023/12/08", date(2023, 12, 8)),
        ("B股 2022/06/15 - 2022/06/16 2022/06/20", date(2022, 6, 16)),
    ],
)
def test_parse_table_variants(line: str, expected: date) -> None:
    parsed = parse_dividend_dates(line)
    assert parsed is not None
    assert parsed.ex_date == expected


# ─── v2 失败形态回归（全部来自真实 PDF 原文）────────────────────────────

PINGAN_2026H1_TEXT = """
三、股权登记日与除权除息日
本次权益分派股权登记日为：2026 年 9 月 23 日，除权除息日为：2026 年 9
月 24 日。
四、权益分派对象
本次分派对象为：截止 2026 年 9 月 23 日下午深圳证券交易所收市后，在中
国证券登记结算有限责任公司深圳分公司（以下简称中国结算深圳分公司）登记
在册的本公司全体股东。
五、权益分派方法
1、本公司此次委托中国结算深圳分公司代派的 A 股股东现金红利将于 2026
年 9 月 24 日通过股东托管证券公司（或其他托管机构）直接划入其资金账户。
"""

CATL_2026_MID_TEXT = """
2、按公司现有A 股总股本折算的每10 股现金分红为14.019358 元（保留小
数点后六位数，最后一位直接截取，不四舍五入）。本次中期分红派息实施后的
除权除息价格=股权登记日收盘价-按公司现有A 股总股本折算的每股现金分红=
股权登记日收盘价-1.4019358 元/股。
3、本次中期分红派息的股权登记日为2026 年8 月7 日。
4、本次中期分红派息的除权除息日及现金红利发放日为2026 年8 月10 日。
"""

CATL_2025_ANNUAL_TEXT = """
2、按公司现有A 股总股本折算的每10 股现金分红为69.065252 元（保留小
数点后六位数，最后一位直接截取，不四舍五入）。本次权益分派实施后的除权
除息价格=股权登记日收盘价-按公司现有A 股总股本折算每股现金分红比例=股
权登记日收盘价-6.9065252 元/股。
3、本次权益分派的股权登记日为2026 年4 月21 日。
4、本次权益分派的除息日及现金红利发放日为2026 年4 月22 日。
"""


def test_parse_prose_heading_does_not_take_record_date_as_ex_date() -> None:
    """陷阱：小标题「三、股权登记日与除权除息日」后紧跟的第一个日期是**登记日**。

    v1 的"关键词后 60 字内取第一个日期"会把 2026-09-23 当成除权日（差 1 天，
    回测会错一年）。v2 的关键词链在"与"处被正文打断，取到正确的 2026-09-24。
    """
    parsed = parse_dividend_dates(PINGAN_2026H1_TEXT)
    assert parsed is not None
    assert parsed.record_date == date(2026, 9, 23)
    assert parsed.ex_date == date(2026, 9, 24)


def test_parse_prose_chained_keyword_shares_date_between_ex_and_pay() -> None:
    """深市链式写法：`除权除息日及现金红利发放日为 <D>`，D 同时是除权日与发放日。

    v1 的正则 `除权[(息)]日` 不认"除权除息日"（多一个"除"字），整份公告直接漏。
    """
    parsed = parse_dividend_dates(CATL_2026_MID_TEXT)
    assert parsed is not None
    assert parsed.record_date == date(2026, 8, 7)
    assert parsed.ex_date == date(2026, 8, 10)
    assert parsed.pay_date == date(2026, 8, 10)


def test_parse_prose_chuxi_variant() -> None:
    """`除息日`（没有"除权"二字）+ 链式写法。"""
    parsed = parse_dividend_dates(CATL_2025_ANNUAL_TEXT)
    assert parsed is not None
    assert parsed.record_date == date(2026, 4, 21)
    assert parsed.ex_date == date(2026, 4, 22)
    assert parsed.pay_date == date(2026, 4, 22)


def test_parse_prose_ignores_ex_price_formula() -> None:
    """行文版不能把"除权除息价格=股权登记日收盘价-1.40 元/股"当成日期来源。"""
    text = "\n".join([
        "本次中期分红派息实施后的除权除息价格=股权登记日收盘价-1.4019358 元/股。",
        "3、本次中期分红派息的股权登记日为2026 年8 月7 日。",
    ])
    parsed = parse_dividend_dates(text)
    # 只有登记日、没有除权日 → 按"不猜"的纪律返回 None（而不是拿公式里的数字凑日期）
    assert parsed is None


def test_parse_table_prefers_a_share_row_over_b_share_row() -> None:
    """同一公告同时有 A/B 股两行时必须取 A 股行（本库 dividends 只装 A 股口径）。

    B 股行由贵州茅台表格版真实行改写而来（合成样本，用于锁住"B 股在前"的顺序问题）。
    """
    text = "\n".join([
        "股份类别 股权登记日 最后交易日 除权（息）日 现金红利发放日",
        "Ｂ股 2026/6/22 － 2026/6/24 2026/6/30",
        "Ａ股 2026/6/25 － 2026/6/26 2026/6/26",
    ])
    parsed = parse_dividend_dates(text)
    assert parsed is not None
    assert parsed.record_date == date(2026, 6, 25)
    assert parsed.ex_date == date(2026, 6, 26)


DELIXI_2024_ANNUAL_TEXT = """
重要内容提示：
 每股分配比例，每股转增比例
每股现金红利0.30元
每股转增0.30股
 相关日期
股权登记日 除权（息）日 新增无限售条件流通 股份上市日 现金红利发放日
2025/5/28 2025/5/29 2025/5/29 2025/5/29
一、 通过分配、转增股本方案的股东大会届次和日期
本次利润分配及转增股本方案经公司2025 年 5 月 15 日的2024年年度股东会审
议通过。
2. 分派对象：
截至股权登记日下午上海证券交易所收市后，在中国证券登记结算有限责任
公司上海分公司（以下简称“中国结算上海分公司”）登记在册的本公司全体股东。
"""


def test_parse_unlabeled_table_row() -> None:
    """科创板版式：表头一行 + 数据一行，数据行**没有** A股/B股 标签。

    德科立（688205）2024 年度实施公告。v2 之前：表格版要求行首有 `A/B 股`、
    行文版要求"…日为…"句式，两者都不匹配 → 整份公告漏掉。
    """
    parsed = parse_dividend_dates(DELIXI_2024_ANNUAL_TEXT)
    assert parsed is not None
    assert parsed.record_date == date(2025, 5, 28)
    assert parsed.ex_date == date(2025, 5, 29)
    assert parsed.pay_date == date(2025, 5, 29)


def test_parse_prose_does_not_take_header_pay_label_date_from_table() -> None:
    """行文版不能把"表头末列(现金红利发放日)+下一行首个日期"错认成派发日。"""
    text = "\n".join([
        "股权登记日 除权（息）日 现金红利发放日",
        "2025/5/18 2025/5/19 2025/5/19",
    ])
    parsed = parse_dividend_dates(text)
    assert parsed is not None
    assert parsed.ex_date == date(2025, 5, 19)
    assert parsed.pay_date is None or parsed.pay_date >= parsed.ex_date


WESTERN_SECURITIES_2024_TEXT = """
三、分红派息日期
本次分红派息的股权登记日为：2025年6月 19日，除权除息日
为：2025年6月 20日。
四、分红派息对象
本次分派对象为：截至 2025年 6月19日下午深圳证券交易所收
市后，在中国证券登记结算有限责任公司深圳分公司（以下简称“中
国结算深圳分公司”）登记在册的公司全体股东。
"""


def test_parse_prose_label_at_line_end() -> None:
    """`除权除息日` 落在行尾、`为：<日期>` 折到下一行（西部证券 002673 实测）。

    跨行只有在"上一行自己带日期"（同一句被折断）时才允许；否则就是
    "表头行 + 数据行"，跨行会把数据行的登记日误判成除权日。
    """
    parsed = parse_dividend_dates(WESTERN_SECURITIES_2024_TEXT)
    assert parsed is not None
    assert parsed.record_date == date(2025, 6, 19)
    assert parsed.ex_date == date(2025, 6, 20)


def test_parse_rejects_table_row_whose_dates_are_months_apart() -> None:
    """表格行里两列日期相差几个月 → 不是「登记日/除权日」两列，放弃而不是硬取。"""
    assert parse_dividend_dates("A股 2026/1/5 2026/6/26") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("向全体股东每 10 股派发现金红利 3.5 元（含税）", 0.35),
        ("每 10 股派 2.3600 元人民币现金（含税）", 0.236),
        ("每10 股派发现金分红人民币14.11 元（含税）", 1.411),
        ("每股派发现金红利 28.02423 元（含税）", 28.02423),
        ("每 10 股补缴税款 0.4980 元", None),
        ("每10股转增5股", None),
    ],
)
def test_parse_dividend_amount_first_candidate(text: str, expected: float | None) -> None:
    """每股现金分红提取：只认"派发/发放 + 现金/红利/股利/股息"白名单，不抓税务说明。"""
    amounts = parse_dividend_amounts(text)
    assert (amounts[0] if amounts else None) == expected


def test_parse_dividend_amounts_collects_all_conventions() -> None:
    """同一公告里的"全年合计/中期已派/税后"多个口径都要收进来（供容错比对）。"""
    text = (
        "2025 年全年以每 10 股派发现金股利人民币 5.96 元(含税)，其中:"
        "2025 年中期已按每 10 股派发现金股利人民币 2.36 元(含税)；"
        "本次每 10 股派发现金股利人民币 3.60 元(含税)。"
    )
    amounts = parse_dividend_amounts(text)
    assert 0.596 in amounts
    assert 0.236 in amounts
    assert 0.36 in amounts


LEGACY_HTML_PAGE = (
    '<html><head><meta http-equiv="Content-Type" '
    'content="text/html; charset=utf-8"></head><body>'
    "<p>本公司2004年度分红派息方案为：向全体股东每10股派2.00元现金（含税）。</p>"
    "<p>二、股权登记日与除息日</p>"
    "<p>本次分红派息的股权登记日为：2005年7月20日(星期三)，除息日为2005年7月21日(星期四)。</p>"
    "</body></html>"
)


def test_extract_html_text_legacy_gbk_page() -> None:
    """2005 年前后的旧公告在巨潮上只有 `.html` 页面，且实际是 GBK（header 却写 utf-8）。

    真实样本：柳工 000528 `finalpage/2005-07-13/15619180.html`（pypdf 会抛
    PdfStreamError）。解码顺序必须是 gb18030 优先，否则正文变乱码、日期全丢。
    """
    text = extract_html_text(LEGACY_HTML_PAGE.encode("gb18030"))
    assert "<p>" not in text
    parsed = parse_dividend_dates(text)
    assert parsed is not None
    assert parsed.record_date == date(2005, 7, 20)
    assert parsed.ex_date == date(2005, 7, 21)


HONGDU_2010_ANNUAL_TEXT = """
扣税前与扣税后每股现金红利
单位：元
每股现金红利（扣税前） 0.2
每股现金红利（扣税后） 0.18

股权登记日
股权登记日 2011年 7月 18 日

 除权（除息）日：2011年 7月 19日

现金红利发放日
现金红利发放日 2011年 7月 25 日
"""


def test_parse_label_date_stacked_lines_and_chuxi_variant() -> None:
    """沪市竖排：标签单独一行 + 「标签 日期」一行，且写作「除权（除息）日」。

    洪都航空（600316）2010 年度实施公告。v2 之前 `除权[(息)]日` 不认括号里的
    「除息」二字，整份公告解析失败。
    """
    parsed = parse_dividend_dates(HONGDU_2010_ANNUAL_TEXT)
    assert parsed is not None
    assert parsed.record_date == date(2011, 7, 18)
    assert parsed.ex_date == date(2011, 7, 19)
    assert parsed.pay_date == date(2011, 7, 25)
