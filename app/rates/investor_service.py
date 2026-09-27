"""Investor-tab leads: create, re-estimate, sign up, and the admin read side.

FLOW (prototype rev 6)
  1. GET  tiers            -> loan rate per tier, for the hint under the tier
                              buttons. Shown before any details are given.
  2. POST leads            -> "Calculate my target yield". Captcha-checked.
                              Stores the lead and returns the first estimate.
  3. POST leads/{id}/estimate
                           -> after the first calculation the number follows
                              every input change. Needs an existing lead, so
                              the yield is never available without step 2 —
                              the prototype gated this in the browser only,
                              which a curl call walks straight past.
  4. POST leads/{id}/signup
                           -> "Sign up for more information". Records the
                              choices on screen at that moment, recomputed here.

The lead id is an unguessable UUID and acts as the capability for steps 3-4.
It never grants read access to the stored name or email.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import desc, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.engine_constraints import params
from app.rates import investor
from app.rates.models import InvestorRateInquiry
from app.rates.schemas import (
    InvestorChoices,
    InvestorEstimateOut,
    InvestorLeadAdminItem,
    InvestorLeadCreate,
    InvestorLeadResponse,
    InvestorLeadsAdminListResponse,
    InvestorSignupResponse,
    InvestorTierOut,
    InvestorTiersResponse,
)
from app.rates.service import parse_valid_ip
from app.underwriting.service import get_current_bank_rate

logger = logging.getLogger(__name__)

# No 0/O or 1/I/L: the reference is read out over the phone.
_REFERENCE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_REFERENCE_ATTEMPTS = 5


async def _bank_rate_pct(db: AsyncSession) -> float:
    """The board rate in force, from the same table admin score runs read.

    Falls back to the params default rather than failing: a missing
    bank_rate_config row is a config error, and on a public page the right
    response is a correct-as-of-default illustration plus a loud log line, not
    a 500 in front of a prospective investor.
    """
    try:
        row = await get_current_bank_rate(db)
        return float(row.rate_pct)
    except HTTPException:
        fallback = float(params().pricing["bank_rate_default_pct"])
        logger.error("No bank_rate_config row in force; investor estimate using default %s", fallback)
        return fallback


def _to_out(result: investor.InvestorEstimate) -> InvestorEstimateOut:
    return InvestorEstimateOut(
        loan_rate_pct=result.loan_rate_pct,
        expected_loss_pct=result.expected_loss_pct,
        fee_pct=result.fee_pct,
        net_per_loan_pct=result.net_per_loan_pct,
        net_apy_pct=result.net_apy_pct,
        estimated_return_vnd=result.estimated_return_vnd,
        avg_loan_months=result.avg_loan_months,
        capital_turns=result.capital_turns,
    )


def _compute(choices: InvestorChoices, bank_rate_pct: float) -> investor.InvestorEstimate:
    return investor.estimate(
        amount_vnd=choices.amount_vnd,
        commitment_months=choices.commitment_months,
        tier=choices.risk_tier,
        cadence=choices.reinvestment_cadence,
        bank_rate_pct=bank_rate_pct,
    )


def _new_reference() -> str:
    return "FL-" + "".join(secrets.choice(_REFERENCE_ALPHABET) for _ in range(6))


async def list_tiers(db: AsyncSession) -> InvestorTiersResponse:
    bank_rate = await _bank_rate_pct(db)
    return InvestorTiersResponse(
        tiers=[
            InvestorTierOut(
                risk_tier=tier,
                loan_rate_pct=round(investor.loan_rate_for_tier(tier, bank_rate), 2),
            )
            for tier in investor.RISK_TIERS
        ]
    )


async def create_lead(
    db: AsyncSession,
    body: InvestorLeadCreate,
    client_ip: str | None,
    user_agent: str | None,
) -> InvestorLeadResponse:
    bank_rate = await _bank_rate_pct(db)
    result = _compute(body, bank_rate)
    now = datetime.now(timezone.utc)

    for attempt in range(_REFERENCE_ATTEMPTS):
        lead = InvestorRateInquiry(
            reference=_new_reference(),
            full_name=body.full_name,
            email=body.email,
            phone=body.phone,
            locale=body.locale,
            amount_vnd=body.amount_vnd,
            commitment_months=body.commitment_months,
            risk_tier=body.risk_tier,
            reinvestment_cadence=body.reinvestment_cadence,
            bank_rate_pct=bank_rate,
            loan_rate_pct=result.loan_rate_pct,
            net_per_loan_pct=result.net_per_loan_pct,
            net_apy_pct=result.net_apy_pct,
            estimated_return_vnd=result.estimated_return_vnd,
            acknowledged_illustrative_at=now,
            consented_contact_at=now,
            session_id=body.session_id,
            ip_address=parse_valid_ip(client_ip),
            user_agent=user_agent,
        )
        db.add(lead)
        try:
            await db.commit()
            break
        except IntegrityError:
            # 31^6 ~ 887M references, so a collision is rare but not
            # impossible; the unique constraint is the arbiter, not a pre-check
            # that could race.
            await db.rollback()
            if attempt == _REFERENCE_ATTEMPTS - 1:
                raise
    await db.refresh(lead)

    return InvestorLeadResponse(lead_id=lead.id, reference=lead.reference, estimate=_to_out(result))


async def _get_lead(db: AsyncSession, lead_id: UUID) -> InvestorRateInquiry:
    lead = (
        await db.execute(select(InvestorRateInquiry).where(InvestorRateInquiry.id == lead_id))
    ).scalar_one_or_none()
    if lead is None:
        raise HTTPException(status_code=404, detail="Enquiry not found")
    return lead


async def estimate_for_lead(
    db: AsyncSession, lead_id: UUID, choices: InvestorChoices
) -> InvestorEstimateOut:
    await _get_lead(db, lead_id)
    return _to_out(_compute(choices, await _bank_rate_pct(db)))


async def sign_up(
    db: AsyncSession, lead_id: UUID, choices: InvestorChoices
) -> InvestorSignupResponse:
    lead = await _get_lead(db, lead_id)
    if lead.signed_up_at is None:
        # First sign-up wins: a double click, or a replay with different
        # numbers, must not rewrite what the investor actually signed up for.
        result = _compute(choices, await _bank_rate_pct(db))
        lead.signed_up_at = datetime.now(timezone.utc)
        lead.signup_amount_vnd = choices.amount_vnd
        lead.signup_commitment_months = choices.commitment_months
        lead.signup_risk_tier = choices.risk_tier
        lead.signup_reinvestment_cadence = choices.reinvestment_cadence
        lead.signup_net_apy_pct = result.net_apy_pct
        await db.commit()
        await db.refresh(lead)
    return InvestorSignupResponse(reference=lead.reference, signed_up_at=lead.signed_up_at)


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #


def _admin_item(row: InvestorRateInquiry) -> InvestorLeadAdminItem:
    def f(value):
        return float(value) if value is not None else None

    return InvestorLeadAdminItem(
        id=row.id,
        reference=row.reference,
        created_at=row.created_at,
        full_name=row.full_name,
        email=row.email,
        phone=row.phone,
        locale=row.locale,
        amount_vnd=row.amount_vnd,
        commitment_months=row.commitment_months,
        risk_tier=row.risk_tier,
        reinvestment_cadence=row.reinvestment_cadence,
        bank_rate_pct=f(row.bank_rate_pct),
        loan_rate_pct=f(row.loan_rate_pct),
        net_per_loan_pct=f(row.net_per_loan_pct),
        net_apy_pct=f(row.net_apy_pct),
        estimated_return_vnd=row.estimated_return_vnd,
        acknowledged_illustrative_at=row.acknowledged_illustrative_at,
        consented_contact_at=row.consented_contact_at,
        signed_up_at=row.signed_up_at,
        signup_amount_vnd=row.signup_amount_vnd,
        signup_commitment_months=row.signup_commitment_months,
        signup_risk_tier=row.signup_risk_tier,
        signup_reinvestment_cadence=row.signup_reinvestment_cadence,
        signup_net_apy_pct=f(row.signup_net_apy_pct),
        session_id=row.session_id,
        ip_address=str(row.ip_address) if row.ip_address else None,
        user_agent=row.user_agent,
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_leads_admin(
    db: AsyncSession,
    page: int,
    page_size: int,
    search: str | None,
    risk_tier: str | None,
    signed_up: bool | None,
) -> InvestorLeadsAdminListResponse:
    query = select(InvestorRateInquiry).order_by(desc(InvestorRateInquiry.created_at))
    if search:
        pattern = f"%{_escape_like(search)}%"
        query = query.where(
            or_(
                InvestorRateInquiry.full_name.ilike(pattern, escape="\\"),
                InvestorRateInquiry.email.ilike(pattern, escape="\\"),
                InvestorRateInquiry.reference.ilike(pattern, escape="\\"),
            )
        )
    if risk_tier:
        query = query.where(InvestorRateInquiry.risk_tier == risk_tier)
    if signed_up is True:
        query = query.where(InvestorRateInquiry.signed_up_at.is_not(None))
    elif signed_up is False:
        query = query.where(InvestorRateInquiry.signed_up_at.is_(None))

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (
        await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    return InvestorLeadsAdminListResponse(
        total=total, page=page, page_size=page_size, items=[_admin_item(r) for r in rows]
    )


async def get_lead_admin(db: AsyncSession, lead_id: UUID) -> InvestorLeadAdminItem:
    return _admin_item(await _get_lead(db, lead_id))
