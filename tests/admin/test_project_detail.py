"""GET /admin/projects/{id} — the admin project preview.

Two things are being protected here: that an admin can see every funding
request a company has made, and that nobody else can. The second matters more
— this route exposes one company's applications to whoever calls it, so the
role check is the feature, not a detail of it.
"""
import uuid


async def test_returns_the_company_and_its_applications(
    client, make_admin, make_user, make_project, make_loan_application
):
    sme = await make_user(role="SME")
    project = await make_project(sme, legal_name="CÔNG TY CỔ PHẦN FUNDLOK")
    first = await make_loan_application(sme, project["id"], requested_amount="200000000")
    second = await make_loan_application(sme, project["id"], requested_amount="500000000")
    admin = await make_admin()

    resp = await client.get(
        f"/admin/projects/{project['id']}", headers=admin["headers"]
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["legal_name"] == "CÔNG TY CỔ PHẦN FUNDLOK"
    assert {a["id"] for a in data["applications"]} == {first["id"], second["id"]}
    # Integer VND, never a float — the column is Numeric(20, 0) and whole dong
    # start disappearing above 2^53 if this is ever serialised as a float.
    amounts = {a["requested_amount"] for a in data["applications"]}
    assert amounts == {200000000, 500000000}
    assert all(isinstance(a["requested_amount"], int) for a in data["applications"])


async def test_applications_are_newest_first(
    client, make_admin, make_user, make_project, make_loan_application
):
    """An admin opening the preview is looking at the latest request."""
    sme = await make_user(role="SME")
    project = await make_project(sme)
    older = await make_loan_application(sme, project["id"], requested_amount="200000000")
    newer = await make_loan_application(sme, project["id"], requested_amount="300000000")
    admin = await make_admin()

    resp = await client.get(
        f"/admin/projects/{project['id']}", headers=admin["headers"]
    )

    ids = [a["id"] for a in resp.json()["applications"]]
    assert ids.index(newer["id"]) < ids.index(older["id"])


async def test_a_company_with_no_applications_returns_an_empty_list(
    client, make_admin, make_user, make_project
):
    """Empty, not 404 — the company exists, it simply has not asked for money
    yet, and the preview has to render that state rather than error."""
    sme = await make_user(role="SME")
    project = await make_project(sme)
    admin = await make_admin()

    resp = await client.get(
        f"/admin/projects/{project['id']}", headers=admin["headers"]
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["applications"] == []


async def test_only_that_company_s_applications_are_returned(
    client, make_admin, make_user, make_project, make_loan_application
):
    """Guards the WHERE clause: a missing project_id filter would leak every
    company's funding requests into one preview."""
    sme = await make_user(role="SME")
    mine = await make_project(sme)
    theirs = await make_project(sme)
    ours = await make_loan_application(sme, mine["id"], requested_amount="200000000")
    await make_loan_application(sme, theirs["id"], requested_amount="400000000")
    admin = await make_admin()

    resp = await client.get(f"/admin/projects/{mine['id']}", headers=admin["headers"])

    assert [a["id"] for a in resp.json()["applications"]] == [ours["id"]]


async def test_unknown_project_is_404(client, make_admin):
    admin = await make_admin()
    resp = await client.get(
        f"/admin/projects/{uuid.uuid4()}", headers=admin["headers"]
    )
    assert resp.status_code == 404


async def test_an_sme_cannot_read_the_preview(
    client, make_user, make_project
):
    """Even for their OWN company. This route is the admin console's view; an
    SME reads their applications through /loans."""
    sme = await make_user(role="SME")
    project = await make_project(sme)

    resp = await client.get(
        f"/admin/projects/{project['id']}", headers=sme["headers"]
    )

    assert resp.status_code == 403, resp.text


async def test_an_investor_cannot_read_the_preview(
    client, make_user, make_project
):
    sme = await make_user(role="SME")
    project = await make_project(sme)
    investor = await make_user(role="INVESTOR")

    resp = await client.get(
        f"/admin/projects/{project['id']}", headers=investor["headers"]
    )

    assert resp.status_code == 403, resp.text


async def test_an_anonymous_caller_cannot_read_the_preview(
    client, make_user, make_project
):
    sme = await make_user(role="SME")
    project = await make_project(sme)

    resp = await client.get(f"/admin/projects/{project['id']}")

    assert resp.status_code in (401, 403), resp.text


async def test_applications_carry_the_uploaded_documents(
    client, make_admin, make_user, make_project, make_loan_application, db_session
):
    """The wizard's uploads are what an operator reviews alongside the numbers,
    so the preview has to carry them — nested under the application they belong
    to, not the company."""
    from app.lending.models import LoanApplicationDocument

    sme = await make_user(role="SME")
    project = await make_project(sme)
    application = await make_loan_application(
        sme, project["id"], requested_amount="500000000"
    )
    db_session.add(
        LoanApplicationDocument(
            loan_application_id=uuid.UUID(application["id"]),
            document_type="business_registration",
            file_key=f"loan/{application['id']}/business_registration.pdf",
            original_filename="giay-dang-ky-kinh-doanh.pdf",
            content_type="application/pdf",
            file_size_bytes=182_000,
            status="UPLOADED",
        )
    )
    await db_session.commit()
    admin = await make_admin()

    resp = await client.get(
        f"/admin/projects/{project['id']}", headers=admin["headers"]
    )

    assert resp.status_code == 200, resp.text
    documents = resp.json()["applications"][0]["documents"]
    assert len(documents) == 1
    assert documents[0]["document_type"] == "business_registration"
    assert documents[0]["original_filename"] == "giay-dang-ky-kinh-doanh.pdf"
    assert documents[0]["status"] == "UPLOADED"


async def test_an_application_with_no_uploads_has_an_empty_document_list(
    client, make_admin, make_user, make_project, make_loan_application
):
    """An SME who has not reached the upload steps yet renders as empty, not
    as a missing key the panel would have to guard against."""
    sme = await make_user(role="SME")
    project = await make_project(sme)
    await make_loan_application(sme, project["id"], requested_amount="500000000")
    admin = await make_admin()

    resp = await client.get(
        f"/admin/projects/{project['id']}", headers=admin["headers"]
    )

    assert resp.json()["applications"][0]["documents"] == []
