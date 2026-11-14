"""The investor confirms a repayment: the business pays them directly and uploads its bank receipt; the investor sees
it on their dashboard, checks their own account and confirms it arrived. Confirming does what staff approval used to:
the days are matched and settled and the ledger is posted; a day the investor had reported missing has that report
closed as paid. Only the investor the receipt pays can see or confirm it.
Spec: docs/specs/direct_transfer/repayment-receipt-confirmation.md.
"""
import datetime as dt
import uuid

from sqlalchemy import select

from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.models import Dispute, EvidenceLog, RepaymentDay, Transfer
from app.ledger.models import LedgerTransaction
from app.lending.models import AuditLog
from tests.admin.test_repayment_receipts import _days, _pay, _repaying
from tests.market.test_round_lifecycle import _approved


async def _mine(client, investor, **params):
    resp = await client.get("/repayment/receipts", params=params, headers=investor["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _confirm(client, user, receipt_id):
    return await client.post(f"/repayment/receipts/{receipt_id}/confirm", headers=user["headers"])


async def _bell(client, user, event):
    items = (await client.get("/notifications", headers=user["headers"])).json()["items"]
    return [n for n in items if n["event"] == event]


async def test_the_investor_sees_the_receipt_paying_them_awaiting_their_confirmation(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    other = await make_eligible_investor(await make_user(role="INVESTOR"))
    assert await _mine(client, s["investor"]) == []

    await _pay(client, s)

    [receipt] = await _mine(client, s["investor"])
    assert receipt["status"] == "AWAITING_CONFIRMATION" and receipt["confirmed_at"] is None
    assert sorted(d["repayment_day_id"] for d in receipt["days"]) == sorted(s["day_ids"])
    assert {d["status"] for d in receipt["days"]} == {"PRESUMED_PAID"}
    assert receipt["net_vnd"] == sum(d["net_vnd"] for d in receipt["days"])
    assert receipt["net_vnd"] == receipt["gross_vnd"] - receipt["tax_withheld_vnd"]
    assert receipt["tranche_id"] == s["ticket"]["id"]
    assert receipt["reference_code"] == s["ticket"]["reference_code"]
    assert receipt["project_name"].startswith("ABC Co")
    # What to look for on their statement: their own account, from the business's.
    assert receipt["payee"]["account_number"] and receipt["payer"]["account_number"]
    assert receipt["content_type"] == "image/png" and receipt["original_filename"] == "receipt.png"

    # Their bell rang, pointing at the receipt.
    [notice] = await _bell(client, s["investor"], "REPAYMENT_RECEIPT_SUBMITTED")
    assert notice["data"]["receipt_id"] == receipt["id"] and notice["data"]["amount_vnd"] == receipt["net_vnd"]
    assert notice["data"]["tranche_id"] == s["ticket"]["id"]

    # Nobody else's.
    assert await _mine(client, other) == []
    assert (await client.get("/repayment/receipts", headers=s["sme"]["headers"])).status_code == 403
    assert (await client.get("/repayment/receipts", headers=s["admin"]["headers"])).status_code == 403


async def test_confirming_matches_and_settles_the_days_and_tells_the_business(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    await _pay(client, s)
    [receipt] = await _mine(client, s["investor"])

    confirmed = await _confirm(client, s["investor"], receipt["id"])

    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["status"] == "CONFIRMED" and body["confirmed_at"] is not None
    assert {d["status"] for d in body["days"]} == {"MATCHED"}
    days = await _days(db_session, s["day_ids"])
    assert {d.status for d in days} == {"MATCHED"} and all(d.settled_at and d.matched_at for d in days)
    assert all(d.transfer_id for d in days if d.net_vnd > 0)
    transfers = (
        await db_session.execute(select(Transfer).where(Transfer.evidence_file_id == uuid.UUID(receipt["id"])))
    ).scalars().all()
    assert {(t.check_status, t.confirmation_level) for t in transfers} == {("MATCHED", "CONFIRMED")}

    # The ledger is posted for each day with principal, once.
    keys = {f"dt:repay:{d.id}:{side}" for d in days if d.principal_vnd > 0 for side in ("borrower", "lender")}
    posted = (
        await db_session.execute(select(LedgerTransaction.idempotency_key).where(LedgerTransaction.idempotency_key.in_(keys)))
    ).scalars().all()
    assert keys and set(posted) == keys

    # Recorded as the investor's doing, not staff's.
    matched = (
        await db_session.execute(
            select(EvidenceLog).where(
                EvidenceLog.entity_id.in_([d.id for d in days]), EvidenceLog.payload["event"].astext == "MATCHED"
            )
        )
    ).scalars().all()
    assert len(matched) == len(days)
    assert {(str(e.actor_id), e.actor_role) for e in matched} == {(s["investor"]["id"], "INVESTOR")}
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "REPAYMENT_RECEIPT_CONFIRMED"))
    ).scalar_one()
    assert str(audit.actor_id) == s["investor"]["id"] and str(audit.entity_id) == s["ticket"]["id"]
    assert audit.after_state["net_vnd"] == receipt["net_vnd"]
    assert sorted(audit.after_state["repayment_day_ids"]) == sorted(s["day_ids"])

    # The business is told; the investor's queue is empty, the receipt still in their history.
    [notice] = await _bell(client, s["sme"], "REPAYMENT_RECEIPT_CONFIRMED")
    assert notice["data"]["tranche_id"] == s["ticket"]["id"] and notice["data"]["amount_vnd"] == receipt["net_vnd"]
    assert notice["data"]["investor"] == "N. A***"
    assert await _mine(client, s["investor"]) == []
    [past] = await _mine(client, s["investor"], status="ALL")
    assert past["id"] == receipt["id"] and past["status"] == "CONFIRMED"
    schedule = (
        await client.get(f"/repayment/tranches/{s['ticket']['id']}/schedule", headers=s["investor"]["headers"])
    ).json()
    assert {r["status"] for r in schedule if r["repayment_day_id"] in s["day_ids"]} == {"MATCHED"}


async def test_only_the_investor_the_receipt_pays_can_confirm_it(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    other = await make_eligible_investor(await make_user(role="INVESTOR"))
    await _pay(client, s)
    [receipt] = await _mine(client, s["investor"])

    assert (await _confirm(client, other, receipt["id"])).status_code == 404
    assert (await _confirm(client, other, uuid.uuid4())).status_code == 404
    assert (await _confirm(client, s["sme"], receipt["id"])).status_code == 403
    assert (await _confirm(client, s["admin"], receipt["id"])).status_code == 403

    assert {d.status for d in await _days(db_session, s["day_ids"])} == {"PRESUMED_PAID"}
    assert (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "REPAYMENT_RECEIPT_CONFIRMED"))
    ).first() is None


async def test_a_receipt_is_confirmed_once(client, db_session, make_admin, make_user, make_eligible_investor):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    await _pay(client, s)
    [receipt] = await _mine(client, s["investor"])
    assert (await _confirm(client, s["investor"], receipt["id"])).status_code == 200

    again = await _confirm(client, s["investor"], receipt["id"])

    assert again.status_code == 409 and again.headers["X-Error-Code"] == "NOTHING_TO_CONFIRM"
    audits = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "REPAYMENT_RECEIPT_CONFIRMED"))
    ).scalars().all()
    assert len(audits) == 1


async def test_confirming_closes_the_investor_s_own_missing_report_as_paid(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    await _pay(client, s)
    reported = await client.post(
        f"/repayment/days/{s['day_ids'][0]}/report-missing", json={"statement": "Nothing arrived"},
        headers=s["investor"]["headers"],
    )
    assert reported.status_code == 200, reported.text
    [receipt] = await _mine(client, s["investor"])
    assert receipt["open_report"] is True

    confirmed = await _confirm(client, s["investor"], receipt["id"])

    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["open_report"] is False
    assert {d.status for d in await _days(db_session, s["day_ids"])} == {"MATCHED"}
    db_session.expire_all()
    dispute = (await db_session.execute(select(Dispute).where(Dispute.repayment_day_id == s["day_ids"][0]))).scalar_one()
    assert dispute.status == "DECIDED" and dispute.outcome == "PAID"
    assert str(dispute.decided_by) == s["investor"]["id"]
    decision = (
        await db_session.execute(
            select(EvidenceLog).where(EvidenceLog.entity_id == dispute.id, EvidenceLog.event_type == "DISPUTE_DECISION")
        )
    ).scalar_one()
    assert decision.actor_role == "INVESTOR"


async def test_a_receipt_that_also_pays_another_investor_is_not_shown(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    """A receipt carries both parties' bank details: one that also pays someone else (the older mark-paid route
    takes any receipt for any of the business's days) is not shown to either investor, nor confirmable."""
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    binh = await make_eligible_investor(await make_user(role="INVESTOR", full_name="Tran Thi Binh"))
    second = await _approved(client, s["admin"], binh, uuid.UUID(s["ticket"]["window_id"]), 20_000_000)
    schedule = (
        await client.get(f"/repayment/tranches/{second['id']}/schedule", headers=s["sme"]["headers"])
    ).json()
    binh_day = schedule[0]["repayment_day_id"]
    db_session.expire_all()
    day = await db_session.get(RepaymentDay, uuid.UUID(binh_day))
    day.due_date = ict_date(dt.datetime.now(dt.timezone.utc))
    await db_session.commit()
    await _pay(client, s)
    [receipt] = await _mine(client, s["investor"])
    marked = await client.post(
        "/repayment/days/mark-paid",
        json={"repayment_day_ids": [binh_day], "receipt_file_id": receipt["id"]},
        headers=s["sme"]["headers"] | {"Idempotency-Key": str(uuid.uuid4())},
    )
    assert marked.status_code == 200, marked.text

    assert await _mine(client, s["investor"]) == [] and await _mine(client, binh) == []
    assert (await _confirm(client, s["investor"], receipt["id"])).status_code == 404
    assert (await _confirm(client, binh, receipt["id"])).status_code == 404
