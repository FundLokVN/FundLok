from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.phone_verification.schemas import PhoneCodeSendRequest, PhoneCodeSentOut, PhoneCodeVerifyRequest
from app.phone_verification.service import CODE_TTL, RESEND_COOLDOWN, send_code, verify_code
from app.users.models import User
from app.users.service import user_me_payload
from app.utils.captcha import verify_turnstile_token_async
from app.utils.jwt import get_current_user

router = APIRouter(prefix="/users/me/phone", tags=["phone-verification"])


@router.post("/send-code", response_model=PhoneCodeSentOut)
async def send_phone_code(
    body: PhoneCodeSendRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Every call is a paid ZBS message to a number the caller chooses.
    await verify_turnstile_token_async(body.turnstile_token)
    row = await send_code(db, current_user, body.phone)
    return PhoneCodeSentOut(
        phone=row.phone,
        channel=row.channel,
        expires_in=int(CODE_TTL.total_seconds()),
        resend_in=int(RESEND_COOLDOWN.total_seconds()),
    )


@router.post("/verify")
async def verify_phone_code(
    body: PhoneCodeVerifyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user = await verify_code(
        db,
        current_user,
        body.code,
        ip_address=request.client.host if request.client else None,
    )
    # Same body as GET /users/me, so the client can seed its cache with it.
    return user_me_payload(user)
