"""Day 13 of a commit-then-accept round: each Investor's loan contract, the SME's signature, the guarantor's.

Spec: docs/specs/direct_transfer/day13-loan-contracts.md (Money flow FINAL v1.0, section 1 Day 13 and the ops rule
"Day-13 signing cut-off").

  00:00 Day 13   The pledge list is final (commit_flow ends the commit phase). One loan contract per lender is
                 generated from it, for the lender's pledged total (`generate_for_window`).
  by 15:00       The SME accepts or declines. Accepting means signing: the SME signs every lender's contract with its
                 enterprise digital signature and uploads the signed copies (`record_sme_signed`, NEAC-checked); an
                 explicit ACCEPT is refused until every contract is signed (`all_sme_signed`). Below 50% of target
                 the SME must finish signing by 12:00, so acceptance completes before 15:00 (`signing_cutoff`).
                 Silence at 15:00 with 50% or more counts as accepted and binds the SME without signing.
  any time       The Personal Guarantor signs every contract with one Zalo code (`sign_as_guarantor`); not a
                 condition of acceptance.

On a decline (or auto-decline) the contracts are VOID. The Investor signs their own copy on Day 14 (next PR).

This module sits in app/market because it joins app/direct_transfer (the round, Edward's) with app/loans (the
contract text and the NEAC check, Phat's); app/direct_transfer imports neither.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import io
import logging
from uuid import UUID

from fastapi import HTTPException
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import ICT
from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.commit_flow import COMMIT_THEN_ACCEPT, pledged_total
from app.direct_transfer.evidence import log_event, store_evidence_file
from app.direct_transfer.models import Commitment, FundingWindow, WindowLoanContract
from app.direct_transfer.storage import EvidenceStorage
from app.lending.models import Contract, LoanApplication, Project
from app.loans import guarantor_otp, signing
from app.loans.agreement import loan_contract
from app.loans.agreement import loan_contract_text as contract_text
from app.loans.agreement.pdf import SME_SIGNATURE_FIELD
from app.loans.agreement.signature import has_digital_signature, is_pdf
from app.loans.agreement.standing_offer import guarantor_for, offer_data
from app.loans.guarantor_models import PURPOSE_WINDOW_LOAN_CONTRACTS, GuarantorSignature
from app.market.investor_agreement import service as investor_agreement
from app.users.models import User
from app.utils.audit import append_audit

logger = logging.getLogger(__name__)

MAX_BYTES = 10 * 1024 * 1024  # the evidence store's limit
LIVE = ("GENERATED", "SME_SIGNED", "INVESTOR_SIGNED")
# The ops rule's cut-off for a round below 50% of target: signing must be finished by 12:00 on Day 13.
SIGNING_CUTOFF = dt.time(12, 0)

WINDOW_CONTRACT = signing.Kind(
    "window-loan-contract", "window_loan_contract", contract_text.VERSION, False, (SME_SIGNATURE_FIELD,)
)


def _refuse(status_code: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail, headers={"X-Error-Code": code})


# --------------------------------------------------------------------------- #
# the round and its parties
# --------------------------------------------------------------------------- #
async def _application(db: AsyncSession, window: FundingWindow) -> LoanApplication:
    contract = await db.get(Contract, window.contract_id)
    application = await db.get(LoanApplication, contract.application_id) if contract and contract.application_id else None
    if application is None:
        raise _refuse(409, "CONTRACTS_UNAVAILABLE", "This round has no approved application to make contracts from")
    return application


def signing_cutoff(window: FundingWindow, pledged_vnd: int) -> dt.datetime | None:
    """12:00 on Day 13 when the pledges are below 50% of target (the SME must sign to accept at all); None at 50%
    or more (silence accepts, and signing stays open to the 15:00 deadline)."""
    if window.sme_deadline_at is None or pledged_vnd >= window.min_raise_vnd:
        return None
    day13 = window.sme_deadline_at.astimezone(ICT).date()
    return dt.datetime.combine(day13, SIGNING_CUTOFF, tzinfo=ICT)


async def contracts_for(db: AsyncSession, window_id: UUID, *, live_only: bool = True) -> list[WindowLoanContract]:
    query = select(WindowLoanContract).where(WindowLoanContract.window_id == window_id)
    if live_only:
        query = query.where(WindowLoanContract.status.in_(LIVE))
    return list((await db.execute(query.order_by(WindowLoanContract.contract_no))).scalars().all())


async def all_sme_signed(db: AsyncSession, window_id: UUID) -> bool:
    """Every live contract carries the SME's verified signature (and there is at least one)."""
    rows = await contracts_for(db, window_id)
    return bool(rows) and all(r.status in ("SME_SIGNED", "INVESTOR_SIGNED") for r in rows)


async def guarantor_signature(db: AsyncSession, window_id: UUID) -> GuarantorSignature | None:
    return await guarantor_otp.signature_for(db, PURPOSE_WINDOW_LOAN_CONTRACTS, window_id)


# --------------------------------------------------------------------------- #
# 00:00 Day 13: generate
# --------------------------------------------------------------------------- #
async def _copy_for(
    db: AsyncSession, window: FundingWindow, ref: str, seq: int, investor_id: UUID, amount_vnd: int
) -> loan_contract.InvestorCopy:
    party = await investor_agreement.current_party_b(db, investor_id)
    return loan_contract.InvestorCopy(
        number=contract_text.INVESTOR_NUMBER.format(ref=ref, seq=seq),
        lender_name=party.name,
        lender_id_number=party.id_number,
        lender_account=party.bank_account_number,
        lender_bank=party.bank_name,
        principal_vnd=amount_vnd,
        rate_pct=window.annual_rate_pct,
        term_months=window.term_months,
        disbursement_date=window.pay_deadline_at.astimezone(ICT).date(),
    )


async def render_copy(
    db: AsyncSession, window: FundingWindow, row: WindowLoanContract, *, now: dt.datetime
) -> tuple[bytes, str]:
    """The contract as generated for `row`'s lender (pdf, check code)."""
    application = await _application(db, window)
    sme = await db.get(User, window.sme_user_id)
    data = await offer_data(db, application, sme)
    signed_on = await loan_contract.offer_signed_on(db, application)
    seq = int(row.contract_no.split("/")[0].rsplit("-", 1)[1])
    copy = await _copy_for(db, window, data.sme.application_ref, seq, row.investor_id, row.amount_vnd)
    pdf = loan_contract.render(data, ict_date(now), signed_on, copy)
    return pdf, loan_contract.investor_check_code(data, signed_on, copy)


async def generate_for_window(
    db: AsyncSession, *, window: FundingWindow, storage: EvidenceStorage, now: dt.datetime
) -> int:
    """One loan contract per lender with PLEDGED commitments, for its total, once the pledge list is final
    (AWAITING_SME). Idempotent: a lender who already has one is skipped. Returns how many were made."""
    if window.flow != COMMIT_THEN_ACCEPT or window.status != "AWAITING_SME":
        return 0
    pledges = (
        await db.execute(
            select(Commitment)
            .where(Commitment.window_id == window.id, Commitment.status == "PLEDGED")
            .order_by(Commitment.committed_at, Commitment.created_at, Commitment.id)
        )
    ).scalars().all()
    totals: dict[UUID, int] = {}
    for c in pledges:  # insertion order = each lender's first pledge: their sequence number
        totals[c.investor_id] = totals.get(c.investor_id, 0) + c.amount_vnd
    have = {r.investor_id for r in await contracts_for(db, window.id, live_only=False)}
    if not totals or set(totals) <= have:
        return 0

    application = await _application(db, window)
    sme = await db.get(User, window.sme_user_id)
    data = await offer_data(db, application, sme)
    signed_on = await loan_contract.offer_signed_on(db, application)
    made = 0
    for seq, (investor_id, amount) in enumerate(totals.items(), start=1):
        if investor_id in have:
            continue
        copy = await _copy_for(db, window, data.sme.application_ref, seq, investor_id, amount)
        pdf = loan_contract.render(data, ict_date(now), signed_on, copy)
        stored = await store_evidence_file(
            db, storage, owner_user_id=window.sme_user_id, purpose="SIGNED_DOCUMENT", content=pdf,
            content_type="application/pdf", filename=f"hop-dong-cho-vay-{copy.number.split('/')[0]}.pdf",
        )
        row = WindowLoanContract(
            window_id=window.id,
            investor_id=investor_id,
            contract_no=copy.number,
            amount_vnd=amount,
            status="GENERATED",
            file_key=stored.storage_key,
            sha256=stored.sha256,
            check_code=loan_contract.investor_check_code(data, signed_on, copy),
            generated_at=now,
        )
        db.add(row)
        await db.flush()
        await log_event(
            db, entity_type="WINDOW_LOAN_CONTRACT", entity_id=row.id, event_type="LOAN_CONTRACT_GENERATED", now=now,
            file_sha256=stored.sha256,
            payload={"window_id": str(window.id), "contract_no": row.contract_no, "amount_vnd": amount},
        )
        made += 1
    return made


async def void_for_window(db: AsyncSession, *, window: FundingWindow, now: dt.datetime) -> int:
    """A declined round's contracts are VOID (no loan was concluded)."""
    if window.status != "FAILED":
        return 0
    voided = 0
    for row in await contracts_for(db, window.id):
        row.status = "VOID"
        row.void_reason = window.close_reason or "DECLINED"
        voided += 1
    if voided:
        await db.flush()
        await log_event(
            db, entity_type="WINDOW", entity_id=window.id, event_type="LOAN_CONTRACTS_VOIDED", now=now,
            payload={"count": voided, "reason": window.close_reason},
        )
    return voided


async def after_advance(db: AsyncSession, *, window: FundingWindow, storage: EvidenceStorage, now: dt.datetime) -> None:
    """What the round's new state asks of its contracts: generate on AWAITING_SME, void on a decline."""
    if window.flow != COMMIT_THEN_ACCEPT:
        return
    await generate_for_window(db, window=window, storage=storage, now=now)
    await void_for_window(db, window=window, now=now)


# --------------------------------------------------------------------------- #
# the SME's signature
# --------------------------------------------------------------------------- #
def _text_of(content: bytes) -> str:
    try:
        return "\n".join((page.extract_text() or "") for page in PdfReader(io.BytesIO(content)).pages)
    except Exception:
        return ""


async def _signing_open(db: AsyncSession, window: FundingWindow, now: dt.datetime) -> None:
    if window.flow != COMMIT_THEN_ACCEPT or window.status != "AWAITING_SME":
        raise _refuse(409, "CONTRACT_SIGNING_NOT_OPEN", "Loan contracts are signed on Day 13, before the decision")
    if now >= window.sme_deadline_at:
        raise _refuse(409, "DECISION_DEADLINE_PASSED", "The 15:00 deadline has passed; the automatic result stands")
    cutoff = signing_cutoff(window, await pledged_total(db, window.id))
    if cutoff is not None and now >= cutoff:
        raise _refuse(
            409, "SIGNING_CUTOFF_PASSED",
            "Below 50% of the target, signing closes at 12:00 on Day 13 so acceptance completes before 15:00",
        )


async def record_sme_signed(
    db: AsyncSession,
    *,
    window: FundingWindow,
    row: WindowLoanContract,
    content: bytes,
    file_name: str | None,
    storage: EvidenceStorage,
    now: dt.datetime,
    actor: User,
    ip_address: str | None = None,
) -> WindowLoanContract:
    """Check and keep the SME's signed copy of one lender's contract.

    409 CONTRACT_SIGNING_NOT_OPEN / DECISION_DEADLINE_PASSED / SIGNING_CUTOFF_PASSED; 409 SIGNING_ALREADY_SIGNED;
    413 SIGNING_FILE_TOO_LARGE; 415 SIGNING_NOT_PDF; 422 SIGNING_UNSIGNED, SIGNING_WRONG_DOCUMENT (not this
    contract), SIGNING_DOCUMENT_OUTDATED (its check code is not this contract's), and NEAC's refusals as for the
    listing documents (SIGNING_SIGNATURE_INVALID, SIGNING_SIGNER_MISMATCH, 503 NEAC_UNAVAILABLE)."""
    await _signing_open(db, window, now)
    if row.status != "GENERATED":
        raise _refuse(409, "SIGNING_ALREADY_SIGNED", "This contract is already signed")
    if len(content) > MAX_BYTES:
        raise _refuse(413, "SIGNING_FILE_TOO_LARGE", "The signed PDF is larger than 10 MB")
    if not is_pdf(content):
        raise _refuse(415, "SIGNING_NOT_PDF", "Upload the signed contract as a PDF")
    if not has_digital_signature(content):
        raise _refuse(422, "SIGNING_UNSIGNED", "The PDF has no digital signature; sign it with the enterprise signature")
    body = _text_of(content)
    if row.contract_no not in " ".join(body.split()) and row.contract_no not in body:
        raise _refuse(
            422, "SIGNING_WRONG_DOCUMENT", f"This is not contract {row.contract_no}. Download it, sign it and upload it."
        )
    if row.check_code not in body:
        raise _refuse(
            422, "SIGNING_DOCUMENT_OUTDATED",
            f"The signed copy is not contract {row.contract_no} as generated (check code {row.check_code}).",
        )
    project = await db.get(Project, window.project_id)
    company_id, company_name, company_source = await signing._company(db, project)
    verdict = await signing._neac_verdict(
        content, file_name, kind=WINDOW_CONTRACT, company_id=company_id, company_name=company_name,
        company_source=company_source, guarantor_id=None, guarantor_name=None,
    )
    # Serialised per contract after the slow NEAC call.
    row = (
        await db.execute(select(WindowLoanContract).where(WindowLoanContract.id == row.id).with_for_update())
    ).scalar_one()
    if row.status != "GENERATED":
        raise _refuse(409, "SIGNING_ALREADY_SIGNED", "This contract is already signed")
    stored = await store_evidence_file(
        db, storage, owner_user_id=window.sme_user_id, purpose="SIGNED_DOCUMENT", content=content,
        content_type="application/pdf", filename=f"hop-dong-cho-vay-{row.contract_no.split('/')[0]}-da-ky.pdf",
    )
    row.sme_signed_file_key = stored.storage_key
    row.sme_signed_sha256 = stored.sha256
    row.sme_signed_at = now
    row.sme_signature_check = {**verdict, "check_code": row.check_code, "checked_at": now.isoformat()}
    row.status = "SME_SIGNED"
    await db.flush()
    await log_event(
        db, entity_type="WINDOW_LOAN_CONTRACT", entity_id=row.id, event_type="LOAN_CONTRACT_SME_SIGNED", now=now,
        actor_id=actor.id, actor_role="SME", file_sha256=stored.sha256,
        payload={"window_id": str(window.id), "contract_no": row.contract_no},
    )
    append_audit(
        db, entity_type="WINDOW_LOAN_CONTRACT", entity_id=row.id, action="NEAC_SIGNATURE_CHECKED", actor_id=actor.id,
        after_state={**verdict, "contract_no": row.contract_no, "check_code": row.check_code,
                     "checked_at": now.isoformat(), "trigger": "SME_SIGNED_UPLOAD"},
        ip_address=ip_address,
    )
    if await all_sme_signed(db, window.id):
        # Staff log the completion time (ops rule): kept as its own event.
        await log_event(
            db, entity_type="WINDOW", entity_id=window.id, event_type="LOAN_CONTRACTS_SIGNING_COMPLETED", now=now,
            actor_id=actor.id, actor_role="SME",
        )
    return row


# --------------------------------------------------------------------------- #
# the Personal Guarantor's Zalo-code signature (not a condition of acceptance)
# --------------------------------------------------------------------------- #
async def _guarantor_step(db: AsyncSession, window: FundingWindow, user: User):
    if window.flow != COMMIT_THEN_ACCEPT or window.status not in ("AWAITING_SME", "AWAITING_PAYMENT"):
        raise _refuse(409, "CONTRACT_SIGNING_NOT_OPEN", "The guarantor signs the loan contracts on Day 13")
    if await guarantor_signature(db, window.id) is not None:
        raise _refuse(409, "GUARANTOR_ALREADY_SIGNED", "The Personal Guarantor has already signed these contracts")
    rows = await contracts_for(db, window.id)
    if not rows:
        raise _refuse(409, "CONTRACTS_UNAVAILABLE", "The loan contracts are not ready yet")
    guarantor = await guarantor_for(db, user, await _application(db, window))
    if guarantor.id_number is None:
        raise _refuse(409, "SIGNING_GUARANTOR_NOT_READY", "The Personal Guarantor needs an approved eKYC first")
    return guarantor, rows


async def send_guarantor_code(db: AsyncSession, *, window: FundingWindow, user: User, now: dt.datetime):
    await _guarantor_step(db, window, user)
    return await guarantor_otp.send_code(
        db, user=user, purpose=PURPOSE_WINDOW_LOAN_CONTRACTS, subject_id=window.id, now=now
    )


async def sign_as_guarantor(
    db: AsyncSession, *, window: FundingWindow, user: User, code: str, now: dt.datetime,
    ip_address: str | None = None, user_agent: str | None = None,
) -> GuarantorSignature:
    """The guarantor signs every live contract of the round with one Zalo code; each contract's number, check code
    and the hash of the copy signed (the SME's signed copy when on file, else the generated one) are kept."""
    guarantor, rows = await _guarantor_step(db, window, user)
    code_row = await guarantor_otp.redeem_code(
        db, user=user, purpose=PURPOSE_WINDOW_LOAN_CONTRACTS, subject_id=window.id, code=code, now=now
    )
    documents = [
        guarantor_otp.SignedDocument(
            document_type="window_loan_contract",
            reference=r.contract_no,
            sha256=r.sme_signed_sha256 or r.sha256,
            check_code=r.check_code,
        )
        for r in rows
    ]
    signature = await guarantor_otp.record_signature(
        db, user=user, purpose=PURPOSE_WINDOW_LOAN_CONTRACTS, subject_id=window.id, code_row=code_row,
        signer_name=guarantor.name, signer_id_number=guarantor.id_number, documents=documents, now=now,
        ip_address=ip_address, user_agent=user_agent,
    )
    await log_event(
        db, entity_type="WINDOW", entity_id=window.id, event_type="LOAN_CONTRACTS_GUARANTOR_SIGNED", now=now,
        actor_id=user.id, actor_role="SME", payload={"contracts": [r.contract_no for r in rows]},
    )
    return signature


# --------------------------------------------------------------------------- #
# the SME's view
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class ContractsView:
    window_id: UUID
    status: str
    pledged_vnd: int
    target_vnd: int
    sme_deadline_at: dt.datetime | None
    signing_cutoff_at: dt.datetime | None
    all_signed: bool
    contracts: list[WindowLoanContract]
    lender_names: dict[UUID, str]
    guarantor_signed_at: dt.datetime | None
    guarantor_phone: str | None


async def view_for_sme(db: AsyncSession, *, window: FundingWindow, sme: User) -> ContractsView:
    from app.direct_transfer.tranches import mask_name

    rows = await contracts_for(db, window.id, live_only=False)
    names = {}
    for r in rows:
        party = await investor_agreement.current_party_b(db, r.investor_id)
        names[r.investor_id] = mask_name(party.name or "")
    pledged = await pledged_total(db, window.id)
    if window.status not in ("OPEN", "AWAITING_SME"):
        pledged = sum(r.amount_vnd for r in rows if r.status != "VOID")
    signature = await guarantor_signature(db, window.id)
    phone = guarantor_otp.guarantor_phone(sme)
    live = [r for r in rows if r.status != "VOID"]
    return ContractsView(
        window_id=window.id,
        status=window.status,
        pledged_vnd=pledged,
        target_vnd=window.target_vnd,
        sme_deadline_at=window.sme_deadline_at,
        signing_cutoff_at=signing_cutoff(window, pledged),
        all_signed=bool(live) and all(r.status in ("SME_SIGNED", "INVESTOR_SIGNED") for r in live),
        contracts=rows,
        lender_names=names,
        guarantor_signed_at=signature.signed_at if signature else None,
        guarantor_phone=guarantor_otp.masked_phone(phone) if phone else None,
    )


def sha256_of(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
