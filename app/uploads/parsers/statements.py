"""Read the step-3 cost figures out of a signed year-end financial statement.

WHAT THE FILE IS
The electronic statement package filed through HTKK (`HSoThueDTu`), e.g.
"Bộ báo cáo tài chính (B01a - DNN)(TT133/2016/TT-BTC)", form 683. One XML
holds several parts; the ones read here:

    PL_KQHDSXKD  income statement (B02)      NamNay = this year
    PL_LCTTTT    cash-flow statement (B03)   NamNay
    PL_CDTK      trial balance               SoPhatSinhTrongKy/No = debits

Two regimes, told apart by the lines present:
  * TT133 (small and micro firms): line 24 is ONE "chi phí quản lý kinh
    doanh" figure; the selling/admin split lives in the trial balance, account
    6421 (selling) and 6422 (administration).
  * TT200: line 25 selling ("chi phí bán hàng"), line 26 administration
    ("chi phí quản lý doanh nghiệp").

WHAT IT PRODUCES, AND HOW MUCH OF IT IS POLICY
  cogs_y1            income statement line 11. Statutory; no judgement.
  fixed_cost_y1      administration expense.     } A STATED POLICY, not a
  variable_cost_...  selling expense.             } statutory line: no filing
                                                  } splits costs by behaviour
                     (grading-input-sources §6.1). Interest (line 23) is left
                     out of both: it is a financing cost, reported alongside
                     so underwriting sees the existing debt.
  owner_withdrawal   profit paid to owners as a % of net profit (income
                     statement line 60). The cash-flow line differs by regime:
                     TT133 (B03-DNN) prints it as line 35 — its line 34 folds
                     loan and finance-lease repayment together — while TT200
                     (B03-DN) prints it as line 36, after a separate line 35
                     for finance leases. None when the year made no profit (a
                     share of a loss means nothing), or when the cash-flow
                     statement does not carry the line at all: absent is
                     unknown, not zero.

`signed` means the file carries an XML-DSig Signature element. It is not
verified against the tax authority's CA — see app/uploads/parsers/vat.py for
the same caveat.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from typing import Any

MAX_XML_BYTES = 20 * 1024 * 1024
_STATEMENT_FORMS = {"683", "684", "699"}  # HTKK statement-package form codes seen in the wild


class StatementParseError(ValueError):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class FinancialStatements:
    fiscal_year: int
    tax_code: str
    taxpayer_name: str | None
    regime: str  # "TT133" | "TT200"
    signed: bool
    revenue_net_vnd: int
    cogs_vnd: int
    selling_expense_vnd: int
    admin_expense_vnd: int
    financial_expense_vnd: int
    interest_expense_vnd: int
    net_profit_vnd: int
    profit_paid_to_owners_vnd: int | None
    # The step-3 figures, in the SME figure names.
    cogs_y1: int
    fixed_cost_y1: int
    variable_cost_excl_cogs_y1: int
    owner_withdrawal_pct: float | None
    split_method: str
    warnings: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(node: ET.Element, *path: str) -> ET.Element | None:
    """Namespace-agnostic child lookup by local names."""
    current: ET.Element | None = node
    for name in path:
        if current is None:
            return None
        current = next((c for c in current.iter() if _local(c.tag) == name and c is not current), None)
    return current


def _values(node: ET.Element | None) -> dict[str, int]:
    """The ctNN children of a block, as integers; blanks read as absent."""
    out: dict[str, int] = {}
    if node is None:
        return out
    for child in node:
        name = _local(child.tag)
        text = (child.text or "").strip()
        if re.fullmatch(r"ct\d+", name) and text:
            try:
                out[name] = int(round(float(text)))
            except ValueError as exc:
                raise StatementParseError(f"{name} is not a number: {text!r}", "UNREADABLE_STATEMENTS") from exc
    return out


def looks_like_statements(xml: str) -> bool:
    return "PL_KQHDSXKD" in xml


def parse_statements_xml(xml: str) -> FinancialStatements:
    if len(xml.encode("utf-8", errors="ignore")) > MAX_XML_BYTES:
        raise StatementParseError("The statement file is too large", "UNREADABLE_STATEMENTS")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise StatementParseError("The statement file is not valid XML", "UNREADABLE_STATEMENTS") from exc

    def text(name: str) -> str | None:
        node = _find(root, name)
        return (node.text or "").strip() if node is not None and node.text else None

    form = text("maTKhai")
    if form not in _STATEMENT_FORMS and _find(root, "PL_KQHDSXKD") is None:
        raise StatementParseError("This XML is not a year-end financial statement", "NOT_STATEMENTS")
    tax_code = text("mst")
    if not tax_code:
        raise StatementParseError("The statement has no tax code", "UNREADABLE_STATEMENTS")
    period = text("kyKKhai") or ""
    if not re.fullmatch(r"\d{4}", period):
        raise StatementParseError(f"The statement period {period!r} is not a fiscal year", "NOT_STATEMENTS")

    income = _values(_find(root, "PL_KQHDSXKD", "NamNay"))
    if "ct11" not in income or "ct10" not in income:
        raise StatementParseError("The income statement has no revenue or cost of goods sold", "UNREADABLE_STATEMENTS")
    cash = _values(_find(root, "PL_LCTTTT", "NamNay"))
    trial_debits = _values(_find(root, "PL_CDTK", "SoPhatSinhTrongKy", "No"))

    warnings: list[dict[str, str]] = []
    if "ct25" in income or "ct26" in income:
        regime = "TT200"
        selling, admin = income.get("ct25", 0), income.get("ct26", 0)
        split_method = "TT200 income statement: selling (line 25) as variable, administration (line 26) as fixed"
    else:
        regime = "TT133"
        combined = income.get("ct24", 0)
        s, a = trial_debits.get("ct6421"), trial_debits.get("ct6422")
        if s is not None or a is not None:
            s, a = s or 0, a or 0
            # Line 24 is the statutory figure; the trial balance only says how
            # to split it. The two can differ by closing entries, so the ratio
            # is applied to line 24 rather than taking the accounts as-is.
            if s + a > 0:
                selling = round(combined * s / (s + a))
            else:
                selling = 0
            admin = combined - selling
            split_method = (
                "TT133: line 24 split by the trial balance, account 6421 (selling) "
                "as variable and 6422 (administration) as fixed"
            )
        else:
            selling, admin = 0, combined
            split_method = "TT133 with no trial balance: all of line 24 treated as fixed"
            warnings.append({"code": "SPLIT_ASSUMED", "detail": "no 6421/6422 breakdown; line 24 counted as fixed"})

    net_profit = income.get("ct60", 0)
    owners_line = "ct35" if regime == "TT133" else "ct36"
    paid = cash.get(owners_line)
    paid_to_owners = abs(paid) if paid is not None else None
    if net_profit <= 0:
        owner_pct = None
        warnings.append({"code": "NO_PROFIT", "detail": "net profit is not positive; owner withdrawal share left blank"})
    elif paid_to_owners is None:
        owner_pct = None
        warnings.append({
            "code": "NO_OWNER_PAYOUT_LINE",
            "detail": f"the cash-flow statement has no line {owners_line[2:]} (profit paid to owners); left blank",
        })
    else:
        owner_pct = round(100 * paid_to_owners / net_profit, 2)
    interest = income.get("ct23", 0)
    if interest > 0:
        warnings.append({
            "code": "INTEREST_EXPENSE",
            "detail": f"interest expense {interest:,} VND (existing debt), not counted as an operating cost",
        })

    return FinancialStatements(
        fiscal_year=int(period),
        tax_code=tax_code,
        taxpayer_name=text("tenNNT"),
        regime=regime,
        signed=bool(re.search(r"<(?:[A-Za-z0-9_.-]+:)?Signature[\s>]", xml)),
        revenue_net_vnd=income["ct10"],
        cogs_vnd=income["ct11"],
        selling_expense_vnd=selling,
        admin_expense_vnd=admin,
        financial_expense_vnd=income.get("ct22", 0),
        interest_expense_vnd=interest,
        net_profit_vnd=net_profit,
        profit_paid_to_owners_vnd=paid_to_owners,
        cogs_y1=income["ct11"],
        fixed_cost_y1=admin,
        variable_cost_excl_cogs_y1=selling,
        owner_withdrawal_pct=owner_pct,
        split_method=split_method,
        warnings=warnings,
    )
