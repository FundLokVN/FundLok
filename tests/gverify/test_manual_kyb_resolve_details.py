"""AM-E17 (SCRUM-622): staff approving a manual-mode KYB record the legal representative and the company address.

Without them the SME's Standing Offer and Service Agreement print LEGAL_REPRESENTATIVE and COMPANY_ADDRESS as blanks
and signing is refused with SIGNING_DETAILS_MISSING. The new fields are stored in the same shape a provider read
leaves them (`representatives` = [{name, position}], `company_address`), so the renderers are unchanged.
"""
import base64
import io
from types import SimpleNamespace
from uuid import UUID

import pytest
from pypdf import PdfReader
from sqlalchemy import select

from app.direct_transfer.models import PaymentAccount
from app.gverify.models import GVerifyKybVerification
from app.lending.models import AuditLog
from conftest import auth_headers
from tests.loans.test_listing_signing import (
    GUARANTOR_ADDRESS,
    GUARANTOR_ID,
    _download,
    _kyc,
    _signed,
    _upload,
)
from tests.loans.test_service_agreement import TAX, _apply

KYB_BODY = {
    "document_b64": base64.b64encode(b"%PDF-1.7\nfundlok-kyb-test-certificate").decode(),
    "document_type": "COMPANY",
    "tax_code": TAX,
}
REP_NAME = "NGUYEN VAN GIAM DOC"
REP_POSITION = "Giam doc"
ADDRESS = "12 Nguyen Hue, Ben Nghe, Quan 1, TP. Ho Chi Minh"
CERT = {"business_name": "CONG TY TNHH MANUAL KYB", "tax_code": TAX}


@pytest.fixture
def stored_kyb(monkeypatch):
    async def store_kyb(verification_id, **kw):
        return None

    monkeypatch.setattr("app.gverify.storage.store_kyb_document", store_kyb)


@pytest.fixture
def stored_signed(monkeypatch):
    held: dict[str, bytes] = {}
    monkeypatch.setattr("app.loans.signing.put_bytes", lambda key, data, ct: held.__setitem__(key, data))
    monkeypatch.setattr("app.loans.signing.get_bytes", lambda key, **kw: held[key])
    return held


@pytest.fixture
def manual_kyb(client, make_user, make_admin, make_system_admin, stored_kyb):
    """An SME whose KYB waits in MANUAL_REVIEW (verification in MANUAL mode), an admin, and the resolve URL."""

    async def _make(**sme_kw):
        sysadmin = await make_system_admin()
        mode = await client.put("/system/verification-mode", json={"mode": "MANUAL"}, headers=sysadmin["headers"])
        assert mode.status_code == 200, mode.text
        sme = await make_user(role="SME", **sme_kw)
        attempt = await client.post("/gverify/kyb/verify", json=KYB_BODY, headers=sme["headers"])
        assert attempt.status_code == 201, attempt.text
        admin = await make_admin()
        url = f"/admin/kyb-verifications/{attempt.json()['verification_id']}/resolve"
        return SimpleNamespace(sme=sme, admin=admin, url=url, id=attempt.json()["verification_id"])

    return _make


async def _resolve(client, case, **extra):
    return await client.post(
        case.url, json={"decision": "APPROVED", **CERT, **extra}, headers=case.admin["headers"]
    )


async def _row(db_session, case) -> GVerifyKybVerification:
    db_session.expire_all()
    return (
        await db_session.execute(select(GVerifyKybVerification).where(GVerifyKybVerification.id == UUID(case.id)))
    ).scalar_one()


async def test_manual_kyb_resolve_accepts_legal_representative_and_company_address(
    client, db_session, manual_kyb
):
    case = await manual_kyb()
    resp = await _resolve(
        client,
        case,
        legal_representative_name=f"  {REP_NAME} ",
        legal_representative_position=REP_POSITION,
        company_address=f" {ADDRESS}  ",
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "APPROVED"
    assert resp.json()["business_name"] == CERT["business_name"]

    # Stored as a provider read leaves them.
    row = await _row(db_session, case)
    assert row.representatives == [{"name": REP_NAME, "position": REP_POSITION}]
    assert row.company_address == ADDRESS


async def test_manual_resolve_without_the_new_fields_still_works(client, db_session, manual_kyb):
    case = await manual_kyb()
    resp = await _resolve(client, case)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "APPROVED" and body["is_approved"] is True
    assert body["business_name"] == CERT["business_name"] and body["tax_code"] == TAX
    assert {"id", "status", "is_approved", "rejection_reason", "business_name", "tax_code", "updated_at"} <= body.keys()
    row = await _row(db_session, case)
    assert row.representatives is None and row.company_address is None


async def test_resolve_response_lists_missing_signing_details(client, manual_kyb, make_user, make_admin):
    nothing = await manual_kyb()
    resp = await _resolve(client, nothing)
    assert resp.json()["missing_signing_details"] == ["LEGAL_REPRESENTATIVE", "COMPANY_ADDRESS"]

    only_rep = await manual_kyb()
    resp = await _resolve(client, only_rep, legal_representative_name=REP_NAME)
    assert resp.json()["missing_signing_details"] == ["COMPANY_ADDRESS"]

    only_address = await manual_kyb()
    resp = await _resolve(client, only_address, company_address=ADDRESS)
    assert resp.json()["missing_signing_details"] == ["LEGAL_REPRESENTATIVE"]

    both = await manual_kyb()
    resp = await _resolve(
        client, both, legal_representative_name=REP_NAME, legal_representative_position=REP_POSITION,
        company_address=ADDRESS,
    )
    assert resp.json()["missing_signing_details"] == []


async def _manually_approved_case(client, db_session, manual_kyb, attach_required_documents, make_admin, *, details=True):
    """A manually approved SME carried through to FundLok's approval with locked terms, as the listing-signing tests
    do for a provider-approved one."""
    import datetime as dt

    from app.lending.models import ScoreRun

    case = await manual_kyb()
    sme = case.sme
    app_id = await _apply(client, sme, tax_id=TAX)
    extra = (
        {
            "legal_representative_name": REP_NAME,
            "legal_representative_position": REP_POSITION,
            "company_address": ADDRESS,
        }
        if details
        else {}
    )
    resolved = await _resolve(client, case, **extra)
    assert resolved.status_code == 200, resolved.text
    db_session.add(
        PaymentAccount(
            user_id=UUID(sme["id"]),
            holder_role="SME",
            holder_type="ENTITY",
            bank_name="Vietcombank",
            account_number="0071000123456",
            account_name="CONG TY TNHH MANUAL KYB",
            status="APPROVED",
        )
    )
    await db_session.commit()
    await _kyc(db_session, sme)
    entered = await client.put(
        f"/loans/applications/{app_id}/guarantor-address",
        json={"address": GUARANTOR_ADDRESS},
        headers=auth_headers(sme),
    )
    assert entered.status_code == 200, entered.text
    submitted = await client.post(f"/loans/applications/{app_id}/submit", headers=auth_headers(sme))
    assert submitted.status_code == 200, submitted.text
    await attach_required_documents(app_id)
    admin = await make_admin()
    decided = await client.post(
        f"/admin/applications/{app_id}/decision",
        json={"decision": "APPROVED", "note": "ok"},
        headers=admin["headers"],
    )
    assert decided.status_code == 200, decided.text
    db_session.add(
        ScoreRun(
            application_id=UUID(app_id),
            status="LOCKED",
            locked_at=dt.datetime.now(dt.timezone.utc),
            final_grade_precise=72.0,
            interest_rate_pct=14.5,
        )
    )
    await db_session.commit()
    return SimpleNamespace(sme=sme, app_id=app_id)


def _text(pdf: bytes) -> str:
    return " ".join("\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages).split())


async def test_manually_approved_sme_documents_render_without_blanks(
    client, db_session, manual_kyb, attach_required_documents, make_admin
):
    case = await _manually_approved_case(client, db_session, manual_kyb, attach_required_documents, make_admin)
    status = await client.get(
        f"/loans/applications/{case.app_id}/listing-documents", headers=auth_headers(case.sme)
    )
    assert status.status_code == 200, status.text
    for kind in ("standing_offer", "service_agreement"):
        missing = status.json()[kind]["missing_details"]
        assert "LEGAL_REPRESENTATIVE" not in missing and "COMPANY_ADDRESS" not in missing, (kind, missing)

    for kind in ("standing-offer", "service-agreement"):
        body = _text(await _download(client, case, kind))
        assert f"{REP_NAME} ({REP_POSITION})" in body, kind
        assert ADDRESS in body, kind


async def test_manually_approved_sme_can_sign(
    client, db_session, manual_kyb, attach_required_documents, make_admin, stored_signed, fake_neac
):
    case = await _manually_approved_case(client, db_session, manual_kyb, attach_required_documents, make_admin)
    resp = await _upload(
        client, case, "standing-offer", await _signed(client, case, "standing-offer", signers=(TAX, GUARANTOR_ID))
    )
    assert resp.status_code == 200, resp.text
    resp = await _upload(
        client, case, "service-agreement", await _signed(client, case, "service-agreement", fields=("SME_Signature",))
    )
    assert resp.headers.get("X-Error-Code") != "SIGNING_DETAILS_MISSING", resp.text



async def test_resolve_requires_admin_role(client, db_session, manual_kyb, make_user):
    case = await manual_kyb()
    investor = await make_user(role="INVESTOR")
    for who in (case.sme, investor):
        resp = await client.post(
            case.url,
            json={"decision": "APPROVED", **CERT, "legal_representative_name": REP_NAME, "company_address": ADDRESS},
            headers=who["headers"],
        )
        assert resp.status_code == 403, resp.text
    row = await _row(db_session, case)
    assert row.status == "MANUAL_REVIEW" and row.company_address is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("legal_representative_name", ""),
        ("legal_representative_name", "   "),
        ("legal_representative_name", "N" * 201),
        ("legal_representative_position", "   "),
        ("legal_representative_position", "P" * 101),
        ("company_address", ""),
        ("company_address", " \t "),
        ("company_address", "A" * 501),
    ],
)
async def test_resolve_rejects_blank_or_oversized_values(client, db_session, manual_kyb, field, value):
    case = await manual_kyb()
    resp = await _resolve(client, case, **{field: value})
    assert resp.status_code == 422, resp.text
    assert (await _row(db_session, case)).status == "MANUAL_REVIEW"


async def test_resolve_rejects_a_position_without_a_name(client, manual_kyb):
    case = await manual_kyb()
    resp = await _resolve(client, case, legal_representative_position=REP_POSITION)
    assert resp.status_code == 422, resp.text


async def test_resolve_writes_an_audit_row(client, db_session, manual_kyb):
    case = await manual_kyb()
    resp = await _resolve(
        client, case, legal_representative_name=REP_NAME, legal_representative_position=REP_POSITION,
        company_address=ADDRESS,
    )
    assert resp.status_code == 200, resp.text
    row = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_id == UUID(case.id), AuditLog.action == "KYB_MANUAL_REVIEW_RESOLVED"
            )
        )
    ).scalar_one()
    assert str(row.actor_id) == case.admin["id"]
    after = row.after_state
    assert after["status"] == "APPROVED"
    # Which fields staff entered, by name; the values themselves live on the KYB record, not in the log.
    assert after["entered_signing_fields"] == ["legal_representative_name", "legal_representative_position", "company_address"]
    assert REP_NAME not in str(after) and ADDRESS not in str(after)
