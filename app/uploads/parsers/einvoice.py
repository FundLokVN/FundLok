"""Parse a zip of monthly e-invoice listings (tax-portal .xlsx exports).

WHAT THE SME UPLOADS
The tax portal (hoadondientu.gdt.gov.vn) exports the invoice list one month at
a time as .xlsx ("DANH SÁCH HÓA ĐƠN"). The SME puts the monthly exports in one
folder and uploads it as a single .zip — file names do not matter and are not
trusted (`HDT1.xlsx` beside `HDT92025.xlsx`); the period comes from inside
each file.

WHAT A LISTING HOLDS
One row per invoice: form + series + number, issue date, seller and buyer tax
codes, buyer name, pre-tax amount, currency and rate, and status (new,
replacement, replaced, adjustment, adjusted, cancelled). That is the invoice
ledger, so it supplies what the typed figures cannot: real monthly revenue,
customer concentration, and customer retention.

⚠️ EVIDENCE WEIGHT — READ BEFORE RELYING ON THIS
Handbook §6 accepts only the signed XML original, because a spreadsheet can be
edited. These listings are unsigned exports; accepting them was a product
decision (Phat, 2026-09-27). Consequently:
  * every feature is stamped `source = "portal_listing_xlsx"` and carries a
    `signed: False` flag, so nothing downstream can mistake it for verified
    evidence;
  * monthly revenue should be cross-checked against the SIGNED VAT
    declarations (app/uploads/parsers/vat.py) whenever both exist — on the
    reference data set they agree to the dong in all 11 overlapping months;
  * concentration and retention depend on the per-buyer split, which has no
    signed counterpart. A listing edited to move sales between buyers keeps
    every total intact. Treat those two features as self-reported-with-detail.

WHAT THIS MODULE DOES NOT DO
No signature checks (there is nothing signed to check), no portal lookup, and
no healing: a numbering gap is surfaced as a warning with the exact numbers,
never filled in (handbook §6 — "a gap is evidence, not a formatting quirk").
"""
from __future__ import annotations

import re
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any

from app.uploads.parsers.xlsx import XlsxError, read_first_sheet

PARSER_VERSION = "einvoice-listing-1.0"
SOURCE = "portal_listing_xlsx"

# Archive limits. A year is 12 files; allow some slack for a partial month or a
# re-export, never an unbounded archive.
MAX_ENTRIES = 36
MAX_ENTRY_BYTES = 30 * 1024 * 1024
MAX_TOTAL_BYTES = 150 * 1024 * 1024

# Statuses whose amount is NOT revenue: the invoice was superseded or voided.
# An adjusted invoice ("đã bị điều chỉnh") stays valid — the adjustment invoice
# carries the delta — so it is counted, like the adjustment itself.
_EXCLUDED_STATUSES = {"hóa đơn đã bị thay thế", "hóa đơn đã bị hủy", "hóa đơn đã bị huỷ"}
_REPLACED = "hóa đơn đã bị thay thế"
_REPLACEMENT = "hóa đơn thay thế"
_KNOWN_STATUSES = _EXCLUDED_STATUSES | {
    _REPLACEMENT,
    "hóa đơn mới",
    "hóa đơn điều chỉnh",
    "hóa đơn đã bị điều chỉnh",
}

# Header names as the portal writes them, compared after normalisation.
_COL_FORM = "ký hiệu mẫu số"
_COL_SERIES = "ký hiệu hóa đơn"
_COL_NUMBER = "số hóa đơn"
_COL_DATE = "ngày lập"
_COL_SELLER = "mst người bán/mst người xuất hàng"
_COL_BUYER = "mst người mua/mst người nhận hàng"
_COL_BUYER_NAME = "tên người mua/tên người nhận hàng"
_COL_PRETAX = "tổng tiền chưa thuế"
_COL_CURRENCY = "đơn vị tiền tệ"
_COL_RATE = "tỷ giá"
_COL_STATUS = "trạng thái hóa đơn"
_REQUIRED = (_COL_SERIES, _COL_NUMBER, _COL_DATE, _COL_SELLER, _COL_BUYER,
             _COL_BUYER_NAME, _COL_PRETAX, _COL_STATUS)

_PERIOD = re.compile(r"(\d{2})/(\d{2})/(\d{4})\D+(\d{2})/(\d{2})/(\d{4})")
_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


# --------------------------------------------------------------------------- #
# Errors and warnings
# --------------------------------------------------------------------------- #


class EInvoiceParseError(ValueError):
    """The archive cannot be used as evidence. `code` is translated by the UI."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Warning_:
    code: str
    detail: str


# --------------------------------------------------------------------------- #
# Parsed records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Invoice:
    form: str
    series: str
    number: int
    issued: date
    buyer_tax_code: str
    buyer_name: str
    pretax_vnd: int
    status: str

    @property
    def counts_as_revenue(self) -> bool:
        return self.status not in _EXCLUDED_STATUSES


@dataclass(frozen=True)
class MonthlyListing:
    year: int
    month: int
    seller_tax_code: str
    invoices: tuple[Invoice, ...]
    source_name: str

    @property
    def key(self) -> tuple[int, int]:
        return (self.year, self.month)

    @property
    def label(self) -> str:
        return f"{self.month:02d}/{self.year}"

    @property
    def revenue_vnd(self) -> int:
        return sum(i.pretax_vnd for i in self.invoices if i.counts_as_revenue)


@dataclass
class EInvoiceFeatures:
    """What the listings supply, in the units the grading engine uses."""

    parser_version: str
    source: str
    signed: bool
    seller_tax_code: str
    period_start: str  # MM/YYYY, oldest month
    period_end: str
    months_covered: int
    monthly_revenue: list[dict[str, Any]]  # [{"period": "MM/YYYY", "revenue_vnd": int}], oldest first
    total_revenue_vnd: int
    invoice_count: int
    invoices_excluded: int
    related_party_revenue_vnd: int
    related_party_pct: float
    external_customers: int
    conc_top1_pct: float | None
    conc_top3_pct: float | None
    crr: float | None
    crr_definition: str
    warnings: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _norm(value: Any) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().lower()


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(value or ""))).strip()


def _decimal(value: Any, what: str, where: str) -> Decimal:
    try:
        return Decimal(str(value).strip()) if value not in (None, "") else Decimal(0)
    except InvalidOperation as exc:
        raise EInvoiceParseError(f"{where}: {what} {value!r} is not a number", "UNREADABLE_LISTING") from exc


def _issue_date(value: Any, where: str) -> date:
    text = str(value or "").strip()
    match = _DATE.match(text)
    if match:
        d, m, y = (int(x) for x in match.groups())
        return date(y, m, d)
    # An .xlsx may store the date as a serial number instead of text.
    try:
        serial = float(text)
    except ValueError as exc:
        raise EInvoiceParseError(f"{where}: issue date {text!r} is unreadable", "UNREADABLE_LISTING") from exc
    return date(1899, 12, 30) + timedelta(days=int(serial))


def _month_end(year: int, month: int) -> date:
    first_next = date(year + (month == 12), month % 12 + 1, 1)
    return first_next - timedelta(days=1)


def _months_between(a: tuple[int, int], b: tuple[int, int]) -> int:
    return (b[0] - a[0]) * 12 + (b[1] - a[1])


# --------------------------------------------------------------------------- #
# One listing
# --------------------------------------------------------------------------- #


def parse_listing(raw: bytes, source_name: str) -> MonthlyListing:
    """One monthly .xlsx export."""
    where = source_name
    try:
        rows = read_first_sheet(raw)
    except XlsxError as exc:
        raise EInvoiceParseError(f"{where}: {exc}", "UNREADABLE_LISTING") from exc

    header_index = next((i for i, r in enumerate(rows) if r and _norm(r[0]) == "stt"), None)
    if header_index is None:
        raise EInvoiceParseError(f"{where}: not an invoice listing (no header row)", "NOT_A_LISTING")
    columns = {_norm(h): i for i, h in enumerate(rows[header_index]) if h}
    missing = [c for c in _REQUIRED if c not in columns]
    if missing:
        raise EInvoiceParseError(f"{where}: missing column(s) {', '.join(missing)}", "NOT_A_LISTING")

    def cell(row: list, name: str) -> Any:
        i = columns.get(name)
        return row[i] if i is not None and i < len(row) else None

    invoices: list[Invoice] = []
    sellers: set[str] = set()
    for row in rows[header_index + 1:]:
        if not row or not re.match(r"^\d", str(row[0] or "").strip()):
            continue  # totals, blank or footer rows
        line = f"{where} row {str(row[0]).split('.')[0]}"
        status = _norm(cell(row, _COL_STATUS))
        amount = _decimal(cell(row, _COL_PRETAX), "pre-tax amount", line)
        currency = _norm(cell(row, _COL_CURRENCY)) or "vnd"
        if currency != "vnd":
            rate = _decimal(cell(row, _COL_RATE), "exchange rate", line)
            if rate <= 0:
                raise EInvoiceParseError(f"{line}: {currency.upper()} invoice without an exchange rate", "UNREADABLE_LISTING")
            amount *= rate
        number = _decimal(cell(row, _COL_NUMBER), "invoice number", line)
        sellers.add(_clean(cell(row, _COL_SELLER)))
        invoices.append(
            Invoice(
                form=_clean(cell(row, _COL_FORM)),
                series=_clean(cell(row, _COL_SERIES)),
                number=int(number),
                issued=_issue_date(cell(row, _COL_DATE), line),
                buyer_tax_code=_clean(cell(row, _COL_BUYER)),
                buyer_name=_clean(cell(row, _COL_BUYER_NAME)),
                pretax_vnd=int(amount.to_integral_value()),
                status=status,
            )
        )

    if not invoices:
        raise EInvoiceParseError(f"{where}: the listing has no invoices", "EMPTY_LISTING")
    if len(sellers) != 1 or "" in sellers:
        raise EInvoiceParseError(
            f"{where}: invoices from more than one seller ({', '.join(sorted(sellers))})",
            "MIXED_SELLERS",
        )

    # The period comes from the export's own header ("Từ ngày 01/01/2026 đến
    # ngày 31/01/2026"), falling back to the invoice dates. It must be exactly
    # one calendar month: a quarter or a half month cannot be placed in a
    # monthly series without inventing a split.
    header_text = " ".join(str(c) for r in rows[:header_index] for c in r if c)
    period = _PERIOD.search(header_text)
    if period:
        d1, m1, y1, d2, m2, y2 = (int(x) for x in period.groups())
        start, end = date(y1, m1, d1), date(y2, m2, d2)
        if start.day != 1 or (y1, m1) != (y2, m2) or end != _month_end(y1, m1):
            raise EInvoiceParseError(
                f"{where}: covers {start:%d/%m/%Y}–{end:%d/%m/%Y}, not one calendar month",
                "NOT_A_MONTH",
            )
        year, month = y1, m1
    else:
        months = {(i.issued.year, i.issued.month) for i in invoices}
        if len(months) != 1:
            raise EInvoiceParseError(f"{where}: invoices span more than one month", "NOT_A_MONTH")
        year, month = months.pop()

    outside = [i for i in invoices if (i.issued.year, i.issued.month) != (year, month)]
    if outside:
        raise EInvoiceParseError(
            f"{where}: {len(outside)} invoice(s) dated outside {month:02d}/{year}",
            "NOT_A_MONTH",
        )

    return MonthlyListing(year=year, month=month, seller_tax_code=sellers.pop(),
                          invoices=tuple(invoices), source_name=source_name)


# --------------------------------------------------------------------------- #
# The archive
# --------------------------------------------------------------------------- #


def read_archive(raw: bytes) -> list[MonthlyListing]:
    """Every listing in the uploaded zip, oldest month first."""
    try:
        archive = zipfile.ZipFile(BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise EInvoiceParseError("The upload is not a readable .zip", "NOT_A_ZIP") from exc

    with archive:
        entries = []
        for info in archive.infolist():
            name = info.filename
            base = name.rsplit("/", 1)[-1]
            # Folders, macOS resource forks and Office lock files are noise.
            if info.is_dir() or name.startswith("__MACOSX/") or base.startswith((".", "~$")):
                continue
            entries.append(info)

        if len(entries) > MAX_ENTRIES:
            raise EInvoiceParseError(f"The .zip holds {len(entries)} files; at most {MAX_ENTRIES}", "TOO_MANY_FILES")
        if sum(i.file_size for i in entries) > MAX_TOTAL_BYTES:
            raise EInvoiceParseError("The .zip is too large once extracted", "ARCHIVE_TOO_LARGE")

        listings = []
        for info in entries:
            if not info.filename.lower().endswith(".xlsx"):
                raise EInvoiceParseError(
                    f"{info.filename}: only the portal's .xlsx monthly exports are read",
                    "UNSUPPORTED_FILE",
                )
            if info.file_size > MAX_ENTRY_BYTES:
                raise EInvoiceParseError(f"{info.filename} is too large", "ARCHIVE_TOO_LARGE")
            with archive.open(info) as handle:
                data = handle.read(MAX_ENTRY_BYTES + 1)
            if len(data) > MAX_ENTRY_BYTES:
                raise EInvoiceParseError(f"{info.filename} is too large", "ARCHIVE_TOO_LARGE")
            listings.append(parse_listing(data, info.filename.rsplit("/", 1)[-1]))

    if not listings:
        raise EInvoiceParseError("The .zip has no invoice listings", "NO_LISTINGS")

    listings.sort(key=lambda l: l.key)
    seen: dict[tuple[int, int], str] = {}
    for listing in listings:
        if listing.key in seen:
            raise EInvoiceParseError(
                f"{listing.label} appears twice ({seen[listing.key]} and {listing.source_name})",
                "DUPLICATE_MONTH",
            )
        seen[listing.key] = listing.source_name
    sellers = {l.seller_tax_code for l in listings}
    if len(sellers) != 1:
        raise EInvoiceParseError(
            f"The listings belong to different sellers: {', '.join(sorted(sellers))}",
            "MIXED_SELLERS",
        )
    return listings


# --------------------------------------------------------------------------- #
# Checks and features
# --------------------------------------------------------------------------- #


def _numbering_warnings(listings: list[MonthlyListing]) -> list[Warning_]:
    """Handbook §6, arithmetically: within a period the numbers of a series
    run without holes, and each period starts right after the last. Cancelled
    invoices stay in the list with their number, so a hole is a missing
    invoice, not a void one."""
    warnings: list[Warning_] = []
    last_by_series: dict[tuple[str, str], tuple[str, int]] = {}
    for listing in listings:
        by_series: dict[tuple[str, str], list[int]] = defaultdict(list)
        for inv in listing.invoices:
            by_series[(inv.form, inv.series)].append(inv.number)
        for key, numbers in sorted(by_series.items()):
            distinct = sorted(set(numbers))
            holes = [n for n in range(distinct[0], distinct[-1] + 1) if n not in set(distinct)]
            if holes:
                shown = ", ".join(str(n) for n in holes[:10]) + ("…" if len(holes) > 10 else "")
                warnings.append(Warning_("NUMBERING_GAP", f"{listing.label} series {key[1]}: missing #{shown}"))
            if len(numbers) != len(distinct):
                warnings.append(Warning_("DUPLICATE_NUMBER", f"{listing.label} series {key[1]}: a number appears twice"))
            previous = last_by_series.get(key)
            if previous and distinct[0] != previous[1] + 1:
                warnings.append(Warning_(
                    "NUMBERING_GAP",
                    f"series {key[1]}: {previous[0]} ends at #{previous[1]}, {listing.label} starts at #{distinct[0]}",
                ))
            last_by_series[key] = (listing.label, distinct[-1])
    return warnings


def summarise(listings: list[MonthlyListing]) -> EInvoiceFeatures:
    warnings: list[Warning_] = []
    first, last = listings[0], listings[-1]

    for earlier, later in zip(listings, listings[1:]):
        gap = _months_between(earlier.key, later.key) - 1
        if gap > 0:
            warnings.append(Warning_("MONTH_GAP", f"{gap} month(s) missing between {earlier.label} and {later.label}"))
    if len(listings) < 12:
        warnings.append(Warning_("FEWER_THAN_12_MONTHS", f"{len(listings)} of 12 months supplied"))
    warnings.extend(_numbering_warnings(listings))

    invoices = [i for l in listings for i in l.invoices]
    unknown = sorted({i.status for i in invoices} - _KNOWN_STATUSES)
    if unknown:
        warnings.append(Warning_("UNKNOWN_STATUS", f"counted as revenue: {', '.join(unknown)}"))
    replaced = sum(i.status == _REPLACED for i in invoices)
    replacements = sum(i.status == _REPLACEMENT for i in invoices)
    if replaced != replacements:
        # A replacement can fall in a month outside the upload; worth a look,
        # not a rejection.
        warnings.append(Warning_(
            "UNMATCHED_REPLACEMENT",
            f"{replaced} invoice(s) marked replaced but {replacements} replacement(s) in these months",
        ))

    seller = first.seller_tax_code
    live = [i for i in invoices if i.counts_as_revenue]
    total = sum(i.pretax_vnd for i in live)

    # Sales to the seller's own branches ("1500407363-001") are internal
    # transfers, not customers. They stay in revenue — the signed VAT
    # declaration includes them too — but are kept out of concentration and
    # retention, and their share is reported so underwriting can judge it.
    def is_related(inv: Invoice) -> bool:
        return inv.buyer_tax_code == seller or inv.buyer_tax_code.startswith(f"{seller}-")

    related = sum(i.pretax_vnd for i in live if is_related(i))

    def buyer_key(inv: Invoice) -> str:
        # Individuals often have no tax code; their name is the only identity.
        return inv.buyer_tax_code or f"name:{_norm(inv.buyer_name)}"

    by_buyer: dict[str, int] = defaultdict(int)
    for inv in live:
        if not is_related(inv):
            by_buyer[buyer_key(inv)] += inv.pretax_vnd
    external_total = sum(by_buyer.values())
    ranked = sorted(by_buyer.values(), reverse=True)
    top1 = round(100 * ranked[0] / external_total, 2) if external_total > 0 else None
    top3 = round(100 * sum(ranked[:3]) / external_total, 2) if external_total > 0 else None

    # Customer retention, 0-100 (the engine's rti scale). PROPOSED DEFINITION —
    # the params file names crr without defining it: of the tax-coded customers
    # who bought in the first half of the covered months, the share who bought
    # again in the second half. Untaxed individuals are left out: a name is not
    # a stable enough identity to say "the same customer came back".
    crr = None
    if len(listings) >= 4:
        half = len(listings) // 2

        def coded_buyers(part: list[MonthlyListing]) -> set[str]:
            return {
                i.buyer_tax_code for l in part for i in l.invoices
                if i.counts_as_revenue and i.buyer_tax_code and not is_related(i)
            }

        early, late = coded_buyers(listings[:half]), coded_buyers(listings[half:])
        if early:
            crr = round(100 * len(early & late) / len(early), 2)

    return EInvoiceFeatures(
        parser_version=PARSER_VERSION,
        source=SOURCE,
        signed=False,
        seller_tax_code=seller,
        period_start=first.label,
        period_end=last.label,
        months_covered=len(listings),
        monthly_revenue=[{"period": l.label, "revenue_vnd": l.revenue_vnd} for l in listings],
        total_revenue_vnd=total,
        invoice_count=len(invoices),
        invoices_excluded=len(invoices) - len(live),
        related_party_revenue_vnd=related,
        related_party_pct=round(100 * related / total, 2) if total > 0 else 0.0,
        external_customers=len(by_buyer),
        conc_top1_pct=top1,
        conc_top3_pct=top3,
        crr=crr,
        crr_definition="share of tax-coded customers in the first half of the months who also bought in the second half",
        warnings=[{"code": w.code, "detail": w.detail} for w in warnings],
    )


def parse_einvoice_zip(raw: bytes) -> EInvoiceFeatures:
    return summarise(read_archive(raw))


# --------------------------------------------------------------------------- #
# Mapping onto the grading engine
# --------------------------------------------------------------------------- #


def grading_fields(features: dict[str, Any]) -> dict[str, Any]:
    """The GradingInput fields these listings supply, keyed by engine name.

    `monthly_revenue_latest` is the newest-last run of observed months, at most
    12: it fills m13..m24. m1..m12 (the prior year) must come from elsewhere —
    the signed VAT declarations, ideally — and the engine refuses to grade
    without all 24 (R8 → INSUFFICIENT_DATA), so this alone does not make an
    application gradeable. Months are only usable if consecutive; a gap makes
    the series unusable for the volatility and seasonality factors.
    """
    months = features["monthly_revenue"][-12:]
    consecutive = not any(w["code"] == "MONTH_GAP" for w in features.get("warnings", []))
    return {
        "monthly_revenue_latest": [m["revenue_vnd"] for m in months] if consecutive else None,
        "monthly_revenue_periods": [m["period"] for m in months],
        "conc_top1_pct": features.get("conc_top1_pct"),
        "conc_top3_pct": features.get("conc_top3_pct"),
        "crr": features.get("crr"),
    }
