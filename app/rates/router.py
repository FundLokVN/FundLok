from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.rates.schemas import (
    RateCalculationRequest,
    RateCalculationResponse,
    RateInquiriesAdminListResponse,
    RateInquiryAdminItem,
)
from app.rates.service import (
    get_rate_inquiry_by_id_admin,
    list_rate_inquiries_admin,
    process_rate_calculation,
)
from app.users.models import Role, User
from app.utils.captcha import verify_turnstile_token_async
from app.utils.rbac import require_roles

router = APIRouter(prefix="/api/v1/rates", tags=["rates"])
admin_rates_router = APIRouter(prefix="/admin/rates", tags=["admin-rates"])

require_admin = require_roles(Role.ADMIN, Role.SYSTEM_ADMIN)

# Long enough for any real browser; short enough that the value cannot be used
# to push megabytes into an unbounded TEXT column on a public endpoint.
_MAX_USER_AGENT = 512


def _extract_client_ip(request: Request) -> str | None:
    """The caller's address, already resolved by TrustedProxyMiddleware.

    X-Forwarded-For is deliberately NOT read here any more. It used to be, with
    its own trust rule, which made this route the one place in the app that
    disagreed with every audit log about who a caller was. The rule now lives
    once in app/utils/client_ip.py and `request.client` is its answer.
    """
    return request.client.host if request.client else None


@router.post("/calculate", response_model=RateCalculationResponse, status_code=status.HTTP_200_OK)
async def calculate_rate(
    body: RateCalculationRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Calculate indicative loan rate and monthly repayment.

    Runs in-memory scoring, saves the calculation into rate_inquiries for audit
    and analytics, and returns the computed rates and monthly payment range.
    """
    await verify_turnstile_token_async(body.turnstile_token)

    client_ip = _extract_client_ip(request)
    user_agent = request.headers.get("user-agent")
    if user_agent:
        user_agent = user_agent[:_MAX_USER_AGENT]

    # If session_id not in payload, check cookie / custom header. Both are
    # caller-controlled strings of unbounded length, and `session_id` is a
    # String(128) column — so this goes through `validate_assignment` on the
    # model (set in RateCalculationRequest) rather than past it. An oversized
    # header is a 422 naming the field, not a 500 from the driver.
    if not body.session_id:
        candidate = request.headers.get("x-session-id") or request.cookies.get("session_id")
        if candidate:
            try:
                body.session_id = candidate
            except PydanticValidationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="session_id must be at most 128 characters",
                ) from exc

    return await process_rate_calculation(
        db=db,
        body=body,
        client_ip=client_ip,
        user_agent=user_agent,
    )


@router.get("/inquiries", response_model=RateInquiriesAdminListResponse)
@admin_rates_router.get("/inquiries", response_model=RateInquiriesAdminListResponse)
async def list_rate_inquiries(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    industry: str | None = Query(default=None, max_length=100),
    risk_tier: str | None = Query(default=None, pattern=r"^TIER_[ABC]$"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Retrieve submitted rate inquiries formatted for the Admin dashboard data table.

    Requires ADMIN or SYSTEM_ADMIN role.
    """
    return await list_rate_inquiries_admin(
        db=db,
        page=page,
        page_size=page_size,
        industry=industry,
        risk_tier=risk_tier,
    )


@router.get("/inquiries/{inquiry_id}", response_model=RateInquiryAdminItem)
@admin_rates_router.get("/inquiries/{inquiry_id}", response_model=RateInquiryAdminItem)
async def get_rate_inquiry_detail(
    inquiry_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Retrieve detailed inquiry information for an admin operator."""
    item = await get_rate_inquiry_by_id_admin(db=db, inquiry_id=inquiry_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inquiry not found")
    return item
