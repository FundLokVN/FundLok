"""HANDOFF-04-phat-tickets.md Epic 0: save a bank account (routes in app/users/payment_accounts_router.py)."""
import pytest
from sqlalchemy import select

from app.direct_transfer import vietqr
from app.direct_transfer.storage import InMemoryStorage
from app.lending.models import AuditLog
from app.main import app
from app.users.payment_accounts_router import get_evidence_storage

TCB = "970407"  # Techcombank, in the bank list
PDF = b"%PDF-1.4 statement"


@pytest.fixture(autouse=True)
def storage():
    store = InMemoryStorage()
    app.dependency_overrides[get_evidence_storage] = lambda: store
    yield store
    app.dependency_overrides.pop(get_evidence_storage, None)


async def _upload(client, user, *, purpose="BANK_STATEMENT", content=PDF, content_type="application/pdf"):
    return await client.post(
        "/evidence-files",
        data={"purpose": purpose},
        files={"file": ("statement.pdf", content, content_type)},
        headers=user["headers"],
    )


async def _save(client, user, *, evidence_id=None, **overrides):
    if evidence_id is None:
        evidence_id = (await _upload(client, user)).json()["file_id"]
    body = {
        "holder_type": "INDIVIDUAL",
        "bank_bin": TCB,
        "account_number": "19036789012",
        "account_name": "NGUYEN VAN A",
        "evidence_file_id": evidence_id,
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


async def test_banks_list_requires_sign_in(client):
    assert (await client.get("/direct-transfer/banks")).status_code == 401


# --- evidence upload -----------------------------------------------------------


async def test_upload_bank_statement(make_user, client, storage):
    investor = await make_user(role="INVESTOR")
    resp = await _upload(client, investor)
    assert resp.status_code == 201, resp.text
    assert set(resp.json()) == {"file_id", "sha256"}
    assert len(storage.objects) == 1


async def test_upload_rejects_other_purposes(make_user, client):
    investor = await make_user(role="INVESTOR")
    resp = await _upload(client, investor, purpose="RECEIPT")
    assert resp.status_code == 400
    assert resp.headers["X-Error-Code"] == "INVALID_PURPOSE"


async def test_upload_rejects_unsupported_type(make_user, client):
    investor = await make_user(role="INVESTOR")
    resp = await _upload(client, investor, content=b"MZ", content_type="application/x-msdownload")
    assert resp.status_code == 415
    assert resp.headers["X-Error-Code"] == "UNSUPPORTED_TYPE"


async def test_upload_rejects_too_large(make_user, client):
    investor = await make_user(role="INVESTOR")
    resp = await _upload(client, investor, content=b"x" * (10 * 1024 * 1024 + 1))
    assert resp.status_code == 413
    assert resp.headers["X-Error-Code"] == "FILE_TOO_LARGE"


async def test_upload_needs_sme_or_investor(make_user, client):
    no_role = await make_user()
    assert (await _upload(client, no_role)).status_code == 403


# --- save + list ---------------------------------------------------------------


async def test_save_account_as_investor(make_user, client, db_session):
    investor = await make_user(role="INVESTOR")
    resp = await _save(client, investor, account_number="1903 6789 012")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["holder_role"] == "INVESTOR"
    assert body["status"] == "PENDING"
    assert body["bank_name"] == "Techcombank"
    assert body["account_number"] == "19036789012"

    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "PAYOUT_ACCOUNT_ADDED"))
    ).scalar_one()
    assert str(audit.actor_id) == investor["id"]
    assert audit.after_state["account_last4"] == "9012"
    assert "19036789012" not in str(audit.after_state)


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
    resp = await _save(client, no_role, evidence_id="00000000-0000-0000-0000-000000000000")
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


async def test_save_account_refuses_someone_elses_statement(make_user, client):
    owner = await make_user(role="INVESTOR")
    other = await make_user(role="INVESTOR")
    evidence_id = (await _upload(client, owner)).json()["file_id"]
    resp = await _save(client, other, evidence_id=evidence_id)
    assert resp.status_code == 404
    assert resp.headers["X-Error-Code"] == "EVIDENCE_NOT_FOUND"


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
