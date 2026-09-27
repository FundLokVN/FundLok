"""Step 3: tax filings — the year-end statements (cost figures) and the monthly
VAT declarations (the signed 24-month revenue series).

Checked against the reference filings during development: FY2025 TT133
package, COGS 64,130,972,146, administration 3,160,138,988, selling 0,
profit paid to owners 0; 24 consecutive signed VAT months.
"""
import io
import zipfile

import pytest
from sqlalchemy import select

from app.uploads.models import TaxFilingSummary
from app.uploads.parsers.statements import StatementParseError, parse_statements_xml
from app.uploads.parsers.tax_filings import TaxFilingsParseError, parse_tax_filings
from conftest import auth_headers
from tests.uploads.test_vat_parser import _xml as vat_xml
from tests.uploads.test_vat_parser import _zip as vat_zip

TAX = "0312345678"


def statements_xml(*, regime="TT133", year="2025", mst=TAX, cogs=6_400_000_000, net=100_000_000,
                   combined=300_000_000, selling=50_000_000, admin=250_000_000, paid_to_owners=-20_000_000,
                   trial_balance=True, signed=True) -> str:
    income = [f"<ct10>7000000000</ct10>", f"<ct11>{cogs}</ct11>", "<ct22>40000000</ct22>",
              "<ct23>35000000</ct23>", f"<ct60>{net}</ct60>"]
    if regime == "TT133":
        income.append(f"<ct24>{combined}</ct24>")
    else:
        income += [f"<ct25>{selling}</ct25>", f"<ct26>{admin}</ct26>"]
    # Profit paid to owners: line 35 on the TT133 cash flow, 36 on TT200's.
    owners_line = "ct35" if regime == "TT133" else "ct36"
    cash = f"<{owners_line}>{paid_to_owners}</{owners_line}>" if paid_to_owners is not None else ""
    tb = (f"<PL_CDTK><SoPhatSinhTrongKy><No><ct6421>{selling}</ct6421><ct6422>{admin}</ct6422></No>"
          f"</SoPhatSinhTrongKy></PL_CDTK>") if trial_balance else ""
    return (
        '<HSoThueDTu xmlns="http://kekhaithue.gdt.gov.vn/TKhaiThue"><HSoKhaiThue><TTinChung><TTinTKhaiThue>'
        f"<TKhaiThue><maTKhai>683</maTKhai><kyKKhai>{year}</kyKKhai></TKhaiThue>"
        f"<NNT><mst>{mst}</mst><tenNNT>CÔNG TY MẪU</tenNNT></NNT></TTinTKhaiThue></TTinChung>"
        "<PLuc><PL_KQHDSXKD><ThuyetMinh><ct01/></ThuyetMinh>"
        f"<NamNay>{''.join(income)}</NamNay><NamTruoc><ct10>1</ct10><ct11>1</ct11></NamTruoc></PL_KQHDSXKD>"
        f"<PL_LCTTTT><NamNay>{cash}</NamNay></PL_LCTTTT>{tb}</PLuc>"
        f"</HSoKhaiThue>{'<Signature>stub</Signature>' if signed else ''}</HSoThueDTu>"
    )


def folder_zip(files: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def vat_months(n=24, start=(2024, 8), mst=TAX) -> dict[str, bytes]:
    out, (y, m) = {}, start
    for i in range(n):
        revenue = 1_000_000_000 + i
        out[f"24 months running VAT/{m}.{y}.zip"] = vat_zip(
            vat_xml(period=f"{m:02d}/{y}", mst=mst, ct29=0, ct32=revenue, ct34=revenue)
        )
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# --- statements --------------------------------------------------------------


def test_tt133_splits_line_24_by_the_trial_balance():
    s = parse_statements_xml(statements_xml())
    assert s.regime == "TT133" and s.fiscal_year == 2025 and s.signed
    assert s.cogs_y1 == 6_400_000_000
    assert s.variable_cost_excl_cogs_y1 == 50_000_000  # 300m × 50/300
    assert s.fixed_cost_y1 == 250_000_000
    assert s.fixed_cost_y1 + s.variable_cost_excl_cogs_y1 == 300_000_000


def test_everything_booked_as_administration_gives_zero_variable_cost():
    s = parse_statements_xml(statements_xml(selling=0, admin=300_000_000))
    assert s.variable_cost_excl_cogs_y1 == 0 and s.fixed_cost_y1 == 300_000_000


def test_tt133_without_a_trial_balance_counts_line_24_as_fixed_and_says_so():
    s = parse_statements_xml(statements_xml(trial_balance=False))
    assert s.fixed_cost_y1 == 300_000_000 and s.variable_cost_excl_cogs_y1 == 0
    assert "SPLIT_ASSUMED" in [w["code"] for w in s.warnings]


def test_tt200_reads_selling_and_administration_lines():
    s = parse_statements_xml(statements_xml(regime="TT200", selling=80_000_000, admin=120_000_000))
    assert s.regime == "TT200"
    assert (s.variable_cost_excl_cogs_y1, s.fixed_cost_y1) == (80_000_000, 120_000_000)


def test_owner_withdrawal_is_profit_paid_out_over_net_profit():
    s = parse_statements_xml(statements_xml(net=100_000_000, paid_to_owners=-20_000_000))
    assert s.owner_withdrawal_pct == 20.0


def test_tt133_reads_profit_paid_to_owners_from_line_35_not_36():
    # The reference TT133 package: financing lines 31-35 sum to line 40, and
    # there is no line 36. A TT200 line number here once read every TT133
    # payout as absent, i.e. 0% withdrawn, the best possible score.
    xml = statements_xml(net=100_000_000, paid_to_owners=None).replace(
        "<PL_LCTTTT><NamNay>", "<PL_LCTTTT><NamNay><ct35>-40000000</ct35><ct36>-5000000</ct36>"
    )
    assert parse_statements_xml(xml).owner_withdrawal_pct == 40.0


def test_tt200_reads_profit_paid_to_owners_from_line_36():
    s = parse_statements_xml(statements_xml(regime="TT200", net=100_000_000, paid_to_owners=-30_000_000))
    assert s.owner_withdrawal_pct == 30.0


def test_a_missing_payout_line_is_unknown_not_zero():
    s = parse_statements_xml(statements_xml(paid_to_owners=None))
    assert s.owner_withdrawal_pct is None and s.profit_paid_to_owners_vnd is None
    assert "NO_OWNER_PAYOUT_LINE" in [w["code"] for w in s.warnings]


def test_a_loss_year_leaves_owner_withdrawal_blank():
    s = parse_statements_xml(statements_xml(net=-5_000_000))
    assert s.owner_withdrawal_pct is None
    assert "NO_PROFIT" in [w["code"] for w in s.warnings]


def test_interest_is_reported_not_counted_as_operating_cost():
    s = parse_statements_xml(statements_xml())
    assert s.interest_expense_vnd == 35_000_000
    assert "INTEREST_EXPENSE" in [w["code"] for w in s.warnings]


@pytest.mark.parametrize("xml,code", [
    ("<not xml", "UNREADABLE_STATEMENTS"),
    ("<HSoThueDTu><maTKhai>842</maTKhai></HSoThueDTu>", "NOT_STATEMENTS"),
])
def test_statement_refusals(xml, code):
    with pytest.raises(StatementParseError) as exc:
        parse_statements_xml(xml)
    assert exc.value.code == code


def test_a_quarterly_period_is_not_a_fiscal_year():
    with pytest.raises(StatementParseError):
        parse_statements_xml(statements_xml(year="Q1/2025"))


# --- the bundle ----------------------------------------------------------------


def test_the_folder_zip_gives_statements_and_24_signed_vat_months():
    r = parse_tax_filings(folder_zip({**vat_months(), "24 months running VAT/B02-DN-2025.xml": statements_xml()}))
    assert r.statements["cogs_y1"] == 6_400_000_000
    assert r.vat["months_covered"] == 24 and r.vat["all_signed"] and not r.vat["has_gaps"]
    assert r.vat["period_start"] == "08/2024" and r.vat["period_end"] == "07/2026"


def test_the_statement_xml_alone_is_accepted():
    r = parse_tax_filings(statements_xml().encode())
    assert r.statements["fiscal_year"] == 2025 and r.vat is None


def test_a_zip_without_statements_is_refused():
    with pytest.raises(TaxFilingsParseError) as exc:
        parse_tax_filings(folder_zip(vat_months(3)))
    assert exc.value.code == "NO_STATEMENTS"


def test_an_unreadable_vat_filing_costs_the_series_not_the_step():
    files = {**vat_months(2), "bad.zip": b"not a zip", "B02.xml": statements_xml()}
    r = parse_tax_filings(folder_zip(files))
    assert "VAT_UNREADABLE" in [w["code"] for w in r.warnings]
    assert r.statements["cogs_y1"] == 6_400_000_000


def test_vat_under_another_tax_code_is_noted():
    r = parse_tax_filings(folder_zip({**vat_months(2, mst="0999999999"), "B02.xml": statements_xml()}))
    assert "TAX_CODES_DIFFER" in [w["code"] for w in r.warnings]


def test_not_a_zip():
    with pytest.raises(TaxFilingsParseError) as exc:
        parse_tax_filings(b"PK\x03\x04broken")
    assert exc.value.code == "NOT_A_ZIP"


# --- endpoints -------------------------------------------------------------------


@pytest.fixture
def storage(monkeypatch):
    state = {"object": folder_zip({**vat_months(), "B02.xml": statements_xml()})}
    monkeypatch.setattr("app.uploads.service.presign_put", lambda key, ct: f"https://r2.test/{key}")
    monkeypatch.setattr("app.uploads.service.head_object", lambda key, **kw: {"size": 1024, "content_type": "application/zip"})
    monkeypatch.setattr("app.uploads.service.get_bytes", lambda key, **kw: state["object"])
    monkeypatch.setattr("app.uploads.service.delete_object", lambda key: None)
    return state


@pytest.fixture
async def draft(make_user, make_project, make_loan_application):
    sme = await make_user(role="SME")
    project = await make_project(sme, tax_id=TAX)
    application = await make_loan_application(sme, project["id"])
    return sme, application["id"]


async def test_preview_prefills_step_3(client, draft, storage):
    sme, app_id = draft
    resp = await client.post(
        f"/uploads/tax-filings-preview?loan_application_id={app_id}",
        content=storage["object"],
        headers={**auth_headers(sme), "Content-Type": "application/zip"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cogs_y1"] == 6_400_000_000
    assert body["admin_expense_vnd"] == 250_000_000 and body["selling_expense_vnd"] == 50_000_000
    assert "fixed_cost_y1" not in body  # typed by the SME; the statements only hint
    assert body["owner_withdrawal_pct"] == 20.0
    assert body["vat_months"] == 24


async def test_preview_refusal_carries_a_code(client, draft, storage):
    sme, app_id = draft
    resp = await client.post(
        f"/uploads/tax-filings-preview?loan_application_id={app_id}",
        content=folder_zip(vat_months(2)),
        headers={**auth_headers(sme), "Content-Type": "application/zip"},
    )
    assert resp.status_code == 422
    assert resp.headers["x-error-code"] == "TAXFILINGS_NO_STATEMENTS"


async def test_confirm_stores_the_filings_and_grading_inputs_use_them(client, db_session, make_admin, draft, storage):
    sme, app_id = draft
    init = await client.post(
        "/uploads/init-upload",
        json={"loan_application_id": app_id, "document_type": "tax_filings", "filename": "24 months running VAT.zip",
              "content_type": "application/zip", "size": 1024},
        headers=auth_headers(sme),
    )
    assert init.status_code == 201, init.text
    confirm = await client.post(
        "/uploads/confirm", json={"loan_application_id": app_id, "file_keys": [init.json()["file_key"]]},
        headers=auth_headers(sme),
    )
    assert confirm.status_code == 200, confirm.text
    summary = (await db_session.execute(select(TaxFilingSummary))).scalar_one()
    assert summary.fiscal_year == 2025 and summary.vat_months == 24

    admin = await make_admin()
    body = (await client.get(f"/admin/applications/{app_id}/grading-inputs", headers=auth_headers(admin))).json()
    assert body["fields"]["monthly_revenue"]["source"] == "vat"
    assert body["fields"]["monthly_revenue"]["status"] == "filled"
    assert len(body["fields"]["monthly_revenue"]["value"]["m1_m24"]) == 24
    assert body["fields"]["cogs_y1"]["source"] == "statements"
    assert body["fields"]["owner_withdrawal"]["source"] == "statements"
    # 20% withdrawn, delivered as the engine's 0-100 score (higher is better).
    from app.loans.lite_grading import owner_withdrawal_score

    assert body["fields"]["owner_withdrawal"]["value"] == round(owner_withdrawal_score(20.0), 2)
    assert "monthly_revenue" not in body["blockers"]
    assert body["tax_filings"]["vat_months"] == 24
