"""Commit-then-accept flow (slice S2) over the API: commit, my commitments, SME decision, window progress.

Spec: docs/specs/direct_transfer/commit-then-accept-flow.md section 4.1 to 4.3.
"""
import datetime as dt
import uuid

from sqlalchemy import select, update

from app.direct_transfer.models import Commitment, FundingWindow, Tranche
from app.market.funding import INVESTOR_TERMS_VERSION

from .test_funding_flow import _open_project, _reserve


def _body(amount=10_000_000, **overrides):
    return {
        "amount_vnd": amount, "ack_risk_disclosure": True, "terms_version": INVESTOR_TERMS_VERSION,
        "payment_account_id": None,
    } | overrides


async def _account_id(db_session, investor):
    from app.direct_transfer.models import PaymentAccount

    return str(
        (await db_session.execute(select(PaymentAccount.id).where(PaymentAccount.user_id == investor["id"]))).scalar_one()
    )


async def _commit(client, db_session, investor, window_id, amount=10_000_000, *, key=None, **overrides):
    body = _body(amount, **overrides)
    if body["payment_account_id"] is None:
        body["payment_account_id"] = await _account_id(db_session, investor)
    headers = investor["headers"] | {"Idempotency-Key": key or str(uuid.uuid4())}
    return await client.post(f"/funding/windows/{window_id}/commitments", json=body, headers=headers)


async def _make_commit_window(db_session, window_id, *, status="OPEN", sme_deadline_in_hours=2):
    now = dt.datetime.now(dt.timezone.utc)
    await db_session.execute(
        update(FundingWindow).where(FundingWindow.id == window_id).values(
            flow="COMMIT_THEN_ACCEPT", status=status, commit_closes_at=now + dt.timedelta(days=3),
            sme_deadline_at=now + dt.timedelta(hours=sme_deadline_in_hours),
            pay_deadline_at=now + dt.timedelta(hours=sme_deadline_in_hours + 24),
        )
    )
    await db_session.commit()
    db_session.expire_all()


async def test_commitment_requires_signed_lender_service_agreement(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    window_id = setup["window"].id
    await _make_commit_window(db_session, window_id)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"), signed_agreement=False)

    refused = await _commit(client, db_session, investor, window_id)
    assert refused.status_code == 409 and refused.headers["X-Error-Code"] == "INVESTOR_AGREEMENT_REQUIRED"
    assert (await db_session.execute(select(Commitment))).first() is None

    await make_eligible_investor(investor)  # now signs the current agreement
    ok = await _commit(client, db_session, investor, window_id)
    assert ok.status_code == 201, ok.text


async def test_commit_decide_and_progress_over_the_api(client, db_session, make_admin, make_user, make_eligible_investor):
    setup = await _open_project(client, db_session, make_admin, make_user)  # 80M target
    sme = setup["sme"]
    window = setup["window"]
    window_id, listing_id = window.id, window.listing_id
    await _make_commit_window(db_session, window_id)
    a = await make_eligible_investor(await make_user(role="INVESTOR"))
    b = await make_eligible_investor(await make_user(role="INVESTOR"))

    first = await _commit(client, db_session, a, window_id, 30_000_000)
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["status"] == "PLEDGED" and body["amount_vnd"] == 30_000_000
    assert body["my_committed_total_vnd"] == 30_000_000
    assert body["window_committed_total_vnd"] == 30_000_000 and body["window_target_vnd"] == 80_000_000
    second = await _commit(client, db_session, a, window_id, 20_000_000)
    assert second.json()["my_committed_total_vnd"] == 50_000_000
    await _commit(client, db_session, b, window_id, 10_000_000)

    mine = await client.get(f"/funding/me/commitments?window_id={window_id}", headers=a["headers"])
    assert mine.status_code == 200 and [c["amount_vnd"] for c in mine.json()] == [20_000_000, 30_000_000]  # newest first

    progress = await client.get(f"/funding/listings/{listing_id}/window", headers=a["headers"])
    assert progress.status_code == 200
    out = progress.json()
    assert out["flow"] == "COMMIT_THEN_ACCEPT" and out["committed_total_vnd"] == 60_000_000 and out["committed_pct"] == 75
    assert out["sme_decision"] is None and out["sme_decided_at"] is None
    assert out["commit_closes_at"] and out["sme_deadline_at"] and out["pay_deadline_at"]

    # The commit phase ends (the job would do it); the SME decides.
    await _make_commit_window(db_session, window_id, status="AWAITING_SME")
    key = str(uuid.uuid4())
    decision = await client.post(
        f"/funding/windows/{window_id}/decision", json={"decision": "ACCEPT"}, headers=sme["headers"] | {"Idempotency-Key": key}
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["status"] == "AWAITING_PAYMENT" and decision.json()["sme_decision"] == "ACCEPTED"
    replay = await client.post(
        f"/funding/windows/{window_id}/decision", json={"decision": "ACCEPT"}, headers=sme["headers"] | {"Idempotency-Key": key}
    )
    assert replay.status_code == 200
    db_session.expire_all()
    assert len((await db_session.execute(select(Tranche))).scalars().all()) == 2

    late = await client.post(
        f"/funding/windows/{window_id}/decision", json={"decision": "DECLINE"},
        headers=sme["headers"] | {"Idempotency-Key": str(uuid.uuid4())},
    )
    assert late.status_code == 409 and late.headers["X-Error-Code"] == "DECISION_NOT_OPEN"


async def test_commitment_idempotency_key_over_the_api(client, db_session, make_admin, make_user, make_eligible_investor):
    setup = await _open_project(client, db_session, make_admin, make_user)
    window_id = setup["window"].id
    await _make_commit_window(db_session, window_id)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    key = str(uuid.uuid4())
    one = await _commit(client, db_session, investor, window_id, 20_000_000, key=key)
    two = await _commit(client, db_session, investor, window_id, 20_000_000, key=key)
    assert one.status_code == two.status_code == 201 and one.json()["id"] == two.json()["id"]
    clash = await _commit(client, db_session, investor, window_id, 30_000_000, key=key)
    assert clash.status_code == 409 and clash.headers["X-Error-Code"] == "IDEMPOTENCY_CONFLICT"
    missing = await client.post(
        f"/funding/windows/{window_id}/commitments",
        json=_body(payment_account_id=await _account_id(db_session, investor)), headers=investor["headers"],
    )
    assert missing.status_code == 400 and missing.headers["X-Error-Code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_reservations_route_refused_for_commit_then_accept_windows_over_the_api(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    window_id = setup["window"].id
    await _make_commit_window(db_session, window_id)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    resp = await _reserve(client, investor, window_id)
    assert resp.status_code == 409 and resp.headers["X-Error-Code"] == "USE_COMMITMENTS"


async def test_rejects_wrong_role_on_every_new_route(client, db_session, make_admin, make_user, make_eligible_investor):
    setup = await _open_project(client, db_session, make_admin, make_user)
    window_id = setup["window"].id
    await _make_commit_window(db_session, window_id)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    sme, admin = setup["sme"], setup["admin"]
    commit_body = _body(payment_account_id=str(uuid.uuid4()))
    key = {"Idempotency-Key": str(uuid.uuid4())}

    for who in (sme, admin):
        r = await client.post(f"/funding/windows/{window_id}/commitments", json=commit_body, headers=who["headers"] | key)
        assert r.status_code == 403, (who["role"] if "role" in who else "", r.text)
        r = await client.get(f"/funding/me/commitments?window_id={window_id}", headers=who["headers"])
        assert r.status_code == 403
    for who in (investor, admin):
        r = await client.post(
            f"/funding/windows/{window_id}/decision", json={"decision": "ACCEPT"}, headers=who["headers"] | key
        )
        assert r.status_code == 403
    client.cookies.clear()  # the login cookies would otherwise authenticate these calls
    assert (await client.post(f"/funding/windows/{window_id}/commitments", json=commit_body, headers=key)).status_code == 401
    assert (await client.get(f"/funding/me/commitments?window_id={window_id}")).status_code == 401
    assert (await client.post(f"/funding/windows/{window_id}/decision", json={"decision": "ACCEPT"}, headers=key)).status_code == 401

    # An SME that does not own the window gets 403 on the decision.
    other_sme = await make_user(role="SME")
    await _make_commit_window(db_session, window_id, status="AWAITING_SME")
    r = await client.post(
        f"/funding/windows/{window_id}/decision", json={"decision": "ACCEPT"}, headers=other_sme["headers"] | key
    )
    assert r.status_code == 403


async def test_staff_advance_runs_the_commit_flow_and_live_state_sets_include_the_new_states(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    from app.admin.funding import OPEN_ROUND_STATUSES
    from app.admin.repayments import _REPAYING_ROUNDS as admin_repaying
    from app.direct_transfer import repayment
    from app.direct_transfer.models import WINDOW_LIVE_STATES
    from app.market.funding import LIVE_WINDOW_STATUSES
    from app.projects.borrower import LIVE_WINDOW_STATUSES as borrower_live
    from app.scheduled_jobs.reminders import _REPAYING_ROUNDS as reminder_rounds

    for live in (LIVE_WINDOW_STATUSES, borrower_live, reminder_rounds, admin_repaying):
        assert set(live) == set(WINDOW_LIVE_STATES) and {"AWAITING_SME", "AWAITING_PAYMENT"} <= set(live)
    assert {"AWAITING_SME", "AWAITING_PAYMENT"} <= set(OPEN_ROUND_STATUSES) and "REPAYING" not in OPEN_ROUND_STATUSES
    assert repayment.WINDOW_LIVE_STATES is WINDOW_LIVE_STATES

    setup = await _open_project(client, db_session, make_admin, make_user)
    window_id = setup["window"].id
    await _make_commit_window(db_session, window_id)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    assert (await _commit(client, db_session, investor, window_id, 30_000_000)).status_code == 201
    # The commit phase is over (by the clock); staff press Advance.
    await db_session.execute(
        update(FundingWindow).where(FundingWindow.id == window_id).values(
            commit_closes_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
        )
    )
    await db_session.commit()
    resp = await client.post(f"/admin/funding/rounds/{window_id}/advance", headers=setup["admin"]["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "AWAITING_SME"
    again = await client.post(f"/admin/funding/rounds/{window_id}/advance", headers=setup["admin"]["headers"])
    assert again.status_code == 200 and again.json()["status"] == "AWAITING_SME"
