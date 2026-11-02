"""Routes for connecting a SePay account (docs/specs/oauth/sepay-connect.md)."""
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.oauth.sepay import service
from app.oauth.sepay.models import SepayConnection
from app.oauth.sepay.schemas import (
    SepayBankAccountOut,
    SepayCallbackIn,
    SepayConnectOut,
    SepayStatusOut,
)
from app.oauth.sepay.service import SepayServiceError
from app.users.models import Role, User
from app.utils.audit import append_audit
from app.utils.rbac import require_roles

router = APIRouter(prefix="/integrations/sepay", tags=["sepay"])

authorized = require_roles(Role.SME, Role.INVESTOR)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _status(conn: SepayConnection | None) -> SepayStatusOut:
    if conn is None:
        return SepayStatusOut(configured=settings.sepay_configured, connected=False)
    return SepayStatusOut(
        configured=settings.sepay_configured,
        connected=True,
        status=conn.status,
        connected_at=conn.connected_at,
        scopes=conn.scopes.split(),
    )


async def _raise(db: AsyncSession, exc: SepayServiceError) -> None:
    if exc.persist:
        await db.commit()
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("", response_model=SepayStatusOut)
async def read_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authorized),
):
    return _status(await service.get_connection(db, current_user.id))


@router.post("/connect", response_model=SepayConnectOut)
async def start_connect(current_user: User = Depends(authorized)):
    try:
        url = service.build_authorize_url(current_user.id, _now())
    except SepayServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return SepayConnectOut(authorize_url=url)


@router.post("/callback", response_model=SepayStatusOut)
async def finish_connect(
    body: SepayCallbackIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authorized),
):
    try:
        conn = await service.connect(db, user_id=current_user.id, code=body.code, state=body.state, now=_now())
    except SepayServiceError as exc:
        await _raise(db, exc)
    append_audit(
        db,
        entity_type="SEPAY_CONNECTION",
        entity_id=conn.id,
        action="CONNECT",
        actor_id=current_user.id,
        after_state={"scopes": conn.scopes.split()},
        ip_address=_ip(request),
    )
    await db.commit()
    return _status(conn)


@router.get("/bank-accounts", response_model=list[SepayBankAccountOut])
async def read_bank_accounts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authorized),
):
    try:
        accounts = await service.list_bank_accounts(db, user_id=current_user.id, now=_now())
    except SepayServiceError as exc:
        await _raise(db, exc)
    # A refresh along the way rotated the tokens: keep the new ones.
    await db.commit()
    return [SepayBankAccountOut.model_validate(a) for a in accounts]


@router.delete("", status_code=204)
async def remove_connection(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authorized),
):
    conn = await service.disconnect(db, user_id=current_user.id)
    if conn is not None:
        append_audit(
            db,
            entity_type="SEPAY_CONNECTION",
            entity_id=conn.id,
            action="DISCONNECT",
            actor_id=current_user.id,
            ip_address=_ip(request),
        )
        await db.commit()
    return Response(status_code=204)
