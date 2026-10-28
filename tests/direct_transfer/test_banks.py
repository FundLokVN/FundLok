"""Bank list and payment-account registration (HANDOFF-04: users save a bank account first)."""
import datetime as dt
import re
from uuid import uuid4

import pytest

from app.direct_transfer.accounts import list_payment_accounts, register_payment_account
from app.direct_transfer.banks import get_bank, list_banks, require_bank
from app.direct_transfer.errors import DirectTransferError

from .helpers import T0, make_investor

NOW = dt.datetime(2026, 10, 28, 3, 0, tzinfo=dt.timezone.utc)


def test_bank_data_is_well_formed():
    banks = list_banks(include_wallets=True)
    assert len(banks) >= 40
    assert all(re.fullmatch(r"\d{6}", b.bin) for b in banks)
    assert len({b.bin for b in banks}) == len(banks)
    assert len({b.code for b in banks}) == len(banks)
    assert all(b.short_name and b.name and b.kind in ("BANK", "WALLET") for b in banks)


@pytest.mark.parametrize("code,bank_bin", [
    ("VCB", "970436"), ("ICB", "970415"), ("BIDV", "970418"), ("VBA", "970405"), ("TCB", "970407"),
    ("MB", "970422"), ("ACB", "970416"), ("VPB", "970432"), ("TPB", "970423"), ("STB", "970403"),
])
def test_major_bank_bins_are_exact(code, bank_bin):
    assert get_bank(bank_bin).code == code


def test_default_list_has_banks_only_popular_first_then_alphabetical():
    banks = list_banks()
    assert all(b.kind == "BANK" for b in banks)
    first = [b for b in banks if b.popular]
    assert banks[: len(first)] == first and first[0].code == "VCB"
    rest = [b.short_name.lower() for b in banks[len(first):]]
    assert rest == sorted(rest)


def test_wallets_are_listed_only_on_request_and_rejected_by_default():
    assert {b.code for b in list_banks(include_wallets=True) if b.kind == "WALLET"} == {"momo", "ZALOPAY", "MVAS"}
    assert not any(b.kind == "WALLET" for b in list_banks())
    with pytest.raises(DirectTransferError):
        require_bank("971025")  # MoMo
    assert require_bank("971025", allow_wallets=True).code == "momo"


async def test_register_rejects_unknown_bin_and_takes_name_from_list(world):
    user = (await make_investor(world)).user
    with pytest.raises(DirectTransferError) as exc:
        await register_payment_account(
            world.db, user_id=user.id, holder_role="INVESTOR", holder_type="INDIVIDUAL", bank_bin="999999",
            account_number="123456789", account_name="NGUYEN VAN A", evidence_file_id=None, now=NOW,
        )
    assert exc.value.status_code == 400
    account = await register_payment_account(
        world.db, user_id=user.id, holder_role="INVESTOR", holder_type="INDIVIDUAL", bank_bin="970436",
        account_number="123456789", account_name="NGUYEN VAN A", evidence_file_id=None, now=NOW,
        bank_name="whatever the client typed",
    )
    assert account.bank_name == "Vietcombank" and account.status == "PENDING"


async def test_list_payment_accounts_returns_only_the_callers_in_order(world):
    mine, other = await make_investor(world), await make_investor(world)
    extra = await register_payment_account(
        world.db, user_id=mine.user.id, holder_role="INVESTOR", holder_type="INDIVIDUAL", bank_bin="970418",
        account_number="777788889999", account_name=mine.name, evidence_file_id=None, now=T0 + dt.timedelta(minutes=1),
    )
    listed = await list_payment_accounts(world.db, user_id=mine.user.id)
    assert [a.id for a in listed] == [mine.account_id, extra.id]
    assert other.account_id not in {a.id for a in listed}
    assert await list_payment_accounts(world.db, user_id=uuid4()) == []
