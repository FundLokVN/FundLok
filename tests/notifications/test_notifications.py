"""The bell's backend.

The point of the table is that a notification survives the moment it was
created: the applicant who was offline when their request was decided must
still find out. These tests hold that, the ownership scoping, and the one
invariant that makes the feature safe to add to `decide_application` -- the
notification lives or dies with the decision.
"""
import uuid

import pytest
from sqlalchemy import select

from app.notifications.models import Notification


@pytest.fixture
def make_submitted_application(
    make_user, make_project, make_loan_application, submit_loan_application, attach_required_documents
):
    async def _make():
        sme = await make_user(role="SME")
        project = await make_project(sme)
        application = await make_loan_application(
            sme, project["id"], requested_amount="50000000"
        )
        resp = await submit_loan_application(sme, application["id"])
        assert resp.status_code == 200, resp.text
        await attach_required_documents(application["id"])
        return sme, project, application

    return _make


async def test_a_decision_notifies_the_applicant(
    client, make_admin, make_submitted_application, db_session
):
    sme, _, application = await make_submitted_application()
    admin = await make_admin()

    resp = await client.post(
        f"/admin/applications/{application['id']}/decision",
        json={"decision": "REJECTED", "note": "Concentration too high"},
        headers=admin["headers"],
    )
    assert resp.status_code == 200, resp.text

    row = (
        await db_session.execute(
            select(Notification).where(Notification.user_id == uuid.UUID(sme["id"]))
        )
    ).scalar_one()
    assert row.event == "APPLICATION_REJECTED"
    assert row.entity_id == uuid.UUID(application["id"])
    assert row.read_at is None
    # The copy is rendered client-side from the event code, so the row carries
    # placeholders and no sentence -- a stored sentence would be frozen in one
    # language.
    assert row.data == {"amount_vnd": 50000000}


async def test_the_applicant_reads_their_own_notifications(
    client, make_admin, make_submitted_application
):
    sme, _, _application = await make_submitted_application()
    admin = await make_admin()
    await client.post(
        f"/admin/applications/{_application['id']}/decision",
        json={"decision": "APPROVED"},
        headers=admin["headers"],
    )

    resp = await client.get("/notifications", headers=sme["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["unread"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["event"] == "APPLICATION_APPROVED"
    assert body["items"][0]["read_at"] is None


async def test_notifications_are_scoped_to_their_owner(
    client, make_user, make_admin, make_submitted_application
):
    """Another SME must not see, or be able to read, someone else's."""
    sme, _, application = await make_submitted_application()
    admin = await make_admin()
    await client.post(
        f"/admin/applications/{application['id']}/decision",
        json={"decision": "REJECTED"},
        headers=admin["headers"],
    )

    stranger = await make_user(role="SME")
    resp = await client.get("/notifications", headers=stranger["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": [], "unread": 0}

    mine = await client.get("/notifications", headers=sme["headers"])
    target = mine.json()["items"][0]["id"]

    # 404, not 403: someone else's notification must not be distinguishable
    # from one that does not exist.
    blocked = await client.patch(
        f"/notifications/{target}/read", headers=stranger["headers"]
    )
    assert blocked.status_code == 404


async def test_marking_read_is_idempotent(
    client, make_admin, make_submitted_application
):
    sme, _, application = await make_submitted_application()
    admin = await make_admin()
    await client.post(
        f"/admin/applications/{application['id']}/decision",
        json={"decision": "REJECTED"},
        headers=admin["headers"],
    )

    listed = await client.get("/notifications", headers=sme["headers"])
    target = listed.json()["items"][0]["id"]

    first = await client.patch(f"/notifications/{target}/read", headers=sme["headers"])
    assert first.status_code == 200, first.text
    read_at = first.json()["read_at"]
    assert read_at is not None

    # Reading it again keeps the FIRST timestamp: when they saw it is the
    # question worth answering, not when they clicked again.
    second = await client.patch(f"/notifications/{target}/read", headers=sme["headers"])
    assert second.json()["read_at"] == read_at

    after = await client.get("/notifications", headers=sme["headers"])
    assert after.json()["unread"] == 0


async def test_mark_all_read_clears_the_badge(
    client, make_admin, make_submitted_application
):
    sme, project, application = await make_submitted_application()
    admin = await make_admin()
    await client.post(
        f"/admin/applications/{application['id']}/decision",
        json={"decision": "REJECTED"},
        headers=admin["headers"],
    )

    resp = await client.post("/notifications/read-all", headers=sme["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 1

    after = await client.get("/notifications", headers=sme["headers"])
    assert after.json()["unread"] == 0
    assert project["id"]


async def test_an_anonymous_caller_gets_nothing(client):
    resp = await client.get("/notifications")
    assert resp.status_code == 401


async def test_a_decision_emails_the_applicant_with_a_link(
    client, make_admin, make_submitted_application, monkeypatch
):
    sent = []
    monkeypatch.setattr(
        "app.admin.service.send_application_updated_email",
        lambda address: sent.append(address),
    )
    sme, _, application = await make_submitted_application()
    admin = await make_admin()

    resp = await client.post(
        f"/admin/applications/{application['id']}/decision",
        json={"decision": "APPROVED", "note": None},
        headers=admin["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert sent == [sme["email"]]


def test_the_update_email_has_a_button_and_does_not_state_the_outcome(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "app.utils.email.send_email", lambda **kw: captured.update(kw)
    )
    from app.core.config import settings
    from app.utils.email import send_application_updated_email

    send_application_updated_email("sme@example.com")

    assert f"{settings.FRONTEND_URL}/dashboard" in captured["html_content"]
    lowered = captured["html_content"].lower()
    for word in ("approved", "rejected", "được duyệt", "từ chối", "guarantee"):
        assert word not in lowered
