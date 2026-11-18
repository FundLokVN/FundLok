"""Lender standing: the lock after a Day-14 no-show or late money, and unlocking it.

Spec: docs/specs/direct_transfer/commit-then-accept-flow.md 4.7 and day14-transfer-rule.md 4.5. Contract 4 Art. 5.4:
to unlock, the lender completes eKYC again (after the lock) and signs an undertaking not to repeat.
Top-up pledges are listed here; signing them in the 7-day reopening is not built yet (Phase 3, gap register B6).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.errors import DirectTransferError, conflict
from app.direct_transfer.models import FundingWindow, LenderStanding, LenderUnlockAcknowledgement, TopUpPledge
from app.direct_transfer.receipts import normalize_name
from app.gverify.models import STATUS_APPROVED, GVerifyVerification
from app.lending.models import Project

UNLOCK_SENTENCE_VERSION = "unlock-v1"
# Vietnamese prevails (Contract 4). Compared diacritic-folded, case- and spacing-insensitive (normalize_name).
UNLOCK_SENTENCE = (
    "Tôi cam kết chuyển tiền đúng hạn vào Ngày giải ngân và hiểu rằng nếu tái phạm, "
    "tài khoản của tôi sẽ bị khóa vĩnh viễn."
)


@dataclasses.dataclass(frozen=True)
class PledgeView:
    id: UUID
    window_id: UUID
    listing_title: str | None
    amount_vnd: int
    recorded_at: dt.datetime
    status: str  # AWAITING_UNLOCK, AWAITING_SIGNING, SIGNED, SME_CONFIRMED, VOID


@dataclasses.dataclass(frozen=True)
class StandingView:
    status: str  # GOOD, LOCKED, CLOSED
    no_show_count: int
    locked_at: dt.datetime | None
    unlock_requires: list[str]
    acknowledgement_sentence: str | None
    top_up_pledges: list[PledgeView]


async def is_locked(db: AsyncSession, investor_id: UUID) -> bool:
    standing = await db.get(LenderStanding, investor_id)
    return standing is not None and standing.status in ("LOCKED", "CLOSED")


async def _fresh_ekyc(db: AsyncSession, investor_id: UUID, locked_at: dt.datetime | None) -> GVerifyVerification | None:
    if locked_at is None:
        return None
    return (
        await db.execute(
            select(GVerifyVerification)
            .where(
                GVerifyVerification.user_id == investor_id,
                GVerifyVerification.verification_type == "KYC",
                GVerifyVerification.status == STATUS_APPROVED,
                GVerifyVerification.created_at > locked_at,
            )
            .order_by(GVerifyVerification.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _pledge_status(pledge: TopUpPledge, standing_status: str) -> str:
    if pledge.status == "VOID":
        return "VOID"
    if pledge.status == "SIGNED":
        return "SIGNED"
    if pledge.status == "CONFIRMED":
        return "SME_CONFIRMED"
    return "AWAITING_UNLOCK" if standing_status != "GOOD" else "AWAITING_SIGNING"


async def get_standing(db: AsyncSession, investor_id: UUID) -> StandingView:
    standing = await db.get(LenderStanding, investor_id)
    status = standing.status if standing else "GOOD"
    rows = (
        await db.execute(
            select(TopUpPledge, Project.legal_name)
            .join(FundingWindow, FundingWindow.id == TopUpPledge.window_id)
            .outerjoin(Project, Project.id == FundingWindow.project_id)
            .where(TopUpPledge.investor_id == investor_id)
            .order_by(TopUpPledge.recorded_at.desc())
        )
    ).all()
    requires: list[str] = []
    if status == "LOCKED":
        if await _fresh_ekyc(db, investor_id, standing.locked_at) is None:
            requires.append("EKYC")
        requires.append("ACKNOWLEDGEMENT")
    return StandingView(
        status=status,
        no_show_count=standing.no_show_count if standing else 0,
        locked_at=standing.locked_at if standing and status == "LOCKED" else None,
        unlock_requires=requires,
        acknowledgement_sentence=UNLOCK_SENTENCE if status == "LOCKED" else None,
        top_up_pledges=[
            PledgeView(
                id=pledge.id, window_id=pledge.window_id, listing_title=title, amount_vnd=pledge.amount_vnd,
                recorded_at=pledge.recorded_at, status=_pledge_status(pledge, status),
            )
            for pledge, title in rows
        ],
    )


async def unlock(
    db: AsyncSession, *, investor_id: UUID, acknowledgement: str, now: dt.datetime,
    ip_address: str | None = None, user_agent: str | None = None,
) -> StandingView:
    standing = await db.get(LenderStanding, investor_id, with_for_update=True)
    if standing is None or standing.status == "GOOD":
        raise conflict("NOT_LOCKED", "Your account is not locked")
    if standing.status == "CLOSED":
        raise conflict("LENDER_CLOSED", "Your account is closed")
    ekyc = await _fresh_ekyc(db, investor_id, standing.locked_at)
    if ekyc is None:
        raise conflict("EKYC_REQUIRED", "Complete identity verification again before unlocking")
    if normalize_name(acknowledgement or "") != normalize_name(UNLOCK_SENTENCE):
        raise DirectTransferError(422, "Type the sentence exactly as shown", "ACKNOWLEDGEMENT_MISMATCH")
    db.add(
        LenderUnlockAcknowledgement(
            investor_id=investor_id, locked_window_id=standing.locked_window_id, sentence_text=UNLOCK_SENTENCE,
            sentence_version=UNLOCK_SENTENCE_VERSION, ekyc_verification_id=ekyc.id, acknowledged_at=now,
            ip_address=ip_address, user_agent=user_agent,
        )
    )
    standing.status = "GOOD"
    standing.unlocked_at = now
    await db.flush()
    return await get_standing(db, investor_id)
