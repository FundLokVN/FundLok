"""Admin > Users shows each user's Terms & Services and data consent
(docs/specs/consent/sign-up-terms-consent.md): a status column in the list,
and the records themselves in the user panel."""
from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.consent import documents
from app.consent.models import UserConsent

ALL_TRUE = {doc: True for doc in documents.DOCUMENTS}


def _row(user, *, version=None, consents=None, at=None, ip=None, agent=None) -> UserConsent:
    return UserConsent(
        user_id=UUID(user["id"]),
        version=version or documents.CURRENT_VERSION,
        consents=consents if consents is not None else ALL_TRUE,
        verified_by="EMAIL_OTP",
        ip_address=ip,
        user_agent=agent,
        created_at=at or datetime.now(timezone.utc),
    )


async def _list(client, admin) -> dict:
    resp = await client.get(
        "/admin/overview", params={"mode": "users", "page_size": 100}, headers=admin["headers"]
    )
    assert resp.status_code == 200, resp.text
    return {row["email"]: row for row in resp.json()["table"]["items"]}


async def test_the_user_list_shows_who_accepted_the_terms(client, make_admin, make_user, db_session):
    admin = await make_admin()
    accepted = await make_user(role="INVESTOR")  # the fixture records consent
    never = await make_user(consent=False)
    older_only = await make_user(consent=False)
    withdrew = await make_user(role="SME")
    db_session.add(_row(older_only, version="tos-v0.2"))
    db_session.add(
        _row(
            withdrew,
            consents={**ALL_TRUE, documents.DATA_SHARING_TPV: False},
            at=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
    )
    await db_session.commit()

    rows = await _list(client, admin)
    assert rows[accepted["email"]]["consent_status"] == "ACCEPTED"
    assert rows[accepted["email"]]["consent_accepted_at"] is not None
    for who in (never, older_only, withdrew):
        assert rows[who["email"]]["consent_status"] == "REQUIRED", who["email"]
        assert rows[who["email"]]["consent_accepted_at"] is None
    # FundLok staff are never asked.
    assert rows[admin["email"]]["consent_status"] == "NOT_APPLICABLE"


async def test_the_user_panel_shows_the_consent_records(client, make_admin, make_user, db_session):
    admin = await make_admin()
    user = await make_user(consent=False)
    then = datetime.now(timezone.utc) - timedelta(days=30)
    now = datetime.now(timezone.utc)
    db_session.add(_row(user, version="tos-v0.2", at=then, ip="10.0.0.1", agent="old-browser"))
    db_session.add(_row(user, at=now, ip="10.0.0.2", agent="new-browser"))
    await db_session.commit()

    resp = await client.get(f"/admin/users/{user['id']}", headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    consent = resp.json()["consent"]
    assert consent["status"] == "ACCEPTED"
    assert consent["current_version"] == documents.CURRENT_VERSION
    assert consent["accepted_at"] is not None

    newest, oldest = consent["records"]
    assert newest["version"] == documents.CURRENT_VERSION and oldest["version"] == "tos-v0.2"
    assert newest["consents"] == {"TERMS": True, "DATA_PROCESSING": True, "DATA_SHARING_TPV": True}
    assert (newest["verified_by"], newest["ip_address"], newest["user_agent"]) == (
        "EMAIL_OTP",
        "10.0.0.2",
        "new-browser",
    )


async def test_a_user_who_has_not_accepted_shows_as_required(client, make_admin, make_user):
    admin = await make_admin()
    user = await make_user(consent=False)
    consent = (await client.get(f"/admin/users/{user['id']}", headers=admin["headers"])).json()["consent"]
    assert consent == {
        "status": "REQUIRED",
        "current_version": documents.CURRENT_VERSION,
        "accepted_at": None,
        "records": [],
    }


async def test_an_admins_panel_says_not_applicable(client, make_admin, make_system_admin):
    viewer = await make_system_admin()
    other_admin = await make_admin()
    consent = (await client.get(f"/admin/users/{other_admin['id']}", headers=viewer["headers"])).json()["consent"]
    assert consent["status"] == "NOT_APPLICABLE"
