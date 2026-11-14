"""Funding windows, investor side (HANDOFF-04 Epic 1, tickets 1.1, 1.3, 1.4 and 1.5).

Edward's services hold the rules (app/direct_transfer); this router adds auth, the investor eligibility gate,
auditing and the HTTP error mapping. Reserving and signing the acceptance are one request here (the investor agrees
once), not the spec's two; see app/market/funding.py.
"""
import datetime as dt
from uuid import UUID

from decimal import Decimal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.direct_transfer.close import request_early_close
from app.direct_transfer.config import DEFAULT_PARAMS
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.models import FundingWindow, RepaymentDay, Tranche, Transfer
from app.direct_transfer.receipts import ManualEntryExtractor
from app.direct_transfer.schemas import FundingWindowOut, PaymentOut, ReceiptResultOut
from app.direct_transfer.tranches import (
    list_investor_tranches,
    list_window_tranches,
    mask_name,
    payment_instructions,
    sme_dispute_tranche,
    submit_receipt,
)
from app.direct_transfer.windows import get_window_progress
from app.lending.models import Contract, LoanApplication, Project
from app.loans import signing as listing_signing
from app.market import funding
from app.market.emails import receipt_notice, send_investment_receipt_email
from app.market.funding import expire_lapsed_holds, reserve_and_accept
from app.market.investor_agreement import service as investor_agreement
from app.market.investor_agreement.models import InvestorServiceAgreement
from app.market.portfolio import business_analytics, business_transactions, investor_analytics, investor_transactions
from app.market.projection import project_ticket, sum_by_day
from app.market.schemas import (
    AgreementCodeOut,
    AgreementCodeRequest,
    AgreementSignRequest,
    BusinessAnalyticsOut,
    InvestmentConfirmationOut,
    InvestorAgreementOut,
    InvestorAnalyticsOut,
    InvestorTrancheOut,
    PartyBOut,
    SignatureCheckOut,
    SignedInvestmentConfirmationOut,
    ReportMissingCreate,
    ReservationCreate,
    SmeRepaymentPlanOut,
    SmeRoundOut,
    SmeTicketOut,
    TransactionOut,
)
from app.market.service import _require_eligible_investor
from app.notifications import realtime
from app.notifications.models import EVENT_INVESTMENT_RECEIPT_SUBMITTED
from app.notifications.service import create_notification
from app.users.models import Role, User
from app.utils.audit import append_audit
from app.utils.jwt import get_current_user
from app.utils.rbac import require_roles

router = APIRouter(prefix="/funding", tags=["funding"])

require_investor = require_roles(Role.INVESTOR)
require_sme = require_roles(Role.SME)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def _raise_mapped(db: AsyncSession, exc: DirectTransferError) -> None:
    # A service that wrote an evidence row before failing marks the error `persist`; that row must survive.
    if exc.persist:
        await db.commit()
    headers = {"X-Error-Code": exc.code}
    retry_after = getattr(exc, "retry_after", None)  # set on 429s (investor_agreement.send_signing_code)
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers=headers) from exc


@router.get("/listings/{listing_id}/window", response_model=FundingWindowOut)
async def read_window(
    listing_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Progress of the listing's funding window: totals only, never another investor's name or amount."""
    now = _now()
    # Lapsed holds are released first, so the capacity shown is what a reservation would get.
    window_id = (
        await db.execute(select(FundingWindow.id).where(FundingWindow.listing_id == listing_id))
    ).scalar_one_or_none()
    if window_id is not None and await expire_lapsed_holds(db, window_id=window_id, now=now):
        await db.commit()
    try:
        progress = await get_window_progress(db, listing_id=listing_id, now=now)
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    return FundingWindowOut.model_validate(progress)


@router.post("/windows/{window_id}/reservations", response_model=InvestorTrancheOut, status_code=201)
async def reserve(
    window_id: UUID,
    body: ReservationCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Hold an amount on the window and sign the acceptance of the investing terms. The tranche comes back SIGNED;
    GET /funding/tranches/{id}/payment then gives the SME's account to pay into."""
    # Verified email, approved KYC and an approved bank account: the same gate as the legacy order path.
    await _require_eligible_investor(db, current_user.id)
    replay = idempotency_key is not None and (
        await db.execute(select(Tranche.id).where(Tranche.idempotency_key == idempotency_key))
    ).first() is not None
    try:
        tranche = await reserve_and_accept(
            db,
            window_id=window_id,
            investor_id=current_user.id,
            amount_vnd=body.amount_vnd,
            ack_risk_disclosure=body.ack_risk_disclosure,
            terms_version=body.terms_version,
            idempotency_key=idempotency_key or "",
            now=_now(),
            payment_account_id=body.payment_account_id,
        )
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    if not replay:
        agreement = await investor_agreement.current_signature(db, current_user.id)  # None: signed next
        append_audit(
            db,
            entity_type="TRANCHE",
            entity_id=tranche.id,
            action="INVESTMENT_RESERVED",
            actor_id=current_user.id,
            after_state={
                "window_id": str(tranche.window_id),
                "amount_vnd": tranche.amount_vnd,
                "reference_code": tranche.reference_code,
                "terms_version": body.terms_version,
                "status": tranche.status,
                # The signed master agreement this commitment (and its Investment Confirmation) falls under; at
                # the first commitment none yet, signed with it before any payment.
                "investor_agreement_id": str(agreement.id) if agreement else None,
                "investor_agreement_version": agreement.version if agreement else None,
            },
            ip_address=request.client.host if request.client else None,
        )
    await db.commit()
    return InvestorTrancheOut.model_validate(tranche)


async def _agreement_status(db: AsyncSession, investor_id: UUID) -> InvestorAgreementOut:
    signed = await investor_agreement.current_signature(db, investor_id)
    latest = signed or await investor_agreement.latest_signature(db, investor_id)
    if signed is not None:
        party = PartyBOut.model_validate(signed.party_b)
    else:
        party = PartyBOut.model_validate((await investor_agreement.current_party_b(db, investor_id)).as_dict())
    phone = await investor_agreement.signing_phone(db, investor_id)
    return InvestorAgreementOut(
        version=investor_agreement.VERSION,
        required=signed is None,
        phone=investor_agreement.masked_phone(phone) if phone else None,
        signed_at=signed.signed_at if signed else None,
        signed_version=latest.version if latest else None,
        file_name=signed.file_name if signed else None,
        tranche_id=signed.tranche_id if signed else None,
        signature_check=_signature_check(signed.signature_check) if signed else None,
        party_b=party,
    )


def _signature_check(check: dict | None) -> SignatureCheckOut | None:
    if not check:
        return None
    signer, issuer = check.get("signer") or {}, check.get("issuer") or {}
    return SignatureCheckOut(
        verified_by=check.get("verified_by", "NEAC"),
        checked_at=check["checked_at"],
        phone=check.get("phone_masked"),
        signer_name=signer.get("cn"),
        signer_id=signer.get("cccd") or signer.get("mst"),
        issuer_name=issuer.get("cn"),
        signed_time=check.get("signed_time"),
        name_matches=bool(check.get("name_matches")),
    )


def _pdf_response(content: bytes, filename: str, download: bool) -> Response:
    disposition = "attachment" if download else "inline"
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.get("/me/investor-agreement", response_model=InvestorAgreementOut)
async def read_my_investor_agreement(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """Whether the investor still confirms the Investor Service Agreement with their next commitment (their first,
    or the first after a new version) before its payment details open; the number the confirmation code goes to;
    and Party B: as confirmed, or as the PDF fills it in now."""
    return await _agreement_status(db, current_user.id)


@router.get(
    "/me/investor-agreement/document",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def read_my_investor_agreement_document(
    tranche_id: UUID | None = Query(None, description="the commitment it is signed with: fills in its Annex"),
    download: bool = Query(False, description="true: as an attachment, to save"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """The Investor Service Agreement (Contract 4) as a PDF, the current version filled in with the investor's
    verified details and, given `tranche_id`, that commitment's code and Annex: for them to read before confirming
    with the one-time code. Generated each time; the copy kept as the record is made when they confirm."""
    tranche = await _own_tranche(db, tranche_id, current_user.id) if tranche_id else None
    pdf, filename = await investor_agreement.build_pdf(db, current_user.id, now=_now(), tranche=tranche)
    return _pdf_response(pdf, filename, download)


@router.post("/me/investor-agreement/code", response_model=AgreementCodeOut)
async def send_my_investor_agreement_code(
    body: AgreementCodeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """Send the one-time code that confirms the agreement with the commitment `tranche_id` (theirs, its hold not
    lapsed): by Zalo, to the phone number on the investor's account. One a minute, five a day; good for five
    minutes; a new code retires the last."""
    await _require_eligible_investor(db, current_user.id)
    tranche = await _own_tranche(db, body.tranche_id, current_user.id)
    try:
        row = await investor_agreement.send_signing_code(
            db, investor_id=current_user.id, tranche=tranche, version=body.version, now=_now()
        )
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    out = AgreementCodeOut(
        phone=investor_agreement.masked_phone(row.phone),
        channel=row.channel,
        expires_in=int(investor_agreement.CODE_TTL.total_seconds()),
        resend_in=int(investor_agreement.RESEND_COOLDOWN.total_seconds()),
    )
    await db.commit()
    return out


@router.post("/me/investor-agreement", response_model=InvestorAgreementOut, status_code=201)
async def sign_my_investor_agreement(
    body: AgreementSignRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """Confirm the agreement with the commitment `tranche_id` (theirs, its hold not lapsed) by entering the
    one-time code sent to the investor's phone: the code is their signature. `version` is the one the status gave.
    Keeps the confirmed PDF as evidence and that commitment's Annex, and opens its payment details. One per
    version."""
    # The agreement names a verified Party B: the same gate as reserving.
    await _require_eligible_investor(db, current_user.id)
    tranche = await _own_tranche(db, body.tranche_id, current_user.id)
    now = _now()
    try:
        agreement = await investor_agreement.sign_with_code(
            db,
            investor_id=current_user.id,
            tranche=tranche,
            version=body.version,
            code=body.code,
            storage=funding.evidence_storage(),
            now=now,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    check = agreement.signature_check or {}
    append_audit(
        db,
        entity_type="USER",
        entity_id=current_user.id,
        action="INVESTOR_AGREEMENT_SIGNED",
        actor_id=current_user.id,
        after_state={
            "agreement_id": str(agreement.id),
            "version": agreement.version,
            "tranche_id": str(tranche.id),
            "reference_code": tranche.reference_code,
            "document_sha256": agreement.document_sha256,
            "signed_by": check.get("verified_by"),
            "phone": check.get("phone_masked"),
            "code_id": check.get("code_id"),
        },
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return await _agreement_status(db, current_user.id)


@router.get(
    "/me/investor-agreement/signed",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def read_my_signed_investor_agreement(
    download: bool = Query(False, description="true: as an attachment, to save"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """The current version as the investor confirmed it: the PDF kept when they entered the code, byte for byte."""
    signed = await investor_agreement.current_signature(db, current_user.id)
    if signed is None:
        raise HTTPException(status_code=404, detail="No signed agreement")
    content = await funding.evidence_storage().get(signed.signed_file_key, max_bytes=DEFAULT_PARAMS.max_evidence_bytes)
    filename = (signed.file_name or f"FundLok-hop-dong-dich-vu-nha-dau-tu-{signed.version}.pdf").replace('"', "")
    return _pdf_response(content, filename, download)


@router.get("/windows/{window_id}/investment-confirmation", response_model=InvestmentConfirmationOut)
async def read_investment_confirmation(
    window_id: UUID,
    amount_vnd: int = Query(gt=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """The Investment Confirmation (Annex) the investor signs with a commitment of `amount_vnd` on this window,
    filled in as it will be signed. The amount's bounds are checked when reserving, not here."""
    window = await db.get(FundingWindow, window_id)
    if window is None or window.sme_user_id == current_user.id:
        raise HTTPException(status_code=404, detail="Window not found")
    confirmation = await investor_agreement.investment_confirmation(
        db, window=window, amount_vnd=amount_vnd, pay_by=_now() + dt.timedelta(hours=DEFAULT_PARAMS.hold_hours)
    )
    return InvestmentConfirmationOut.model_validate(confirmation, from_attributes=True)


@router.get(
    "/windows/{window_id}/loan-contract",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def read_window_loan_contract(
    window_id: UUID,
    download: bool = Query(False, description="true: as an attachment, to save"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """The Loan Contract template (Contract 5) the SME and its Personal Guarantor signed for this listing, exactly
    as uploaded: the contract an investor's commitment concludes, shown before they commit. It names the
    Guarantor, so it is given only to an eligible investor (verified, with an approved bank account). 404 when the
    listing has none (one published before listing documents were signed)."""
    await _require_eligible_investor(db, current_user.id)
    window = await db.get(FundingWindow, window_id)
    if window is None or window.sme_user_id == current_user.id:
        raise HTTPException(status_code=404, detail="Window not found")
    contract = await db.get(Contract, window.contract_id)
    application = (
        await db.get(LoanApplication, contract.application_id) if contract and contract.application_id else None
    )
    if application is None:
        raise HTTPException(status_code=404, detail="No signed copy on file")
    content, filename = await listing_signing.signed_copy(db, application, listing_signing.LOAN_CONTRACT)
    append_audit(
        db,
        entity_type="LOAN_APPLICATION",
        entity_id=application.id,
        action="LOAN_CONTRACT_VIEWED_BY_INVESTOR",
        actor_id=current_user.id,
        after_state={"window_id": str(window.id), "download": download},
    )
    await db.commit()
    return _pdf_response(content, filename, download)


async def _own_tranche(db: AsyncSession, tranche_id: UUID, investor_id: UUID) -> Tranche:
    tranche = await db.get(Tranche, tranche_id)
    if tranche is None or tranche.investor_id != investor_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return tranche


async def _own_confirmation(db: AsyncSession, tranche_id: UUID, investor_id: UUID):
    confirmation = await investor_agreement.confirmation_for(db, tranche_id)
    if confirmation is None or confirmation.investor_id != investor_id:
        raise HTTPException(status_code=404, detail="No investment confirmation for this ticket")
    return confirmation


@router.get("/tranches/{tranche_id}/investment-confirmation", response_model=SignedInvestmentConfirmationOut)
async def read_signed_investment_confirmation(
    tranche_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """The Investment Confirmation (Annex) the investor confirmed with this commitment, as filled in then. Their
    own tickets only; 404 for one made before confirmations were kept."""
    confirmation = await _own_confirmation(db, tranche_id, current_user.id)
    tranche = await db.get(Tranche, tranche_id)
    agreement = await db.get(InvestorServiceAgreement, confirmation.agreement_id)
    return SignedInvestmentConfirmationOut(
        id=confirmation.id,
        tranche_id=confirmation.tranche_id,
        reference_code=tranche.reference_code,
        listing_id=confirmation.listing_id,
        listing_ref=str(confirmation.listing_id).split("-")[0].upper(),
        sme_name=confirmation.sme_name,
        amount_vnd=confirmation.amount_vnd,
        annual_rate_pct=confirmation.annual_rate_pct,
        term_months=confirmation.term_months,
        service_fee_vnd=confirmation.service_fee_vnd,
        service_fee_bps_per_year=confirmation.service_fee_bps_per_year,
        expected_disbursement_date=confirmation.expected_disbursement_date,
        confirmed_at=confirmation.confirmed_at,
        agreement_version=agreement.version,
    )


@router.get(
    "/tranches/{tranche_id}/investment-confirmation/document",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def read_signed_investment_confirmation_document(
    tranche_id: UUID,
    download: bool = Query(False, description="true: as an attachment, to save"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """The filled-in Investment Confirmation PDF kept for this commitment, exactly as stored."""
    confirmation = await _own_confirmation(db, tranche_id, current_user.id)
    tranche = await db.get(Tranche, tranche_id)
    content = await funding.evidence_storage().get(confirmation.file_key, max_bytes=DEFAULT_PARAMS.max_evidence_bytes)
    return _pdf_response(content, f"FundLok-xac-nhan-dau-tu-{tranche.reference_code}.pdf", download)


@router.get("/tranches/{tranche_id}/payment", response_model=PaymentOut)
async def read_payment(
    tranche_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """Where and how to pay: the SME's account, the exact amount, the memo, and a VietQR carrying all three.

    Refused (409 INVESTOR_AGREEMENT_REQUIRED) until the investor has confirmed the Investor Service Agreement of
    the current version: at their first commitment it is confirmed with a one-time code, with this commitment's
    Annex filled in, before any money moves. Receipts are never refused for it: money already sent still reaches staff."""
    if await investor_agreement.current_signature(db, current_user.id) is None:
        raise HTTPException(
            status_code=409,
            detail="Confirm the Investor Service Agreement with this commitment before paying",
            headers={"X-Error-Code": "INVESTOR_AGREEMENT_REQUIRED"},
        )
    try:
        instructions = await payment_instructions(db, tranche_id=tranche_id, investor_id=current_user.id, now=_now())
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    return PaymentOut.model_validate(instructions)


@router.get("/me/transactions", response_model=list[TransactionOut])
async def read_my_transactions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """The investor's money in and out, newest first: each investment transferred, and each day the business repaid
    them (what reached them, after the tax the business keeps back)."""
    return [TransactionOut.model_validate(row) for row in await investor_transactions(db, investor_id=current_user.id)]


@router.get("/me/analytics", response_model=InvestorAnalyticsOut)
async def read_my_analytics(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """The investor's portfolio: monthly capital invested and returned (capital and earnings after tax apart), by
    industry, and where the capital stands."""
    return InvestorAnalyticsOut.model_validate(
        await investor_analytics(db, investor_id=current_user.id, today=ict_date(_now()))
    )


@router.get("/me/business-transactions", response_model=list[TransactionOut])
async def read_business_transactions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """The business's money in and out, newest first: each investment received, each day it repaid an investor
    (what it transferred), and FundLok's fee invoices. Investors are masked."""
    return [TransactionOut.model_validate(row) for row in await business_transactions(db, sme_user_id=current_user.id)]


@router.get("/me/business-analytics", response_model=BusinessAnalyticsOut)
async def read_business_analytics(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """The business's loan: what it owes and has repaid, its daily amount, month by month against its verified
    revenue, and the four groups of its business score."""
    return BusinessAnalyticsOut.model_validate(
        await business_analytics(db, sme_user_id=current_user.id, today=ict_date(_now()))
    )


@router.get("/me/tranches", response_model=list[InvestorTrancheOut])
async def read_my_tranches(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """The caller's tickets, newest first, each with the project it is in."""
    tranches = await list_investor_tranches(db, investor_id=current_user.id)
    windows = {
        window_id: (project, term_months)
        for window_id, project, term_months in (
            await db.execute(
                select(FundingWindow.id, Project, FundingWindow.term_months)
                .join(Project, Project.id == FundingWindow.project_id)
                .where(FundingWindow.id.in_({t.window_id for t in tranches}))
            )
        ).all()
    } if tranches else {}
    out = []
    for tranche in tranches:
        project, term_months = windows.get(tranche.window_id, (None, None))
        out.append(
            InvestorTrancheOut.model_validate(tranche).model_copy(
                update={
                    "project_id": project.id if project else None,
                    "project_name": project.legal_name if project else None,
                    "term_months": term_months,
                }
            )
        )
    return out


@router.post("/tranches/{tranche_id}/receipt", response_model=ReceiptResultOut, status_code=202)
async def upload_receipt(
    tranche_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """The investor's bank receipt for their transfer to the SME (ticket 1.5): PDF, PNG, JPEG or WEBP, at most
    10 MB. Staff read it in the MVP (check_status AWAITING_FIELDS). A receipt sent after the hold lapsed is still
    taken, for staff to review (LATE_RECEIPT), since the money may already have moved. The business is emailed to
    look for the money under the memo (once, not on a replay); its bell rings when staff approve the transfer."""
    limit = DEFAULT_PARAMS.max_evidence_bytes
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(status_code=413, detail="File too large", headers={"X-Error-Code": "FILE_TOO_LARGE"})
    content_type = funding.sniff_receipt_type(content)
    if content_type is None:
        raise HTTPException(
            status_code=415, detail="Upload a PDF, PNG, JPEG or WEBP", headers={"X-Error-Code": "UNSUPPORTED_TYPE"}
        )
    replay = idempotency_key is not None and (
        await db.execute(select(Transfer.id).where(Transfer.idempotency_key == idempotency_key))
    ).first() is not None
    try:
        result = await submit_receipt(
            db,
            tranche_id=tranche_id,
            investor_id=current_user.id,
            content=content,
            content_type=content_type,
            filename=(file.filename or "")[:255] or None,
            extractor=ManualEntryExtractor(),
            storage=funding.evidence_storage(),
            idempotency_key=idempotency_key,
            now=_now(),
        )
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    notice = await receipt_notice(db, tranche_id=tranche_id) if not replay else None
    if not replay:
        append_audit(
            db,
            entity_type="TRANCHE",
            entity_id=tranche_id,
            action="INVESTMENT_RECEIPT_UPLOADED",
            actor_id=current_user.id,
            after_state={
                "transfer_id": str(result.transfer_id),
                "tranche_status": result.tranche_status,
                "check_status": result.check_status,
            },
            ip_address=request.client.host if request.client else None,
        )
        if notice is not None:
            create_notification(
                db,
                user_id=notice.sme_user_id,
                event=EVENT_INVESTMENT_RECEIPT_SUBMITTED,
                entity_type="TRANCHE",
                entity_id=tranche_id,
                data=notice.notification_data(result.tranche_status),
            )
    await db.commit()
    if notice is not None:
        await realtime.publish([notice.sme_user_id])
        background_tasks.add_task(send_investment_receipt_email, **notice.email)
    return ReceiptResultOut.model_validate(result)


# --------------------------------------------------------------------------- #
# the business's side (HANDOFF-04 tickets 1.6 and 1.7)
# --------------------------------------------------------------------------- #
@router.post("/tranches/{tranche_id}/report-missing", response_model=SmeTicketOut)
async def report_missing_transfer(
    tranche_id: UUID,
    request: Request,
    body: ReportMissingCreate | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """The business reports that an investor's transfer has not reached its account after a receipt was submitted.
    Moves the ticket to UNDER_REVIEW (SME_DISPUTE) and opens a dispute for FundLok staff to review."""
    statement = ((body.statement if body else None) or "").strip() or (
        "The business reported that the transfer was not received."
    )
    try:
        tranche = await sme_dispute_tranche(
            db,
            tranche_id=tranche_id,
            sme_user_id=current_user.id,
            statement=statement,
            file_id=None,
            now=_now(),
        )
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    append_audit(
        db,
        entity_type="TRANCHE",
        entity_id=tranche.id,
        action="INVESTMENT_REPORTED_MISSING",
        actor_id=current_user.id,
        after_state={
            "tranche_status": tranche.status,
            "review_cause": tranche.review_cause,
            "reason": statement,
        },
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    investor = await db.get(User, tranche.investor_id)
    return SmeTicketOut(
        id=tranche.id,
        amount_vnd=tranche.amount_vnd,
        status=tranche.status,
        reference_code=tranche.reference_code,
        investor_display=mask_name((investor.full_name or investor.email) if investor else ""),
        receipt_submitted_at=tranche.receipt_submitted_at,
        attested_at=tranche.attested_at,
        confirm_deadline=tranche.confirm_deadline,
        review_cause=tranche.review_cause,
    )
@router.get("/me/rounds", response_model=list[SmeRoundOut])
async def read_my_rounds(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """The business's funding rounds, newest first: progress, and every investor's ticket (investor masked)."""
    now = _now()
    windows = (
        await db.execute(
            select(FundingWindow)
            .where(FundingWindow.sme_user_id == current_user.id)
            .order_by(FundingWindow.window_start.desc())
        )
    ).scalars().all()
    # Lapsed holds are released first, as on the investor's read.
    released = 0
    for window in windows:
        if window.status in ("OPEN", "FULL"):
            released += await expire_lapsed_holds(db, window_id=window.id, now=now)
    if released:
        await db.commit()
    rounds = []
    for window in windows:
        progress = await get_window_progress(db, listing_id=window.listing_id, now=now)
        project = await db.get(Project, window.project_id)
        tickets = await list_window_tranches(db, window_id=window.id, sme_user_id=current_user.id)
        rounds.append(
            SmeRoundOut(
                **FundingWindowOut.model_validate(progress).model_dump(),
                project_id=window.project_id,
                project_name=project.legal_name if project else "",
                tickets=[SmeTicketOut.model_validate(ticket) for ticket in tickets],
            )
        )
    return rounds


@router.get("/me/rounds/{window_id}/repayment-plan", response_model=SmeRepaymentPlanOut)
async def read_repayment_plan(
    window_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """The business's daily repayments, one row per working day summed over its investors. Once the loan has formed,
    the real days with how many were marked paid or reported missing; before, a projection from the investments
    confirmed so far, if the round closed on its last day (the latest it can): projected, not guaranteed. What goes
    to investors only: FundLok's fee is invoiced separately."""
    window = await db.get(FundingWindow, window_id)
    if window is None or window.sme_user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Round not found")

    days = (
        await db.execute(
            select(RepaymentDay)
            .join(Tranche, Tranche.id == RepaymentDay.tranche_id)
            .where(Tranche.window_id == window_id)
            .order_by(RepaymentDay.due_date)
        )
    ).scalars().all()
    # Real days as soon as any investment repays (with repay_from_approval, while the round is still open).
    if days:
        rows = sum_by_day(
            [
                {"due_date": d.due_date, "principal_vnd": d.principal_vnd, "interest_vnd": d.interest_vnd,
                 "gross_vnd": d.gross_vnd, "tax_withheld_vnd": d.tax_withheld_vnd, "net_vnd": d.net_vnd}
                for d in days
            ]
        )
        counts: dict[dt.date, list[int]] = {}
        for d in days:
            paid, missing = counts.setdefault(d.due_date, [0, 0])
            counts[d.due_date] = [
                paid + (d.status in ("PRESUMED_PAID", "MATCHED")),
                missing + (d.status == "REPORTED_MISSED"),
            ]
        for row in rows:
            row["paid"], row["missing"] = counts[row["due_date"]]
        kind, assumed, start = "ACTUAL", None, min(d.due_date for d in days)
    elif window.status in ("OPEN", "FULL", "CLOSING"):
        close_at = (
            window.close_wait_until if window.status == "CLOSING" and window.close_wait_until else window.window_end
        )
        tickets = (
            await db.execute(select(Tranche).where(Tranche.window_id == window_id, Tranche.status == "CONFIRMED"))
        ).scalars().all()
        projected: list[dict] = []
        start = None
        for ticket in tickets:
            plan = project_ticket(
                amount_vnd=ticket.amount_vnd,
                loan_rate_pct=Decimal(window.annual_rate_pct),
                term_months=window.term_months,
                individual=ticket.investor_type == "INDIVIDUAL",
                confirmed_on=ict_date(ticket.attested_at or ticket.confirmed_at),
                close_on=ict_date(close_at),
            )
            projected.extend(plan["days"])
            start = plan["start_date"]
        rows = sum_by_day(projected)
        kind, assumed = "PROJECTED", ict_date(close_at)
    else:
        rows, kind, assumed, start = [], "ACTUAL", None, None

    return SmeRepaymentPlanOut(
        kind=kind,
        assumed_close_date=assumed,
        start_date=start,
        days=rows,
        totals={
            "gross_vnd": sum(r["gross_vnd"] for r in rows),
            "tax_withheld_vnd": sum(r["tax_withheld_vnd"] for r in rows),
            "net_vnd": sum(r["net_vnd"] for r in rows),
        },
    )


@router.post("/windows/{window_id}/close-early", response_model=FundingWindowOut)
async def close_round_early(
    window_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """The business closes its round before day 14, once at least the minimum raise is confirmed. The round goes
    CLOSING; staff then form the loan (Advance round) once nothing is pending."""
    now = _now()
    try:
        window = await request_early_close(db, window_id=window_id, sme_user_id=current_user.id, now=now)
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    append_audit(
        db,
        entity_type="FUNDING_WINDOW",
        entity_id=window_id,
        action="FUNDING_ROUND_CLOSE_REQUESTED",
        actor_id=current_user.id,
        after_state={"status": window.status, "close_reason": window.close_reason},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return FundingWindowOut.model_validate(await get_window_progress(db, listing_id=window.listing_id, now=now))
