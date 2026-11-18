"""Day-14 transfer rule over HTTP (day14-transfer-rule.md section 4): payment QR codes, "new QR", standing, the
LENDER_LOCKED reserve guard, and the payment timing on the investor's tickets."""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import update

from app.core.timezone import ICT
from app.direct_transfer.day14_times import set_day14_cutoffs
from app.direct_transfer.models import LenderStanding, PaymentQr
from tests.market.test_funding_flow import _open_project, _reserve


async def _signed_ticket(client, db_session, make_admin, make_user, make_eligible_investor):
    setup = await _open_project(client, db_session, make_admin, make_user)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    resp = await _reserve(client, investor, setup["window"].id, 10_000_000)
    assert resp.status_code == 201, resp.text
    return setup, investor, resp.json()


async def test_payment_shows_a_qr_code_and_a_new_qr_replaces_it(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    _, investor, tranche = await _signed_ticket(client, db_session, make_admin, make_user, make_eligible_investor)
    pay = await client.get(f"/funding/tranches/{tranche['id']}/payment", headers=investor["headers"])
    assert pay.status_code == 200, pay.text
    first = pay.json()
    assert first["qr_code"].startswith(tranche["reference_code"]) and len(first["qr_code"]) == 14
    issued = dt.datetime.fromisoformat(first["qr_issued_at"])
    assert dt.datetime.fromisoformat(first["qr_expires_at"]) - issued == dt.timedelta(minutes=15)
    assert first["pay_deadline_at"] is None and first["receipt_deadline_at"] is None  # LEGACY round

    too_soon = await client.post(f"/funding/tranches/{tranche['id']}/qr", headers=investor["headers"])
    assert too_soon.status_code == 429
    assert too_soon.headers["X-Error-Code"] == "QR_TOO_SOON" and int(too_soon.headers["Retry-After"]) > 0

    # A minute later (move the first QR back instead of waiting).
    await db_session.execute(
        update(PaymentQr)
        .where(PaymentQr.code == first["qr_code"])
        .values(issued_at=issued - dt.timedelta(minutes=2), expires_at=issued + dt.timedelta(minutes=13))
    )
    await db_session.commit()
    fresh = await client.post(f"/funding/tranches/{tranche['id']}/qr", headers=investor["headers"])
    assert fresh.status_code == 201, fresh.text
    assert fresh.json()["qr_code"] != first["qr_code"]
    again = await client.get(f"/funding/tranches/{tranche['id']}/payment", headers=investor["headers"])
    assert again.json()["qr_code"] == fresh.json()["qr_code"]


async def test_another_investor_cannot_get_a_qr_for_the_ticket(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    _, _, tranche = await _signed_ticket(client, db_session, make_admin, make_user, make_eligible_investor)
    other = await make_eligible_investor(await make_user(role="INVESTOR"))
    resp = await client.post(f"/funding/tranches/{tranche['id']}/qr", headers=other["headers"])
    assert resp.status_code == 404


async def test_standing_is_good_by_default(client, make_user, make_eligible_investor):
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    resp = await client.get("/funding/me/standing", headers=investor["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "status": "GOOD", "no_show_count": 0, "locked_at": None, "unlock_requires": [],
        "acknowledgement_sentence": None, "top_up_pledges": [],
    }
    unlock = await client.post("/funding/me/standing/unlock", json={"acknowledgement": "x"}, headers=investor["headers"])
    assert unlock.status_code == 409 and unlock.headers["X-Error-Code"] == "NOT_LOCKED"


async def test_a_locked_lender_cannot_reserve_and_sees_how_to_unlock(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    db_session.add(
        LenderStanding(
            investor_id=uuid.UUID(investor["id"]), status="LOCKED", no_show_count=1,
            locked_at=dt.datetime.now(dt.timezone.utc), locked_window_id=setup["window"].id,
        )
    )
    await db_session.commit()

    resp = await _reserve(client, investor, setup["window"].id, 10_000_000)
    assert resp.status_code == 409 and resp.headers["X-Error-Code"] == "LENDER_LOCKED"

    view = (await client.get("/funding/me/standing", headers=investor["headers"])).json()
    assert view["status"] == "LOCKED" and view["unlock_requires"] == ["EKYC", "ACKNOWLEDGEMENT"]
    assert view["acknowledgement_sentence"]
    unlock = await client.post(
        "/funding/me/standing/unlock", json={"acknowledgement": view["acknowledgement_sentence"]},
        headers=investor["headers"],
    )
    assert unlock.status_code == 409 and unlock.headers["X-Error-Code"] == "EKYC_REQUIRED"


async def _upload(client, investor, tranche_id):
    return await client.post(
        f"/funding/tranches/{tranche_id}/receipt",
        files={"file": ("r.pdf", b"%PDF-1.4 " + uuid.uuid4().bytes, "application/pdf")},
        headers=investor["headers"],
    )


async def test_my_tickets_show_no_day14_timing_on_a_legacy_round(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    _, investor, tranche = await _signed_ticket(client, db_session, make_admin, make_user, make_eligible_investor)
    assert (await _upload(client, investor, tranche["id"])).status_code == 202
    mine = (await client.get("/funding/me/tranches", headers=investor["headers"])).json()
    assert mine[0]["payment_timing"] is None and mine[0]["late_reason"] is None


async def test_my_tickets_show_a_receipt_waiting_to_be_read_as_pending_check(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    tomorrow = (dt.datetime.now(ICT) + dt.timedelta(days=1)).date()
    set_day14_cutoffs(setup["window"], tomorrow)
    await db_session.commit()
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    resp = await _reserve(client, investor, setup["window"].id, 10_000_000)
    assert resp.status_code == 201, resp.text
    tranche = resp.json()
    mine = (await client.get("/funding/me/tranches", headers=investor["headers"])).json()
    assert mine[0]["payment_timing"] is None

    assert (await _upload(client, investor, tranche["id"])).status_code == 202
    mine = (await client.get("/funding/me/tranches", headers=investor["headers"])).json()
    assert mine[0]["payment_timing"] == "PENDING_CHECK"


async def test_a_locked_investor_with_an_approved_ekyc_is_asked_to_verify_again(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    before = (await client.get("/gverify/kyc/status", headers=investor["headers"])).json()
    assert before["is_approved"] is True and before["reverify_required"] is False

    db_session.add(
        LenderStanding(
            investor_id=uuid.UUID(investor["id"]), status="LOCKED", no_show_count=1,
            locked_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1), locked_window_id=setup["window"].id,
        )
    )
    await db_session.commit()
    after = (await client.get("/gverify/kyc/status", headers=investor["headers"])).json()
    assert after["is_approved"] is True and after["reverify_required"] is True
