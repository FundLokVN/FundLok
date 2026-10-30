"""Who approves a payment account (accounts.py, PROVISIONAL(Q-D18); Edward 2026-10-29).

Investor accounts are approved on save; a name that differs from the KYC name is FLAGGED, never blocking. An SME
account is approved on save only when its account name matches the verified KYB business name; otherwise it waits
for staff.
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
from app.gverify.models import GVerifyKybVerification

from .helpers import T0, make_user


async def _register(db, user, *, role, name, number="900100200300", holder_type=None):
    return await register_payment_account(
        db, user_id=user.id, holder_role=role, holder_type=holder_type or ("ENTITY" if role == "SME" else "INDIVIDUAL"),
        bank_bin="970407", account_number=number, account_name=name, evidence_file_id=None, now=T0,
    )


async def _kyb(db, user, *, status="APPROVED", business_name="CÔNG TY TNHH ABC", at=T0):
    row = GVerifyKybVerification(
        user_id=user.id, status=status, document_type="COMPANY", business_name=business_name, created_at=at,
    )
    db.add(row)
    await db.flush()
    return row


async def _events(db, account, event_type):
    rows = await db.execute(select(EvidenceLog).where(EvidenceLog.entity_id == account.id, EvidenceLog.event_type == event_type))
    return list(rows.scalars())


# --- name comparison ----------------------------------------------------------------

def test_names_match_ignores_case_accents_and_punctuation():
    assert names_match("NGUYEN VAN A", "Nguyễn Văn A") is True
    assert names_match("cong ty tnhh abc", "CÔNG TY TNHH ABC") is True
    assert names_match("CONG TY TNHH A.B.C", "CÔNG TY TNHH ABC") is False  # staff decide anything unusual
    assert names_match("NGUYEN VAN B", "Nguyễn Văn A") is False


def test_names_match_is_none_when_there_is_no_verified_name():
    assert names_match("NGUYEN VAN A", None) is None
    assert names_match("NGUYEN VAN A", "") is None


# --- investors ----------------------------------------------------------------------

async def test_investor_account_is_approved_on_save_and_matches(world):
    user = await make_user(world.db, role="INVESTOR", full_name="Nguyễn Văn A")
    account = await _register(world.db, user, role="INVESTOR", name="NGUYEN VAN A")
    assert account.status == "APPROVED" and account.name_match is True
    assert account.approved_at == T0 and account.approved_by is None  # auto-approved: no approver
    assert await _events(world.db, account, "PAYMENT_ACCOUNT_NAME_MISMATCH") == []


async def test_investor_account_with_another_name_is_approved_but_flagged(world):
    user = await make_user(world.db, role="INVESTOR", full_name="Nguyễn Văn A")
    account = await _register(world.db, user, role="INVESTOR", name="TRAN THI B")
    assert account.status == "APPROVED"  # the flag never blocks an investor
    assert account.name_match is False and account.expected_name == "Nguyễn Văn A"
    usable = await get_usable_account(world.db, account_id=account.id, user_id=user.id, holder_role="INVESTOR")
    assert usable.id == account.id
    events = await _events(world.db, account, "PAYMENT_ACCOUNT_NAME_MISMATCH")
    assert len(events) == 1 and events[0].payload["auto_approved"] is True


# --- SMEs ----------------------------------------------------------------------------

async def test_sme_account_matching_the_verified_kyb_name_is_approved_on_save(world):
    sme = await make_user(world.db, role="SME", full_name="Owner")
    await _kyb(world.db, sme, business_name="CÔNG TY TNHH ABC")
    account = await _register(world.db, sme, role="SME", name="cong ty tnhh abc")
    assert account.status == "APPROVED" and account.name_match is True and account.approved_by is None
    assert (await get_usable_account(world.db, account_id=account.id, user_id=sme.id, holder_role="SME")).id == account.id


async def test_sme_account_with_another_name_waits_for_staff(world):
    sme = await make_user(world.db, role="SME", full_name="Owner")
    await _kyb(world.db, sme, business_name="CÔNG TY TNHH ABC")
    account = await _register(world.db, sme, role="SME", name="NGUYEN VAN MULE")
    assert account.status == "PENDING" and account.approved_at is None
    assert account.name_match is False and account.expected_name == "CÔNG TY TNHH ABC"
    with pytest.raises(DirectTransferError) as refused:
        await get_usable_account(world.db, account_id=account.id, user_id=sme.id, holder_role="SME")
    assert refused.value.code == "ACCOUNT_NOT_APPROVED"
    queue = await admin.pending_payment_accounts(world.db)
    assert [(a.id, a.name_match, a.expected_name) for a in queue] == [(account.id, False, "CÔNG TY TNHH ABC")]
    assert len(await _events(world.db, account, "PAYMENT_ACCOUNT_NAME_MISMATCH")) == 1

    await decide_payment_account(world.db, account_id=account.id, decision="APPROVED", reason="statement shows the company name", staff_id=world.admin.id, now=T0)
    usable = await get_usable_account(world.db, account_id=account.id, user_id=sme.id, holder_role="SME")
    assert usable.approved_by == world.admin.id  # a person decided it
    assert await admin.pending_payment_accounts(world.db) == []


@pytest.mark.parametrize("kyb_status", [None, "PENDING", "REJECTED", "MANUAL_REVIEW"])
async def test_sme_without_an_approved_kyb_waits_for_staff(world, kyb_status):
    sme = await make_user(world.db, role="SME", full_name="Owner")
    if kyb_status:
        await _kyb(world.db, sme, status=kyb_status)
    account = await _register(world.db, sme, role="SME", name="CONG TY TNHH ABC")
    assert account.status == "PENDING" and account.name_match is None and account.expected_name is None
    assert await _events(world.db, account, "PAYMENT_ACCOUNT_NAME_MISMATCH") == []  # unknown is not a mismatch


async def test_only_the_newest_kyb_attempt_counts(world):
    sme = await make_user(world.db, role="SME", full_name="Owner")
    await _kyb(world.db, sme, status="APPROVED", business_name="CÔNG TY TNHH ABC", at=T0 - dt.timedelta(days=2))
    await _kyb(world.db, sme, status="REJECTED", business_name="CÔNG TY TNHH ABC", at=T0 - dt.timedelta(days=1))
    account = await _register(world.db, sme, role="SME", name="CONG TY TNHH ABC")
    assert account.status == "PENDING"


async def test_self_declared_project_name_is_not_a_verified_name(world):
    # world.sme owns a project (legal_name is self-declared) but has no approved KYB: still PENDING.
    account = await _register(world.db, world.sme, role="SME", name=world.project.legal_name, number="777000111222")
    assert account.status == "PENDING" and account.name_match is None


# --- reading --------------------------------------------------------------------------

async def test_list_carries_the_flag_and_expected_name(world):
    sme = await make_user(world.db, role="SME", full_name="Owner")
    await _kyb(world.db, sme)
    await _register(world.db, sme, role="SME", name="CONG TY TNHH ABC", number="111122223333")
    await _register(world.db, sme, role="SME", name="OTHER NAME", number="444455556666")
    listed = await list_payment_accounts(world.db, user_id=sme.id)
    assert [(a.status, a.name_match) for a in listed] == [("APPROVED", True), ("PENDING", False)]
    assert all(a.expected_name == "CÔNG TY TNHH ABC" for a in listed)
