"""Day 14 of a commit-then-accept round: the Investor signs their loan contract with a Zalo one-time code.

Spec: docs/specs/direct_transfer/day14-investor-signing.md (Money flow FINAL v1.0, section 1 Day 14; ops rule
"Day-14 signing close and 15-minute QR").

Money flow FINAL v1.0, Day 14: "Each investor signs one loan contract for its total pledged (plus the investor
service agreement the first time) with one OTP sent by Zalo. It then gets a VietQR ..." So:

  GET  contract        the Investor's own copy, generated on Day 13 (the SME's signed copy once on file)
  POST contract/code   a code by Zalo to the phone on the Investor's account, through the same OTP template as the
                       Investor Service Agreement (investor_agreement_codes, purpose LOAN_CONTRACT, bound to the
                       ticket and the contract version). One a minute, five a day, five minutes, five attempts.
  POST contract        the code plus the logged click sign the contract; on the Investor's first Day 14 the same
                       code also confirms the Investor Service Agreement. The ticket goes RESERVED -> SIGNED and the
                       VietQR opens (GET /funding/tranches/{id}/payment).

Signing closes at 14:45 on Day 14 (`pay_deadline_at`, 409 CONTRACT_DEADLINE_PASSED); a ticket not paid by 15:00 is a
no-show (app/direct_transfer/day14.py).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.commit_flow import COMMIT_THEN_ACCEPT
from app.direct_transfer.day14_times import window_cutoffs
from app.direct_transfer.errors import DirectTransferError, bad_request, conflict
from app.direct_transfer.evidence import log_event
from app.direct_transfer.models import FundingWindow, Tranche, WindowLoanContract
from app.direct_transfer.storage import EvidenceStorage
from app.direct_transfer.tranche_state import transition_tranche
from app.loans.agreement import loan_contract_text
from app.market.investor_agreement import service as agreement
from app.market.investor_agreement.models import AgreementSigningCode
from app.phone_verification import service as phone_codes
from app.phone_verification import zalo
from app.phone_verification.phone import zalo_number
from app.utils.audit import append_audit
from app.utils.locks import advisory_xact_lock

PURPOSE = "LOAN_CONTRACT"
VERSION = loan_contract_text.VERSION
MAX_BYTES = 10 * 1024 * 1024


async def contract_for(db: AsyncSession, tranche: Tranche) -> WindowLoanContract | None:
    return (
        await db.execute(
            select(WindowLoanContract).where(
                WindowLoanContract.window_id == tranche.window_id,
                WindowLoanContract.investor_id == tranche.investor_id,
                WindowLoanContract.status != "VOID",
            )
        )
    ).scalar_one_or_none()


async def _signable(db: AsyncSession, tranche: Tranche, now: dt.datetime) -> tuple[FundingWindow, WindowLoanContract]:
    """409 TRANCHE_NOT_SIGNABLE unless a RESERVED ticket on a commit-then-accept round waiting for payment; 409
    CONTRACT_DEADLINE_PASSED from 14:45 on Day 14; 409 CONTRACT_UNAVAILABLE when its contract was not generated."""
    window = await db.get(FundingWindow, tranche.window_id)
    if window is None or window.flow != COMMIT_THEN_ACCEPT or window.status != "AWAITING_PAYMENT":
        raise conflict("TRANCHE_NOT_SIGNABLE", "This ticket has no loan contract to sign")
    cutoffs = window_cutoffs(window)
    if cutoffs is not None and now >= cutoffs.pay_deadline_at:
        raise conflict("CONTRACT_DEADLINE_PASSED", "Signing closed at 14:45 on disbursement day")
    if tranche.status != "RESERVED":
        raise conflict("TRANCHE_NOT_SIGNABLE", "This loan contract is already signed or no longer open")
    contract = await contract_for(db, tranche)
    if contract is None:
        raise conflict("CONTRACT_UNAVAILABLE", "Your loan contract is not ready yet")
    return window, contract


async def contract_document(db: AsyncSession, tranche: Tranche, storage: EvidenceStorage) -> tuple[bytes, str]:
    """The Investor's own copy: the SME's signed copy once on file, else the one generated on Day 13."""
    contract = await contract_for(db, tranche)
    if contract is None:
        raise conflict("CONTRACT_UNAVAILABLE", "Your loan contract is not ready yet")
    key = contract.sme_signed_file_key or contract.file_key
    content = await storage.get(key, max_bytes=MAX_BYTES)
    return content, f"FundLok-hop-dong-cho-vay-{contract.contract_no.split('/')[0]}.pdf"


def _hash(row: AgreementSigningCode, code: str) -> str:
    return agreement._hash_code(row, code)


async def send_code(db: AsyncSession, *, investor_id: UUID, tranche: Tranche, now: dt.datetime) -> AgreementSigningCode:
    """A fresh code by Zalo for this ticket's loan contract. The same limits as the agreement's code (shared per
    investor): 429 CODE_COOLDOWN / CODE_DAILY_LIMIT; 409 PHONE_REQUIRED; 400 PHONE_NO_ZALO; 502 CODE_NOT_SENT; 503
    CODE_UNAVAILABLE."""
    await _signable(db, tranche, now)
    phone = await agreement.signing_phone(db, investor_id)
    if phone is None:
        raise conflict(
            "PHONE_REQUIRED", "Add a Vietnamese mobile number to your profile: the code is sent to it by Zalo"
        )
    await advisory_xact_lock(db, f"investor-agreement-code:{investor_id}")
    sends, first, last = (
        await db.execute(
            select(
                func.count(AgreementSigningCode.id),
                func.min(AgreementSigningCode.created_at),
                func.max(AgreementSigningCode.created_at),
            ).where(
                AgreementSigningCode.investor_id == investor_id,
                AgreementSigningCode.created_at >= now - dt.timedelta(days=1),
            )
        )
    ).one()
    if last is not None and now - last < agreement.RESEND_COOLDOWN:
        raise agreement._too_many(
            "CODE_COOLDOWN", "Please wait before requesting another code", agreement.RESEND_COOLDOWN - (now - last)
        )
    if sends >= agreement.MAX_CODES_PER_DAY:
        raise agreement._too_many(
            "CODE_DAILY_LIMIT", "Too many codes requested today. Try again later.", first + dt.timedelta(days=1) - now
        )
    code = phone_codes._new_code()
    try:
        channel = phone_codes._channel()
        row = AgreementSigningCode(
            id=uuid4(), investor_id=investor_id, tranche_id=tranche.id, window_id=tranche.window_id, purpose=PURPOSE,
            version=VERSION, phone=phone, channel=channel, expires_at=now + agreement.CODE_TTL, created_at=now,
        )
        row.code_hash = _hash(row, code)
        db.add(row)
        await db.flush()
        if channel == "zalo":
            await zalo.send_otp(zalo_number(phone), code, tracking_id=row.id.hex)
        else:
            agreement.logger.warning(
                "Loan contract code for %s (ticket %s): %s (PHONE_OTP_CHANNEL=log)", phone, tranche.reference_code, code
            )
    except zalo.ZaloNotConfigured as exc:
        await db.rollback()
        raise DirectTransferError(503, "The code is not available right now", "CODE_UNAVAILABLE") from exc
    except zalo.ZaloError as exc:
        await db.rollback()
        if exc.code in phone_codes.NO_ZALO_ERRORS:
            raise DirectTransferError(
                400, "Your phone number has no Zalo account that can receive the code", "PHONE_NO_ZALO"
            ) from exc
        raise DirectTransferError(502, "Could not send the code. Please try again.", "CODE_NOT_SENT") from exc
    return row


async def _redeem(db: AsyncSession, *, investor_id: UUID, tranche: Tranche, code: str, now: dt.datetime):
    await advisory_xact_lock(db, f"investor-agreement-code:{investor_id}")
    row = (
        await db.execute(
            select(AgreementSigningCode)
            .where(AgreementSigningCode.investor_id == investor_id)
            .order_by(AgreementSigningCode.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if (
        row is None
        or row.consumed_at is not None
        or row.expires_at <= now
        or row.attempts >= agreement.MAX_ATTEMPTS_PER_CODE
        or row.purpose != PURPOSE
        or row.tranche_id != tranche.id
        or row.version != VERSION
    ):
        raise bad_request("This code has expired. Request a new one.", "CODE_EXPIRED")
    if not hmac.compare_digest(row.code_hash, _hash(row, (code or "").strip())):
        row.attempts += 1
        exhausted = row.attempts >= agreement.MAX_ATTEMPTS_PER_CODE
        exc = bad_request(
            "Too many incorrect attempts. Request a new code." if exhausted else "Incorrect code",
            "CODE_TOO_MANY_ATTEMPTS" if exhausted else "CODE_INCORRECT",
        )
        exc.persist = True
        raise exc
    row.consumed_at = now
    await db.flush()
    return row


async def sign_contract(
    db: AsyncSession,
    *,
    investor_id: UUID,
    tranche: Tranche,
    code: str,
    click_to_sign: bool,
    storage: EvidenceStorage,
    now: dt.datetime,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> Tranche:
    """The Investor signs their loan contract with the code and a logged click; on their first Day 14 the same code
    confirms the Investor Service Agreement. The ticket goes SIGNED and its VietQR opens. 400 CLICK_REQUIRED; the
    code errors as for the agreement; 409 TRANCHE_NOT_SIGNABLE / CONTRACT_DEADLINE_PASSED / CONTRACT_UNAVAILABLE."""
    if not click_to_sign:
        raise bad_request("Tick that you sign the loan contract", "CLICK_REQUIRED")
    tranche = (await db.execute(select(Tranche).where(Tranche.id == tranche.id).with_for_update())).scalar_one()
    window, contract = await _signable(db, tranche, now)
    row = await _redeem(db, investor_id=investor_id, tranche=tranche, code=code, now=now)

    signed_copy = await storage.get(contract.sme_signed_file_key or contract.file_key, max_bytes=MAX_BYTES)
    digest = hashlib.sha256(signed_copy).hexdigest()
    masked = agreement.masked_phone(row.phone)
    signature = {
        "signed_by": "ZALO_OTP",
        "click_to_sign": True,
        "code_id": str(row.id),
        "code_sent_at": row.created_at.isoformat(),
        "phone": row.phone,
        "phone_masked": masked,
        "channel": row.channel,
        "document_sha256": digest,
        "contract_no": contract.contract_no,
        "check_code": contract.check_code,
        "signed_at": now.isoformat(),
        "ip_address": ip_address,
        "user_agent": (user_agent or "")[:500] or None,
    }
    contract.investor_signed_at = now
    contract.investor_signed_file_key = contract.sme_signed_file_key or contract.file_key
    contract.status = "INVESTOR_SIGNED"

    # The ticket's acceptance is this signature (commit-then-accept-flow.md section 3: the acceptance_* columns hold
    # the Day-14 loan contract signature).
    tranche.acceptance_file_key = contract.investor_signed_file_key
    tranche.acceptance_sha256 = digest
    tranche.acceptance_certificate = {"signed_by": "ZALO_OTP", "code_id": str(row.id), "phone_masked": masked}

    # Plus the Investor Service Agreement the first time, with the same code; later, this ticket's Annex.
    current = await agreement.current_signature(db, investor_id)
    if current is None:
        current = await agreement.record_agreement(
            db, investor_id=investor_id, tranche=tranche, code_row=row, storage=storage, now=now,
            ip_address=ip_address, user_agent=user_agent,
        )
        signature["investor_agreement_id"] = str(current.id)
    else:
        await agreement.record_confirmation(db, tranche=tranche, window=window, agreement=current, storage=storage, now=now)
    contract.investor_signature = dict(signature)  # assigned once, complete (a JSONB mutation would not be saved)

    await transition_tranche(db, tranche, "SIGNED", now=now, actor_id=investor_id, actor_role="INVESTOR")
    await log_event(
        db, entity_type="WINDOW_LOAN_CONTRACT", entity_id=contract.id, event_type="LOAN_CONTRACT_INVESTOR_SIGNED",
        now=now, actor_id=investor_id, actor_role="INVESTOR", file_sha256=digest,
        payload={"tranche_id": str(tranche.id), "contract_no": contract.contract_no, "code_id": str(row.id)},
    )
    append_audit(
        db, entity_type="TRANCHE", entity_id=tranche.id, action="LOAN_CONTRACT_SIGNED", actor_id=investor_id,
        after_state={k: v for k, v in signature.items() if k not in ("phone",)}, ip_address=ip_address,
    )
    await db.flush()
    return tranche
