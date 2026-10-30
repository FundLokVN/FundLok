"""How a payment account is verified (accounts.py, name_lookup.py; D-031, PROVISIONAL(Q-D18); Edward and Loc 2026-10-30).

Standard: FundLok name = KYC/KYB name = BANK account holder name, for investors and SMEs. Every account starts
PENDING; the typed account name is a hint only. Staff record the holder name the bank shows when approving: an
investor's must equal the KYC name, an SME's may differ only if accepted explicitly. A bank-name lookup, when one
is connected, can approve automatically on an exact match of the name the BANK returns.
"""
import datetime as dt

import pytest
from sqlalchemy import select

from app.direct_transfer import admin
from app.direct_transfer.accounts import (
    decide_payment_account,
    get_usable_account,
    list_payment_accounts,
    names_match,
    register_payment_account,
)
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.models import EvidenceLog
from app.direct_transfer.name_lookup import NoLookup
from app.gverify.models import GVerifyKybVerification

from .helpers import T0, make_user


async def _register(db, user, *, role, name, number="900100200300", lookup=None):
    return await register_payment_account(
        db, user_id=user.id, holder_role=role, holder_type="ENTITY" if role == "SME" else "INDIVIDUAL",
        bank_bin="970407", account_number=number, account_name=name, evidence_file_id=None, now=T0, lookup=lookup,
    )


async def _kyb(db, user, *, status="APPROVED", business_name="CÔNG TY TNHH ABC", at=T0):
    db.add(GVerifyKybVerification(user_id=user.id, status=status, document_type="COMPANY", business_name=business_name, created_at=at))
    await db.flush()


async def _decide(db, account, staff, *, holder, decision="APPROVED", accept=False, reason="looked up at the bank"):
    return await decide_payment_account(
        db, account_id=account.id, decision=decision, reason=reason, staff_id=staff.id, now=T0,
        verified_holder_name=holder, accept_difference=accept,
    )


async def _events(db, account, event_type):
    rows = await db.execute(select(EvidenceLog).where(EvidenceLog.entity_id == account.id, EvidenceLog.event_type == event_type))
    return list(rows.scalars())


class _FakeLookup:
    """A bank-name lookup that returns what the 'bank' says for each account number."""

    def __init__(self, names):
        self.names, self.calls = names, []

    async def lookup(self, *, bank_bin, account_number):
        self.calls.append((bank_bin, account_number))
        return self.names.get(account_number)


# --- name comparison ----------------------------------------------------------------

def test_names_match_ignores_case_accents_and_punctuation():
    assert names_match("NGUYEN VAN A", "Nguyễn Văn A") is True
    assert names_match("cong ty tnhh abc", "CÔNG TY TNHH ABC") is True
    assert names_match("CONG TY TNHH A.B.C", "CÔNG TY TNHH ABC") is False
    assert names_match("NGUYEN VAN B", "Nguyễn Văn A") is False


def test_names_match_is_none_when_there_is_no_verified_name():
    assert names_match("NGUYEN VAN A", None) is None
    assert names_match("NGUYEN VAN A", "") is None


# --- saving: everyone waits ------------------------------------------------------------

async def test_every_account_starts_pending_even_when_the_typed_name_matches(world):
    inv = await make_user(world.db, role="INVESTOR", full_name="Nguyễn Văn A")
    sme = await make_user(world.db, role="SME", full_name="Owner")
    await _kyb(world.db, sme)
    a = await _register(world.db, inv, role="INVESTOR", name="NGUYEN VAN A")
    b = await _register(world.db, sme, role="SME", name="CONG TY TNHH ABC")
    assert (a.status, a.name_match) == ("PENDING", True)
    assert (b.status, b.name_match) == ("PENDING", True)
    assert a.approved_at is None and b.approved_at is None and a.approved_by is None


async def test_typed_name_mismatch_is_a_hint_and_a_log_entry_not_a_block(world):
    inv = await make_user(world.db, role="INVESTOR", full_name="Nguyễn Văn A")
    account = await _register(world.db, inv, role="INVESTOR", name="TRAN THI B")
    assert account.status == "PENDING" and account.name_match is False and account.expected_name == "Nguyễn Văn A"
    assert len(await _events(world.db, account, "PAYMENT_ACCOUNT_NAME_MISMATCH")) == 1
    with pytest.raises(DirectTransferError) as refused:
        await get_usable_account(world.db, account_id=account.id, user_id=inv.id, holder_role="INVESTOR")
    assert refused.value.code == "ACCOUNT_NOT_APPROVED"


async def test_only_the_newest_kyb_attempt_and_only_an_approved_one_gives_the_expected_name(world):
    sme = await make_user(world.db, role="SME", full_name="Owner")
    await _kyb(world.db, sme, status="APPROVED", at=T0 - dt.timedelta(days=2))
    await _kyb(world.db, sme, status="REJECTED", at=T0 - dt.timedelta(days=1))
    account = await _register(world.db, sme, role="SME", name="CONG TY TNHH ABC")
    assert account.expected_name is None and account.name_match is None


async def test_self_declared_project_name_is_not_a_verified_name(world):
    account = await _register(world.db, world.sme, role="SME", name=world.project.legal_name, number="777000111222")
    assert account.expected_name is None


# --- staff approval: the bank's holder name ------------------------------------------------

async def test_approving_needs_the_holder_name_the_bank_shows(world):
    inv = await make_user(world.db, role="INVESTOR", full_name="Nguyễn Văn A")
    account = await _register(world.db, inv, role="INVESTOR", name="NGUYEN VAN A")
    for missing in (None, "", "   "):
        with pytest.raises(DirectTransferError) as info:
            await _decide(world.db, account, world.admin, holder=missing)
        assert info.value.code == "HOLDER_NAME_REQUIRED"


async def test_investor_account_is_approved_only_when_the_bank_name_equals_the_kyc_name(world):
    inv = await make_user(world.db, role="INVESTOR", full_name="Nguyễn Văn A")
    account = await _register(world.db, inv, role="INVESTOR", name="NGUYEN VAN A")
    with pytest.raises(DirectTransferError) as info:
        await _decide(world.db, account, world.admin, holder="TRAN THI B", accept=True)  # accept is ignored
    assert info.value.code == "HOLDER_NAME_MISMATCH"
    approved = await _decide(world.db, account, world.admin, holder="NGUYEN VAN A")
    assert approved.status == "APPROVED" and approved.approved_by == world.admin.id
    event = (await _events(world.db, account, "PAYMENT_ACCOUNT_DECISION"))[0]
    assert event.payload["verified_holder_name"] == "NGUYEN VAN A" and event.payload["holder_name_matches"] is True
    assert event.payload["verified_by"] == "STAFF_LOOKUP" and event.reason == "looked up at the bank"
    assert (await get_usable_account(world.db, account_id=account.id, user_id=inv.id, holder_role="INVESTOR")).id == account.id


async def test_investor_without_a_kyc_name_cannot_be_checked(world):
    inv = await make_user(world.db, role="INVESTOR", full_name="")
    account = await _register(world.db, inv, role="INVESTOR", name="NGUYEN VAN A")
    with pytest.raises(DirectTransferError) as info:
        await _decide(world.db, account, world.admin, holder="NGUYEN VAN A")
    assert info.value.code == "KYC_NAME_MISSING"


async def test_sme_account_matching_the_kyb_name_is_approved_without_accepting_a_difference(world):
    sme = await make_user(world.db, role="SME", full_name="Owner")
    await _kyb(world.db, sme)
    account = await _register(world.db, sme, role="SME", name="CONG TY TNHH ABC")
    approved = await _decide(world.db, account, world.admin, holder="CÔNG TY TNHH ABC")
    assert approved.status == "APPROVED"
    assert (await _events(world.db, account, "PAYMENT_ACCOUNT_DECISION"))[0].payload["accept_difference"] is False


async def test_sme_difference_needs_accept_difference_and_is_logged(world):
    sme = await make_user(world.db, role="SME", full_name="Owner")
    await _kyb(world.db, sme)
    account = await _register(world.db, sme, role="SME", name="CTY TNHH ABC")
    with pytest.raises(DirectTransferError) as info:
        await _decide(world.db, account, world.admin, holder="CTY TNHH ABC")
    assert info.value.code == "HOLDER_NAME_MISMATCH"
    approved = await _decide(world.db, account, world.admin, holder="CTY TNHH ABC", accept=True, reason="CTY = CONG TY, bank letter seen")
    assert approved.status == "APPROVED"
    payload = (await _events(world.db, account, "PAYMENT_ACCOUNT_DECISION"))[0].payload
    assert payload["accept_difference"] is True and payload["holder_name_matches"] is False
    assert payload["expected_name"] == "CÔNG TY TNHH ABC"


async def test_sme_without_a_verified_kyb_name_also_needs_accept_difference(world):
    sme = await make_user(world.db, role="SME", full_name="Owner")
    account = await _register(world.db, sme, role="SME", name="CONG TY TNHH ABC")
    with pytest.raises(DirectTransferError):
        await _decide(world.db, account, world.admin, holder="CONG TY TNHH ABC")
    assert (await _decide(world.db, account, world.admin, holder="CONG TY TNHH ABC", accept=True)).status == "APPROVED"


async def test_rejecting_needs_a_reason_but_no_holder_name_and_is_final(world):
    inv = await make_user(world.db, role="INVESTOR", full_name="Nguyễn Văn A")
    account = await _register(world.db, inv, role="INVESTOR", name="TRAN THI B")
    with pytest.raises(DirectTransferError) as info:
        await decide_payment_account(world.db, account_id=account.id, decision="REJECTED", reason=" ", staff_id=world.admin.id, now=T0)
    assert info.value.code == "REASON_REQUIRED"
    rejected = await decide_payment_account(
        world.db, account_id=account.id, decision="REJECTED", reason="Bank holder is not the investor", staff_id=world.admin.id, now=T0,
    )
    assert rejected.status == "REJECTED" and rejected.rejection_reason == "Bank holder is not the investor"
    with pytest.raises(DirectTransferError) as again:
        await _decide(world.db, account, world.admin, holder="NGUYEN VAN A")
    assert again.value.code == "ALREADY_DECIDED"


# --- the staff queue and the hint -------------------------------------------------------------

async def test_queue_and_list_carry_the_expected_name_and_the_hint(world):
    sme = await make_user(world.db, role="SME", full_name="Owner")
    await _kyb(world.db, sme)
    await _register(world.db, sme, role="SME", name="CONG TY TNHH ABC", number="111122223333")
    await _register(world.db, sme, role="SME", name="OTHER NAME", number="444455556666")
    listed = await list_payment_accounts(world.db, user_id=sme.id)
    assert [(a.status, a.name_match) for a in listed] == [("PENDING", True), ("PENDING", False)]
    queue = await admin.pending_payment_accounts(world.db)
    assert {(a.account_number, a.expected_name) for a in queue} >= {("111122223333", "CÔNG TY TNHH ABC"), ("444455556666", "CÔNG TY TNHH ABC")}


# --- automatic verification hook -----------------------------------------------------------------

async def test_no_lookup_means_everything_waits_for_staff(world):
    inv = await make_user(world.db, role="INVESTOR", full_name="Nguyễn Văn A")
    assert (await _register(world.db, inv, role="INVESTOR", name="NGUYEN VAN A", lookup=NoLookup())).status == "PENDING"


async def test_a_lookup_approves_only_on_an_exact_match_of_the_name_the_bank_returns(world):
    inv = await make_user(world.db, role="INVESTOR", full_name="Nguyễn Văn A")
    lookup = _FakeLookup({"1110": "NGUYEN VAN A", "2220": "TRAN THI B"})
    ok = await _register(world.db, inv, role="INVESTOR", name="whatever the user typed", number="1110", lookup=lookup)
    assert ok.status == "APPROVED" and ok.approved_by is None and ok.approved_at == T0
    event = (await _events(world.db, ok, "PAYMENT_ACCOUNT_REGISTERED"))[0]
    assert event.payload["auto_approved"] is True and event.payload["verified_by"] == "BANK_LOOKUP"
    assert event.payload["verified_holder_name"] == "NGUYEN VAN A"

    other = await _register(world.db, inv, role="INVESTOR", name="NGUYEN VAN A", number="2220", lookup=lookup)
    assert other.status == "PENDING"  # the bank says someone else, whatever was typed

    unknown = await _register(world.db, inv, role="INVESTOR", name="NGUYEN VAN A", number="3330", lookup=lookup)
    assert unknown.status == "PENDING"  # the bank could not tell
    assert [c[1] for c in lookup.calls] == ["1110", "2220", "3330"]
