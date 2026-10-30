"""Only a verified, identified investor can place an order, and an
Idempotency-Key only replays the caller's own order.

An order fills the moment it is placed (GAP-2: there is no payment step yet),
and the INVESTOR role is self-selected right after sign-up. So any account,
with an unproven email and no KYC, could take the whole of a listing: the
listing went FUNDED, real investors were locked out, and the account was
credited a share of every repayment. These tests pin the gate: email verified
and an APPROVED GVerify KYC attempt (the signal the frontend gates on).

Separately, keys are unique across every order, and the replay lookup did not
check who was asking: another investor's key returned their order.
"""
import uuid

from sqlalchemy import select

from app.gverify.models import STATUS_APPROVED, STATUS_MANUAL_REVIEW, GVerifyVerification
from app.lending.models import Listing, Order
from app.users.models import User


async def _set_verified(db_session, user_id, verified: bool) -> None:
    user = (await db_session.execute(select(User).where(User.id == user_id))).scalar_one()
    user.email_verified = verified
    await db_session.commit()


async def _orders(db_session) -> list:
    db_session.expire_all()
    return (await db_session.execute(select(Order))).scalars().all()


async def test_an_investor_without_kyc_cannot_order(open_listing, make_user, place_order, db_session):
    setup = await open_listing()
    investor = await make_user(role="INVESTOR")
    await _set_verified(db_session, investor["id"], True)

    resp = await place_order(investor, setup["listing"]["id"], eligible=False)

    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": "Complete identity verification before investing"}
    assert await _orders(db_session) == []


async def test_an_investor_with_unverified_email_cannot_order(
    open_listing, make_user, place_order, make_eligible_investor, db_session
):
    setup = await open_listing()
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    await _set_verified(db_session, investor["id"], False)

    resp = await place_order(investor, setup["listing"]["id"], eligible=False)

    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": "Verify your email address before investing"}


async def test_kyc_still_under_review_is_not_enough(open_listing, make_user, place_order, db_session):
    setup = await open_listing()
    investor = await make_user(role="INVESTOR")
    await _set_verified(db_session, investor["id"], True)
    db_session.add(
        GVerifyVerification(
            user_id=uuid.UUID(investor["id"]),
            verification_type="KYC",
            status=STATUS_MANUAL_REVIEW,
            person_number="079203009999",
        )
    )
    await db_session.commit()

    resp = await place_order(investor, setup["listing"]["id"], eligible=False)

    assert resp.status_code == 403, resp.text


async def test_an_ineligible_order_leaves_the_listing_untouched(
    open_listing, make_user, place_order, db_session
):
    setup = await open_listing(target_amount="10000.00")
    investor = await make_user(role="INVESTOR")

    resp = await place_order(investor, setup["listing"]["id"], amount="10000.00", eligible=False)

    assert resp.status_code == 403, resp.text
    db_session.expire_all()
    listing = (
        await db_session.execute(select(Listing).where(Listing.id == setup["listing"]["id"]))
    ).scalar_one()
    assert listing.status == "OPEN"
    assert listing.funded_amount == 0


async def test_an_eligible_investor_can_order(open_listing, make_user, place_order, db_session):
    setup = await open_listing()
    investor = await make_user(role="INVESTOR")

    resp = await place_order(investor, setup["listing"]["id"])

    assert resp.status_code == 201, resp.text
    approved = (
        await db_session.execute(
            select(GVerifyVerification).where(GVerifyVerification.status == STATUS_APPROVED)
        )
    ).scalars().all()
    assert [str(a.user_id) for a in approved] == [investor["id"]]


async def test_another_investors_idempotency_key_does_not_return_their_order(
    open_listing, make_user, place_order
):
    setup = await open_listing()
    first, second = await make_user(role="INVESTOR"), await make_user(role="INVESTOR")
    key = f"order-{uuid.uuid4()}"
    placed = await place_order(first, setup["listing"]["id"], idempotency_key=key)
    assert placed.status_code == 201, placed.text

    resp = await place_order(second, setup["listing"]["id"], idempotency_key=key)

    assert resp.status_code == 409, resp.text
    assert resp.json() == {"detail": "Idempotency-Key already used"}
    assert placed.json()["id"] not in resp.text


async def test_the_same_investor_still_gets_a_replay(open_listing, make_user, place_order, db_session):
    setup = await open_listing()
    investor = await make_user(role="INVESTOR")
    key = f"order-{uuid.uuid4()}"
    first = await place_order(investor, setup["listing"]["id"], idempotency_key=key)
    assert first.status_code == 201, first.text

    again = await place_order(investor, setup["listing"]["id"], idempotency_key=key)

    assert again.json()["id"] == first.json()["id"]
    assert len(await _orders(db_session)) == 1


async def test_an_investor_without_a_bank_account_cannot_order(
    open_listing, make_user, place_order, make_eligible_investor, client, db_session
):
    # Repayments go back to the investor's own bank account, so one must be saved first.
    setup = await open_listing()
    investor = await make_eligible_investor(await make_user(role="INVESTOR"), bank_account=False)

    resp = await place_order(investor, setup["listing"]["id"], eligible=False)

    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": "Add a bank account before investing"}
    assert resp.headers["X-Error-Code"] == "BANK_ACCOUNT_REQUIRED"
    assert await _orders(db_session) == []

    saved = await client.post(
        "/payment-accounts",
        json={"holder_type": "INDIVIDUAL", "bank_bin": "970407", "account_number": "19036789012", "account_name": "TEST"},
        headers={"Authorization": investor["headers"]["Authorization"]},
    )
    assert saved.status_code == 201, saved.text

    # Saved but not yet approved by an admin: still refused.
    resp = await place_order(investor, setup["listing"]["id"], eligible=False)
    assert resp.status_code == 403, resp.text
    assert resp.headers["X-Error-Code"] == "BANK_ACCOUNT_REQUIRED"

    from app.direct_transfer.models import PaymentAccount

    account = await db_session.get(PaymentAccount, uuid.UUID(saved.json()["id"]))
    account.status = "APPROVED"
    await db_session.commit()

    resp = await place_order(investor, setup["listing"]["id"], eligible=False)
    assert resp.status_code == 201, resp.text
