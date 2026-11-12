"""Email the SME its platform activation-fee invoice as soon as it is issued.

FundLok charges the SME one platform fee per funding round: 3% of the money the
investors confirmed sending, plus VAT, paid separately into FundLok's own
account and never netted from principal (ADR-009, D-023). Edward's
app/direct_transfer issues it as a `fee_invoices` row (kind SME_ACTIVATION)
when the round closes, that is once every investor's transfer is confirmed.
Nothing told the SME; this does.

WHEN
`run_fee_invoice_emails()` runs in the background after an admin advances a
round (the only way a round closes in production, the scheduled worker not
being deployed) and is the ARQ job to register once that worker is. Each run
sweeps every invoice not yet emailed, so one that failed is retried by the
next run.

WHO SENDS
The admin Gmail account chosen under System settings (the reminder sender),
the free route. If none is chosen or it cannot send, the platform's own SMTP
relay, so the invoice always goes out.

Only an invoice still ISSUED (unpaid, not yet overdue) and issued within its
payment period is emailed, so no old or settled invoice is sent when this
first runs.
"""

import asyncio
import datetime as dt
import html
import logging
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.fee_notice.models import (
    CHANNEL_GMAIL,
    CHANNEL_PLATFORM,
    STATUS_FAILED,
    STATUS_SENDING,
    STATUS_SENT,
    FeeInvoiceNotice,
)
from app.admin.internal_email import service as email_service
from app.admin.internal_email.schemas import EmailContent
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.timezone import ICT
from app.direct_transfer.banks import get_bank
from app.direct_transfer.config import DirectTransferParams, resolve
from app.direct_transfer.models import FeeInvoice, FundingWindow
from app.lending.models import Project
from app.users.models import User
from app.utils.email import send_email

logger = logging.getLogger(__name__)

KIND = "SME_ACTIVATION"
# Tries per invoice before it is left FAILED for a person to look at.
MAX_ATTEMPTS = 5


class _NotSent(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _vnd(amount: int) -> str:
    return f"{amount:,}".replace(",", ".") + " VND"


def compose(
    invoice: FeeInvoice,
    *,
    sme: User,
    project_name: str,
    params: DirectTransferParams,
) -> EmailContent:
    """The invoice email, in Vietnamese with the English below it."""
    e = html.escape
    name = e(sme.full_name or sme.email)
    project = e(project_name)
    due = invoice.due_at.astimezone(ICT).strftime("%d/%m/%Y")

    bank = get_bank(params.fee_account_bin) if params.fee_account_bin else None
    if params.fee_account_bin and params.fee_account_number:
        bank_name = bank.short_name if bank else params.fee_account_bin
        pay_to = (
            "<p><b>Tài khoản nhận (Pay to):</b><br>"
            f"{e(bank_name)} · {e(params.fee_account_number)}"
            + (f"<br>{e(params.fee_account_name)}" if params.fee_account_name else "")
            + "</p>"
        )
    else:
        pay_to = (
            "<p>Để nhận thông tin tài khoản thanh toán của FundLok, vui lòng trả lời email này.<br>"
            "<i>To get FundLok's account details, reply to this email.</i></p>"
        )

    body = (
        f"<p>Xin chào {name},</p>"
        f"<p>Các nhà đầu tư đã chuyển xong vốn cho dự án <b>{project}</b> và đợt gọi vốn đã đóng. "
        "FundLok thu một khoản phí kích hoạt nền tảng duy nhất cho đợt này: 3% trên tổng số tiền đã xác nhận, "
        "cộng thuế GTGT. Phí được thanh toán riêng vào tài khoản của FundLok và không bao giờ bị trừ vào số vốn "
        "bạn đã nhận.</p>"
        "<ul>"
        f"<li>Tổng vốn đã xác nhận (Confirmed total): <b>{_vnd(invoice.base_vnd)}</b></li>"
        f"<li>Phí kích hoạt 3% (Activation fee): {_vnd(invoice.fee_vnd)}</li>"
        f"<li>Thuế GTGT (VAT): {_vnd(invoice.vat_vnd)}</li>"
        f"<li>Tổng cần thanh toán (Total due): <b>{_vnd(invoice.total_vnd)}</b></li>"
        f"<li>Hạn thanh toán (Due date): <b>{due}</b></li>"
        f"<li>Số hóa đơn (Invoice number): {e(invoice.invoice_number)}</li>"
        f"<li>Nội dung chuyển khoản (Transfer memo): <b>{e(invoice.reference_code)}</b></li>"
        "</ul>"
        f"{pay_to}"
        "<p>Vui lòng ghi đúng nội dung chuyển khoản để FundLok đối soát. Hóa đơn GTGT điện tử sẽ được gửi riêng. "
        "Khi phí chưa được thanh toán, doanh nghiệp chưa thể mở đợt gọi vốn tiếp theo.</p>"
        f"<p><i>The investors have finished transferring the funds for {project} and the round has closed. "
        "FundLok charges one platform activation fee for this round: 3% of the confirmed total, plus VAT. "
        "It is paid separately into FundLok's own account and is never deducted from the money you received. "
        "Please put the transfer memo above on your payment. The VAT e-invoice follows separately. While the "
        "fee is unpaid, your next funding round cannot open.</i></p>"
    )
    dashboard = f"{settings.FRONTEND_URL.rstrip('/')}/dashboard"
    https = dashboard.startswith("https://")
    return EmailContent(
        template="action_required",
        subject=f"FundLok: phí kích hoạt / activation fee {invoice.invoice_number}",
        heading="Phí kích hoạt nền tảng FundLok · FundLok platform activation fee",
        body=body,
        # The composer's templates take an https link only (local dev runs on http).
        button_label="Mở FundLok · Open FundLok" if https else None,
        button_url=dashboard if https else None,
    )


async def _candidates(
    db: AsyncSession, *, now: dt.datetime, params: DirectTransferParams
) -> list[tuple[UUID, bool]]:
    """(invoice id, whether it is a retry). Ids, not objects: a failed send
    rolls back, which expires every object the session holds."""
    since = now - dt.timedelta(days=params.sme_invoice_due_days)
    rows = await db.execute(
        select(FeeInvoice.id, FeeInvoiceNotice.invoice_id)
        .outerjoin(FeeInvoiceNotice, FeeInvoiceNotice.invoice_id == FeeInvoice.id)
        .where(
            FeeInvoice.kind == KIND,
            FeeInvoice.status == "ISSUED",
            FeeInvoice.issued_at > since,
            or_(
                FeeInvoiceNotice.invoice_id.is_(None),
                and_(
                    FeeInvoiceNotice.status == STATUS_FAILED,
                    FeeInvoiceNotice.attempts < MAX_ATTEMPTS,
                ),
            ),
        )
        .order_by(FeeInvoice.issued_at)
    )
    return [(invoice_id, notice_id is not None) for invoice_id, notice_id in rows.all()]


async def _claim(db: AsyncSession, invoice_id: UUID, *, sent_to: str, retry: bool) -> bool:
    """Take the invoice for this run, committed before anything is sent. False
    when another run took it first."""
    if retry:
        stmt = (
            update(FeeInvoiceNotice)
            .where(
                FeeInvoiceNotice.invoice_id == invoice_id,
                FeeInvoiceNotice.status == STATUS_FAILED,
                FeeInvoiceNotice.attempts < MAX_ATTEMPTS,
            )
            .values(
                status=STATUS_SENDING,
                attempts=FeeInvoiceNotice.attempts + 1,
                sent_to=sent_to,
                last_error=None,
            )
            .returning(FeeInvoiceNotice.invoice_id)
        )
    else:
        stmt = (
            insert(FeeInvoiceNotice)
            .values(invoice_id=invoice_id, status=STATUS_SENDING, sent_to=sent_to, attempts=1)
            .on_conflict_do_nothing(index_elements=["invoice_id"])
            .returning(FeeInvoiceNotice.invoice_id)
        )
    claimed = (await db.execute(stmt)).scalar_one_or_none() is not None
    await db.commit()
    return claimed


async def _finish(db: AsyncSession, invoice_id, **values) -> None:
    await db.execute(
        update(FeeInvoiceNotice).where(FeeInvoiceNotice.invoice_id == invoice_id).values(**values)
    )
    await db.commit()


async def _deliver(
    db: AsyncSession, *, to: str, content: EmailContent, invoice_id: UUID
) -> tuple[str, str]:
    """Send it; (channel, from address). The reminder sender first, the
    platform relay if that cannot."""
    html_body, text = email_service.render_system_email(content)
    sender = await email_service.resolve_reminder_sender(db)
    if sender is not None:
        try:
            await email_service.send_as_reminder_sender(
                db,
                sender,
                recipients=[to],
                template=content.template,
                subject=content.subject,
                content={"fee_invoice_id": str(invoice_id), "heading": content.heading},
                html=html_body,
                text=text,
            )
            return CHANNEL_GMAIL, sender.account.gmail_address
        except HTTPException as exc:
            # The send may have left a transaction open (the hourly-limit lock).
            await db.rollback()
            code = (exc.headers or {}).get("X-Error-Code", str(exc.status_code))
            logger.warning(
                "Fee invoice %s: the reminder sender could not send (%s); using the platform email",
                invoice_id,
                code,
            )
    if not await asyncio.to_thread(send_email, to, content.subject, html_body, text):
        raise _NotSent("INVALID_ADDRESS")
    return CHANNEL_PLATFORM, settings.EMAILS_FROM_EMAIL


async def send_pending_fee_invoice_emails(
    db: AsyncSession,
    *,
    now: dt.datetime | None = None,
    params: DirectTransferParams | None = None,
) -> int:
    """Email every SME activation invoice not emailed yet. Returns how many
    went out."""
    now = now or dt.datetime.now(dt.timezone.utc)
    p = resolve(params)
    if not (p.fee_account_bin and p.fee_account_number):
        logger.warning(
            "Fee invoice emails go without FundLok's account: FUNDLOK_FEE_BANK_BIN / "
            "FUNDLOK_FEE_ACCOUNT_NUMBER are not set"
        )
    sent = 0
    for invoice_id, retry in await _candidates(db, now=now, params=p):
        invoice = await db.get(FeeInvoice, invoice_id, populate_existing=True)
        sme = await db.get(User, invoice.payer_user_id, populate_existing=True)
        window = await db.get(FundingWindow, invoice.window_id)
        project = await db.get(Project, window.project_id) if window is not None else None
        if sme is None or not sme.email:
            continue
        if not await _claim(db, invoice_id, sent_to=sme.email, retry=retry):
            continue
        try:
            content = compose(
                invoice,
                sme=sme,
                project_name=project.legal_name if project is not None else "",
                params=p,
            )
            channel, from_address = await _deliver(
                db, to=sme.email, content=content, invoice_id=invoice_id
            )
        except Exception as exc:  # noqa: BLE001 - recorded on the row, retried next run
            await db.rollback()
            code = exc.code if isinstance(exc, _NotSent) else type(exc).__name__
            logger.warning("Fee invoice %s: email not sent (%s)", invoice_id, code)
            await _finish(db, invoice_id, status=STATUS_FAILED, last_error=code[:100])
            continue
        await _finish(
            db,
            invoice_id,
            status=STATUS_SENT,
            channel=channel,
            from_address=from_address,
            sent_at=dt.datetime.now(dt.timezone.utc),
        )
        sent += 1
    return sent


async def run_fee_invoice_emails() -> int:
    """For a background task: its own session, and never raises."""
    try:
        async with SessionLocal() as db:
            return await send_pending_fee_invoice_emails(db)
    except Exception:
        logger.exception("Fee invoice emails: the run failed")
        return 0


async def fee_invoice_email_job(ctx: dict) -> int:
    """ARQ job (ctx first, per ARQ). Not registered yet: no worker is deployed.
    Register it on the direct-transfer worker next to invoice_job when one is."""
    return await run_fee_invoice_emails()
