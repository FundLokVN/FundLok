"""POST /projects with an inline loan_application — the shape the SME
application form actually submits.

Regression: the endpoint returns ProjectWithApplicationOut, whose
loan_application carries a `documents` list. On the create path the freshly
inserted LoanApplication had that relationship unloaded, so Pydantic's
model_validate() read triggered a lazy load inside an AsyncSession and raised
MissingGreenlet -> 500 on every create-with-loan request.

The existing `make_project` fixture posts without `loan_application`, which
leaves `loan_app is None` and skips the validation entirely — which is why the
suite stayed green while the real form 500'd. These tests exercise the inline
path directly.
"""
import uuid

from conftest import auth_headers
from tests.tax_codes import fake_tax_code


def _payload(**overrides):
    payload = {
        "legal_name": f"Inline Co {uuid.uuid4().hex[:8]}",
        "tax_id": fake_tax_code(),
        "industry": "IT Services",
        "incorporation_date": "2023-05-10",
        "address": {
            "street": "12 Nguyen Hue",
            "city": "Ho Chi Minh",
            "country": "Vietnam",
        },
        "loan_application": {
            "requested_amount": 80000000,
            "purpose": "Working capital to fulfil new orders",
            "repayment_preference": "MONTHLY",
        },
    }
    payload.update(overrides)
    return payload


async def test_create_project_with_inline_loan_application_returns_201(
    client, make_user
):
    sme = await make_user(role="SME")

    resp = await client.post("/projects", json=_payload(), headers=auth_headers(sme))

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["loan_application"] is not None
    assert body["loan_application"]["status"] == "DRAFT"
    assert body["loan_application"]["project_id"] == body["id"]


async def test_inline_loan_application_documents_serialize_as_empty_list(
    client, make_user
):
    """The `documents` relationship must be loaded, not lazily touched. A new
    application has no documents, so an empty list is the correct answer — the
    bug was that reading it at all raised."""
    sme = await make_user(role="SME")

    resp = await client.post("/projects", json=_payload(), headers=auth_headers(sme))

    assert resp.status_code == 201, resp.text
    assert resp.json()["loan_application"]["documents"] == []


async def test_created_inline_application_is_visible_on_list(client, make_user):
    """The create response and the list endpoint must agree — the list path
    already eager-loaded documents, the create path did not."""
    sme = await make_user(role="SME")
    created = await client.post("/projects", json=_payload(), headers=auth_headers(sme))
    assert created.status_code == 201, created.text
    created_app = created.json()["loan_application"]

    listed = await client.get("/projects", headers=auth_headers(sme))
    assert listed.status_code == 200, listed.text
    projects = listed.json()
    assert len(projects) == 1
    listed_app = projects[0]["loan_application"]

    assert listed_app["id"] == created_app["id"]
    assert listed_app["requested_amount"] == created_app["requested_amount"]
    assert listed_app["documents"] == created_app["documents"]


async def test_create_project_without_loan_application_still_works(
    client, make_user
):
    """The inline block is optional; omitting it must not start returning one."""
    sme = await make_user(role="SME")
    payload = _payload()
    payload.pop("loan_application")

    resp = await client.post("/projects", json=payload, headers=auth_headers(sme))

    assert resp.status_code == 201, resp.text
    assert resp.json()["loan_application"] is None


async def test_public_listing_carries_the_loan_terms(client, make_user, db_session):
    """FE-008: the marketplace card shows the asking amount, so the public
    listing has to carry the application — otherwise an investor must open every
    project to find out what the deal is."""
    from sqlalchemy import select

    from app.lending.models import Project

    sme = await make_user(role="SME")
    created = await client.post("/projects", json=_payload(), headers=auth_headers(sme))
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]

    # Only ACTIVE projects are listed; creation leaves them DRAFT.
    row = (
        await db_session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one()
    row.status = "ACTIVE"
    await db_session.commit()

    investor = await make_user(role="INVESTOR")
    listed = await client.get("/projects/public", headers=auth_headers(investor))
    assert listed.status_code == 200, listed.text

    match = next(p for p in listed.json() if p["id"] == project_id)
    assert match["loan_application"] is not None
    # VND is integer everywhere (T1, HANDOFF-03) -- a plain JSON number, never
    # a decimal-formatted string. This was previously asserted as
    # "800000000.00", a stale pre-T1 assumption; it also masked a real bug,
    # since a Decimal-typed field here could round-trip through Postgres
    # NUMERIC and come back as e.g. Decimal('8E+8') for a round amount like
    # this one, which `int` sidesteps entirely (see app/projects/schemas.py).
    assert match["loan_application"]["requested_amount"] == 80000000


async def test_public_listing_hides_the_applicant_only_fields(client, make_user, db_session):
    """vbsec 2026-10-16: the listing reused the applicant's own schema, so every
    investor read each SME's operator decision_note and its document metadata
    (storage key, original filename)."""
    from sqlalchemy import select

    from app.lending.models import LoanApplication, Project

    sme = await make_user(role="SME")
    created = await client.post("/projects", json=_payload(), headers=auth_headers(sme))
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]
    project = (
        await db_session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one()
    project.status = "ACTIVE"
    application = (
        await db_session.execute(
            select(LoanApplication).where(LoanApplication.project_id == project_id)
        )
    ).scalar_one()
    application.decision_note = "Internal: director has a prior default"
    await db_session.commit()

    investor = await make_user(role="INVESTOR")
    listed = await client.get("/projects/public", headers=auth_headers(investor))
    assert listed.status_code == 200, listed.text
    match = next(p for p in listed.json() if p["id"] == project_id)

    assert set(match["loan_application"]) == {
        "id",
        "project_id",
        "requested_amount",
        "duration_months",
        "purpose",
        "repayment_preference",
        "status",
        "submitted_at",
        "created_at",
        "verified_documents",
        "business_score",
        "listing",
        "bank_rate_pct",
        # The offer's DRR and s (one figure, the locked run's) and the Personal
        # Guarantor's name only (SCRUM-637).
        "daily_repayment_rate",
        "revenue_share",
        "guarantor_name",
    }
    assert "prior default" not in listed.text
    assert match["tax_id"] == created.json()["tax_id"]

    # The applicant still sees their own note.
    own = await client.get("/projects", headers=auth_headers(sme))
    assert own.json()[0]["loan_application"]["decision_note"] == application.decision_note


async def test_marketplace_redacts_household_identifiers_and_address(client, make_user, db_session):
    from app.lending.models import Project, ProjectOwnership

    sme = await make_user(role="SME")
    tax_id = "079123456789"
    address = {"street": "12 Test Street", "city": "Ho Chi Minh City", "country": "Vietnam"}
    project = Project(legal_name="Test Household", tax_id=tax_id, address=address, status="ACTIVE")
    db_session.add(project)
    await db_session.flush()
    project_id = str(project.id)
    db_session.add(ProjectOwnership(project_id=project.id, user_id=uuid.UUID(sme["id"]), role="OWNER"))
    await db_session.commit()

    investor = await make_user(role="INVESTOR")
    listed = await client.get("/projects/public", headers=auth_headers(investor))
    assert listed.status_code == 200, listed.text
    listing = next(row for row in listed.json() if row["id"] == project_id)
    assert listing["tax_id"] is None
    assert listing["address"] is None

    own = await client.get("/projects", headers=auth_headers(sme))
    assert own.json()[0]["tax_id"] == tax_id
    assert own.json()[0]["address"]["street"] == address["street"]


async def test_public_listing_without_an_application_returns_null(
    client, make_user, db_session
):
    from sqlalchemy import select

    from app.lending.models import Project

    sme = await make_user(role="SME")
    payload = _payload()
    payload.pop("loan_application")
    created = await client.post("/projects", json=payload, headers=auth_headers(sme))
    project_id = created.json()["id"]

    row = (
        await db_session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one()
    row.status = "ACTIVE"
    await db_session.commit()

    investor = await make_user(role="INVESTOR")
    listed = await client.get("/projects/public", headers=auth_headers(investor))
    match = next(p for p in listed.json() if p["id"] == project_id)
    assert match["loan_application"] is None


async def test_public_listing_names_the_reviewed_document_types_only(client, make_user, db_session):
    """The investor's due-diligence tab lists what the FundLok team reviewed.
    It gets the document TYPES of an approved request: never a file, a
    filename or a storage key."""
    from sqlalchemy import select

    from app.admin.service import REQUIRED_DOCUMENT_TYPES
    from app.lending.models import LoanApplication, LoanApplicationDocument, Project

    sme = await make_user(role="SME")
    created = await client.post("/projects", json=_payload(), headers=auth_headers(sme))
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]
    project = (await db_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    project.status = "ACTIVE"
    application = (
        await db_session.execute(select(LoanApplication).where(LoanApplication.project_id == project_id))
    ).scalar_one()
    for document_type, status in (("legal_charter", "UPLOADED"), ("financial_report", "UPLOADED"), ("vat_tax_zip", "PENDING")):
        db_session.add(
            LoanApplicationDocument(
                loan_application_id=application.id,
                document_type=document_type,
                file_key=f"application_documents/{document_type}-secret-key.pdf",
                original_filename=f"{document_type}-secret-name.pdf",
                content_type="application/pdf",
                file_size_bytes=1024,
                status=status,
            )
        )
    await db_session.commit()

    investor = await make_user(role="INVESTOR")

    async def listed_documents():
        listed = await client.get("/projects/public", headers=auth_headers(investor))
        assert listed.status_code == 200, listed.text
        assert "secret" not in listed.text
        match = next(p for p in listed.json() if p["id"] == project_id)
        return match["loan_application"]["verified_documents"]

    # Not approved yet: there is nothing the team has vouched for.
    assert await listed_documents() == []

    application.admin_approval = "APPROVED"
    await db_session.commit()
    # Every required type (approval is refused without them), then the extra
    # upload; a PENDING row never landed, so it is not a document.
    assert await listed_documents() == [*REQUIRED_DOCUMENT_TYPES, "financial_report"]


async def test_public_listing_carries_the_locked_score_runs_business_score(client, make_user, db_session):
    """The card and the project page show the score the request was approved
    on, not a sample: the LOCKED run's, 2dp, as in the SME's approval summary.
    An unlocked (READY) run is not shown."""
    from sqlalchemy import select

    from app.lending.models import LoanApplication, Project, ScoreRun

    sme = await make_user(role="SME")
    created = await client.post("/projects", json=_payload(), headers=auth_headers(sme))
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]
    project = (await db_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    project.status = "ACTIVE"
    application = (
        await db_session.execute(select(LoanApplication).where(LoanApplication.project_id == project_id))
    ).scalar_one()
    run = ScoreRun(application_id=application.id, status="READY", final_grade_precise=79.24661)
    db_session.add(run)
    await db_session.commit()

    investor = await make_user(role="INVESTOR")

    async def listed_score():
        listed = await client.get("/projects/public", headers=auth_headers(investor))
        assert listed.status_code == 200, listed.text
        match = next(p for p in listed.json() if p["id"] == project_id)
        return match["loan_application"]["business_score"]

    assert await listed_score() is None

    run.status = "LOCKED"
    await db_session.commit()
    assert await listed_score() == 79.25


async def test_public_listing_carries_the_rounds_real_funding_figures(client, make_user, db_session):
    """The card's funding block reads the listing: target, funded, minimum
    ticket and how many investors have a FILLED order. A DRAFT listing is not
    open, so a request with only that shows no round."""
    from sqlalchemy import select

    from app.lending.models import Contract, LoanApplication, Listing, Order, Project, ScoreRun

    sme = await make_user(role="SME")
    created = await client.post("/projects", json=_payload(), headers=auth_headers(sme))
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]
    project = (await db_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    project.status = "ACTIVE"
    application = (
        await db_session.execute(select(LoanApplication).where(LoanApplication.project_id == project_id))
    ).scalar_one()
    run = ScoreRun(application_id=application.id, status="LOCKED")
    db_session.add(run)
    await db_session.flush()
    contract = Contract(application_id=application.id, score_run_id=run.id, status="ACTIVE_PENDING_FUNDING", final_terms={},
                        target_amount=80_000_000, funded_amount=0)
    db_session.add(contract)
    await db_session.flush()
    listing = Listing(contract_id=contract.id, target_amount=80_000_000, min_ticket=1_000_000,
                      status="DRAFT", funded_amount=20_000_000)
    db_session.add(listing)
    await db_session.commit()

    investor = await make_user(role="INVESTOR")
    other = await make_user(role="INVESTOR")

    async def listed_round():
        listed = await client.get("/projects/public", headers=auth_headers(investor))
        assert listed.status_code == 200, listed.text
        match = next(p for p in listed.json() if p["id"] == project_id)
        return match["loan_application"]["listing"]

    assert await listed_round() is None

    listing.status = "OPEN"
    for who, status in ((investor, "FILLED"), (investor, "FILLED"), (other, "FILLED"), (other, "CANCELLED")):
        db_session.add(Order(listing_id=listing.id, investor_id=who["id"], amount=5_000_000, status=status))
    await db_session.commit()

    round_ = await listed_round()
    assert {k: round_[k] for k in ("status", "target_amount", "funded_amount", "min_ticket", "investor_count")} == {
        "status": "OPEN",
        "target_amount": 80_000_000,
        "funded_amount": 20_000_000,
        "min_ticket": 1_000_000,
        "investor_count": 2,
    }


async def test_public_listing_carries_the_bank_rate_in_force(client, make_user, db_session):
    """The card's ROI is FundLok's bank rate in force (bank_rate_config), the
    same on every listing: the seeded 12.0% here."""
    from sqlalchemy import select

    from app.lending.models import Project

    sme = await make_user(role="SME")
    created = await client.post("/projects", json=_payload(), headers=auth_headers(sme))
    project_id = created.json()["id"]
    project = (await db_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    project.status = "ACTIVE"
    await db_session.commit()

    investor = await make_user(role="INVESTOR")
    listed = await client.get("/projects/public", headers=auth_headers(investor))
    match = next(p for p in listed.json() if p["id"] == project_id)
    assert match["loan_application"]["bank_rate_pct"] == 12.0
