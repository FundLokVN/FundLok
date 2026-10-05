"""Read monthly revenue out of signed VAT declarations (Mẫu số 01/GTGT).

WHY THIS EXISTS
`GradingInput.monthly_revenue` wants 24 ordered monthly figures. Until now the
Lite flow MODELLED them from four typed numbers, so revenue volatility, trend
and seasonality — three of the four RSG core factors, roughly 23% of the final
grade — were computed from a shape nobody had observed. The SME already uploads
the declarations that contain the real series; nothing read them.

WHAT A DECLARATION LOOKS LIKE
Each monthly upload is a zip holding one XML: an electronic tax dossier
(`HSoThueDTu`) carrying a `TKhaiThue` declaration and an XML-DSig `Signature`.
The fields this module needs:

    maTKhai   842                         form code
    kyKKhai   01/2025                     the period, MM/YYYY
    mst       1500407363                  taxpayer code
    tenNNT    CÔNG TY TNHH ...            taxpayer name
    ct29      419,110,755                 revenue NOT subject to VAT
    ct32      3,697,809,528               revenue subject to VAT
    ct34      4,116,920,283               TOTAL revenue sold  <- what we want

Indicator [34] is the total the engine wants, and [29] + [32] = [34] is a
self-check the form guarantees. A file failing it has been altered or is a form
we do not understand; either way it must not silently feed a grade.

WHAT THIS MODULE DOES NOT DO
No signature verification. The presence of a `Signature` element is recorded,
not validated — checking the certificate chain against the tax authority's
CA is a separate job, and pretending otherwise would overstate what we know.
`signed` here means "the file claims to be signed".
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from io import BytesIO

# Indicator codes on form 01/GTGT.
_CT_REVENUE_UNTAXED = "ct29"
_CT_REVENUE_TAXED = "ct32"
_CT_REVENUE_TOTAL = "ct34"

_PERIOD = re.compile(r"^(\d{1,2})/(\d{4})$")

# Real filings namespace the signature — `<ds:Signature xmlns:ds="...">` — so
# matching a bare `<Signature>` reports every genuine declaration as unsigned.
# Any prefix counts; the xmldsig namespace is what actually identifies it.
_SIGNATURE = re.compile(r"<(?:[A-Za-z0-9_.-]+:)?Signature[\s>]")


class VatParseError(ValueError):
    """A declaration that cannot be trusted to supply a revenue figure."""


@dataclass(frozen=True)
class VatDeclaration:
    """One month, as read off the filing."""

    period_month: int
    period_year: int
    tax_code: str
    taxpayer_name: str | None
    revenue_total: int
    signed: bool

    @property
    def period_key(self) -> tuple[int, int]:
        """Sortable (year, month) — the filename is not reliable ordering."""
        return (self.period_year, self.period_month)

    @property
    def period_label(self) -> str:
        return f"{self.period_month:02d}/{self.period_year}"


def _tag(xml: str, name: str) -> str | None:
    match = re.search(rf"<{name}(?:\s[^>]*)?>([^<]*)</{name}>", xml)
    return match.group(1).strip() if match else None


def _int_tag(xml: str, name: str) -> int | None:
    raw = _tag(xml, name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def parse_declaration_xml(xml: str) -> VatDeclaration:
    """Read one declaration. Raises VatParseError on anything untrustworthy."""
    period_raw = _tag(xml, "kyKKhai")
    if not period_raw:
        raise VatParseError("No kyKKhai (period) — not a recognisable declaration")
    matched = _PERIOD.match(period_raw)
    if not matched:
        # A quarterly filing (e.g. "Q1/2025") is a valid form but not a month,
        # and silently treating it as one would misplace a quarter's revenue
        # into a single month.
        raise VatParseError(f"Period {period_raw!r} is not a monthly MM/YYYY period")
    month, year = int(matched.group(1)), int(matched.group(2))
    if not 1 <= month <= 12:
        raise VatParseError(f"Month {month} out of range in period {period_raw!r}")

    tax_code = _tag(xml, "mst")
    if not tax_code:
        raise VatParseError("No mst (tax code) — the filing cannot be attributed")

    total = _int_tag(xml, _CT_REVENUE_TOTAL)
    if total is None:
        raise VatParseError(f"No [{_CT_REVENUE_TOTAL[2:]}] total revenue indicator")
    if total < 0:
        raise VatParseError("Total revenue is negative")

    # [29] + [32] = [34] is guaranteed by the form. Both parts are optional in
    # the sense that a zero may be omitted, so only check when both are present.
    untaxed = _int_tag(xml, _CT_REVENUE_UNTAXED)
    taxed = _int_tag(xml, _CT_REVENUE_TAXED)
    if untaxed is not None and taxed is not None and untaxed + taxed != total:
        raise VatParseError(
            f"Indicators do not reconcile: [29] {untaxed} + [32] {taxed} != [34] {total}"
        )

    return VatDeclaration(
        period_month=month,
        period_year=year,
        tax_code=tax_code,
        taxpayer_name=_tag(xml, "tenNNT"),
        revenue_total=total,
        signed=_SIGNATURE.search(xml) is not None,
    )


def parse_declaration_zip(raw: bytes) -> VatDeclaration:
    """Read the single XML inside one monthly zip."""
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".xml")]
            if not names:
                raise VatParseError("Archive contains no XML declaration")
            if len(names) > 1:
                raise VatParseError(
                    f"Archive holds {len(names)} XML files; expected exactly one"
                )
            payload = archive.read(names[0])
    except zipfile.BadZipFile as exc:
        raise VatParseError("Not a readable zip archive") from exc

    return parse_declaration_xml(payload.decode("utf-8", errors="replace"))


@dataclass(frozen=True)
class VatSeries:
    """An ordered run of declarations, ready to become monthly_revenue."""

    declarations: tuple[VatDeclaration, ...]
    tax_codes: frozenset[str]

    @property
    def revenues(self) -> tuple[int, ...]:
        """Oldest first, matching GradingInput.monthly_revenue (m1..m24)."""
        return tuple(d.revenue_total for d in self.declarations)

    @property
    def months_covered(self) -> int:
        return len(self.declarations)

    @property
    def all_signed(self) -> bool:
        return all(d.signed for d in self.declarations)

    def has_gaps(self) -> bool:
        """True when the periods are not consecutive months.

        A gap matters more than a short series: 24 declarations with March
        missing is not 24 months of history, and the engine's volatility and
        seasonality factors would read the jump as real movement.
        """
        for earlier, later in zip(self.declarations, self.declarations[1:]):
            months = (later.period_year - earlier.period_year) * 12 + (
                later.period_month - earlier.period_month
            )
            if months != 1:
                return True
        return False


def build_series(declarations: list[VatDeclaration]) -> VatSeries:
    """Order declarations oldest-first and reject duplicate periods.

    Sorted by the period INSIDE the file, never by filename — the uploads are
    named inconsistently (`1.2026.zip` beside `01.2025.zip`), and a lexical
    sort would interleave years.
    """
    if not declarations:
        raise VatParseError("No declarations to build a series from")

    ordered = sorted(declarations, key=lambda d: d.period_key)
    seen: set[tuple[int, int]] = set()
    for declaration in ordered:
        if declaration.period_key in seen:
            raise VatParseError(
                f"Period {declaration.period_label} appears more than once"
            )
        seen.add(declaration.period_key)

    return VatSeries(
        declarations=tuple(ordered),
        tax_codes=frozenset(d.tax_code for d in ordered),
    )
