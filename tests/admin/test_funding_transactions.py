"""The admin Transactions tab: open funding rounds, investor transfers, and the receipt checks staff do by hand.

An investor reserves, pays the business directly and uploads the bank receipt; staff open the receipt, type in what
it shows, and the checks attest the ticket or send it to review, where staff decide whether the money arrived.
"""
import datetime as dt
import uuid

from sqlalchemy import select

from app.direct_transfer.models import Tranche
from app.lending.models import AuditLog
from tests.market.test_funding_flow import PNG, SME_ACCOUNT, _lapse, _open_project, _reserve, _upload


async def _paid(client, db_session, make_admin, make_user, make_eligible_investor, amount=20_000_000):
    """An open round, and one investor who reserved, paid and uploaded the receipt."""
    setup = await _open_project(client, db_session, make_admin, make_user)
    window_id = setup["window"].id
    investor = await make_eligible_investor(await make_user(role="INVESTOR", full_name="Nguyễn Văn An"))
    tranche = (await _reserve(client, investor, window_id, amount)).json()
    upload = await _upload(client, investor, tranche["id"], PNG + uuid.uuid4().bytes)
    assert upload.status_code == 202, upload.text
    return setup | {"window_id": window_id, "investor": investor, "tranche": tranche,
                    "transfer_id": upload.json()["transfer_id"]}


def _fields(tranche, **overrides):
    return {
        "amount_vnd": tranche["amount_vnd"],
        "transferred_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "payer_name": "NGUYEN VAN AN",
        "payer_account": "0123456789",
        "payee_account": SME_ACCOUNT,
        "memo": f"{tranche['reference_code']} chuyen tien",
        "bank_txn_id": f"FT{uuid.uuid4().hex[:12]}",
        "reason": "Read from the investor's receipt",
    } | overrides


async def _approvals(client, sme):
    """The business's bell: the transfers staff approved."""
    bell = (await client.get("/notifications", headers=sme["headers"])).json()
    return [n for n in bell["items"] if n["event"] == "INVESTMENT_TRANSFER_APPROVED"]


async def _transfers(client, admin, **params):
    resp = await client.get("/admin/funding/transfers", params=params, headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_the_open_rounds_show_their_totals_and_what_waits_on_staff(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _paid(client, db_session, make_admin, make_user, make_eligible_investor)

    resp = await client.get("/admin/funding/rounds", headers=s["admin"]["headers"])

    assert resp.status_code == 200, resp.text
    [round_] = resp.json()
    assert round_["id"] == str(s["window_id"]) and round_["status"] == "OPEN"
    assert round_["project_name"].startswith("ABC Co")
    assert round_["reserved_vnd"] == 20_000_000 and round_["capacity_vnd"] == 60_000_000
    assert round_["needs_action"] == 1  # the receipt to read


async def test_a_lapsed_hold_is_released_before_the_round_is_shown(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    tranche = (await _reserve(client, investor, setup["window"].id, 30_000_000)).json()
    await _lapse(db_session, tranche["id"])

    [round_] = (await client.get("/admin/funding/rounds", headers=setup["admin"]["headers"])).json()

    assert round_["reserved_vnd"] == 0 and round_["capacity_vnd"] == 80_000_000


async def test_transfers_waiting_on_staff_come_first(client, db_session, make_admin, make_user, make_eligible_investor):
    s = await _paid(client, db_session, make_admin, make_user, make_eligible_investor)
    other = await make_eligible_investor(await make_user(role="INVESTOR"))
    unpaid = (await _reserve(client, other, s["window_id"], 10_000_000)).json()

    page = await _transfers(client, s["admin"])

    assert [row["id"] for row in page["items"]] == [s["tranche"]["id"], unpaid["id"]]
    first = page["items"][0]
    assert first["needs_action"] is True
    assert first["transfer"]["check_status"] == "AWAITING_FIELDS" and first["transfer"]["has_file"] is True
    assert first["expected"]["payee_account_number"] == SME_ACCOUNT
    assert first["expected"]["payer_kyc_name"] == "Nguyễn Văn An"
    assert first["investor_email"] == s["investor"]["email"]

    detail = await client.get(f"/admin/funding/transfers/{first['id']}", headers=s["admin"]["headers"])
    assert detail.status_code == 200 and detail.json() == first

    queue = await _transfers(client, s["admin"], status="NEEDS_ACTION")
    assert [row["id"] for row in queue["items"]] == [s["tranche"]["id"]]
    waiting = await _transfers(client, s["admin"], status="AWAITING_TRANSFER")
    assert {row["id"] for row in waiting["items"]} == {s["tranche"]["id"], unpaid["id"]}  # both still SIGNED


async def test_staff_open_the_receipt_and_the_view_is_audited(
    client, db_session, make_admin, make_user, make_eligible_investor, monkeypatch
):
    s = await _paid(client, db_session, make_admin, make_user, make_eligible_investor)
    monkeypatch.setattr("app.utils.r2.presign_get", lambda key, **kw: f"https://r2.test/{key}?ttl={kw['expires_in']}")

    resp = await client.get(f"/admin/funding/transfers/{s['transfer_id']}/receipt-url", headers=s["admin"]["headers"])

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["url"].startswith("https://r2.test/") and body["expires_in"] == 600
    assert body["content_type"] == "image/png"
    audit = (await db_session.execute(select(AuditLog).where(AuditLog.action == "TRANSFER_RECEIPT_VIEWED"))).scalar_one()
    assert str(audit.actor_id) == s["admin"]["id"]


async def test_a_matching_receipt_attests_the_ticket(client, db_session, make_admin, make_user, make_eligible_investor):
    s = await _paid(client, db_session, make_admin, make_user, make_eligible_investor)

    resp = await client.post(
        f"/admin/funding/transfers/{s['transfer_id']}/fields", json=_fields(s["tranche"]), headers=s["admin"]["headers"]
    )

    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["status"] == "ATTESTED" and row["needs_action"] is True  # staff then approve it as received
    assert row["transfer"]["check_status"] == "MATCHED" and row["transfer"]["failure_codes"] == []
    assert row["transfer"]["parsed_fields"]["payee_account"] == SME_ACCOUNT
    audit = (await db_session.execute(select(AuditLog).where(AuditLog.action == "TRANSFER_RECEIPT_CHECKED"))).scalar_one()
    assert audit.after_state["check_status"] == "MATCHED"

    # The business hears its money was approved, in its own language (the client renders the copy).
    [ring] = await _approvals(client, s["sme"])
    assert ring["entity_type"] == "TRANCHE" and ring["entity_id"] == s["tranche"]["id"] and ring["read_at"] is None
    assert ring["data"]["status"] == "ATTESTED"
    assert ring["data"]["amount_vnd"] == 20_000_000 and ring["data"]["memo"] == s["tranche"]["reference_code"]
    assert ring["data"]["investor"] == "N. A***" and ring["data"]["project"].startswith("ABC Co")

    again = await client.post(
        f"/admin/funding/transfers/{s['transfer_id']}/fields", json=_fields(s["tranche"]), headers=s["admin"]["headers"]
    )
    assert again.status_code == 400 and again.headers["X-Error-Code"] == "FIELDS_ALREADY_ENTERED"


async def test_a_mismatch_goes_to_review_and_staff_decide_it_arrived(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _paid(client, db_session, make_admin, make_user, make_eligible_investor)

    checked = await client.post(
        f"/admin/funding/transfers/{s['transfer_id']}/fields",
        json=_fields(s["tranche"], amount_vnd=19_000_000),
        headers=s["admin"]["headers"],
    )
    row = checked.json()
    assert row["status"] == "UNDER_REVIEW" and row["needs_action"] is True
    assert "AMOUNT_MISMATCH" in row["transfer"]["failure_codes"]
    assert row["dispute"]["cause"] == "RECEIPT_MISMATCH" and row["dispute"]["status"] != "DECIDED"
    assert await _approvals(client, s["sme"]) == []  # sent to review is not an approval

    no_reason = await client.post(
        f"/admin/funding/reviews/{row['dispute']['id']}/decision",
        json={"outcome": "RECEIVED", "reason": ""},
        headers=s["admin"]["headers"],
    )
    assert no_reason.status_code == 422

    decided = await client.post(
        f"/admin/funding/reviews/{row['dispute']['id']}/decision",
        json={"outcome": "RECEIVED", "reason": "Bank statement shows the full amount in two parts"},
        headers=s["admin"]["headers"],
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "CONFIRMED" and decided.json()["needs_action"] is False
    assert decided.json()["dispute"]["outcome"] == "RECEIVED"
    [ring] = await _approvals(client, s["sme"])
    assert ring["data"]["status"] == "CONFIRMED" and ring["entity_id"] == s["tranche"]["id"]


async def test_not_received_voids_the_ticket_and_frees_its_place(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _paid(client, db_session, make_admin, make_user, make_eligible_investor)
    row = (
        await client.post(
            f"/admin/funding/transfers/{s['transfer_id']}/fields",
            json=_fields(s["tranche"], payee_account="999999999"),
            headers=s["admin"]["headers"],
        )
    ).json()

    decided = await client.post(
        f"/admin/funding/reviews/{row['dispute']['id']}/decision",
        json={"outcome": "NOT_RECEIVED", "reason": "Paid into the wrong account"},
        headers=s["admin"]["headers"],
    )

    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "VOID"
    assert await _approvals(client, s["sme"]) == []
    [round_] = (await client.get("/admin/funding/rounds", headers=s["admin"]["headers"])).json()
    assert round_["capacity_vnd"] == 80_000_000


async def test_staff_cannot_check_their_own_transfer(client, db_session, make_admin, make_user, make_eligible_investor):
    s = await _paid(client, db_session, make_admin, make_user, make_eligible_investor)
    db_session.expire_all()
    row = await db_session.get(Tranche, uuid.UUID(s["tranche"]["id"]))
    row.investor_id = uuid.UUID(s["admin"]["id"])  # as if the admin had invested
    await db_session.commit()

    resp = await client.post(
        f"/admin/funding/transfers/{s['transfer_id']}/fields", json=_fields(s["tranche"]), headers=s["admin"]["headers"]
    )
    assert resp.status_code == 403


async def test_only_staff_see_the_transactions_tab(client, db_session, make_admin, make_user, make_eligible_investor):
    s = await _paid(client, db_session, make_admin, make_user, make_eligible_investor)

    for path in ("/admin/funding/rounds", "/admin/funding/transfers"):
        assert (await client.get(path, headers=s["investor"]["headers"])).status_code == 403
    resp = await client.post(
        f"/admin/funding/transfers/{s['transfer_id']}/fields", json=_fields(s["tranche"]), headers=s["investor"]["headers"]
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# activity log
# --------------------------------------------------------------------------- #
async def _activity(client, admin, **params):
    resp = await client.get("/admin/funding/activity", params=params, headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_the_activity_log_shows_who_approved_and_who_denied(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _paid(client, db_session, make_admin, make_user, make_eligible_investor)
    admin = s["admin"]
    approved = await client.post(
        f"/admin/funding/transfers/{s['transfer_id']}/fields", json=_fields(s["tranche"]), headers=admin["headers"]
    )
    assert approved.json()["status"] == "ATTESTED"

    other = await make_eligible_investor(await make_user(role="INVESTOR", full_name="Tran Thi Binh"))
    ticket = (await _reserve(client, other, s["window_id"], 10_000_000)).json()
    upload = (await _upload(client, other, ticket["id"], PNG + uuid.uuid4().bytes)).json()
    review = (
        await client.post(
            f"/admin/funding/transfers/{upload['transfer_id']}/fields",
            json=_fields(ticket, payer_name="TRAN THI BINH", amount_vnd=1_000_000),
            headers=admin["headers"],
        )
    ).json()
    await client.post(
        f"/admin/funding/reviews/{review['dispute']['id']}/decision",
        json={"outcome": "NOT_RECEIVED", "reason": "Nothing on the business's statement"},
        headers=admin["headers"],
    )

    log = await _activity(client, admin)

    assert [(e["action"], e["check_status"]) for e in log["items"]] == [
        ("TRANSFER_NOT_RECEIVED", None),
        ("TRANSFER_RECEIPT_CHECKED", "MISMATCH"),
        ("TRANSFER_RECEIPT_CHECKED", "MATCHED"),
    ]
    denied, mismatch, matched = log["items"]
    assert denied["reason"] == "Nothing on the business's statement"
    assert denied["actor"]["id"] == admin["id"]
    assert denied["reference_code"] == ticket["reference_code"] and denied["amount_vnd"] == 10_000_000
    assert denied["investor"]["full_name"] == "Tran Thi Binh"
    assert denied["project_name"].startswith("ABC Co")
    assert "AMOUNT_MISMATCH" in mismatch["failure_codes"]
    assert matched["tranche_id"] == s["tranche"]["id"]

    mine = await _activity(client, admin, group="ALL", tranche_id=ticket["id"])
    assert [e["action"] for e in mine["items"]] == [
        "TRANSFER_NOT_RECEIVED",
        "TRANSFER_RECEIPT_CHECKED",
        "INVESTMENT_RECEIPT_UPLOADED",
        "INVESTMENT_RESERVED",
    ]


async def test_receipt_views_and_investor_actions_are_their_own_groups(
    client, db_session, make_admin, make_user, make_eligible_investor, monkeypatch
):
    s = await _paid(client, db_session, make_admin, make_user, make_eligible_investor)
    monkeypatch.setattr("app.utils.r2.presign_get", lambda key, **kw: f"https://r2.test/{key}")
    await client.get(f"/admin/funding/transfers/{s['transfer_id']}/receipt-url", headers=s["admin"]["headers"])

    views = await _activity(client, s["admin"], group="VIEWS")
    assert [e["action"] for e in views["items"]] == ["TRANSFER_RECEIPT_VIEWED"]
    assert views["items"][0]["tranche_id"] == s["tranche"]["id"]  # a view is filed under the transfer
    investor = await _activity(client, s["admin"], group="INVESTOR")
    assert [e["action"] for e in investor["items"]] == ["INVESTMENT_RECEIPT_UPLOADED", "INVESTMENT_RESERVED"]
    assert investor["items"][0]["actor"]["id"] == s["investor"]["id"]
    assert (await _activity(client, s["admin"]))["items"] == []  # no decision yet


async def test_a_receipt_rejected_as_a_duplicate_is_on_record(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _paid(client, db_session, make_admin, make_user, make_eligible_investor)
    txn = "FT-SAME-TXN"
    first = await client.post(
        f"/admin/funding/transfers/{s['transfer_id']}/fields",
        json=_fields(s["tranche"], bank_txn_id=txn),
        headers=s["admin"]["headers"],
    )
    assert first.status_code == 200
    other = await make_eligible_investor(await make_user(role="INVESTOR"))
    ticket = (await _reserve(client, other, s["window_id"], 10_000_000)).json()
    upload = (await _upload(client, other, ticket["id"], PNG + uuid.uuid4().bytes)).json()

    reused = await client.post(
        f"/admin/funding/transfers/{upload['transfer_id']}/fields",
        json=_fields(ticket, bank_txn_id=txn),
        headers=s["admin"]["headers"],
    )

    assert reused.status_code == 409 and reused.headers["X-Error-Code"] == "DUPLICATE_BANK_TXN"
    rejected = (await _activity(client, s["admin"]))["items"][0]
    assert rejected["action"] == "TRANSFER_RECEIPT_REJECTED" and rejected["check_status"] == "REJECTED"
    assert rejected["failure_codes"] == ["DUPLICATE_BANK_TXN"] and rejected["tranche_id"] == ticket["id"]


async def test_only_staff_read_the_activity_log(client, db_session, make_admin, make_user, make_eligible_investor):
    s = await _paid(client, db_session, make_admin, make_user, make_eligible_investor)
    resp = await client.get("/admin/funding/activity", headers=s["investor"]["headers"])
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# approve / deny by looking at the receipt (nothing typed in)
# --------------------------------------------------------------------------- #
async def _decide(client, admin, transfer_id, outcome, reason=None):
    body = {"outcome": outcome} | ({"reason": reason} if reason is not None else {})
    return await client.post(
        f"/admin/funding/transfers/{transfer_id}/decision", json=body, headers=admin["headers"]
    )


async def test_approving_a_receipt_means_the_money_is_received(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _paid(client, db_session, make_admin, make_user, make_eligible_investor)

    resp = await _decide(client, s["admin"], s["transfer_id"], "APPROVED")

    assert resp.status_code == 200, resp.text
    row = resp.json()
    # Staff checked it, so it counts and starts repaying: nothing is left for the business to confirm.
    assert row["status"] == "ACTIVE" and row["needs_action"] is False
    assert row["transfer"]["check_status"] == "MATCHED" and row["transfer"]["parsed_fields"] is None
    [ring] = await _approvals(client, s["sme"])
    assert ring["data"]["status"] == "ACTIVE" and ring["entity_id"] == s["tranche"]["id"]
    [entry] = (await _activity(client, s["admin"]))["items"]
    assert entry["action"] == "TRANSFER_RECEIVED" and entry["reason"] == "Approved after viewing the receipt"
    assert entry["tranche_status"] == "ACTIVE"
    schedule = (
        await client.get(f"/repayment/tranches/{s['tranche']['id']}/schedule", headers=s["investor"]["headers"])
    ).json()
    assert len(schedule) == 85  # 84 working days and the first-period interest, saved at approval
    [round_] = (await client.get("/admin/funding/rounds", headers=s["admin"]["headers"])).json()
    assert round_["confirmed_vnd"] == 20_000_000

    again = await _decide(client, s["admin"], s["transfer_id"], "APPROVED")
    assert again.status_code == 409 and again.headers["X-Error-Code"] == "NOTHING_TO_DECIDE"


async def test_denying_a_receipt_lets_the_investor_upload_the_right_one(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _paid(client, db_session, make_admin, make_user, make_eligible_investor)

    resp = await _decide(client, s["admin"], s["transfer_id"], "DENIED", "Screenshot is of another transfer")

    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["status"] == "SIGNED" and row["receipt_submitted_at"] is None and row["needs_action"] is False
    assert row["transfer"]["check_status"] == "REJECTED"
    assert await _approvals(client, s["sme"]) == []
    [entry] = (await _activity(client, s["admin"]))["items"]
    assert entry["action"] == "TRANSFER_NOT_RECEIVED" and entry["reason"] == "Screenshot is of another transfer"

    retry = await _upload(client, s["investor"], s["tranche"]["id"], PNG + uuid.uuid4().bytes)
    assert retry.status_code == 202, retry.text
    queue = await _transfers(client, s["admin"], status="NEEDS_ACTION")
    assert [r["transfer"]["id"] for r in queue["items"]] == [retry.json()["transfer_id"]]


async def test_a_late_receipt_is_approved_without_typing_it_in(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    window_id = setup["window"].id
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    tranche = (await _reserve(client, investor, window_id, 10_000_000)).json()
    await _lapse(db_session, tranche["id"])
    upload = (await _upload(client, investor, tranche["id"], PNG + uuid.uuid4().bytes)).json()
    assert upload["tranche_status"] == "UNDER_REVIEW"

    resp = await _decide(client, setup["admin"], upload["transfer_id"], "APPROVED", "Seen on the statement")

    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["status"] == "ACTIVE" and row["needs_action"] is False  # out of the queue, and repaying
    assert row["transfer"]["check_status"] == "MATCHED" and row["dispute"]["outcome"] == "RECEIVED"
    [ring] = await _approvals(client, setup["sme"])
    assert ring["data"]["status"] == "ACTIVE"


async def test_staff_cannot_decide_their_own_transfer(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _paid(client, db_session, make_admin, make_user, make_eligible_investor)
    db_session.expire_all()
    row = await db_session.get(Tranche, uuid.UUID(s["tranche"]["id"]))
    row.investor_id = uuid.UUID(s["admin"]["id"])
    await db_session.commit()

    assert (await _decide(client, s["admin"], s["transfer_id"], "APPROVED")).status_code == 403
    assert (await _decide(client, s["investor"], s["transfer_id"], "APPROVED")).status_code == 403


async def test_a_ticket_left_waiting_for_the_business_is_settled_by_staff(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    """ATTESTED (checked field by field, or approved before approval meant received) waits on staff now."""
    s = await _paid(client, db_session, make_admin, make_user, make_eligible_investor)
    checked = await client.post(
        f"/admin/funding/transfers/{s['transfer_id']}/fields", json=_fields(s["tranche"]), headers=s["admin"]["headers"]
    )
    assert checked.json()["status"] == "ATTESTED" and checked.json()["needs_action"] is True
    queue = await _transfers(client, s["admin"], status="NEEDS_ACTION")
    assert [r["id"] for r in queue["items"]] == [s["tranche"]["id"]]

    resp = await _decide(client, s["admin"], s["transfer_id"], "APPROVED")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ACTIVE" and resp.json()["needs_action"] is False


async def test_denying_a_ticket_left_waiting_voids_it(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _paid(client, db_session, make_admin, make_user, make_eligible_investor)
    await client.post(
        f"/admin/funding/transfers/{s['transfer_id']}/fields", json=_fields(s["tranche"]), headers=s["admin"]["headers"]
    )

    resp = await _decide(client, s["admin"], s["transfer_id"], "DENIED", "The business's statement shows nothing")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "VOID" and resp.json()["needs_action"] is False
    [round_] = (await client.get("/admin/funding/rounds", headers=s["admin"]["headers"])).json()
    assert round_["capacity_vnd"] == 80_000_000
