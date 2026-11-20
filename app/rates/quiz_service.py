"""Quiz leads from the illustrative SME quiz on /rate.

The quiz rate is computed in the visitor's browser from a short published
formula (fundlok-front-end `lib/rate-quiz.ts`). This module only stores what
the visitor saw and the contact they left, as a `rate_inquiries` row with
`source = 'QUIZ'`, so it appears in the admin "Rate inquiries" list.

Deliberately imports nothing from the underwriting engine or `lite_grading`:
the quiz is an illustration, not a price. See docs/specs/rates/sme-rate-quiz.md.
"""
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.rates.models import RateInquiry
from app.rates.schemas import SmeQuizLeadCreate, SmeQuizLeadResponse
from app.rates.service import parse_valid_ip


async def create_quiz_lead(
    db: AsyncSession,
    body: SmeQuizLeadCreate,
    client_ip: str | None,
    user_agent: str | None,
) -> SmeQuizLeadResponse:
    tracking = body.tracking
    rate = round(body.monthly_rate_pct, 2) if body.monthly_rate_pct is not None else None

    inquiry = RateInquiry(
        source="QUIZ",
        session_id=body.session_id,
        ip_address=parse_valid_ip(client_ip),
        user_agent=user_agent,
        industry=body.industry,
        requested_amount=body.requested_amount,
        tenor_months=body.tenor_months,
        monthly_revenue=body.monthly_revenue,
        operating_band=body.operating_band,
        quiz_outcome=body.outcome,
        quiz_params_version=body.quiz_params_version,
        # The monthly rate the visitor saw, in both columns so the admin list's
        # existing rate column shows it. None when the quiz declined.
        estimated_rate_min=rate,
        estimated_rate_max=rate,
        full_name=body.full_name,
        phone=body.phone,
        consented_contact_at=datetime.now(timezone.utc),
        utm_source=tracking.utm_source if tracking else None,
        utm_medium=tracking.utm_medium if tracking else None,
        utm_campaign=tracking.utm_campaign if tracking else None,
        utm_content=tracking.utm_content if tracking else None,
        utm_term=tracking.utm_term if tracking else None,
        fbclid=tracking.fbclid if tracking else None,
    )
    db.add(inquiry)
    await db.commit()
    await db.refresh(inquiry)
    return SmeQuizLeadResponse(inquiry_id=inquiry.id)
