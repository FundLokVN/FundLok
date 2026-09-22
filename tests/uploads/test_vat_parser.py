"""Reading monthly revenue out of signed VAT declarations (01/GTGT).

The fixtures below mirror the real filings byte-shape: an HSoThueDTu dossier
with kyKKhai / mst / tenNNT and the ct29 / ct32 / ct34 indicators. Real
declarations are ~7KB of namespaced XML and a signature block; reproducing all
of that would test the XML, not the parser.
"""
import io
import zipfile

import pytest

from app.uploads.parsers.vat import (
    VatParseError,
    build_series,
    parse_declaration_xml,
    parse_declaration_zip,
)


def _xml(
    *,
    period="01/2025",
    mst="1500407363",
    name="CÔNG TY TNHH NHỰA THỊNH HÒA",
    ct29=419110755,
    ct32=3697809528,
    ct34=4116920283,
    signed=True,
) -> str:
    parts = [
        "<HSoThueDTu><TKhaiThue><TTinChung><TTinTKhaiThue>",
        "<TKhaiThue><maTKhai>842</maTKhai>",
        f"<kyKKhai>{period}</kyKKhai></TKhaiThue>",
        f"<NNT><mst>{mst}</mst><tenNNT>{name}</tenNNT></NNT>",
        "</TTinTKhaiThue></TTinChung><CTieuTKhaiChinh>",
    ]
    if ct29 is not None:
        parts.append(f"<ct29>{ct29}</ct29>")
    if ct32 is not None:
        parts.append(f"<ct32>{ct32}</ct32>")
    if ct34 is not None:
        parts.append(f"<ct34>{ct34}</ct34>")
    parts.append("</CTieuTKhaiChinh></TKhaiThue>")
    if signed:
        parts.append("<Signature>stub</Signature>")
    parts.append("</HSoThueDTu>")
    return "".join(parts)


def _zip(xml: str, filename="files_113.xml") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(filename, xml)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# One declaration
# --------------------------------------------------------------------------- #


def test_reads_the_total_revenue_indicator():
    """[34] is the figure the engine wants — not [32], which excludes
    non-taxable revenue and would understate the month."""
    d = parse_declaration_xml(_xml())
    assert d.revenue_total == 4116920283
    assert d.period_month == 1 and d.period_year == 2025
    assert d.tax_code == "1500407363"
    assert d.signed is True


def test_rejects_indicators_that_do_not_reconcile():
    """[29] + [32] = [34] is guaranteed by the form. A file that fails it has
    been altered or is a form we do not understand — either way it must not
    silently feed a grade."""
    with pytest.raises(VatParseError, match="reconcile"):
        parse_declaration_xml(_xml(ct29=1, ct32=2, ct34=999))


def test_accepts_a_filing_that_omits_a_zero_part():
    """A zero indicator may simply be absent, so the cross-check only applies
    when both parts are present."""
    d = parse_declaration_xml(_xml(ct29=None, ct32=None, ct34=500))
    assert d.revenue_total == 500


def test_rejects_a_quarterly_period():
    """A quarter is a valid filing but not a month. Treating "Q1/2025" as a
    month would drop a quarter's revenue into one slot."""
    with pytest.raises(VatParseError, match="monthly"):
        parse_declaration_xml(_xml(period="Q1/2025"))


def test_rejects_a_filing_with_no_tax_code():
    """Without mst the filing cannot be attributed to the applicant, which is
    the whole basis of the cross-check."""
    with pytest.raises(VatParseError, match="attributed"):
        parse_declaration_xml(_xml(mst=""))


def test_records_an_unsigned_filing_without_rejecting_it():
    """`signed` reports what the file claims. It is not a verification — the
    caller decides what to do with an unsigned declaration."""
    assert parse_declaration_xml(_xml(signed=False)).signed is False


# --------------------------------------------------------------------------- #
# The zip wrapper
# --------------------------------------------------------------------------- #


def test_reads_the_single_xml_inside_a_zip():
    assert parse_declaration_zip(_zip(_xml())).revenue_total == 4116920283


def test_rejects_a_zip_holding_more_than_one_declaration():
    """Which month would it be? Better to refuse than to pick one."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.xml", _xml(period="01/2025"))
        archive.writestr("b.xml", _xml(period="02/2025"))
    with pytest.raises(VatParseError, match="expected exactly one"):
        parse_declaration_zip(buffer.getvalue())


def test_rejects_something_that_is_not_a_zip():
    with pytest.raises(VatParseError, match="readable zip"):
        parse_declaration_zip(b"%PDF-1.7 this is not a zip")


# --------------------------------------------------------------------------- #
# The series
# --------------------------------------------------------------------------- #


def test_orders_by_the_period_inside_the_file_not_the_filename():
    """The real uploads are named inconsistently — `1.2026.zip` sits beside
    `01.2025.zip` — so a lexical sort would interleave years."""
    series = build_series(
        [
            parse_declaration_xml(_xml(period="1/2026", ct29=None, ct32=None, ct34=30)),
            parse_declaration_xml(_xml(period="01/2025", ct29=None, ct32=None, ct34=10)),
            parse_declaration_xml(_xml(period="12/2025", ct29=None, ct32=None, ct34=20)),
        ]
    )
    assert series.revenues == (10, 20, 30)
    assert series.declarations[0].period_label == "01/2025"


def test_rejects_a_duplicated_period():
    with pytest.raises(VatParseError, match="more than once"):
        build_series(
            [
                parse_declaration_xml(_xml(period="01/2025")),
                parse_declaration_xml(_xml(period="01/2025")),
            ]
        )


def test_detects_a_missing_month():
    """24 declarations with March missing is not 24 months of history — the
    engine's volatility and seasonality factors would read the jump as real."""
    series = build_series(
        [
            parse_declaration_xml(_xml(period=p, ct29=None, ct32=None, ct34=1))
            for p in ("01/2025", "02/2025", "04/2025")
        ]
    )
    assert series.has_gaps() is True


def test_consecutive_months_have_no_gaps_across_a_year_boundary():
    series = build_series(
        [
            parse_declaration_xml(_xml(period=p, ct29=None, ct32=None, ct34=1))
            for p in ("11/2025", "12/2025", "01/2026")
        ]
    )
    assert series.has_gaps() is False
    assert series.months_covered == 3


def test_surfaces_every_tax_code_present():
    """More than one means the batch mixes companies — the caller cross-checks
    this against the verified KYB tax code."""
    series = build_series(
        [
            parse_declaration_xml(_xml(period="01/2025", mst="111")),
            parse_declaration_xml(_xml(period="02/2025", mst="222")),
        ]
    )
    assert series.tax_codes == frozenset({"111", "222"})


def test_detects_a_namespaced_signature():
    """Real filings sign with `<ds:Signature xmlns:ds="...">`. Matching only a
    bare `<Signature>` reported every genuine declaration as unsigned — caught
    by running the parser over 24 real files, not by the fixtures above.
    """
    xml = _xml(signed=False).replace(
        "</HSoThueDTu>",
        '<ds:Signature xmlns:ds="http://www.w3.org/2000/09/xmldsig#">'
        "<ds:SignatureValue>x</ds:SignatureValue></ds:Signature></HSoThueDTu>",
    )
    assert parse_declaration_xml(xml).signed is True
