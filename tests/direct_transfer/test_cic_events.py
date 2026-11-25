"""Spec: docs/specs/direct_transfer/cic-event-log.md section 8 (B11)."""
import ast
import datetime as dt
import pathlib

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.timezone import ICT
from app.direct_transfer import cic, disputes, repayment
from app.direct_transfer.models import AppendOnlyViolation, CicEvent, RepaymentDay

from .helpers import at, attested_tranche, make_investor, open_window, pay_days_through

M = 1_000_000


def ict(y, m, d, h=0):
    return dt.datetime(y, m, d, h, tzinfo=ICT)


async def test_attestation_emits_event_once_idempotently(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await attested_tranche(world, inv, 10 * M)
    events = (await world.db.execute(select(CicEvent).where(CicEvent.event_type == "ATTESTED"))).scalars().all()
    assert len(events) == 1 and events[0].emit_key == f"attested:{t.id}" and events[0].tranche_id == t.id
    assert events[0].payload["amount_vnd"] == 10 * M and events[0].payload["agreement_ref"] == t.reference_code
    assert events[0].payload["term_months"] == 4 and events[0].payload["lender_user_id"] == str(inv.user.id)
    again, created = await cic.emit_attested(world.db, t, now=at(hours=1))
    assert created is False and again.id == events[0].id


async def test_attested_event_requires_attested_tranche(world):
    from .helpers import reserve

    await open_window(world)
    inv = await make_investor(world)
    t = await reserve(world, inv, 10 * M)
    with pytest.raises(ValueError):
        await cic.emit_attested(world.db, t, now=at(hours=1))


async def test_void_emits_correction_event_and_keeps_original(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await attested_tranche(world, inv, 10 * M)
    from app.direct_transfer import tranches

    await tranches.sme_dispute_tranche(world.db, tranche_id=t.id, sme_user_id=world.sme.id, statement="x", file_id=None, now=at(hours=1), params=world.params)
    d = (await world.db.execute(select(__import__("app.direct_transfer.models", fromlist=["Dispute"]).Dispute))).scalar_one()
    await disputes.decide_dispute(world.db, dispute_id=d.id, staff_id=world.admin.id, outcome="NOT_RECEIVED", reason="not received", now=at(hours=2))
    events = {e.event_type: e for e in (await world.db.execute(select(CicEvent))).scalars()}
    assert set(events) == {"ATTESTED", "VOID"}
    assert events["VOID"].payload["reason"] == "NOT_RECEIVED" and events["VOID"].emit_key == f"void:{t.id}"
    assert events["ATTESTED"].sent_at is None and events["ATTESTED"].payload["amount_vnd"] == 10 * M  # original untouched


async def test_void_of_never_attested_tranche_emits_nothing(world):
    """A tranche that was never reported has nothing to correct."""
    from app.direct_transfer import tranches
    from .helpers import accept, reserve, upload_receipt, good_fields

    await open_window(world)
    inv = await make_investor(world)
    t = await reserve(world, inv, 10 * M)
    await accept(world, inv, t, now=at(minutes=1))
    await upload_receipt(world, inv, t, now=at(minutes=5), fields=good_fields(world, t, inv, when=at(minutes=5), amount_vnd=1))
    d = (await world.db.execute(select(__import__("app.direct_transfer.models", fromlist=["Dispute"]).Dispute))).scalar_one()
    await disputes.decide_dispute(world.db, dispute_id=d.id, staff_id=world.admin.id, outcome="NOT_RECEIVED", reason="no", now=at(hours=1))
    assert (await world.db.execute(select(CicEvent))).scalars().all() == []


async def test_daily_balance_event_per_tranche_per_day_idempotent(loan):
    world, _, tr = loan
    await pay_days_through(world, ict(2026, 11, 6, 0))
    assert await cic.emit_daily_balances(world.db, on_date=dt.date(2026, 11, 5), now=ict(2026, 11, 6, 1)) == 10
    assert await cic.emit_daily_balances(world.db, on_date=dt.date(2026, 11, 5), now=ict(2026, 11, 6, 2)) == 0
    assert await cic.emit_daily_balances(world.db, on_date=dt.date(2026, 11, 6), now=ict(2026, 11, 7, 1)) == 10
    assert await cic.emit_daily_balances(world.db, on_date=dt.date(2026, 11, 3), now=ict(2026, 11, 4, 1)) == 0  # before the first instalment


async def test_daily_balance_counts_presumed_paid_excludes_reported_missed(loan):
    world, _, tr = loan
    await pay_days_through(world, ict(2026, 11, 6, 0))
    day = (await world.db.execute(select(RepaymentDay).where(RepaymentDay.tranche_id == tr[0].id, RepaymentDay.day_index == 1, RepaymentDay.kind == "INSTALMENT"))).scalar_one()
    await repayment.report_missing_day(world.db, day_id=day.id, investor_id=tr[0].investor_id, statement="x", file_id=None, now=ict(2026, 11, 5, 9), params=world.params)  # inside the 24 hours from the proof
    await cic.emit_daily_balances(world.db, on_date=dt.date(2026, 11, 5), now=ict(2026, 11, 6, 10))
    e0 = (await world.db.execute(select(CicEvent).where(CicEvent.emit_key == f"daily:{tr[0].id}:2026-11-05"))).scalar_one()
    e1 = (await world.db.execute(select(CicEvent).where(CicEvent.emit_key == f"daily:{tr[1].id}:2026-11-05"))).scalar_one()
    assert e0.payload["missed_days"] == 1 and e0.payload["paid_days"] == 1 and e1.payload["missed_days"] == 0 and e1.payload["paid_days"] == 2
    assert e0.payload["outstanding_principal_vnd"] > e1.payload["outstanding_principal_vnd"]


async def test_missed_day_correction_emitted_on_report_and_on_resolution(loan):
    world, _, tr = loan
    await pay_days_through(world, ict(2026, 11, 5), paid_at=dt.time(17))  # the SME's proof, Wed 17:00
    day = (await world.db.execute(select(RepaymentDay).where(RepaymentDay.tranche_id == tr[0].id, RepaymentDay.day_index == 1, RepaymentDay.kind == "INSTALMENT"))).scalar_one()
    await repayment.report_missing_day(world.db, day_id=day.id, investor_id=tr[0].investor_id, statement="x", file_id=None, now=ict(2026, 11, 5, 10), params=world.params)
    d = (await world.db.execute(select(__import__("app.direct_transfer.models", fromlist=["Dispute"]).Dispute))).scalar_one()
    await disputes.decide_dispute(world.db, dispute_id=d.id, staff_id=world.admin.id, outcome="PAID", reason="paid", now=ict(2026, 11, 6, 10))
    evs = (await world.db.execute(select(CicEvent).where(CicEvent.event_type == "MISSED_DAY_CORRECTION").order_by(CicEvent.created_at))).scalars().all()
    assert [e.payload["stage"] for e in evs] == ["reported", "resolved_paid"]
    with pytest.raises(ValueError):
        await cic.emit_missed_day_correction(world.db, day, now=ict(2026, 11, 7), stage="bogus")


async def test_cic_events_delete_rejected_by_trigger(world):
    await open_window(world)
    inv = await make_investor(world)
    await attested_tranche(world, inv, 10 * M)
    with pytest.raises(DBAPIError) as info:
        async with world.db.begin_nested():
            await world.db.execute(text("DELETE FROM cic_events"))
    assert "append-only" in str(info.value)


async def test_cic_events_update_rejected_except_sent_at_null_to_value(world):
    await open_window(world)
    inv = await make_investor(world)
    await attested_tranche(world, inv, 10 * M)
    with pytest.raises(DBAPIError) as e1:
        async with world.db.begin_nested():
            await world.db.execute(text("UPDATE cic_events SET payload = '{}'::jsonb"))
    assert "except setting sent_at" in str(e1.value)
    # setting sent_at (NULL -> value) is the one permitted mutation, and only once
    await world.db.execute(text("UPDATE cic_events SET sent_at = now()"))
    with pytest.raises(DBAPIError):
        async with world.db.begin_nested():
            await world.db.execute(text("UPDATE cic_events SET sent_at = now() + interval '1 day'"))


async def test_sent_at_defaults_null_and_pending_events_lists_them_and_mark_sent_once(world):
    await open_window(world)
    inv = await make_investor(world)
    await attested_tranche(world, inv, 10 * M)
    pending = await cic.pending_events(world.db)
    assert len(pending) == 1 and pending[0].sent_at is None
    assert await cic.mark_sent(world.db, [pending[0].id], sent_at=ict(2026, 11, 20)) == 1
    assert pending[0].sent_at == ict(2026, 11, 20) and await cic.pending_events(world.db) == []
    # sent_at may be set exactly once: the DB trigger refuses a second change
    with pytest.raises(DBAPIError):
        async with world.db.begin_nested():
            await world.db.execute(text("UPDATE cic_events SET sent_at = now() + interval '1 day'"))


async def test_cic_orm_guard_allows_only_sent_at(world):
    await open_window(world)
    inv = await make_investor(world)
    await attested_tranche(world, inv, 10 * M)
    ev = (await world.db.execute(select(CicEvent))).scalar_one()
    with pytest.raises(AppendOnlyViolation):
        async with world.db.begin_nested():
            ev.payload = {"tampered": True}
            await world.db.flush()
    ev = (await world.db.execute(select(CicEvent))).scalar_one()
    assert ev.payload.get("tampered") is None
    with pytest.raises(AppendOnlyViolation):
        async with world.db.begin_nested():
            await world.db.delete(ev)
            await world.db.flush()


def test_cic_module_has_no_http_client_or_sender():
    src = (pathlib.Path(cic.__file__)).read_text()
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"httpx", "requests", "aiohttp", "urllib", "urllib3", "smtplib", "socket", "boto3"}
    funcs = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert not [f for f in funcs if f.startswith(("send", "transmit", "submit", "post", "push", "upload"))]
