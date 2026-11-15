"""分红「权益分派实施公告」PDF 解析（2026-11-14 建，2026-11-15 扩展 v2，reports/121）。

为什么需要
----------
`dividends_quarantine` 里有 **8,467 行 / 2,436 只** 的 `quarantine_reason` 全是
`unverified_period_end_placeholder` —— 除权日实际是"报告期末占位"，不是真实除权日
（外部数据包只有报告期末，没有除权日）。主链 `dividends.ex_date` 目前靠 TDX `xdxr`
匹配补齐，匹配不上的就进了隔离区。

而「权益分派实施公告」PDF 里**明确写着**真实日期，例如贵州茅台 2025 年度：

    三、相关日期
    股份类别 股权登记日 最后交易日 除权（息）日 现金红利发放日
    Ａ股 2026/6/25 － 2026/6/26 2026/6/26

设计
----
- 本模块**只做纯文本解析**（`parse_dividend_dates`），不依赖任何 PDF 库；
- PDF → 文本由调用方提供（`scripts/resolve_dividend_ex_dates.py` 用 pypdf，
  未安装时给出明确安装提示，不做静默降级）；
- 解析不到的形态一律返回 None，**绝不猜日期**（除权日错一天就会让回测错一年）。

两种版式
--------
1. **表格版**（沪深主板最常见）：整行 `A股 2026/6/25 - 2026/6/26 2026/6/26`
   —— 行内日期按「登记日 → 除权日 → 派发日」顺序取值（`-` 是"最后交易日"占位）。
   同一公告可能同时有 A/B 股两行，**A 股优先**（本库 `dividends` 只装 A 股口径）。
2. **行文版**（深市/创业板常见）：`…股权登记日为 2026 年 9 月 23 日，除权除息日为
   2026 年 9 月 24 日。` —— 用「关键词链」把日期归属到标签（见下）。

关键词链算法（v2，修掉 v1 的两个真实错判）
------------------------------------------
v1 是"关键词后 60 字内取第一个日期"，在真实公告上会取错：

- 小标题 `三、股权登记日与除权除息日` 里同时含两个关键词，紧随其后的第一个日期
  其实是**登记日**，v1 会把它当成除权日（差一天 = 回测错一年）；
- 深市写法 `除权除息日及现金红利发放日为 2026 年 8 月 10 日`：`除权` 与 `息` 之间
  多了个 `除` 字，v1 的 `除权[(息)]日` 正则根本不匹配。

v2 改为：从每个日期**向左**吃掉紧邻的「连接词（为/是/：/及/和/与/、/空白）+ 关键词」链，
链上是哪些标签，这个日期就归属哪些标签：
`除权除息日及现金红利发放日为 <D>` → D 同时是除权日和发放日；
小标题 `股权登记日与除权除息日` 后面接的是正文而不是日期，链在"与"处被正文打断，不会误判。

依赖说明
--------
文本提取需要 `pypdf`。当前 `uv.lock` 尚未包含它（2026-11-14 环境无法访问 PyPI，
`uv lock` 失败），因此脚本里是**惰性导入 + 显式报错**；联网后执行
`uv add pypdf && uv lock` 即可启用。解析逻辑本身用固定文本 fixture 做了单测，
不依赖 pypdf。
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass
from datetime import date

__all__ = [
    "DividendDates",
    "PdfSupportUnavailable",
    "extract_html_text",
    "extract_pdf_text",
    "parse_dividend_dates",
    "parse_dividend_amounts",
    "parse_dividend_per_share",
]


class PdfSupportUnavailable(RuntimeError):
    """环境里没有可用的 PDF 文本提取库。"""


_DATE_RE = re.compile(
    r"(\d{4})\s*[/\-年]\s*(\d{1,2})\s*[/\-月]\s*(\d{1,2})\s*日?"
)
_FULLWIDTH = str.maketrans(
    {
        "Ａ": "A", "Ｂ": "B", "（": "(", "）": ")", "：": ":", "／": "/", "－": "-",
        "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
        "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
    }
)

#: 标签 → 关键词（同一标签内多个写法等价）。匹配时**最长优先**，
#: 保证 `除权除息日` 不会被拆成 `除息日`/`除权日`。
_KEYWORD_GROUPS: dict[str, tuple[str, ...]] = {
    "record": ("股权登记日", "登记日"),
    "ex": (
        "除权除息日", "除权(息)日", "除权(除息)日", "除息日", "除权日",
        "除权除息基准日", "除权(除息)基准日", "除权基准日", "除息基准日",
    ),
    "pay": ("现金红利发放日", "现金股利发放日", "红利发放日", "派息日", "发放日"),
}
_GROUP_OF: dict[str, str] = {
    word: group for group, words in _KEYWORD_GROUPS.items() for word in words
}
_WORDS_BY_LEN: tuple[str, ...] = tuple(sorted(_GROUP_OF, key=len, reverse=True))

#: 标签与日期之间允许出现的填充字符（`为`/`是`/`:`/并列连词/空白）。
#: **不含** `，` `。` —— 句读越界一律视为链断，避免把上一句的标签算进来。
_CONNECTORS = frozenset(" \t\u3000:：为是及和与、,")
#: 换行要单独判定（不是无条件连接词），规则见 `_line_before_ends_with_date`。
_NEWLINES = frozenset("\r\n")

#: 表格版：`A股` / `Ａ股` / `A 股`
_TABLE_ROW_RE = re.compile(r"^(A|B)\s*股\s*(.*)$")
#: 登记日与除权日之间允许的最大间隔（A 股通常 1 天；B 股因最后交易日可能更久）
_MAX_RECORD_TO_EX_DAYS = 60


@dataclass(frozen=True)
class DividendDates:
    """一条权益分派实施公告里的关键日期（任一缺失为 None）。"""

    record_date: date | None      # 股权登记日
    ex_date: date | None          # 除权（息）日
    pay_date: date | None         # 现金红利发放日
    evidence: str                 # 命中的原文片段（便于人工复核）

    @property
    def complete(self) -> bool:
        return self.ex_date is not None


#: 「每 10 股派/派发/派送/发放 …（现金/红利/股利/股息/分红）…（人民币）X 元」
#: 动词与名词都做成白名单，避免抓到「每 10 股补缴税款 0.4980 元」这类税务说明。
_CASH_VERB = r"(?:派发?|派送?|发放|支付)?"
_CASH_NOUN = r"(?:现金)?\s*(?:红利|股利|股息|分红)?"
_RE_CASH_PER_10 = re.compile(
    rf"每\s*10\s*股\s*{_CASH_VERB}\s*{_CASH_NOUN}\s*(?:人民币)?\s*(\d+(?:\.\d+)?)\s*元"
)
_RE_CASH_PER_SHARE = re.compile(
    rf"每股\s*{_CASH_VERB}\s*{_CASH_NOUN}\s*(?:人民币)?\s*(\d+(?:\.\d+)?)\s*元"
)


def _to_date(year: str, month: str, day: str) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _date_hits(text: str) -> list[tuple[int, date]]:
    out: list[tuple[int, date]] = []
    for match in _DATE_RE.finditer(text):
        value = _to_date(match.group(1), match.group(2), match.group(3))
        if value is not None:
            out.append((match.start(), value))
    return out


def _line_before_ends_with_date(text: str, newline_index: int) -> bool:
    """`newline_index` 所在的上一行里是否已经出现过日期。

    用来区分两种"标签与日期之间隔着换行"的情形：

    - **同一句被折断**（西部证券 002673 实测）：
      `…股权登记日为：2025年6月 19日，除权除息日` + 下一行 `为：2025年6月 20日。`
      上一行自己带日期 → 允许跨行，把 `除权除息日` 与下一行的日期连起来；
    - **表头行 + 数据行**（德科立 688205 实测）：
      `股权登记日 除权（息）日 现金红利发放日` + 下一行 `2025/5/28 2025/5/29 …`
      上一行没有任何日期 → 禁止跨行，否则数据行首个日期会被同时算成
      登记日/除权日/派发日（"登记日=除权日"的错判）。
    """
    head = text.rfind("\n", 0, newline_index)
    return bool(_date_hits(text[head + 1: newline_index]))


def _labels_before(text: str, start: int) -> tuple[set[str], int]:
    """从 `start` 向左吃掉「连接词 + 关键词」链，返回 (标签集合, 链起点)。"""
    labels: set[str] = set()
    pos = start
    crossed_newline = False
    while pos > 0:
        anchor = pos
        while anchor > 0:
            char = text[anchor - 1]
            if char in _CONNECTORS:
                anchor -= 1
                continue
            if char in _NEWLINES:
                # 最多跨一行，且上一行必须自带日期（否则是"表头 + 数据行"版式）
                if crossed_newline or not _line_before_ends_with_date(text, anchor - 1):
                    return labels, pos
                crossed_newline = True
                anchor -= 1
                continue
            break
        word = next(
            (w for w in _WORDS_BY_LEN if anchor >= len(w) and text[anchor - len(w):anchor] == w),
            None,
        )
        if word is None:  # 链断（遇到正文/数字/句读），立刻停
            break
        labels.add(_GROUP_OF[word])
        pos = anchor - len(word)
        if crossed_newline:  # 跨行时只认紧邻的那一个标签
            break
    return labels, pos


def _from_table_row(normalized: str) -> DividendDates | None:
    """表格版：`A股 2026/6/25 - 2026/6/26 2026/6/26`（A 股优先于 B 股）。"""
    by_class: dict[str, list[str]] = {"A": [], "B": []}
    for line in normalized.splitlines():
        stripped = line.strip()
        match = _TABLE_ROW_RE.match(stripped)
        if match is None:
            continue
        by_class[match.group(1)].append(stripped)
    for share_class in ("A", "B"):
        for stripped in by_class[share_class]:
            values = [value for _, value in _date_hits(stripped)]
            if len(values) < 2:
                continue
            record, ex_date = values[0], values[1]
            # 除权日不可能早于股权登记日；两者也不可能隔几个月
            # （间隔过大说明这一行的日期不是「登记 → 除权」两列，放弃本行）
            if not 0 <= (ex_date - record).days <= _MAX_RECORD_TO_EX_DAYS:
                continue
            pay = values[2] if len(values) >= 3 else None
            return DividendDates(
                record_date=record, ex_date=ex_date, pay_date=pay, evidence=stripped[:160],
            )
    return None


def _from_unlabeled_table(normalized: str) -> DividendDates | None:
    """无股份类别标签的表格：表头一行、数据一行（科创板/沪市单一股类常见）。

    实测德科立（688205）2024 年年度权益分派实施公告：

        股权登记日 除权（息）日 新增无限售条件流通 股份上市日 现金红利发放日
        2025/5/28 2025/5/29 2025/5/29 2025/5/29

    数据行不带 `A股` 前缀，表格版（要求行首 `A/B` 股）识别不到、行文版也没有
    "…日为…"的句式，v2 之前整份公告漏掉。这类版式的列序固定是
    「登记日 → 除权日 →（可能的中间列）→ 派发日」，因此取**第 1 个日期为登记日、
    第 2 个日期为除权日、最后一个日期为派发日**，并用「两日相隔 ≤ 60 天」兜底。
    """
    lines = normalized.splitlines()
    for index, line in enumerate(lines):
        header = line.strip()
        if "股权登记日" not in header or "除权" not in header:
            continue
        if _date_hits(header):  # 表头自己带日期 → 不是表头行
            continue
        for offset in range(1, 4):  # 数据行可能紧邻，也可能隔一个空行/续行
            if index + offset >= len(lines):
                break
            row = lines[index + offset].strip()
            if not row:
                continue
            values = [value for _, value in _date_hits(row)]
            if len(values) < 2:
                continue
            record, ex_date = values[0], values[1]
            if not 0 <= (ex_date - record).days <= _MAX_RECORD_TO_EX_DAYS:
                break  # 这一行的日期不是「登记 → 除权」两列
            pay = values[-1] if values[-1] >= ex_date else None
            return DividendDates(
                record_date=record, ex_date=ex_date, pay_date=pay,
                evidence=f"{header[:60]} / {row[:80]}",
            )
    return None


def _from_prose(normalized: str) -> DividendDates | None:
    """行文版：按「关键词链」把每个日期归属到标签。"""
    hits: dict[str, tuple[date, str]] = {}
    for start, value in _date_hits(normalized):
        labels, chain_start = _labels_before(normalized, start)
        for label in labels:
            if label in hits:
                continue
            evidence = normalized[max(0, chain_start): start + 16].strip()
            hits[label] = (value, evidence[:160])
    if "ex" not in hits:
        return None
    ex_date, ex_evidence = hits["ex"]
    record_date = hits["record"][0] if "record" in hits else None
    pay_date = hits["pay"][0] if "pay" in hits else None
    if record_date is not None and not (
        0 <= (ex_date - record_date).days <= _MAX_RECORD_TO_EX_DAYS
    ):
        record_date = None  # 登记日与除权日对不上，宁可弃用登记日也不弃用除权日
    if pay_date is not None and pay_date < ex_date:
        # 表头里的"现金红利发放日"后面紧跟的往往是表格里第一列（登记日）的日期，
        # 链式归属会把它误当派发日；派发日早于除权日说明归属错了，直接弃用。
        pay_date = None
    return DividendDates(
        record_date=record_date,
        ex_date=ex_date,
        pay_date=pay_date,
        evidence=ex_evidence,
    )


def parse_dividend_dates(text: str) -> DividendDates | None:
    """从「权益分派实施公告」正文里解析股权登记日 / 除权(息)日 / 派发日。

    依次尝试：① 带股份类别标签的表格（`A股 登记日 - 除权日 派发日`）；
    ② 行文版（`股权登记日为 …，除权除息日为 …`）；③ 无标签表格（表头 + 数据行）。
    都识别不到时返回 None（不猜）。
    """
    if not text:
        return None
    normalized = text.translate(_FULLWIDTH)
    normalized = re.sub(r"[ \t\u3000]+", " ", normalized)
    return (
        _from_table_row(normalized)
        or _from_prose(normalized)
        or _from_unlabeled_table(normalized)
    )


def parse_dividend_amounts(text: str) -> list[float]:
    """从公告正文里取出**全部**每股现金分红候选值（元/股，按出现顺序）。

    同一份公告里常有多个金额口径：「全年合计每 10 股派 5.96 元（含税），其中
    中期已派 1.72 元，本次派 4.24 元」——只取第一个会把"本期增量"判错。因此这里
    返回全部候选，由调用方做「任一候选匹配即可」的容错比对。

    动词/名词都走白名单，避免抓到「每 10 股补缴税款 0.4980 元」这类税务说明。
    """
    if not text:
        return []
    normalized = text.translate(_FULLWIDTH)
    normalized = re.sub(r"[ 	　]+", " ", normalized)
    values: list[float] = []
    for match in _RE_CASH_PER_SHARE.finditer(normalized):
        values.append(float(match.group(1)))
    for match in _RE_CASH_PER_10.finditer(normalized):
        values.append(round(float(match.group(1)) / 10, 10))
    unique: list[float] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return unique


def parse_dividend_per_share(text: str) -> float | None:
    """从公告正文里取**每股**税前现金分红金额（元/股，取第一个候选）。

    只做「同一事件」的辅助核对（和隔离行里外部数据的每股股息比一比），
    取不到返回 None——`送转股`公告、纯转增公告本来就没有现金金额。
    """
    amounts = parse_dividend_amounts(text)
    return amounts[0] if amounts else None


def _decode_legacy_html(payload: bytes) -> str:
    """解码 CNINFO 旧公告页：2005 年前多为 GBK，且 `Content-Type` 常写成 utf-8。

    先试 gb18030（GBK 的超集），解不开再依次退到 utf-8/utf-16——顺序不能反：
    先按 utf-8 解会把正文变成乱码而不报错，日期就彻底找不到了。
    """
    for encoding in ("gb18030", "utf-8", "utf-16"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("gb18030", errors="replace")


def extract_html_text(payload: bytes) -> str:
    """从 CNINFO 旧公告的 `.html` 页面里抽正文。

    2005 年前后的「分红派息公告」在巨潮上只有 HTML 页面
    （`finalpage/2005-07-13/15619180.html`），pypdf 拿到会抛 `PdfStreamError`——
    实测柳工 000528 的 2004 年度实施公告因此整条漏掉。
    这里做最小可用的正文抽取：去 script/style、去标签、反转义、压空白。
    """
    text = _decode_legacy_html(payload)
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_module.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """用 pypdf 提取 PDF 文本；库缺失时抛 `PdfSupportUnavailable`（不静默降级）。"""
    try:  # 惰性导入：解析逻辑本身不依赖 pypdf
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError as error:  # pragma: no cover - 取决于环境
        raise PdfSupportUnavailable(
            "解析分红公告 PDF 需要 pypdf：联网后执行 `uv add pypdf && uv lock`，"
            "或临时 `python -m pip install pypdf`。"
        ) from error
    import io

    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)
