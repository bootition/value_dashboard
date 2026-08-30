"""Sina 免费财务三表适配器 — 封装 quotes.sina.cn 财报接口

支持的数据类型:
  - balance_sheet     资产负债表 (source=fzb)
  - income_statement  利润表 (source=lrb)
  - cash_flow         现金流量表 (source=llb)

API: CompanyFinanceService.getFinanceReport2022?paperCode={code}&source={fzb|lrb|llb}

返回结构 (已实测验证, 600519 fzb):
  {"result": {"status": {"code": 0}, "data": {
      "report_count": N,
      "report_date": [{"date_value": "20260331", "date_description": "...", "date_type": 1..4}, ...],
      "report_list": {          # dict, 键 = 报告期 YYYYMMDD
          "20260331": {
              "rType": "合并期末", "rCurrency": "CNY", "data_source": "定期报告",
              "is_audit": "未审计", "audit_opinion": "", "publish_date": "20260425",
              "update_time": int, "is_exist_yoy": true,
              "data": [          # 字段字典列表
                  {"item_field": "TOTASSET", "item_title": "资产总计",
                   "item_value": "319918844905.580000", "item_display_type": 2,
                   "item_display": "小计", "item_precision": "f2",
                   "item_group_no": 1, "item_source": "fzb", "item_tongbi": ...},
                  ...
              ]
          }
      }
  }}

解析规则:
  - 以 item_title（中文项目名）为准, 精确匹配映射表; 兼容同义变体。
  - 绝不使用子串匹配: "营业总收入" 不得映射为 revenue, 只有精确的 "营业收入" 可以。
  - item_value 为字符串数值 (如 "48786691397.550000"), 空串/None 视为缺失,
    找不到目标字段就不写该字段, 绝不伪造。
  - 每个报告期输出一行标准化记录: stock_code + report_date(YYYY-MM-DD) + 命中的字段。

符号格式:
  - 6 开头 → shXXXXXX (上交所)
  - 0/3/4/8/9 开头 (含北交所 920000) → szXXXXXX

原始 HTTP response.content 原样放入 FetchResult.raw_response (多股时按请求顺序拼接)。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

from app.core.adapters.base import BaseAdapter, FetchRequest, FetchResult

logger = logging.getLogger(__name__)

_API_VERSION = "sina-getFinanceReport2022-1"
_URL = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
_HEADERS = {"User-Agent": "Mozilla/5.0"}

_SOURCE_PARAM: dict[str, str] = {
    "balance_sheet": "fzb",
    "income_statement": "lrb",
    "cash_flow": "llb",
}

# ─── 中文项目名 → 标准化字段 ─────────────────────────────────────────

# 目标中文项目名（含同义变体）。仅精确匹配, 不用子串匹配。
# 银行/券商（金融业）报表用词与一般行业不同: 归母权益/净利润无"合计/所有"等后缀。
_FIELD_BY_TITLE: dict[str, tuple[str, ...]] = {
    # 资产负债表（一般行业 + 常见金融业变体）
    "monetary_funds": ("货币资金",),
    "trading_financial_assets": ("交易性金融资产",),
    "notes_receivable": ("应收票据",),
    "accounts_receivable": ("应收账款",),
    "prepayments": ("预付款项",),
    "other_receivables": ("其他应收款", "其他应收款(合计)"),
    "inventory": ("存货",),
    "contract_assets": ("合同资产",),
    "total_current_assets": ("流动资产合计",),
    "long_term_equity_investment": ("长期股权投资",),
    "fixed_assets": ("固定资产净额", "固定资产",),
    "construction_in_progress": ("在建工程合计", "在建工程"),
    "right_of_use_assets": ("使用权资产",),
    "intangible_assets": ("无形资产",),
    "goodwill": ("商誉",),
    "deferred_tax_assets": ("递延所得税资产",),
    "total_non_current_assets": ("非流动资产合计",),
    "total_assets": ("资产总计",),
    "short_term_loans": ("短期借款",),
    "notes_payable": ("应付票据",),
    "accounts_payable": ("应付账款",),
    "prepayments_received": ("预收款项",),
    "contract_liabilities": ("合同负债",),
    "employee_benefits_payable": ("应付职工薪酬",),
    "taxes_payable": ("应交税费",),
    "total_current_liabilities": ("流动负债合计",),
    "long_term_loans": ("长期借款",),
    "bonds_payable": ("应付债券",),
    "lease_liabilities": ("租赁负债",),
    "total_non_current_liabilities": ("非流动负债合计",),
    "total_liabilities": ("负债合计",),
    "paid_in_capital": ("实收资本(或股本)", "实收资本"),
    "capital_reserve": ("资本公积", "资本公积金"),
    "surplus_reserve": ("盈余公积", "盈余公积金"),
    "undistributed_profit": ("未分配利润",),
    "minority_interest": ("少数股东权益",),
    "total_equity": (
        "所有者权益(或股东权益)合计",
        "所有者权益合计",
        "股东权益合计",
        "股东权益",  # 金融业
    ),
    "total_equity_parent": (
        "归属于母公司股东权益合计",
        "归属于母公司所有者权益合计",
        "归属于母公司股东的权益",  # 金融业
    ),
    # 利润表
    "total_operating_revenue": ("营业总收入",),
    "revenue": ("营业收入",),  # 营业总收入 ≠ 营业收入, 绝不映射
    "total_operating_cost": ("营业总成本",),
    "cost_of_revenue": ("营业成本",),
    "taxes_and_surcharges": ("营业税金及附加",),
    "selling_expenses": ("销售费用",),
    "administrative_expenses": ("管理费用",),
    "rd_expenses": ("研发费用",),
    "financial_expenses": ("财务费用",),
    "interest_expense": ("利息费用", "利息支出"),
    "interest_income": ("利息收入",),
    "asset_impairment_loss": ("资产减值损失",),
    "credit_impairment_loss": ("信用减值损失",),
    "exchange_gain": ("汇兑收益",),
    "investment_income": ("投资收益",),
    "operating_profit": ("营业利润",),
    "non_operating_income": ("营业外收入",),
    "non_operating_expenses": ("营业外支出",),
    "total_profit": ("利润总额",),
    "income_tax": ("所得税费用",),
    "net_profit": ("净利润",),
    "parent_net_profit": (
        "归属于母公司所有者的净利润",
        "归属于上市公司股东的净利润",
        "归属于母公司的净利润",  # 金融业
    ),
    "minority_shareholder_profit": ("少数股东损益",),
    "basic_eps": ("基本每股收益",),
    "diluted_eps": ("稀释每股收益",),
    # 现金流量表
    "cash_received_sales": ("销售商品、提供劳务收到的现金",),
    "taxes_refunded": ("收到的税费返还",),
    "other_operating_cf_in": ("收到的其他与经营活动有关的现金",),
    "total_operating_cf_in": ("经营活动现金流入小计",),
    "cash_paid_goods": ("购买商品、接受劳务支付的现金",),
    "cash_paid_employees": ("支付给职工以及为职工支付的现金",),
    "cash_paid_taxes": ("支付的各项税费",),
    "other_operating_cf_out": ("支付的其他与经营活动有关的现金",),
    "total_operating_cf_out": ("经营活动现金流出小计",),
    "cf_from_operating": (
        "经营活动产生的现金流量净额",
        "经营活动产生的现金流量",  # 金融业
    ),
    "cf_from_investing": ("投资活动产生的现金流量净额",),
    "cf_from_financing": ("筹资活动产生的现金流量净额",),
    "exchange_rate_effect": ("汇率变动对现金及现金等价物的影响",),
    "cf_net": ("现金及现金等价物净增加额",),
    "cash_beginning": ("期初现金及现金等价物余额", "现金的期初余额"),
    "cash_ending": ("期末现金及现金等价物余额", "现金的期末余额"),
}


def _normalize_title(title: str) -> str:
    """规范化项目名: 去空白、全角括号转半角（用于精确匹配）"""
    return (
        title.replace("（", "(")
        .replace("）", ")")
        .replace(" ", "")
        .replace("\u3000", "")
        .strip()
    )


_TITLE_LOOKUP: dict[str, str] = {
    _normalize_title(title): field
    for field, titles in _FIELD_BY_TITLE.items()
    for title in titles
}


# ─── 辅助函数 ───────────────────────────────────────────────────────


def _paper_code(stock_code: str) -> str | None:
    """6 位代码 → Sina paperCode: 6→sh, 0/3/4/8/9(含北交所)→sz"""
    code = stock_code.strip()
    if len(code) != 6 or not code.isdigit():
        return None
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("0", "3", "4", "8", "9")):
        return f"sz{code}"
    return None


def _parse_value(value: Any) -> float | None:
    """item_value → float; None/空串/不可解析 → None"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _date_key(report_date_key: str) -> str | None:
    """'20260331' → '2026-03-31'"""
    if len(report_date_key) != 8 or not report_date_key.isdigit():
        return None
    return f"{report_date_key[:4]}-{report_date_key[4:6]}-{report_date_key[6:]}"


def _parse_payload(content: bytes, plain_code: str) -> list[dict[str, Any]]:
    """解析原始响应字节 → 标准化记录列表（仅写入真实存在的字段）"""
    try:
        payload = json.loads(content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        logger.warning("sina payload decode failed for %s: %s", plain_code, error)
        return []

    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    status = result.get("status")
    if isinstance(status, dict) and status.get("code") not in (0, "0"):
        return []
    data = result.get("data")
    if not isinstance(data, dict):
        return []
    report_list = data.get("report_list")
    if not isinstance(report_list, dict):
        return []

    records: list[dict[str, Any]] = []
    for report_date_key, period in report_list.items():
        report_date = _date_key(report_date_key)
        if report_date is None or not isinstance(period, dict):
            continue
        items = period.get("data")
        if not isinstance(items, list):
            continue

        found: dict[str, float] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            title = item.get("item_title")
            if not isinstance(title, str):
                continue
            field = _TITLE_LOOKUP.get(_normalize_title(title))
            if field is None or field in found:
                continue
            value = _parse_value(item.get("item_value"))
            if value is None:
                continue
            found[field] = value

        if not found:
            continue
        record: dict[str, Any] = {"stock_code": plain_code, "report_date": report_date}
        record.update(found)
        records.append(record)
    return records


# ─── 适配器 ─────────────────────────────────────────────────────────


class SinaAdapter(BaseAdapter):
    """Sina 财报适配器: 免费 HTTP 接口, 长历史 (茅台 102 期), 北交所可用"""

    _SUPPORTED = {"balance_sheet", "income_statement", "cash_flow"}

    def __init__(self, rate_limit: float = 0.35) -> None:
        super().__init__(
            name="sina",
            supported=self._SUPPORTED,  # type: ignore[arg-type]
            rate_limit=rate_limit,
        )

    def fetch(self, request: FetchRequest) -> FetchResult:
        if not self.can_handle(request):
            return self._make_empty_result(f"sina does not support {request.data_type}")
        source_param = _SOURCE_PARAM.get(request.data_type)
        if source_param is None:
            return self._make_empty_result(f"sina has no source for {request.data_type}")
        return self._fetch_statement(request, source_param)

    def _fetch_statement(
        self, request: FetchRequest, source_param: str
    ) -> FetchResult:
        if not request.stock_codes:
            return self._make_empty_result("sina financial statements require stock_codes")

        all_records: list[dict[str, Any]] = []
        raw_chunks: list[bytes] = []
        skipped: list[str] = []

        for raw_code in request.stock_codes:
            plain_code = raw_code.strip()
            paper = _paper_code(plain_code)
            if paper is None:
                skipped.append(raw_code)
                logger.debug("sina 跳过无效代码: %s", raw_code)
                continue

            self._wait_rate_limit()
            try:
                num = str(request.extra_params.get("num", "1000"))
                response = requests.get(
                    _URL,
                    params={
                        "paperCode": paper,
                        "source": source_param,
                        "type": "0",
                        "page": "1",
                        "num": num,
                    },
                    headers=_HEADERS,
                    timeout=30,
                )
                response.raise_for_status()
            except requests.RequestException as error:
                logger.warning("sina %s %s failed: %s", source_param, paper, error)
                skipped.append(raw_code)
                continue

            # 原始响应原样归档 (BaseAdapter._make_result 自动计算 SHA256)
            raw_chunks.append(response.content)
            all_records.extend(_parse_payload(response.content, plain_code))

        if not all_records:
            return self._make_empty_result(
                f"sina returned no {source_param} records for {request.stock_codes}"
            )

        error = f"skipped invalid codes: {','.join(skipped)}" if skipped else None
        return self._make_result(
            data=all_records,
            raw_response=b"\n".join(raw_chunks),
            confidence="strict",
            error=error,
            api_version=_API_VERSION,
        )
