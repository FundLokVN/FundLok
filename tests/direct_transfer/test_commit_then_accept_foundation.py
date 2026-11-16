"""Phase 2 slice S1: schema foundation for the commit-then-accept flow.

Spec: docs/specs/direct_transfer/commit-then-accept-flow.md section 3 (data model) and ADR-011.
No behaviour changes: these tests hold the tables, constraints and indexes the later slices build on.
"""
import uuid

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from app.direct_transfer.models import (
    WINDOW_LIVE_STATES,
    WINDOW_STATES,
    AcceptanceReminder,
    Commitment,
    FundingWindow,
    LenderStanding,
    LenderUnlockAcknowledgement,
)
from app.notifications.service import create_notification

from .helpers import T0, make_investor, make_world, open_window

NEW_STATES = ("AWAITING_SME", "AWAITING_PAYMENT")


async def _violates(db, stmt) -> bool:
    """True when the statement breaks a constraint (run in a savepoint so the test session survives)."""
    try:
        async with db.begin_nested():
            await db.execute(stmt)
    except IntegrityError:
        return True
    return False


def _set(window_id, **values):
    return update(FundingWindow).where(FundingWindow.id == window_id).values(**values)


async def _two_windows(db_session):
    a = await open_window(await make_world(db_session))
    b = await open_window(await make_world(db_session))
    return a, b


async def test_migration_recreates_live_window_indexes_with_new_states(db_session):
    a, b = await _two_windows(db_session)
    # Different SMEs and projects: fine, whatever their states.
    await db_session.execute(_set(a.id, status="AWAITING_SME"))
    await db_session.execute(_set(b.id, status="AWAITING_PAYMENT"))

    # Same SME, one AWAITING_SME and one OPEN: refused by the live-per-SME index.
    assert await _violates(db_session, _set(b.id, status="OPEN", sme_user_id=a.sme_user_id))
    # Same project: refused by the live-per-project index.
    assert await _violates(db_session, _set(b.id, status="OPEN", project_id=a.project_id))
    # AWAITING_PAYMENT is live too.
    await db_session.execute(_set(a.id, status="AWAITING_PAYMENT"))
    assert await _violates(db_session, _set(b.id, status="AWAITING_PAYMENT", sme_user_id=a.sme_user_id))
    # Once the first is no longer live, the same SME and project may have another.
    await db_session.execute(_set(a.id, status="CANCELLED"))
    assert not await _violates(db_session, _set(b.id, status="OPEN", sme_user_id=a.sme_user_id, project_id=a.project_id))

    defs = (
        await db_session.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE indexname IN "
                "('uq_funding_windows_one_live_per_project','uq_funding_windows_one_live_per_sme')"
            )
        )
    ).scalars().all()
    assert len(defs) == 2
    assert all("AWAITING_SME" in d and "AWAITING_PAYMENT" in d for d in defs)


async def test_existing_windows_default_to_legacy_flow(db_session):
    window = await open_window(await make_world(db_session))
    await db_session.refresh(window)
    assert window.flow == "LEGACY"
    assert window.commit_closes_at is None and window.sme_deadline_at is None and window.pay_deadline_at is None
    assert window.sme_decision is None and window.sme_decided_at is None and window.sme_decided_by is None
    assert await _violates(db_session, _set(window.id, flow="OTHER"))
    assert await _violates(db_session, _set(window.id, sme_decision="MAYBE"))
    assert not await _violates(db_session, _set(window.id, flow="COMMIT_THEN_ACCEPT", sme_decision="AUTO_DECLINED"))


async def test_funding_window_status_check_allows_the_two_new_states(db_session):
    assert set(NEW_STATES) <= set(WINDOW_STATES) and set(NEW_STATES) <= set(WINDOW_LIVE_STATES)
    window = await open_window(await make_world(db_session))
    for state in NEW_STATES:
        assert not await _violates(db_session, _set(window.id, status=state))
    assert await _violates(db_session, _set(window.id, status="NOPE"))


def _commitment(window, investor, **kw):
    base = dict(
        window_id=window.id, investor_id=investor.user.id, investor_account_id=investor.account_id,
        investor_type="INDIVIDUAL", amount_vnd=10_000_000, pledge_file_key="pledge/x.pdf", pledge_sha256="0" * 64,
        pledge_certificate={}, committed_at=T0,
    )
    base.update(kw)
    return Commitment(**base)


async def _flush_refused(db, row) -> None:
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            db.add(row)
            await db.flush()


async def test_commitments_allow_several_per_lender_and_check_amount_and_status(db_session):
    world = await make_world(db_session)
    window = await open_window(world)
    investor = await make_investor(world)
    db_session.add_all([_commitment(window, investor), _commitment(window, investor, amount_vnd=5_000_000)])
    await db_session.flush()
    rows = (await db_session.execute(select(Commitment).where(Commitment.window_id == window.id))).scalars().all()
    assert len(rows) == 2 and {r.status for r in rows} == {"PLEDGED"}

    for bad in (dict(amount_vnd=0), dict(status="DONE"), dict(investor_type="FUND")):
        await _flush_refused(db_session, _commitment(window, investor, **bad))
    # idempotency_key is unique when set; NULLs repeat freely (above).
    db_session.add(_commitment(window, investor, idempotency_key="k1"))
    await db_session.flush()
    await _flush_refused(db_session, _commitment(window, investor, idempotency_key="k1"))


async def test_lender_standing_status_check(db_session):
    world = await make_world(db_session)
    investor = await make_investor(world)
    row = LenderStanding(investor_id=investor.user.id)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    assert row.status == "GOOD" and row.no_show_count == 0
    where = LenderStanding.investor_id == investor.user.id
    for ok in ("LOCKED", "CLOSED", "GOOD"):
        assert not await _violates(db_session, update(LenderStanding).where(where).values(status=ok))
    assert await _violates(db_session, update(LenderStanding).where(where).values(status="BANNED"))
    # The unlock acknowledgement table exists and takes a row.
    window = await open_window(world)
    db_session.add(
        LenderUnlockAcknowledgement(
            investor_id=investor.user.id, locked_window_id=window.id, sentence_text="I understand",
            sentence_version="v1", ekyc_verification_id=uuid.uuid4(), acknowledged_at=T0,
        )
    )
    await db_session.flush()


async def test_acceptance_reminders_primary_key_is_window_kind_channel(db_session):
    window = await open_window(await make_world(db_session))

    def row(kind="D_MINUS_3", channel="EMAIL", status="SENT"):
        return AcceptanceReminder(window_id=window.id, kind=kind, channel=channel, status=status, sent_at=T0)

    db_session.add_all([row(), row(channel="IN_APP"), row(kind="D_MINUS_1")])
    await db_session.flush()
    await _flush_refused(db_session, row())
    for bad in (dict(kind="D_MINUS_2"), dict(channel="FAX"), dict(status="LOST", channel="SMS")):
        await _flush_refused(db_session, row(**bad))


@pytest.mark.parametrize(
    "event",
    [
        "COMMITMENT_RECEIVED", "ACCEPTANCE_DUE_3D", "ACCEPTANCE_DUE_1D", "WINDOW_ACCEPTED", "WINDOW_DECLINED",
        "CONTRACT_SIGNING_OPEN", "LENDER_LOCKED", "TRANCHE_CONFIRMED", "LOAN_FORMED",
    ],
)
async def test_new_notification_events_are_accepted(db_session, event):
    world = await make_world(db_session)
    create_notification(db_session, user_id=world.sme.id, event=event)
    await db_session.flush()
    with pytest.raises(ValueError):
        create_notification(db_session, user_id=world.sme.id, event="NOT_AN_EVENT")
