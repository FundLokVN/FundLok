"""Day 13 of a commit-then-accept round (docs/specs/direct_transfer/day13-loan-contracts.md, Money flow FINAL v1.0).

00:00 Day 13: one loan contract per lender from the final pledge list. By 15:00 the SME accepts (by signing every
contract with its enterprise digital signature) or declines; below 50% signing closes at 12:00; silence at 50% or
more accepts without signing. The Personal Guarantor signs every contract with one Zalo code (not a condition).
3 days and 1 day before 15:00 the SME is reminded in the app, by email and on Zalo, each logged.
Day 0 is Mon 2 Nov 2026 (T0); Day 13 is Sun 15 Nov.
"""
import datetime as dt
import io
import uuid

import pytest
from pypdf import PdfReader
from sqlalchemy import func, select

from app.core.config import settings
from app.core.timezone import ICT
from app.direct_transfer import commit_flow
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.models import AcceptanceReminder, FundingWindow, WindowLoanContract
from app.gverify.models import STATUS_APPROVED, GVerifyVerification
from app.loans.guarantor_models import GuarantorSignature
from app.market import day13_contracts, funding
from app.notifications.models import Notification
from app.phone_verification import zalo
from app.scheduled_jobs import acceptance
from app.users.models import User
from tests.direct_transfer.helpers import T0, make_investor, make_world
from tests.direct_transfer.test_commit_then_accept import COMMIT_CLOSES, SME_DEADLINE, _commit, _open
from tests.loans.test_listing_signing import _fill_signature_fields, _markers

M = 1_000_000
GUARANTOR_ID = "079123456789"


@pytest.fixture
async def world(db_session):
    return await make_world(db_session)


@pytest.fixture
def outbox(monkeypatch):
    sent: list[dict] = []

    async def _record(to, code, *, tracking_id):
        sent.append({"to": to, "code": code, "tracking_id": tracking_id})

    monkeypatch.setattr(zalo, "send_otp", _record)
    monkeypatch.setattr(settings, "PHONE_OTP_CHANNEL", "zalo")
    return sent


def _text(pdf: bytes) -> str:
    return " ".join("\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages).split())


async def _day13(world, amounts_by_lender):
    """A round whose pledges (several per lender allowed) ended at 00:00 on Day 13, contracts generated."""
    await _open(world)
    lenders = []
    step = 0
    for amounts in amounts_by_lender:
        inv = await make_investor(world)
        for amount in amounts:
            step += 1
            await _commit(world, inv, amount, now=T0 + dt.timedelta(hours=step))
        lenders.append(inv)
    window = await commit_flow.advance_window(world.db, window_id=world.window.id, now=COMMIT_CLOSES)
    assert window.status == "AWAITING_SME"
    made = await day13_contracts.generate_for_window(world.db, window=window, storage=world.storage, now=COMMIT_CLOSES)
    return window, lenders, made


async def _signed_copy(world, row) -> bytes:
    pdf = await world.storage.get(row.file_key, max_bytes=10 * M)
    return _markers(_fill_signature_fields(pdf, ("SME_Signature",)), f"FAKE-NEAC-SIGNER:{world.project.tax_id}")


async def _sign_all(world, window, *, now=COMMIT_CLOSES + dt.timedelta(hours=9)):
    sme = await world.db.get(User, world.sme.id)
    for row in await day13_contracts.contracts_for(world.db, window.id):
        await day13_contracts.record_sme_signed(
            world.db, window=window, row=row, content=await _signed_copy(world, row), file_name="signed.pdf",
            storage=world.storage, now=now, actor=sme,
        )


async def _refused(coro, code):
    with pytest.raises(Exception) as info:
        await coro
    got = getattr(info.value, "code", None) or (info.value.headers or {}).get("X-Error-Code")
    assert got == code, (got, getattr(info.value, "detail", None))
    return info.value


async def test_contracts_are_generated_at_the_end_of_day_12_one_per_lender_for_its_total(world):
    window, (a, b), made = await _day13(world, [[20 * M, 10 * M], [30 * M]])
    assert made == 2
    rows = await day13_contracts.contracts_for(world.db, window.id)
    assert [(r.investor_id, r.amount_vnd, r.status) for r in rows] == [
        (a.user.id, 30 * M, "GENERATED"), (b.user.id, 30 * M, "GENERATED")
    ]
    assert rows[0].contract_no.endswith("-01/HĐCV-FL") and rows[1].contract_no.endswith("-02/HĐCV-FL")
    text = _text(await world.storage.get(rows[0].file_key, max_bytes=10 * M))
    assert rows[0].contract_no in text and rows[0].check_code in text
    assert "30.000.000 VND" in text  # the lender's own P, not the template's rule
    assert "16/11/2026" in text  # the Disbursement Date: Day 14
    # Idempotent: a second run makes nothing, even after a new contract would have been possible.
    assert await day13_contracts.generate_for_window(world.db, window=window, storage=world.storage, now=COMMIT_CLOSES) == 0


async def test_accepting_means_signing_every_lenders_contract(world):
    window, _, _ = await _day13(world, [[40 * M], [20 * M]])
    noon = COMMIT_CLOSES + dt.timedelta(hours=10)
    await _refused(
        commit_flow.decide_window(
            world.db, window_id=window.id, sme_user_id=world.sme.id, decision="ACCEPT", idempotency_key="d1", now=noon,
            contracts_signed=await day13_contracts.all_sme_signed(world.db, window.id),
        ),
        "CONTRACTS_NOT_SIGNED",
    )
    await _sign_all(world, window)
    assert await day13_contracts.all_sme_signed(world.db, window.id)
    rows = await day13_contracts.contracts_for(world.db, window.id)
    assert all(r.status == "SME_SIGNED" and r.sme_signature_check["status"] == "VERIFIED" for r in rows)
    decided = await commit_flow.decide_window(
        world.db, window_id=window.id, sme_user_id=world.sme.id, decision="ACCEPT", idempotency_key="d2", now=noon,
        contracts_signed=await day13_contracts.all_sme_signed(world.db, window.id),
    )
    assert decided.status == "AWAITING_PAYMENT" and decided.sme_decision == "ACCEPTED"


async def test_below_fifty_percent_signing_closes_at_noon_on_day_13(world):
    window, _, _ = await _day13(world, [[30 * M]])  # 30% of 100M
    noon = dt.datetime(2026, 11, 15, 12, 0, tzinfo=ICT)
    assert day13_contracts.signing_cutoff(window, 30 * M) == noon
    row = (await day13_contracts.contracts_for(world.db, window.id))[0]
    sme = await world.db.get(User, world.sme.id)
    await _refused(
        day13_contracts.record_sme_signed(
            world.db, window=window, row=row, content=await _signed_copy(world, row), file_name="s.pdf",
            storage=world.storage, now=noon, actor=sme,
        ),
        "SIGNING_CUTOFF_PASSED",
    )
    await day13_contracts.record_sme_signed(
        world.db, window=window, row=row, content=await _signed_copy(world, row), file_name="s.pdf",
        storage=world.storage, now=noon - dt.timedelta(minutes=1), actor=sme,
    )
    # At 50% or more there is no noon cut-off: silence accepts anyway.
    assert day13_contracts.signing_cutoff(window, 50 * M) is None


async def test_a_copy_of_another_contract_or_an_edited_one_is_refused(world):
    window, _, _ = await _day13(world, [[30 * M], [30 * M]])
    first, second = await day13_contracts.contracts_for(world.db, window.id)
    sme = await world.db.get(User, world.sme.id)
    at = COMMIT_CLOSES + dt.timedelta(hours=8)
    await _refused(
        day13_contracts.record_sme_signed(
            world.db, window=window, row=first, content=await _signed_copy(world, second), file_name="s.pdf",
            storage=world.storage, now=at, actor=sme,
        ),
        "SIGNING_WRONG_DOCUMENT",
    )
    unsigned = await world.storage.get(first.file_key, max_bytes=10 * M)
    await _refused(
        day13_contracts.record_sme_signed(
            world.db, window=window, row=first, content=unsigned, file_name="s.pdf", storage=world.storage, now=at,
            actor=sme,
        ),
        "SIGNING_UNSIGNED",
    )
    someone_else = _markers(_fill_signature_fields(unsigned, ("SME_Signature",)), "FAKE-NEAC-SIGNER:0101010101")
    await _refused(
        day13_contracts.record_sme_signed(
            world.db, window=window, row=first, content=someone_else, file_name="s.pdf", storage=world.storage, now=at,
            actor=sme,
        ),
        "SIGNING_SIGNER_MISMATCH",
    )


async def test_silence_at_fifty_percent_or_more_accepts_without_signatures(world):
    window, _, _ = await _day13(world, [[30 * M], [30 * M]])
    window = await commit_flow.advance_window(world.db, window_id=window.id, now=SME_DEADLINE)
    assert window.sme_decision == "AUTO_ACCEPTED" and window.status == "AWAITING_PAYMENT"
    rows = await day13_contracts.contracts_for(world.db, window.id)
    assert {r.status for r in rows} == {"GENERATED"}  # bound by every contract without signing


async def test_a_declined_round_voids_its_contracts(world):
    window, _, _ = await _day13(world, [[20 * M]])
    window = await commit_flow.advance_window(world.db, window_id=window.id, now=SME_DEADLINE)
    assert window.status == "FAILED" and window.sme_decision == "AUTO_DECLINED"
    assert await day13_contracts.void_for_window(world.db, window=window, now=SME_DEADLINE) == 1
    row = (await day13_contracts.contracts_for(world.db, window.id, live_only=False))[0]
    assert row.status == "VOID" and row.void_reason == "AUTO_DECLINED"


async def _guarantor_ready(world):
    world.db.add(
        GVerifyVerification(
            user_id=world.sme.id, verification_type="KYC", status=STATUS_APPROVED, full_name="TRAN VAN OWNER",
            person_number=GUARANTOR_ID,
        )
    )
    sme = await world.db.get(User, world.sme.id)
    sme.phone = "+84903111222"
    sme.phone_verified_at = T0
    await world.db.flush()
    return sme


async def test_the_guarantor_signs_every_contract_with_one_zalo_code_and_it_is_not_a_condition(world, outbox):
    window, _, _ = await _day13(world, [[30 * M], [30 * M]])
    sme = await _guarantor_ready(world)
    at = COMMIT_CLOSES + dt.timedelta(hours=8)
    sent = await day13_contracts.send_guarantor_code(world.db, window=window, user=sme, now=at)
    assert sent.purpose == "WINDOW_LOAN_CONTRACTS" and outbox[0]["to"] == "84903111222"
    await _refused(
        day13_contracts.sign_as_guarantor(world.db, window=window, user=sme, code="999999" if outbox[0]["code"] != "999999" else "111111", now=at),
        "CODE_INCORRECT",
    )
    signature = await day13_contracts.sign_as_guarantor(world.db, window=window, user=sme, code=outbox[0]["code"], now=at)
    rows = await day13_contracts.contracts_for(world.db, window.id)
    assert signature.signer_id_number == GUARANTOR_ID
    assert [d["reference"] for d in signature.documents] == [r.contract_no for r in rows]
    await _refused(day13_contracts.send_guarantor_code(world.db, window=window, user=sme, now=at), "GUARANTOR_ALREADY_SIGNED")
    # Not a condition of acceptance: the SME's signatures alone open ACCEPT, and silence still accepts.
    assert not await day13_contracts.all_sme_signed(world.db, window.id)


async def test_funding_advance_job_ends_the_commit_phase_and_generates_the_contracts(world, monkeypatch):
    monkeypatch.setattr(funding, "evidence_storage", lambda: world.storage)
    await _open(world)
    await _commit(world, await make_investor(world), 60 * M)
    result = await acceptance.run_funding_advance(world.db, now=COMMIT_CLOSES)
    assert result["contracts"] == 1 and result["failed"] == 0
    assert (await world.db.get(FundingWindow, world.window.id, populate_existing=True)).status == "AWAITING_SME"
    again = await acceptance.run_funding_advance(world.db, now=COMMIT_CLOSES)
    assert again["contracts"] == 0
    assert (await world.db.execute(select(func.count()).select_from(WindowLoanContract))).scalar_one() == 1


@pytest.fixture
def mailbox(monkeypatch):
    sent: list[dict] = []

    async def _deliver(db, *, to, content, record, label):
        sent.append({"to": to, "subject": content.subject})

    monkeypatch.setattr("app.scheduled_jobs.acceptance.system_email.deliver", _deliver)
    return sent


async def test_acceptance_reminders_sent_once_per_channel_and_logged(world, mailbox, monkeypatch):
    zalo_sent = []

    async def _template(to, template_id, data, *, tracking_id):
        zalo_sent.append({"to": to, "data": data})

    monkeypatch.setattr(zalo, "send_template", _template)
    monkeypatch.setattr(settings, "ZALO_ZBS_ACCEPTANCE_TEMPLATE_ID", "tpl-accept")
    await _open(world)
    await _commit(world, await make_investor(world), 40 * M)
    await _guarantor_ready(world)
    d_minus_3 = dt.datetime(2026, 11, 12, 9, 0, tzinfo=ICT)
    counts = await acceptance.send_acceptance_reminders(world.db, now=d_minus_3)
    assert counts["in_app"]["sent"] == 1 and counts["email"]["sent"] == 1 and counts["zalo"]["sent"] == 1
    assert counts["sms"]["skipped"] == "NO_PROVIDER"
    assert zalo_sent[0]["data"]["amount"] == str(40 * M)
    rows = (await world.db.execute(select(AcceptanceReminder))).scalars().all()
    assert {(r.kind, r.channel, r.status) for r in rows} == {
        ("D_MINUS_3", "IN_APP", "SENT"), ("D_MINUS_3", "EMAIL", "SENT"), ("D_MINUS_3", "ZALO", "SENT"),
        ("D_MINUS_3", "SMS", "SKIPPED"),
    }
    # Once per channel: the same day again sends nothing.
    again = await acceptance.send_acceptance_reminders(world.db, now=d_minus_3 + dt.timedelta(hours=2))
    assert again["in_app"]["sent"] == again["email"]["sent"] == again["zalo"]["sent"] == 0
    # Day 2 has no reminder; D-1 (Sat 14 Nov) sends the second.
    assert (await acceptance.send_acceptance_reminders(world.db, now=d_minus_3 + dt.timedelta(days=1)))["in_app"]["sent"] == 0
    d_minus_1 = await acceptance.send_acceptance_reminders(world.db, now=d_minus_3 + dt.timedelta(days=2))
    assert d_minus_1["in_app"]["sent"] == 1 and len(mailbox) == 2
    events = (await world.db.execute(select(Notification.event).where(Notification.user_id == world.sme.id))).scalars().all()
    assert sorted(e for e in events if e.startswith("ACCEPTANCE")) == ["ACCEPTANCE_DUE_1D", "ACCEPTANCE_DUE_3D"]


async def test_reminder_skipped_when_window_already_decided(world, mailbox):
    window, _, _ = await _day13(world, [[30 * M], [30 * M]])
    await _sign_all(world, window)
    await commit_flow.decide_window(
        world.db, window_id=window.id, sme_user_id=world.sme.id, decision="DECLINE", idempotency_key="no",
        now=COMMIT_CLOSES + dt.timedelta(hours=9),
    )
    # The D-1 run for a round decided early: logged SKIPPED, nothing sent.
    await acceptance.send_acceptance_reminders(world.db, now=dt.datetime(2026, 11, 14, 9, 0, tzinfo=ICT))
    rows = (await world.db.execute(select(AcceptanceReminder).where(AcceptanceReminder.window_id == window.id))).scalars().all()
    assert rows and all(r.status == "SKIPPED" and r.error == "DECIDED" for r in rows)
    assert mailbox == []
