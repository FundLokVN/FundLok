"""HANDOFF-04-phat-tickets.md Epic 0: save a bank account (routes in app/users/payment_accounts_router.py)."""
import pytest
from sqlalchemy import select

from app.direct_transfer import vietqr
from app.gverify.models import GVerifyKybVerification
from app.lending.models import AuditLog

TCB = "970407"  # Techcombank, in the bank list


async def _save(client, user, **overrides):
    body = {
        "holder_type": "INDIVIDUAL",
        "bank_bin": TCB,
        "account_number": "19036789012",
        "account_name": "NGUYEN VAN A",
        **overrides,
    }
    return await client.post("/payment-accounts", json=body, headers=user["headers"])


# --- bank list ---------------------------------------------------------------


async def test_banks_list_popular_first_and_hides_wallets(make_user, client):
    user = await make_user()
    resp = await client.get("/direct-transfer/banks", headers=user["headers"])
    assert resp.status_code == 200, resp.text
    banks = resp.json()
    assert banks[0]["popular"] is True
    first_rest = next(i for i, b in enumerate(banks) if not b["popular"])
    assert all(b["popular"] for b in banks[:first_rest])
    assert all(b["kind"] == "BANK" for b in banks)

    with_wallets = (await client.get("/direct-transfer/banks?include_wallets=true", headers=user["headers"])).json()
    assert any(b["kind"] == "WALLET" for b in with_wallets)


async def test_banks_list_includes_banks_without_vietqr(make_user, client):
    user = await make_user()
    banks = (await client.get("/direct-transfer/banks", headers=user["headers"])).json()
    by_bin = {b["bin"]: b for b in banks}
    assert by_bin["458761"]["short_name"] == "HSBC" and by_bin["458761"]["vietqr"] is False
    assert by_bin[TCB]["vietqr"] is True


async def test_banks_list_requires_sign_in(client):
    assert (await client.get("/direct-transfer/banks")).status_code == 401


# --- save + list ---------------------------------------------------------------


async def test_save_account_as_investor(make_user, client, db_session):
    investor = await make_user(role="INVESTOR")
    resp = await _save(client, investor, account_number="1903 6789 012")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["holder_role"] == "INVESTOR"
    assert body["status"] == "APPROVED"
    assert body["approved_at"] is not None  # usable at once: there is no staff review
    assert body["bank_name"] == "Techcombank"
    assert body["account_number"] == "19036789012"

    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "PAYOUT_ACCOUNT_ADDED"))
    ).scalar_one()
    assert str(audit.actor_id) == investor["id"]
    assert audit.after_state["account_last4"] == "9012"
    assert "19036789012" not in str(audit.after_state)


async def test_saved_account_is_usable_by_the_funding_code_at_once(make_user, client, db_session):
    from uuid import UUID

    from app.direct_transfer.accounts import get_usable_account
    from app.direct_transfer.models import PaymentAccount

    investor = await make_user(role="INVESTOR")
    body = (await _save(client, investor)).json()
    account = await get_usable_account(
        db_session, account_id=UUID(body["id"]), user_id=UUID(investor["id"]), holder_role="INVESTOR"
    )
    assert account.status == "APPROVED"
    # Nobody reviewed it: approved_by stays empty, unlike an account a member of staff decided.
    stored = await db_session.get(PaymentAccount, UUID(body["id"]))
    assert stored.approved_by is None


async def test_save_account_as_sme_stamps_role(make_user, client):
    sme = await make_user(role="SME")
    resp = await _save(client, sme, holder_type="ENTITY")
    assert resp.status_code == 201, resp.text
    assert resp.json()["holder_role"] == "SME"
    assert resp.json()["holder_type"] == "ENTITY"


async def test_save_account_ignores_client_bank_name(make_user, client):
    investor = await make_user(role="INVESTOR")
    resp = await _save(client, investor, bank_name="Not A Bank")
    assert resp.status_code == 201, resp.text
    assert resp.json()["bank_name"] == "Techcombank"


async def test_save_account_needs_sme_or_investor(make_user, client):
    no_role = await make_user()
    resp = await _save(client, no_role)
    assert resp.status_code == 403


async def test_save_account_rejects_unsupported_bank(make_user, client):
    investor = await make_user(role="INVESTOR")
    resp = await _save(client, investor, bank_bin="123456")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Unsupported bank"


async def test_save_account_rejects_bad_account_number(make_user, client):
    investor = await make_user(role="INVESTOR")
    resp = await _save(client, investor, account_number="12-34")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid account number"


async def test_save_account_rejects_duplicate(make_user, client):
    investor = await make_user(role="INVESTOR")
    assert (await _save(client, investor)).status_code == 201
    resp = await _save(client, investor)
    assert resp.status_code == 409
    assert resp.headers["X-Error-Code"] == "ACCOUNT_EXISTS"


async def test_save_account_at_a_bank_outside_the_list(make_user, client):
    investor = await make_user(role="INVESTOR")
    resp = await _save(client, investor, bank_bin=None, other_bank_name="  Quỹ tín dụng nhân dân   Phú Thọ ")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["bank_bin"] is None
    assert body["bank_name"] == "Quỹ tín dụng nhân dân Phú Thọ"
    assert body["status"] == "APPROVED"

    qr = await client.get(f"/payment-accounts/{body['id']}/vietqr", headers=investor["headers"])
    assert qr.status_code == 400
    assert qr.headers["X-Error-Code"] == "QR_UNSUPPORTED"

    again = await _save(client, investor, bank_bin=None, other_bank_name="Quỹ tín dụng nhân dân Phú Thọ")
    assert again.status_code == 409
    assert again.headers["X-Error-Code"] == "ACCOUNT_EXISTS"


@pytest.mark.parametrize(
    "bank",
    [
        {"bank_bin": TCB, "other_bank_name": "Some Bank"},  # both
        {"bank_bin": None, "other_bank_name": None},  # neither
        {"bank_bin": None, "other_bank_name": "   "},  # a blank name is no name
    ],
)
async def test_save_account_needs_exactly_one_bank(make_user, client, bank):
    investor = await make_user(role="INVESTOR")
    resp = await _save(client, investor, **bank)
    assert resp.status_code == 422, resp.text


async def test_list_returns_only_own_accounts(make_user, client):
    investor = await make_user(role="INVESTOR")
    other = await make_user(role="SME")
    await _save(client, investor)
    await _save(client, investor, bank_bin="970436", account_number="0011001234567")  # Vietcombank
    await _save(client, other)

    resp = await client.get("/payment-accounts", headers=investor["headers"])
    assert resp.status_code == 200, resp.text
    accounts = resp.json()
    assert [a["account_number"] for a in accounts] == ["19036789012", "0011001234567"]
    assert all(a["holder_role"] == "INVESTOR" for a in accounts)


# --- VietQR --------------------------------------------------------------------


async def test_account_vietqr_is_static_and_valid(make_user, client):
    investor = await make_user(role="INVESTOR")
    account_id = (await _save(client, investor)).json()["id"]
    resp = await client.get(f"/payment-accounts/{account_id}/vietqr", headers=investor["headers"])
    assert resp.status_code == 200, resp.text
    payload = resp.json()["payload"]
    assert vietqr.verify_crc(payload)
    top = vietqr.parse_tlv(payload)
    assert top["01"] == "11"
    assert "54" not in top
    parsed = vietqr.parse_account_vietqr(payload)
    assert (parsed.bank_bin, parsed.account_number) == (TCB, "19036789012")


async def test_account_vietqr_is_owner_only(make_user, client):
    investor = await make_user(role="INVESTOR")
    other = await make_user(role="INVESTOR")
    account_id = (await _save(client, investor)).json()["id"]
    resp = await client.get(f"/payment-accounts/{account_id}/vietqr", headers=other["headers"])
    assert resp.status_code == 404


async def test_account_at_a_bank_without_vietqr_saves_but_has_no_qr(make_user, client):
    investor = await make_user(role="INVESTOR")
    saved = await _save(client, investor, bank_bin="458761", account_number="001234567041")  # HSBC
    assert saved.status_code == 201, saved.text
    assert saved.json()["bank_name"] == "HSBC"
    resp = await client.get(f"/payment-accounts/{saved.json()['id']}/vietqr", headers=investor["headers"])
    assert resp.status_code == 400
    assert resp.headers["X-Error-Code"] == "QR_UNSUPPORTED"


async def test_parse_vietqr_reads_a_static_account_qr(make_user, client):
    investor = await make_user(role="INVESTOR")
    payload = vietqr.build_account_vietqr_payload(bank_bin=TCB, account_number="19036789012")
    resp = await client.post("/payment-accounts/parse-vietqr", json={"payload": payload}, headers=investor["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "bank_bin": TCB,
        "bank_name": "Techcombank",
        "account_number": "19036789012",
        "account_name": None,
    }


async def test_parse_vietqr_reads_a_dynamic_qr(make_user, client):
    investor = await make_user(role="INVESTOR")
    payload = vietqr.build_vietqr_payload(bank_bin=TCB, account_number="19036789012", amount_vnd=500000, memo="FLABCD1234")
    resp = await client.post("/payment-accounts/parse-vietqr", json={"payload": payload}, headers=investor["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["account_number"] == "19036789012"


def _with_crc(body: str) -> str:
    return body + f"{vietqr.crc16_ccitt_false(body.encode('ascii')):04X}"


def _account_qr_body(*, service: str = vietqr.SERVICE_TO_ACCOUNT, aid: str = vietqr.NAPAS_AID, bank_bin: str = TCB, extra: str = "") -> str:
    beneficiary = vietqr._tlv("00", bank_bin) + vietqr._tlv("01", "19036789012")
    merchant = vietqr._tlv("00", aid) + vietqr._tlv("01", beneficiary) + vietqr._tlv("02", service)
    return (
        vietqr._tlv("00", "01") + vietqr._tlv("01", "11") + vietqr._tlv("38", merchant)
        + vietqr._tlv("53", "704") + vietqr._tlv("58", "VN") + extra + "6304"
    )


async def test_parse_vietqr_returns_name_when_bank_includes_it(make_user, client):
    investor = await make_user(role="INVESTOR")
    payload = _with_crc(_account_qr_body(extra=vietqr._tlv("59", "NGUYEN VAN A")))
    resp = await client.post("/payment-accounts/parse-vietqr", json={"payload": payload}, headers=investor["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["account_name"] == "NGUYEN VAN A"


async def test_parse_vietqr_accepts_lowercase_crc(make_user, client):
    investor = await make_user(role="INVESTOR")
    payload = vietqr.build_account_vietqr_payload(bank_bin=TCB, account_number="19036789012")
    resp = await client.post(
        "/payment-accounts/parse-vietqr", json={"payload": payload[:-4] + payload[-4:].lower()}, headers=investor["headers"]
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ("hello world", "INVALID_QR"),
        ("https://example.com/pay?x=1", "INVALID_QR"),
        ("BAD_CRC", "INVALID_QR"),
        ("CARD", "NOT_AN_ACCOUNT_QR"),
        ("NOT_NAPAS", "NOT_AN_ACCOUNT_QR"),
    ],
)
async def test_parse_vietqr_rejects(make_user, client, payload, code):
    investor = await make_user(role="INVESTOR")
    if payload == "BAD_CRC":
        good = vietqr.build_account_vietqr_payload(bank_bin=TCB, account_number="19036789012")
        payload = good[:-4] + ("0000" if good[-4:] != "0000" else "FFFF")
    elif payload == "CARD":
        payload = _with_crc(_account_qr_body(service=vietqr.SERVICE_TO_CARD))
    elif payload == "NOT_NAPAS":
        payload = _with_crc(_account_qr_body(aid="A000000999"))
    resp = await client.post("/payment-accounts/parse-vietqr", json={"payload": payload}, headers=investor["headers"])
    assert resp.status_code == 400, resp.text
    assert resp.headers["X-Error-Code"] == code


async def test_parse_vietqr_rejects_unknown_bank(make_user, client):
    investor = await make_user(role="INVESTOR")
    payload = _with_crc(_account_qr_body(bank_bin="123456"))
    resp = await client.post("/payment-accounts/parse-vietqr", json={"payload": payload}, headers=investor["headers"])
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Unsupported bank"


# --- who approves an account (accounts.py, PROVISIONAL(Q-D18)) ------------------------------------------

async def _verified_company(db_session, sme, business_name):
    db_session.add(GVerifyKybVerification(user_id=sme["id"], status="APPROVED", document_type="COMPANY", business_name=business_name))
    await db_session.commit()


async def test_sme_account_matching_the_verified_company_name_is_approved(make_user, client, db_session):
    sme = await make_user(role="SME")
    await _verified_company(db_session, sme, "CÔNG TY TNHH ABC")
    resp = await _save(client, sme, holder_type="ENTITY", account_name="CONG TY TNHH ABC")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "APPROVED" and body["name_match"] is True and body["approved_at"] is not None


async def test_sme_account_with_another_name_waits_for_staff(make_user, client, db_session):
    sme = await make_user(role="SME")
    await _verified_company(db_session, sme, "CÔNG TY TNHH ABC")
    body = (await _save(client, sme, holder_type="ENTITY", account_name="NGUYEN VAN MULE")).json()
    assert body["status"] == "PENDING" and body["approved_at"] is None
    assert body["name_match"] is False and body["expected_name"] == "CÔNG TY TNHH ABC"


async def test_investor_account_with_another_name_is_approved_but_flagged(make_user, client):
    investor = await make_user(role="INVESTOR", full_name="Nguyen Van A")
    body = (await _save(client, investor, account_name="TRAN THI B")).json()
    assert body["status"] == "APPROVED"  # the flag never blocks an investor
    assert body["name_match"] is False and body["expected_name"] == "Nguyen Van A"


async def test_listing_shows_the_flag_to_the_owner(make_user, client):
    investor = await make_user(role="INVESTOR", full_name="Nguyen Van A")
    await _save(client, investor, account_name="NGUYEN VAN A")
    listed = (await client.get("/payment-accounts", headers=investor["headers"])).json()
    assert [(a["status"], a["name_match"]) for a in listed] == [("APPROVED", True)]
