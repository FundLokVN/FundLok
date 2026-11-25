"""The e-invoice listing parser (app/uploads/parsers/einvoice.py).

Checked against the reference data set during development (12 real monthly
exports): revenue matched the signed VAT declarations to the dong in all 11
overlapping months. These tests pin the rules on synthetic listings.
"""
import pytest

from app.uploads.parsers import einvoice as parser
from app.uploads.parsers.einvoice import EInvoiceParseError, grading_fields, parse_einvoice_zip
from tests.uploads.einvoice_fixtures import SELLER, Inv, archive, listing_xlsx, year_of_listings


def codes(features):
    return [w["code"] for w in features.warnings]


def test_a_year_of_listings_becomes_twelve_months_oldest_first():
    f = parse_einvoice_zip(archive(year_of_listings()))
    assert f.months_covered == 12
    assert [m["period"] for m in f.monthly_revenue][:2] == ["09/2025", "10/2025"]
    assert f.monthly_revenue[-1]["period"] == "08/2026"
    assert f.seller_tax_code == SELLER
    assert f.signed is False and f.source == "portal_listing_xlsx"
    assert codes(f) == []


def test_file_names_do_not_order_the_months():
    files = year_of_listings(months=3)
    renamed = {"z.xlsx": files["Einvoice Data/HDT1.xlsx"],
               "a.xlsx": files["Einvoice Data/HDT3.xlsx"],
               "m.xlsx": files["Einvoice Data/HDT2.xlsx"]}
    f = parse_einvoice_zip(archive(renamed))
    assert [m["period"] for m in f.monthly_revenue] == ["09/2025", "10/2025", "11/2025"]


def test_replaced_and_cancelled_invoices_are_not_revenue_but_adjustments_are():
    listing = listing_xlsx(2026, 1, [
        Inv(1, "0100000001", 1_000_000),
        Inv(2, "0100000001", 2_000_000, status="Hóa đơn đã bị thay thế"),
        Inv(3, "0100000001", 2_100_000, status="Hóa đơn thay thế"),
        Inv(4, "0100000002", 5_000_000, status="Hóa đơn đã bị hủy"),
        Inv(5, "0100000002", 3_000_000, status="Hóa đơn đã bị điều chỉnh"),
        Inv(6, "0100000002", -500_000, status="Hóa đơn điều chỉnh"),
    ])
    f = parse_einvoice_zip(archive({"a.xlsx": listing}))
    assert f.monthly_revenue[0]["revenue_vnd"] == 1_000_000 + 2_100_000 + 3_000_000 - 500_000
    assert f.invoices_excluded == 2


def test_foreign_currency_is_converted_at_the_invoice_rate():
    listing = listing_xlsx(2026, 1, [Inv(1, "0100000001", 100, currency="USD", rate="25000")])
    f = parse_einvoice_zip(archive({"a.xlsx": listing}))
    assert f.monthly_revenue[0]["revenue_vnd"] == 2_500_000


def test_sales_to_own_branches_stay_in_revenue_but_not_in_concentration():
    listing = listing_xlsx(2026, 1, [
        Inv(1, f"{SELLER}-001", 6_000_000),
        Inv(2, "0100000001", 3_000_000),
        Inv(3, "0100000002", 1_000_000),
    ])
    f = parse_einvoice_zip(archive({"a.xlsx": listing}))
    assert f.total_revenue_vnd == 10_000_000
    assert f.related_party_pct == 60.0
    assert f.conc_top1_pct == 75.0  # 3m of the 4m external
    assert f.external_customers == 2


def test_customers_without_a_tax_code_are_told_apart_by_name():
    listing = listing_xlsx(2026, 1, [
        Inv(1, "", 1_000_000, buyer_name="NGUYỄN VĂN A"),
        Inv(2, "", 1_000_000, buyer_name="Nguyễn  Văn A"),
        Inv(3, "", 1_000_000, buyer_name="TRẦN THỊ B"),
    ])
    f = parse_einvoice_zip(archive({"a.xlsx": listing}))
    assert f.external_customers == 2


def test_retention_is_the_share_of_early_customers_who_came_back():
    files = {}
    for i, (m, buyers) in enumerate([(1, "AB"), (2, "C"), (3, "A"), (4, "D")]):
        invs = [Inv(i * 10 + k + 1, f"01000000{b}", 1_000_000, day=5) for k, b in enumerate(buyers)]
        files[f"{m}.xlsx"] = listing_xlsx(2026, m, invs)
    f = parse_einvoice_zip(archive(files))
    # first half {A,B,C}, second half {A,D}: 1 of 3 came back
    assert f.crr == pytest.approx(33.33)


def test_too_few_months_gives_no_retention():
    f = parse_einvoice_zip(archive(year_of_listings(months=3)))
    assert f.crr is None
    assert "FEWER_THAN_12_MONTHS" in codes(f)


# --- completeness: surfaced, never healed ----------------------------------


def test_a_hole_in_the_numbering_is_a_warning_with_the_number():
    listing = listing_xlsx(2026, 1, [Inv(1, "0100000001", 1), Inv(2, "0100000001", 1), Inv(5, "0100000001", 1)])
    f = parse_einvoice_zip(archive({"a.xlsx": listing}))
    gap = [w for w in f.warnings if w["code"] == "NUMBERING_GAP"]
    assert gap and "#3, 4" in gap[0]["detail"]


def test_a_month_that_does_not_continue_the_last_ones_numbering_is_flagged():
    jan = listing_xlsx(2026, 1, [Inv(1, "0100000001", 1), Inv(2, "0100000001", 1)])
    feb = listing_xlsx(2026, 2, [Inv(9, "0100000001", 1)])
    f = parse_einvoice_zip(archive({"1.xlsx": jan, "2.xlsx": feb}))
    assert any("ends at #2" in w["detail"] for w in f.warnings if w["code"] == "NUMBERING_GAP")


def test_a_new_series_at_the_new_year_restarts_numbering_cleanly():
    f = parse_einvoice_zip(archive(year_of_listings()))  # C25TAA → C26TAA
    assert "NUMBERING_GAP" not in codes(f)


def test_a_missing_month_is_flagged_and_the_series_is_not_used_for_grading():
    files = year_of_listings(months=4)
    del files["Einvoice Data/HDT2.xlsx"]
    f = parse_einvoice_zip(archive(files))
    assert "MONTH_GAP" in codes(f)
    assert grading_fields(f.as_dict())["monthly_revenue_latest"] is None


def test_unmatched_replacement_is_a_warning():
    listing = listing_xlsx(2026, 1, [Inv(1, "0100000001", 1, status="Hóa đơn đã bị thay thế")])
    f = parse_einvoice_zip(archive({"a.xlsx": listing}))
    assert "UNMATCHED_REPLACEMENT" in codes(f)


# --- refusals ----------------------------------------------------------------


def _code(raw: bytes) -> str:
    with pytest.raises(EInvoiceParseError) as exc:
        parse_einvoice_zip(raw)
    return exc.value.code


def test_not_a_zip():
    assert _code(b"not a zip") == "NOT_A_ZIP"


def test_empty_zip():
    assert _code(archive({})) == "NO_LISTINGS"


def test_the_same_month_twice():
    one = listing_xlsx(2026, 1, [Inv(1, "0100000001", 1)])
    assert _code(archive({"a.xlsx": one, "b.xlsx": one})) == "DUPLICATE_MONTH"


def test_listings_from_two_companies():
    a = listing_xlsx(2026, 1, [Inv(1, "0100000001", 1)])
    b = listing_xlsx(2026, 2, [Inv(2, "0100000001", 1, seller="0999999999")])
    assert _code(archive({"a.xlsx": a, "b.xlsx": b})) == "MIXED_SELLERS"


def test_a_period_that_is_not_one_calendar_month():
    half = listing_xlsx(2026, 1, [Inv(1, "0100000001", 1)], period="Từ ngày 01/01/2026 đến ngày 15/01/2026")
    assert _code(archive({"a.xlsx": half})) == "NOT_A_MONTH"


def test_an_invoice_dated_outside_the_listed_month():
    listing = listing_xlsx(2026, 2, [Inv(1, "0100000001", 1, day=30)])  # 30/02 does not exist
    with pytest.raises((EInvoiceParseError, ValueError)):
        parse_einvoice_zip(archive({"a.xlsx": listing}))


def test_something_that_is_not_a_listing():
    assert _code(archive({"a.xlsx": archive({"xl/worksheets/sheet1.xml": b"<worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'><sheetData/></worksheet>"})})) == "NOT_A_LISTING"


def test_other_file_types_inside_the_zip():
    assert _code(archive({"a.pdf": b"%PDF"})) == "UNSUPPORTED_FILE"


def test_macos_and_office_noise_is_ignored():
    files = year_of_listings(months=1)
    files["__MACOSX/Einvoice Data/._HDT1.xlsx"] = b"resource fork"
    files["Einvoice Data/.DS_Store"] = b"junk"
    files["Einvoice Data/~$HDT1.xlsx"] = b"lock"
    assert parse_einvoice_zip(archive(files)).months_covered == 1


def test_too_many_files(monkeypatch):
    monkeypatch.setattr(parser, "MAX_ENTRIES", 2)
    assert _code(archive(year_of_listings(months=3))) == "TOO_MANY_FILES"


def test_a_workbook_part_over_the_cap_is_refused_before_parsing(monkeypatch):
    from app.uploads.parsers import xlsx

    monkeypatch.setattr(xlsx, "MAX_PART_BYTES", 100)
    assert _code(archive(year_of_listings(months=1))) == "UNREADABLE_LISTING"


def _workbook_with_sheet_of(size: int) -> bytes:
    """A workbook whose one sheet is valid XML of exactly `size` bytes (blank padding inside sheetData)."""
    head = b"<worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'><sheetData>"
    tail = b"</sheetData></worksheet>"
    return archive({"xl/worksheets/sheet1.xml": head + b" " * (size - len(head) - len(tail)) + tail})


def test_a_workbook_part_over_8_mb_is_refused_and_one_of_8_mb_is_read():
    from app.uploads.parsers import xlsx

    with pytest.raises(xlsx.XlsxError, match="too large"):
        xlsx.read_first_sheet(_workbook_with_sheet_of(8 * 1024 * 1024 + 1))
    assert xlsx.read_first_sheet(_workbook_with_sheet_of(8 * 1024 * 1024)) == []


def test_a_busy_month_of_5000_invoices_fits_under_the_part_cap():
    buyer = "CÔNG TY TNHH THƯƠNG MẠI VÀ DỊCH VỤ KHÁCH HÀNG MIỀN NAM"
    invoices = [Inv(n, "0100000001", 1_000_000 + n, buyer_name=buyer) for n in range(1, 5001)]

    listing = parser.parse_listing(listing_xlsx(2026, 10, invoices), "HDT10.xlsx")

    assert len(listing.invoices) == 5000


def test_grading_fields_map_onto_engine_names():
    fields = grading_fields(parse_einvoice_zip(archive(year_of_listings())).as_dict())
    assert len(fields["monthly_revenue_latest"]) == 12
    assert set(fields) >= {"conc_top1_pct", "conc_top3_pct", "crr"}
