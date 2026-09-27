"""E-invoice zip: parsed at confirm, stored apart from the file, and surfaced
to admins as grading-engine inputs.

Object storage is stubbed; `get_bytes` returns the synthetic archive.
"""
import pytest
from sqlalchemy import select

from app.uploads.models import EInvoiceSummary
from conftest import auth_headers
from tests.uploads.einvoice_fixtures import SELLER, Inv, archive, listing_xlsx, year_of_listings


@pytest.fixture
def storage(monkeypatch):
    state = {"object": archive(year_of_listings())}
    monkeypatch.setattr("app.uploads.service.presign_put", lambda key, ct: f"https://r2.test/{key}")
    monkeypatch.setattr(
        "app.uploads.service.head_object",
        lambda key, **kw: {"size": len(state["object"]), "content_type": "application/zip"},
    )
    monkeypatch.setattr("app.uploads.service.get_bytes", lambda key, **kw: state["object"])
    monkeypatch.setattr("app.uploads.service.delete_object", lambda key: None)
    return state


@pytest.fixture
async def draft(make_user, make_project, make_loan_application):
    sme = await make_user(role="SME")
    project = await make_project(sme, tax_id=SELLER)
    application = await make_loan_application(sme, project["id"])
    return sme, project, application["id"]


async def _upload_and_confirm(client, sme, app_id, filename="Einvoice Data.zip"):
    init = await client.post(
        "/uploads/init-upload",
        json={
            "loan_application_id": app_id,
            "document_type": "e_invoice_data",
            "filename": filename,
            "content_type": "application/zip",
            "size": 1024,
        },
        headers=auth_headers(sme),
    )
    assert init.status_code == 201, init.text
    return await client.post(
        "/uploads/confirm",
        json={"loan_application_id": app_id, "file_keys": [init.json()["file_key"]]},
        headers=auth_headers(sme),
    )


async def test_confirming_the_zip_stores_what_it_says(client, db_session, draft, storage):
    sme, _, app_id = draft
    resp = await _upload_and_confirm(client, sme, app_id)
    assert resp.status_code == 200, resp.text

    summary = (await db_session.execute(select(EInvoiceSummary))).scalar_one()
    assert str(summary.loan_application_id) == app_id
    assert summary.months_covered == 12
    assert summary.period_start == "09/2025" and summary.period_end == "08/2026"
    assert summary.seller_tax_code == SELLER
    assert summary.parser_version.startswith("einvoice-listing")
    assert summary.features["signed"] is False
    assert len(summary.features["monthly_revenue"]) == 12


async def test_another_companys_listings_are_accepted_but_recorded(client, db_session, draft, storage):
    """Not refused (product decision, 2026-09-27), but never silent: the
    mismatch is on the stored summary for underwriting to see."""
    sme, _, app_id = draft
    storage["object"] = archive({"a.xlsx": listing_xlsx(2026, 1, [Inv(1, "0100000001", 1, seller="0999999999")])})
    resp = await _upload_and_confirm(client, sme, app_id)
    assert resp.status_code == 200, resp.text
    summary = (await db_session.execute(select(EInvoiceSummary))).scalar_one()
    assert summary.seller_tax_code == "0999999999"
    assert "SELLER_MISMATCH" in [w["code"] for w in summary.warnings]


async def test_a_broken_archive_is_refused_with_a_translatable_code(client, draft, storage):
    sme, _, app_id = draft
    storage["object"] = b"not a zip at all"
    resp = await _upload_and_confirm(client, sme, app_id)
    assert resp.status_code == 422
    assert resp.headers["x-error-code"] == "EINVOICE_NOT_A_ZIP"


async def test_a_refused_upload_can_be_fixed_and_confirmed(client, db_session, draft, storage):
    sme, _, app_id = draft
    storage["object"] = b"broken"
    assert (await _upload_and_confirm(client, sme, app_id)).status_code == 422
    storage["object"] = archive(year_of_listings())
    assert (await _upload_and_confirm(client, sme, app_id)).status_code == 200
    assert (await db_session.execute(select(EInvoiceSummary))).scalar_one().months_covered == 12


async def test_a_reupload_replaces_the_summary(client, db_session, draft, storage):
    sme, _, app_id = draft
    await _upload_and_confirm(client, sme, app_id)
    storage["object"] = archive(year_of_listings(months=6))
    await _upload_and_confirm(client, sme, app_id, filename="again.zip")
    rows = (await db_session.execute(select(EInvoiceSummary))).scalars().all()
    assert len(rows) == 1 and rows[0].months_covered == 6


@pytest.mark.parametrize(
    "filename,content_type",
    [
        ("HDT1.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("HDT1.csv", "text/csv"),
        ("hoa-don.xml", "application/xml"),
    ],
)
async def test_only_a_zip_is_accepted(client, draft, storage, filename, content_type):
    sme, _, app_id = draft
    resp = await client.post(
        "/uploads/init-upload",
        json={
            "loan_application_id": app_id,
            "document_type": "e_invoice_data",
            "filename": filename,
            "content_type": content_type,
            "size": 1024,
        },
        headers=auth_headers(sme),
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Admin: grading inputs
# --------------------------------------------------------------------------- #


async def test_grading_inputs_show_what_the_evidence_filled(client, make_admin, draft, storage):
    sme, _, app_id = draft
    await _upload_and_confirm(client, sme, app_id)
    admin = await make_admin()

    resp = await client.get(f"/admin/applications/{app_id}/grading-inputs", headers=auth_headers(admin))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    fields = body["fields"]

    assert fields["monthly_revenue"]["source"] == "e-invoice"
    assert fields["monthly_revenue"]["status"] == "partial"  # 12 of the 24 months
    assert len(fields["monthly_revenue"]["value"]["m13_m24"]) == 12
    assert fields["conc_top1_pct"]["source"] == "e-invoice"
    assert fields["crr"]["source"] == "e-invoice"
    assert fields["loan_size_vnd"]["status"] == "filled"
    assert body["e_invoice"]["signed"] is False

    # What still blocks a grade, stated rather than guessed around.
    assert body["gradeable"] is False
    for blocker in ("company_size", "duration_months", "monthly_revenue", "cic_score"):
        assert blocker in body["blockers"]


async def test_grading_inputs_without_evidence(client, make_admin, draft, storage):
    _, _, app_id = draft
    admin = await make_admin()
    body = (await client.get(f"/admin/applications/{app_id}/grading-inputs", headers=auth_headers(admin))).json()
    assert body["e_invoice"] is None
    assert body["fields"]["monthly_revenue"]["status"] == "missing"


async def test_grading_inputs_are_admin_only(client, draft, storage):
    sme, _, app_id = draft
    resp = await client.get(f"/admin/applications/{app_id}/grading-inputs", headers=auth_headers(sme))
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Step-2 preview: read the zip as soon as it is picked, store nothing
# --------------------------------------------------------------------------- #


async def _preview(client, sme, app_id, raw):
    return await client.post(
        f"/uploads/einvoice-preview?loan_application_id={app_id}",
        content=raw,
        headers={**auth_headers(sme), "Content-Type": "application/zip"},
    )


async def test_preview_prefills_the_revenue_figures(client, db_session, draft, storage):
    sme, _, app_id = draft
    resp = await _preview(client, sme, app_id, archive(year_of_listings()))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    amounts = [m["revenue_vnd"] for m in body["monthly_revenue"]]
    assert body["months_covered"] == 12
    assert body["revenue_last_12m"] == sum(amounts)
    assert body["revenue_best_month"] == max(amounts)
    assert body["revenue_worst_month"] == min(amounts)
    # A preview writes nothing: the file has not even been sent yet.
    assert (await db_session.execute(select(EInvoiceSummary))).scalar_one_or_none() is None


async def test_preview_of_a_partial_year_does_not_invent_a_12_month_total(client, draft, storage):
    sme, _, app_id = draft
    body = (await _preview(client, sme, app_id, archive(year_of_listings(months=5)))).json()
    assert body["revenue_last_12m"] is None
    assert body["revenue_best_month"] is not None


async def test_preview_accepts_another_companys_invoices_with_a_warning(client, draft, storage):
    sme, _, app_id = draft
    raw = archive({"a.xlsx": listing_xlsx(2026, 1, [Inv(1, "0100000001", 1, seller="0999999999")])})
    resp = await _preview(client, sme, app_id, raw)
    assert resp.status_code == 200, resp.text
    assert "SELLER_MISMATCH" in [w["code"] for w in resp.json()["warnings"]]


async def test_preview_rejects_an_oversized_body(client, draft, storage, monkeypatch):
    from app.uploads import schemas

    monkeypatch.setitem(schemas.DOCUMENT_TYPE_RULES["e_invoice_data"], "max_size", 100)
    sme, _, app_id = draft
    resp = await _preview(client, sme, app_id, archive(year_of_listings()))
    assert resp.status_code == 413


async def test_preview_is_only_for_the_owning_sme(client, make_user, draft, storage):
    _, _, app_id = draft
    other = await make_user(role="SME")
    resp = await _preview(client, other, app_id, archive(year_of_listings()))
    assert resp.status_code in (403, 404)


async def test_confirm_flags_typed_revenue_that_differs_from_the_invoices(client, db_session, draft, storage):
    sme, _, app_id = draft
    figures = {
        "revenue_last_12m": 999, "revenue_prior_12m": 1_000_000,
        "cogs_y1": 1, "fixed_cost_y1": 1, "variable_cost_excl_cogs_y1": 1,
    }
    saved = await client.put(f"/loans/applications/{app_id}/figures", json=figures, headers=auth_headers(sme))
    assert saved.status_code in (200, 204), saved.text
    await _upload_and_confirm(client, sme, app_id)
    summary = (await db_session.execute(select(EInvoiceSummary))).scalar_one()
    assert "FIGURES_DIFFER" in [w["code"] for w in summary.warnings]
