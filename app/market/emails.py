"""Emails about investing through a funding window. Sent through the platform's one SMTP sender
(`app.utils.email.send_email`), as background tasks after the response, like every other email.

Called as `platform_email.send_email`, not imported by name: the tests' backstop stub replaces that module
attribute (conftest `_mock_external_io`), and a name bound at import would slip past it to a real SMTP server.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.direct_transfer.models import FundingWindow, Tranche
from app.direct_transfer.tranches import mask_name, resolve_kyc_name
from app.lending.models import Project
from app.users.models import User
from app.utils import email as platform_email
from app.utils.email_templates import render_email

ICT = ZoneInfo("Asia/Ho_Chi_Minh")


def _vnd_vi(amount: int) -> str:
    return f"{amount:,}".replace(",", ".") + " ₫"


def _vnd_en(amount: int) -> str:
    return f"₫{amount:,}"


@dataclasses.dataclass(frozen=True)
class ReceiptNotice:
    """Who to tell that a ticket's receipt came in, and what: the business's account (for the bell) and the
    arguments of `send_investment_receipt_email`."""

    sme_user_id: UUID
    email: dict[str, Any]

    def notification_data(self, status: str) -> dict[str, Any]:
        """The placeholders the bell's copy names (the client renders the sentence, in the reader's language),
        and the ticket's status after the approval (ATTESTED or CONFIRMED)."""
        return {key: self.email[key] for key in ("amount_vnd", "memo", "project", "investor")} | {"status": status}


async def receipt_notice(db: AsyncSession, *, tranche_id: UUID) -> ReceiptNotice | None:
    """Who and what to tell for a ticket whose receipt just came in: the business's account and address, the
    project, the amount, the memo and when. None if the ticket or its business is gone."""
    tranche = await db.get(Tranche, tranche_id)
    window = await db.get(FundingWindow, tranche.window_id) if tranche else None
    sme = await db.get(User, window.sme_user_id) if window else None
    if tranche is None or window is None or sme is None:
        return None
    project = await db.get(Project, window.project_id)
    email = {
        # An unusable address is skipped by send_email; the bell still rings.
        "to_email": sme.email or "",
        # Masked as on the business's own view of its round (list_window_tranches).
        "investor": mask_name(await resolve_kyc_name(db, tranche.investor_id)),
        "project": project.legal_name if project else "",
        "amount_vnd": tranche.amount_vnd,
        "memo": tranche.reference_code,
        "uploaded_at": tranche.receipt_submitted_at or dt.datetime.now(dt.timezone.utc),
    }
    return ReceiptNotice(sme_user_id=sme.id, email=email)


def send_investment_receipt_email(
    to_email: str,
    *,
    investor: str,
    project: str,
    amount_vnd: int,
    memo: str,
    uploaded_at: dt.datetime,
) -> None:
    """Tell the business an investor uploaded the receipt for a transfer to its account, so it can look for the
    money under the payment memo.

    Says the transfer was reported, not that it arrived: until staff check the receipt FundLok has only the
    investor's word and a picture. The memo is in the subject so the business can match it to its bank
    statement at a glance. Vietnamese first, English beneath, because the account has no stored language.
    """
    link = f"{settings.FRONTEND_URL}/dashboard"
    when = uploaded_at.astimezone(ICT).strftime("%d/%m/%Y %H:%M") + " (GMT+7)"
    amount_vi, amount_en = _vnd_vi(amount_vnd), _vnd_en(amount_vnd)
    subject = (
        f"Nhà đầu tư đã báo chuyển tiền, nội dung {memo} | An investor reported a transfer, memo {memo}"
    )
    html_content = render_email(
        "investment-receipt",
        investor=investor,
        project=project,
        amount_vi=amount_vi,
        amount_en=amount_en,
        memo=memo,
        uploaded_at=when,
        link=link,
    )
    text_content = (
        f"Nhà đầu tư {investor} vừa tải lên biên lai cho khoản chuyển vào tài khoản của doanh nghiệp bạn "
        f"(dự án {project}): {amount_vi}, nội dung {memo}, lúc {when}. Hãy kiểm tra tài khoản ngân hàng của "
        f"bạn. Nếu không thấy khoản tiền này, hãy liên hệ FundLok. {link}\n\n"
        f"Investor {investor} has uploaded the receipt for a transfer to your business's account "
        f"(project {project}): {amount_en}, memo {memo}, at {when}. Check your bank account. If you don't see "
        f"this money, contact FundLok support. {link}"
    )
    platform_email.send_email(
        to_email=to_email, subject=subject, html_content=html_content, text_content=text_content
    )
