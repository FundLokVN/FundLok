"""SEC-05: only the owner (or an admin) may commit a generic document.

PROJECT documents belong to the SMEs who own the project. A document of any
other entity type has no owner rule, so nobody may commit it; before SEC-05
every SME and admin account could.
"""
import uuid

from app.lending.models import Document

COMMIT_BODY = {"checksum": "0" * 64, "size": 1024}


async def _presign(client, sme, project_id) -> str:
    resp = await client.post(
        "/files/presign",
        json={
            "business_id": project_id,
            "purpose": "BANK_STATEMENT",
            "filename": "statement.pdf",
            "mime_type": "application/pdf",
        },
        headers=sme["headers"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["file_id"]


async def _commit(client, user, file_id):
    return await client.post(f"/files/{file_id}/commit", json=COMMIT_BODY, headers=user["headers"])


async def test_the_owning_sme_can_commit(client, make_user, make_project):
    sme = await make_user(role="SME")
    project = await make_project(sme)
    file_id = await _presign(client, sme, project["id"])

    resp = await _commit(client, sme, file_id)

    assert resp.status_code == 200, resp.text


async def test_another_sme_cannot_commit(client, make_user, make_project):
    owner = await make_user(role="SME")
    project = await make_project(owner)
    file_id = await _presign(client, owner, project["id"])
    stranger = await make_user(role="SME")

    resp = await _commit(client, stranger, file_id)

    assert resp.status_code == 403, resp.text


async def test_an_admin_can_commit(client, make_user, make_admin, make_project):
    sme = await make_user(role="SME")
    project = await make_project(sme)
    file_id = await _presign(client, sme, project["id"])
    admin = await make_admin()

    resp = await _commit(client, admin, file_id)

    assert resp.status_code == 200, resp.text


async def test_an_investor_cannot_commit(client, make_user, make_project):
    sme = await make_user(role="SME")
    project = await make_project(sme)
    file_id = await _presign(client, sme, project["id"])
    investor = await make_user(role="INVESTOR")

    resp = await _commit(client, investor, file_id)

    assert resp.status_code == 403, resp.text


async def test_a_document_of_another_entity_type_is_refused_to_everyone(
    client, db_session, make_user, make_admin
):
    """The app only creates PROJECT documents; this row stands in for one
    written some other way (a seed, a script, a future feature)."""
    doc = Document(
        entity_type="USER",
        entity_id=uuid.uuid4(),
        purpose="OTHER",
        filename="anything.pdf",
        mime_type="application/pdf",
        status="PENDING",
        storage_key=f"fundlok/elsewhere/{uuid.uuid4()}/anything.pdf",
    )
    db_session.add(doc)
    await db_session.commit()
    sme = await make_user(role="SME")
    admin = await make_admin()

    for user in (sme, admin):
        resp = await _commit(client, user, doc.id)
        assert resp.status_code == 403, resp.text

    await db_session.refresh(doc)
    assert doc.status == "PENDING"
