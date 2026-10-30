"""Admins approve or reject the bank accounts users save; each decision is in the audit log."""
from sqlalchemy import select

from app.lending.models import AuditLog


async def _save(client, user, number="19036789012"):
    resp = await client.post(
        "/payment-accounts",
        json={"holder_type": "INDIVIDUAL", "bank_bin": "970407", "account_number": number, "account_name": "TEST"},
        headers=user["headers"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _decide(client, admin, account_id, decision, note=None):
    body = {"decision": decision} | ({"note": note} if note is not None else {})
    return await client.post(f"/admin/payment-accounts/{account_id}/decision", json=body, headers=admin["headers"])


async def test_queue_lists_accounts_waiting_for_a_decision(client, make_admin, make_user):
    admin = await make_admin()
    sme = await make_user(role="SME")
    investor = await make_user(role="INVESTOR")
    first = await _save(client, sme)
    second = await _save(client, investor)

    resp = await client.get("/admin/payment-accounts", headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert [r["id"] for r in rows] == [first["id"], second["id"]]  # oldest first
    assert {r["user_email"] for r in rows} == {sme["email"], investor["email"]}
    assert rows[0]["account_number"] == "19036789012" and rows[0]["holder_role"] == "SME"


async def test_approving_records_who_approved_it_in_the_audit_log(client, make_admin, make_user, db_session):
    admin = await make_admin()
    investor = await make_user(role="INVESTOR")
    account = await _save(client, investor)

    resp = await _decide(client, admin, account["id"], "APPROVED", note="Statement matches")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "APPROVED"
    assert body["approved_by_email"] == admin["email"]

    mine = (await client.get("/payment-accounts", headers=investor["headers"])).json()
    assert mine[0]["status"] == "APPROVED"

    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "PAYMENT_ACCOUNT_APPROVED"))
    ).scalar_one()
    assert str(audit.actor_id) == admin["id"]
    assert audit.entity_type == "PAYMENT_ACCOUNT" and str(audit.entity_id) == account["id"]
    assert audit.before_state == {"status": "PENDING"}
    assert audit.after_state["status"] == "APPROVED"
    assert audit.after_state["note"] == "Statement matches"
    assert audit.after_state["user_id"] == investor["id"]
    assert audit.after_state["account_last4"] == "9012"

    queue = (await client.get("/admin/payment-accounts", headers=admin["headers"])).json()
    assert queue == []
    approved = (await client.get("/admin/payment-accounts?status=APPROVED", headers=admin["headers"])).json()
    assert [r["id"] for r in approved] == [account["id"]]


async def test_rejecting_needs_a_reason_the_user_then_sees(client, make_admin, make_user, db_session):
    admin = await make_admin()
    sme = await make_user(role="SME")
    account = await _save(client, sme)

    missing = await _decide(client, admin, account["id"], "REJECTED", note="   ")
    assert missing.status_code == 422
    assert missing.headers["X-Error-Code"] == "REASON_REQUIRED"

    resp = await _decide(client, admin, account["id"], "REJECTED", note="Name does not match the KYC record")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "REJECTED"

    mine = (await client.get("/payment-accounts", headers=sme["headers"])).json()
    assert mine[0]["status"] == "REJECTED"
    assert mine[0]["rejection_reason"] == "Name does not match the KYC record"

    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "PAYMENT_ACCOUNT_REJECTED"))
    ).scalar_one()
    assert str(audit.actor_id) == admin["id"]
    assert audit.after_state["note"] == "Name does not match the KYC record"


async def test_a_decision_is_final(client, make_admin, make_user):
    admin = await make_admin()
    investor = await make_user(role="INVESTOR")
    account = await _save(client, investor)
    assert (await _decide(client, admin, account["id"], "APPROVED")).status_code == 200

    again = await _decide(client, admin, account["id"], "REJECTED", note="changed my mind")
    assert again.status_code == 409
    assert again.headers["X-Error-Code"] == "ALREADY_DECIDED"


async def test_only_admins_review_bank_accounts(client, make_user):
    investor = await make_user(role="INVESTOR")
    account = await _save(client, investor)

    assert (await client.get("/admin/payment-accounts", headers=investor["headers"])).status_code == 403
    resp = await client.post(
        f"/admin/payment-accounts/{account['id']}/decision", json={"decision": "APPROVED"}, headers=investor["headers"]
    )
    assert resp.status_code == 403


async def test_unknown_account_is_not_found(client, make_admin):
    admin = await make_admin()
    resp = await _decide(client, admin, "00000000-0000-0000-0000-000000000000", "APPROVED")
    assert resp.status_code == 404
