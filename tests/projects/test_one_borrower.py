"""One borrower across its projects (docs/specs/projects/one-borrower.md, D-016).

At most 100M outstanding per borrower and one listing per SME until its loan is repaid in full used to be checked
per project: an SME repaying 100M opened a second 50M listing under another project (Appendix M 2.6). The database
now holds one project per business (however its tax code is written) and one live listing per SME account; the
code holds the rest: a valid tax code, the business the SME verified, no new project while one is in progress.
"""
import datetime as dt
import uuid

from sqlalchemy import select, update

from app.direct_transfer.models import FundingWindow
from app.gverify.models import STATUS_APPROVED, GVerifyKybVerification
from app.lending.models import LoanApplication, Project
from conftest import auth_headers
from tests.admin.test_repayment_receipts import _repaying
from tests.market.test_funding_flow import _project
from tests.tax_codes import fake_tax_code


async def _create(client, sme, tax_id, **extra):
    return await client.post(
        "/projects",
        json={"legal_name": f"Co {uuid.uuid4().hex[:6]}", "tax_id": tax_id} | extra,
        headers=auth_headers(sme),
    )


def _code(resp) -> str | None:
    return resp.headers.get("X-Error-Code")


async def _verified(db_session, sme, tax_code):
    db_session.add(
        GVerifyKybVerification(
            user_id=uuid.UUID(sme["id"]), status=STATUS_APPROVED, document_type="COMPANY", tax_code=tax_code,
            business_name="CONG TY VERIFIED",
        )
    )
    await db_session.commit()


async def _open(client, admin, project_id):
    return await client.post(f"/admin/projects/{project_id}/funding-window", headers=admin["headers"])


# --------------------------------------------------------------------------- #
# the tax code
# --------------------------------------------------------------------------- #
async def test_a_project_needs_a_valid_tax_code_kept_in_one_form(client, db_session, make_user):
    for bad in (None, "", "TAX-123", "12345", "0123456789-01", "01234567890"):
        sme = await make_user(role="SME")
        resp = await _create(client, sme, bad)
        assert resp.status_code == 422 and _code(resp) == "INVALID_TAX_CODE", (bad, resp.text)

    company, branch = fake_tax_code(), fake_tax_code()
    spaced = await _create(client, await make_user(role="SME"), f" {company[:4]} {company[4:]} ")
    assert spaced.status_code == 201, spaced.text
    assert spaced.json()["tax_id"] == company
    dashless = await _create(client, await make_user(role="SME"), f"{branch}002")
    assert dashless.status_code == 201, dashless.text
    assert dashless.json()["tax_id"] == f"{branch}-002"
    household = await _create(client, await make_user(role="SME"), "079123456789")
    assert household.status_code == 201 and household.json()["tax_id"] == "079123456789"

    stored = (
        await db_session.execute(select(Project.tax_business).where(Project.tax_id == f"{branch}-002"))
    ).scalar_one()
    assert stored == branch  # a branch is its company


async def test_a_business_has_one_project_however_its_tax_code_is_written(client, make_user):
    code = fake_tax_code()
    assert (await _create(client, await make_user(role="SME"), code)).status_code == 201

    for variant in (code, f"{code}-001", f"{code[:5]} {code[5:]}"):
        resp = await _create(client, await make_user(role="SME"), variant)
        assert resp.status_code == 409 and _code(resp) == "DUPLICATE_TAX_CODE", (variant, resp.text)


async def test_a_project_must_be_the_business_the_sme_verified(client, db_session, make_user):
    sme = await make_user(role="SME")
    verified = fake_tax_code()
    await _verified(db_session, sme, verified)

    other = await _create(client, sme, fake_tax_code())
    assert other.status_code == 409 and _code(other) == "TAX_CODE_NOT_VERIFIED_BUSINESS"

    # Its own branch is the business it verified.
    own = await _create(client, sme, f"{verified}-003")
    assert own.status_code == 201, own.text


# --------------------------------------------------------------------------- #
# one project in progress
# --------------------------------------------------------------------------- #
async def test_an_sme_cannot_start_a_second_project_while_one_is_in_progress(client, db_session, make_user):
    sme = await make_user(role="SME")
    first = await _create(
        client, sme, fake_tax_code(),
        loan_application={"requested_amount": 80_000_000, "duration_months": 4, "repayment_preference": "MONTHLY"},
    )
    assert first.status_code == 201, first.text

    second = await _create(client, sme, fake_tax_code())
    assert second.status_code == 409 and _code(second) == "SME_HAS_OPEN_PROJECT"

    # Once the first ends (here: its application was rejected), another can start.
    await db_session.execute(
        update(LoanApplication)
        .where(LoanApplication.project_id == uuid.UUID(first.json()["id"]))
        .values(status="REJECTED", admin_approval="REJECTED")
    )
    await db_session.commit()
    third = await _create(client, sme, fake_tax_code())
    assert third.status_code == 201, third.text


async def test_an_sme_repaying_a_loan_can_neither_start_nor_list_another_project(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    # The QA case: an investment repaying on the SME's first project.
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)

    # No new project while it repays.
    blocked = await _create(client, s["sme"], fake_tax_code())
    assert blocked.status_code == 409 and _code(blocked) == "SME_HAS_OPEN_PROJECT"

    # A second project that already exists (from before the rule) cannot open a listing either.
    second = await _project(db_session, s["sme"])
    refused = await _open(client, s["admin"], second)
    assert refused.status_code == 409 and _code(refused) == "SME_HAS_LIVE_LISTING", refused.text
    # The database refused the insert; the request's session (shared with the test here) is rolled back.
    await db_session.rollback()
    assert (
        await db_session.execute(select(FundingWindow.id).where(FundingWindow.project_id == second))
    ).first() is None

    # Once the first loan is repaid in full, the second can list.
    await db_session.execute(
        update(FundingWindow).where(FundingWindow.project_id == s["project"]).values(status="REPAID")
    )
    await db_session.commit()
    opened = await _open(client, s["admin"], second)
    assert opened.status_code == 200, opened.text


async def test_a_listing_cannot_open_for_a_business_other_than_the_one_verified(
    client, db_session, make_admin, make_user
):
    admin = await make_admin()
    sme = await make_user(role="SME")
    project = await _project(db_session, sme)  # created before the SME verified its business
    await _verified(db_session, sme, fake_tax_code())

    resp = await _open(client, admin, project)

    assert resp.status_code == 409 and _code(resp) == "TAX_CODE_NOT_VERIFIED_BUSINESS", resp.text


async def test_the_database_holds_one_live_listing_per_sme(client, db_session, make_admin, make_user, make_eligible_investor):
    """Whatever path inserts it, a second live window for the same SME account is refused by the database."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    db_session.expire_all()
    window = (
        await db_session.execute(select(FundingWindow).where(FundingWindow.project_id == s["project"]))
    ).scalar_one()
    second = await _project(db_session, s["sme"])
    clone = {c.name: getattr(window, c.name) for c in FundingWindow.__table__.columns if c.name != "id"}
    db_session.add(
        FundingWindow(
            **clone | {"project_id": second, "listing_id": uuid.uuid4(), "idempotency_key": str(uuid.uuid4()),
                       "window_start": dt.datetime.now(dt.timezone.utc)}
        )
    )
    with pytest.raises(IntegrityError, match="uq_funding_windows_one_live_per_sme"):
        await db_session.flush()
    await db_session.rollback()
