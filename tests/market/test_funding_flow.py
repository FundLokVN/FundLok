"""Investing through a funding window, end to end over the API.

A live project opens for investment by itself once an admin approves its SME's bank account (or when it goes live,
if the account was approved first). An investor then picks an amount within the window's bounds, agrees to the
investing terms, and gets the SME's account to pay into directly: FundLok never holds the money (ADR-009).
"""
import datetime as dt
import uuid

from sqlalchemy import select

from app.direct_transfer.models import FundingWindow, PaymentAccount, Tranche
from app.direct_transfer.vietqr import parse_tlv, verify_crc
from app.lending.models import AuditLog, Contract, Listing, LoanApplication, Project, ProjectOwnership, ScoreRun
from app.market.funding import INVESTOR_TERMS_VERSION
from app.projects.service import activate_project_if_approved

SME_ACCOUNT = "19036789012"


async def _project(db_session, sme, *, requested=80_000_000, status="ACTIVE", rate=16.0, term=4):
    """A project with an approved request and LOCKED terms (what the admin and the engine leave behind); its id."""
    project = Project(legal_name=f"ABC Co {uuid.uuid4().hex[:6]}", tax_id=f"TAX-{uuid.uuid4().hex[:10]}", status=status)
    db_session.add(project)
    await db_session.flush()
    db_session.add(ProjectOwnership(project_id=project.id, user_id=uuid.UUID(sme["id"])))
    application = LoanApplication(
        project_id=project.id, requested_amount=requested, duration_months=term, status="APPROVED",
        admin_approval="APPROVED",
    )
    db_session.add(application)
    await db_session.flush()
    db_session.add(
        ScoreRun(
            application_id=application.id, status="LOCKED", interest_rate_pct=rate, engine_version="eng-1",
            params_version="par-1", locked_at=dt.datetime.now(dt.timezone.utc),
        )
    )
    await db_session.commit()
    return project.id


async def _save_sme_account(client, sme):
    resp = await client.post(
        "/payment-accounts",
        json={"holder_type": "ENTITY", "bank_bin": "970407", "account_number": SME_ACCOUNT, "account_name": "CONG TY ABC"},
        headers=sme["headers"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _approve(client, admin, account_id):
    resp = await client.post(
        f"/admin/payment-accounts/{account_id}/decision", json={"decision": "APPROVED"}, headers=admin["headers"]
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _window(db_session, project_id) -> FundingWindow | None:
    db_session.expire_all()
    return (
        await db_session.execute(select(FundingWindow).where(FundingWindow.project_id == project_id))
    ).scalar_one_or_none()


async def _open_project(client, db_session, make_admin, make_user, **kwargs):
    admin = await make_admin()
    sme = await make_user(role="SME")
    project = await _project(db_session, sme, **kwargs)
    account = await _save_sme_account(client, sme)
    await _approve(client, admin, account["id"])
    window = await _window(db_session, project)
    assert window is not None
    return {"admin": admin, "sme": sme, "project": project, "window": window}


async def _reserve(client, investor, window_id, amount=10_000_000, *, key=None, **overrides):
    body = {"amount_vnd": amount, "ack_risk_disclosure": True, "terms_version": INVESTOR_TERMS_VERSION} | overrides
    headers = investor["headers"] | {"Idempotency-Key": key or str(uuid.uuid4())}
    return await client.post(f"/funding/windows/{window_id}/reservations", json=body, headers=headers)


# --------------------------------------------------------------------------- #
# publishing
# --------------------------------------------------------------------------- #
async def test_approving_the_sme_bank_account_opens_its_live_project_for_investment(
    client, db_session, make_admin, make_user
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    window, project = setup["window"], setup["project"]

    assert window.status == "OPEN"
    assert window.target_vnd == 80_000_000
    assert window.min_ticket_vnd == 10_000_000
    assert window.term_months == 4 and str(window.annual_rate_pct) == "16.000"
    account = await db_session.get(PaymentAccount, window.sme_payment_account_id)
    assert account.account_number == SME_ACCOUNT
    listing = await db_session.get(Listing, window.listing_id)
    assert listing.status == "OPEN"  # on the marketplace card
    contract = await db_session.get(Contract, window.contract_id)
    assert contract.status == "ACTIVE_PENDING_FUNDING"

    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "FUNDING_WINDOW_OPENED"))
    ).scalar_one()
    assert audit.entity_type == "PROJECT" and audit.entity_id == project
    assert str(audit.actor_id) == setup["admin"]["id"]
    assert audit.after_state["trigger"] == "PAYMENT_ACCOUNT_APPROVED"
    assert audit.after_state["window_id"] == str(window.id)
    assert audit.after_state["account_last4"] == "9012"


async def test_a_project_going_live_after_the_account_was_approved_opens_then(
    client, db_session, make_admin, make_user
):
    admin = await make_admin()
    sme = await make_user(role="SME")
    project = await _project(db_session, sme, status="DRAFT")
    account = await _save_sme_account(client, sme)
    await _approve(client, admin, account["id"])
    assert await _window(db_session, project) is None  # not live yet

    application = (
        await db_session.execute(select(LoanApplication).where(LoanApplication.project_id == project))
    ).scalar_one()
    assert await activate_project_if_approved(db_session, application.id, actor_id=uuid.UUID(admin["id"]))
    await db_session.commit()

    window = await _window(db_session, project)
    assert window is not None and window.status == "OPEN"
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "FUNDING_WINDOW_OPENED"))
    ).scalar_one()
    assert audit.after_state["trigger"] == "PROJECT_ACTIVATED"


async def test_the_approval_stands_when_the_project_cannot_open_and_the_admin_sees_why(
    client, db_session, make_admin, make_user
):
    admin = await make_admin()
    sme = await make_user(role="SME")
    # Above the 100M direct-transfer cap: no window can take it.
    project = await _project(db_session, sme, requested=150_000_000)
    account = await _save_sme_account(client, sme)

    approved = await _approve(client, admin, account["id"])

    assert approved["status"] == "APPROVED"
    assert await _window(db_session, project) is None
    assert (await db_session.execute(select(Contract))).scalars().all() == []  # nothing half-made

    detail = await client.get(f"/admin/projects/{project}", headers=admin["headers"])
    assert detail.status_code == 200, detail.text
    assert detail.json()["funding_window"] is None

    resp = await client.post(f"/admin/projects/{project}/funding-window", headers=admin["headers"])
    assert resp.status_code == 400, resp.text
    assert resp.headers["X-Error-Code"] == "TARGET_ABOVE_CAP"


async def test_an_admin_can_open_a_live_project_that_did_not_open_on_its_own(
    client, db_session, make_admin, make_user
):
    admin = await make_admin()
    sme = await make_user(role="SME")
    project = await _project(db_session, sme)
    # Approved before approvals opened projects (the accounts saved before 30 Oct 2026 were approved on save).
    db_session.add(
        PaymentAccount(
            user_id=uuid.UUID(sme["id"]), holder_role="SME", holder_type="ENTITY", bank_bin="970407",
            bank_name="Techcombank", account_number=SME_ACCOUNT, account_name="CONG TY ABC", status="APPROVED",
        )
    )
    await db_session.commit()

    first = await client.post(f"/admin/projects/{project}/funding-window", headers=admin["headers"])
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "OPEN"
    assert first.json()["capacity_vnd"] == 80_000_000

    again = await client.post(f"/admin/projects/{project}/funding-window", headers=admin["headers"])
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]

    detail = (await client.get(f"/admin/projects/{project}", headers=admin["headers"])).json()
    assert detail["funding_window"]["id"] == first.json()["id"]
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "FUNDING_WINDOW_OPENED"))
    ).scalar_one()
    assert audit.after_state["trigger"] == "ADMIN" and str(audit.actor_id) == admin["id"]


async def test_only_an_admin_opens_a_project_by_hand(client, db_session, make_user):
    sme = await make_user(role="SME")
    project = await _project(db_session, sme)
    resp = await client.post(f"/admin/projects/{project}/funding-window", headers=sme["headers"])
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# investing
# --------------------------------------------------------------------------- #
async def test_the_window_shows_its_bounds_to_an_investor(client, db_session, make_admin, make_user):
    setup = await _open_project(client, db_session, make_admin, make_user)
    investor = await make_user(role="INVESTOR")

    resp = await client.get(f"/funding/listings/{setup['window'].listing_id}/window", headers=investor["headers"])

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "OPEN"
    assert body["min_ticket_vnd"] == 10_000_000
    assert body["capacity_vnd"] == 80_000_000
    assert body["confirmed_vnd"] == 0 and body["reserved_vnd"] == 0
    assert body["days_left"] == 14


async def test_an_investor_reserves_agrees_and_gets_the_sme_account_to_pay(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))

    resp = await _reserve(client, investor, setup["window"].id, 20_000_000)

    assert resp.status_code == 201, resp.text
    tranche = resp.json()
    assert tranche["status"] == "SIGNED"  # reserved and the acceptance signed, in one step
    assert tranche["amount_vnd"] == 20_000_000
    assert tranche["reference_code"]

    pay = await client.get(f"/funding/tranches/{tranche['id']}/payment", headers=investor["headers"])
    assert pay.status_code == 200, pay.text
    body = pay.json()
    assert body["account_number"] == SME_ACCOUNT
    assert body["account_name"] == "CONG TY ABC"
    assert body["bank_bin"] == "970407"
    assert body["amount_vnd"] == 20_000_000
    assert body["reference_code"] == tranche["reference_code"]
    payload = body["vietqr_payload"]
    assert verify_crc(payload)
    tags = dict(parse_tlv(payload))
    assert tags["54"] == "20000000"  # the amount travels in the QR
    assert tranche["reference_code"] in tags["62"]  # and so does the memo

    window = (await client.get(f"/funding/listings/{setup['window'].listing_id}/window", headers=investor["headers"])).json()
    assert window["reserved_vnd"] == 20_000_000 and window["capacity_vnd"] == 60_000_000

    mine = (await client.get("/funding/me/tranches", headers=investor["headers"])).json()
    assert [t["id"] for t in mine] == [tranche["id"]]

    db_session.expire_all()
    row = await db_session.get(Tranche, uuid.UUID(tranche["id"]))
    assert row.acceptance_sha256  # the signed acceptance is kept as evidence
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "INVESTMENT_RESERVED"))
    ).scalar_one()
    assert str(audit.actor_id) == investor["id"]
    assert audit.after_state["terms_version"] == INVESTOR_TERMS_VERSION
    assert audit.after_state["amount_vnd"] == 20_000_000


async def test_replaying_the_same_key_returns_the_same_ticket(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    key = str(uuid.uuid4())

    first = await _reserve(client, investor, setup["window"].id, key=key)
    again = await _reserve(client, investor, setup["window"].id, key=key)

    assert first.status_code == 201 and again.status_code == 201, again.text
    assert again.json()["id"] == first.json()["id"]
    audits = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "INVESTMENT_RESERVED"))
    ).scalars().all()
    assert len(audits) == 1


async def test_the_amount_must_be_within_the_window_bounds(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))

    below = await _reserve(client, investor, setup["window"].id, 5_000_000)
    assert below.status_code == 409 and below.headers["X-Error-Code"] == "BELOW_MIN_TICKET"

    above = await _reserve(client, investor, setup["window"].id, 90_000_000)
    assert above.status_code == 409 and above.headers["X-Error-Code"] == "CAPACITY_EXCEEDED"


async def test_the_investing_terms_must_be_accepted_in_their_current_version(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))

    not_agreed = await _reserve(client, investor, setup["window"].id, ack_risk_disclosure=False)
    assert not_agreed.status_code == 400 and not_agreed.headers["X-Error-Code"] == "RISK_ACK_REQUIRED"

    stale = await _reserve(client, investor, setup["window"].id, terms_version="2020-01-01")
    assert stale.status_code == 409 and stale.headers["X-Error-Code"] == "TERMS_OUTDATED"

    assert (await db_session.execute(select(Tranche))).scalars().all() == []


async def test_a_reservation_needs_an_idempotency_key(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))

    resp = await client.post(
        f"/funding/windows/{setup['window'].id}/reservations",
        json={"amount_vnd": 10_000_000, "ack_risk_disclosure": True, "terms_version": INVESTOR_TERMS_VERSION},
        headers=investor["headers"],
    )
    assert resp.status_code == 400 and resp.headers["X-Error-Code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_an_investor_without_kyc_or_bank_account_cannot_reserve(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)

    unverified = await make_user(role="INVESTOR")
    resp = await _reserve(client, unverified, setup["window"].id)
    assert resp.status_code == 403, resp.text

    no_account = await make_eligible_investor(await make_user(role="INVESTOR"), bank_account=False)
    resp = await _reserve(client, no_account, setup["window"].id)
    assert resp.status_code == 403 and resp.headers["X-Error-Code"] == "BANK_ACCOUNT_REQUIRED"


async def test_only_the_investor_who_reserved_sees_the_payment_details(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    other = await make_eligible_investor(await make_user(role="INVESTOR"))
    tranche = (await _reserve(client, investor, setup["window"].id)).json()

    resp = await client.get(f"/funding/tranches/{tranche['id']}/payment", headers=other["headers"])
    assert resp.status_code == 404

    sme = await client.get(f"/funding/tranches/{tranche['id']}/payment", headers=setup["sme"]["headers"])
    assert sme.status_code == 403


async def test_one_live_ticket_per_investor_per_window(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    assert (await _reserve(client, investor, setup["window"].id)).status_code == 201

    second = await _reserve(client, investor, setup["window"].id)
    assert second.status_code == 409 and second.headers["X-Error-Code"] == "ALREADY_HAS_LIVE_TRANCHE"


async def _lapse(db_session, tranche_id):
    """The ticket's hold ran out an hour ago, and no expiry job has run since."""
    db_session.expire_all()
    row = await db_session.get(Tranche, uuid.UUID(tranche_id))
    row.hold_expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    await db_session.commit()


async def test_an_investor_whose_hold_lapsed_can_reserve_again(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    window_id, listing_id = setup["window"].id, setup["window"].listing_id  # read before _lapse expires them
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    first = (await _reserve(client, investor, window_id, 20_000_000)).json()
    await _lapse(db_session, first["id"])

    again = await _reserve(client, investor, window_id, 20_000_000)

    assert again.status_code == 201, again.text
    ticket = again.json()
    assert ticket["id"] != first["id"] and ticket["status"] == "SIGNED"
    assert ticket["reference_code"] != first["reference_code"]  # a new memo for the new transfer
    db_session.expire_all()
    assert (await db_session.get(Tranche, uuid.UUID(first["id"]))).status == "EXPIRED"
    window = (await client.get(f"/funding/listings/{listing_id}/window", headers=investor["headers"])).json()
    assert window["reserved_vnd"] == 20_000_000 and window["capacity_vnd"] == 60_000_000


async def test_reading_the_window_releases_lapsed_holds(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    window_id, listing_id = setup["window"].id, setup["window"].listing_id
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    tranche = (await _reserve(client, investor, window_id, 80_000_000)).json()
    assert (await _window(db_session, setup["project"])).status == "FULL"
    await _lapse(db_session, tranche["id"])

    resp = await client.get(f"/funding/listings/{listing_id}/window", headers=investor["headers"])

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "OPEN"  # the lapse reopens a window that was full
    assert body["reserved_vnd"] == 0 and body["capacity_vnd"] == 80_000_000
    mine = (await client.get("/funding/me/tranches", headers=investor["headers"])).json()
    assert [t["status"] for t in mine] == ["EXPIRED"]


async def test_replaying_the_key_of_a_lapsed_reservation_says_it_expired(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    window_id = setup["window"].id
    key = str(uuid.uuid4())
    first = (await _reserve(client, investor, window_id, key=key)).json()
    await _lapse(db_session, first["id"])

    replay = await _reserve(client, investor, window_id, key=key)

    assert replay.status_code == 409 and replay.headers["X-Error-Code"] == "HOLD_EXPIRED"


async def test_the_legacy_order_path_refuses_a_listing_with_a_funding_window(
    client, db_session, make_admin, make_user, place_order
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    investor = await make_user(role="INVESTOR")

    resp = await place_order(investor, str(setup["window"].listing_id), amount="10000000")

    assert resp.status_code == 409, resp.text
    assert resp.headers["X-Error-Code"] == "USE_FUNDING_WINDOW"
