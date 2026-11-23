"""Change an existing account's role: promote it to admin, or demote an admin.

docs/specs/admin/user-role-change.md. The rules mirror set_user_status: nobody
acts on themselves, and admin-level accounts are a SYSTEM_ADMIN's to change.
On top of that, an ADMIN may grant ADMIN but never SYSTEM_ADMIN, a member with
money in progress is not promoted (one role per account, so they would lose the
screens they still need), and every change ends every session of the account.
"""
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.schemas import AdminUserDetail
from app.auth.service import revoke_other_sessions
from app.direct_transfer.models import (
    TRANCHE_LIVE_STATES,
    WINDOW_LIVE_STATES,
    Commitment,
    FundingWindow,
    TopUpPledge,
    Tranche,
)
from app.projects.borrower import unfinished_project
from app.users.models import USER_STATUS_SUSPENDED, Role, User
from app.utils.audit import append_audit

ADMIN_ROLES = (Role.ADMIN.value, Role.SYSTEM_ADMIN.value)
_RANK = {Role.ADMIN.value: 1, Role.SYSTEM_ADMIN.value: 2}  # members and no role rank 0

USER_NOT_FOUND = "USER_NOT_FOUND"
ROLE_CHANGE_SELF = "ROLE_CHANGE_SELF"
ROLE_CHANGE_FORBIDDEN = "ROLE_CHANGE_FORBIDDEN"
ROLE_UNCHANGED = "ROLE_UNCHANGED"
ROLE_CHANGE_NOT_SUPPORTED = "ROLE_CHANGE_NOT_SUPPORTED"
ROLE_CHANGE_SUSPENDED = "ROLE_CHANGE_SUSPENDED"
ROLE_CHANGE_EMAIL_UNVERIFIED = "ROLE_CHANGE_EMAIL_UNVERIFIED"
ROLE_CHANGE_OPEN_PROJECT = "ROLE_CHANGE_OPEN_PROJECT"
ROLE_CHANGE_LIVE_INVESTMENT = "ROLE_CHANGE_LIVE_INVESTMENT"
LAST_SYSTEM_ADMIN = "LAST_SYSTEM_ADMIN"


def _refuse(status_code: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail, headers={"X-Error-Code": code})


async def _money_in_progress(db: AsyncSession, user_id: UUID) -> HTTPException | None:
    """Why this member cannot be promoted yet (spec rule 6.3), or None."""
    if await unfinished_project(db, user_id) is not None:
        return _refuse(
            409, ROLE_CHANGE_OPEN_PROJECT,
            "This account has a project in progress. Promote it once the project has ended",
        )
    live_round = select(FundingWindow.id).where(FundingWindow.status.in_(WINDOW_LIVE_STATES))
    lending = await db.execute(
        select(
            or_(
                exists().where(
                    Tranche.investor_id == user_id,
                    Tranche.status.in_(TRANCHE_LIVE_STATES),
                    Tranche.window_id.in_(live_round),
                ),
                exists().where(
                    Commitment.investor_id == user_id,
                    Commitment.status == "PLEDGED",
                    Commitment.window_id.in_(live_round),
                ),
                exists().where(
                    TopUpPledge.investor_id == user_id,
                    TopUpPledge.status.in_(("PLEDGED", "SIGNED")),
                    TopUpPledge.window_id.in_(live_round),
                ),
            )
        )
    )
    if lending.scalar():
        return _refuse(
            409, ROLE_CHANGE_LIVE_INVESTMENT,
            "This account has money in a funding round that is still live. Promote it once the loan is repaid",
        )
    return None


async def change_user_role(
    db: AsyncSession,
    user_id: UUID,
    *,
    new_role: str,
    note: str | None,
    actor: User,
    ip_address: str | None = None,
) -> AdminUserDetail:
    # The target and every SYSTEM_ADMIN, locked in one statement in id order: two system admins demoting each other
    # at once queue here instead of deadlocking, and the second sees the first's change (spec rule 7).
    rows = (
        await db.execute(
            select(User)
            .where(or_(User.id == user_id, User.role == Role.SYSTEM_ADMIN.value))
            .order_by(User.id)
            .with_for_update(of=User)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    target = next((row for row in rows if row.id == user_id), None)
    if target is None:
        raise _refuse(404, USER_NOT_FOUND, "User not found")
    if target.id == actor.id:
        raise _refuse(409, ROLE_CHANGE_SELF, "You cannot change your own role")

    old_role = target.role
    actor_is_system_admin = actor.role == Role.SYSTEM_ADMIN.value
    if old_role in ADMIN_ROLES and not actor_is_system_admin:
        raise _refuse(403, ROLE_CHANGE_FORBIDDEN, "Only a system admin can change an admin account's role")
    if new_role == Role.SYSTEM_ADMIN.value and not actor_is_system_admin:
        raise _refuse(403, ROLE_CHANGE_FORBIDDEN, "Only a system admin can grant the system admin role")
    if new_role == old_role:
        raise _refuse(409, ROLE_UNCHANGED, "The account already has this role")

    if old_role not in ADMIN_ROLES:
        if new_role not in ADMIN_ROLES:
            raise _refuse(
                409, ROLE_CHANGE_NOT_SUPPORTED,
                "Members choose between investor and business themselves; only admin roles are granted here",
            )
        if target.status == USER_STATUS_SUSPENDED:
            raise _refuse(409, ROLE_CHANGE_SUSPENDED, "Restore this account before promoting it")
        if not target.email_verified:
            raise _refuse(409, ROLE_CHANGE_EMAIL_UNVERIFIED, "This account has not verified its email address")
        refusal = await _money_in_progress(db, target.id)
        if refusal is not None:
            raise refusal

    if old_role == Role.SYSTEM_ADMIN.value:
        others = [
            row for row in rows
            if row.id != target.id and row.role == Role.SYSTEM_ADMIN.value and row.status != USER_STATUS_SUSPENDED
        ]
        if not others:
            raise _refuse(409, LAST_SYSTEM_ADMIN, "The platform needs at least one system admin")

    target.role = new_role
    # Every session ends, so the account signs in again under its new role; an admin role then needs a second
    # factor (app/utils/jwt.SECOND_FACTOR_ROLES) and an old member session cannot carry on as an admin one.
    sessions_revoked = (await revoke_other_sessions(db, target.id, current_session_id=None))["revoked"]
    append_audit(
        db,
        entity_type="USER",
        entity_id=target.id,
        action="USER_ROLE_CHANGED",
        actor_id=actor.id,
        before_state={"role": old_role},
        after_state={
            "role": new_role,
            "direction": "PROMOTION" if _RANK.get(new_role, 0) > _RANK.get(old_role, 0) else "DEMOTION",
            "note": note,
            "sessions_revoked": sessions_revoked,
            "is_external_domain": not target.email.strip().lower().endswith("@fundlok.com"),
        },
        ip_address=ip_address,
    )
    await db.commit()
    await db.refresh(target)
    return AdminUserDetail.model_validate(target)
