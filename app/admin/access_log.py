"""Audit every admin read of personal data (compliance audit SEC-02).

Writes already leave audit rows; reads did not, so nothing recorded which
operator opened whose KYC images, documents or profile. Each admin GET route
that returns personal data declares one dependency:

    @router.get(
        "/users/{user_id}",
        dependencies=[Depends(audit_admin_read("USER", id_param="user_id"))],
    )

It runs before the handler and commits a VIEW row -- who, what, when, from
which IP, and the path and query asked for (a search term is part of what
was looked at). It logs the access, not its outcome: a request that then
404s still leaves its row. If the row cannot be written the request fails
and nothing is read. tests/admin/test_admin_read_audit.py fails for any
/admin GET route that does not declare it, unless the route is exempted
there with a reason.

The role check here repeats the route's own: route-level dependencies run
before the handler's parameters, and a non-admin must get 403 without
leaving a row. `get_current_user` underneath is cached per request, so the
token is still read only once.
"""
from collections.abc import Callable, Coroutine
from typing import Any
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.users.models import Role, User
from app.utils.audit import append_audit
from app.utils.rbac import require_roles

VIEW_ACTION = "VIEW"

_require_admin = require_roles(Role.ADMIN, Role.SYSTEM_ADMIN)


def _as_uuid(value: Any) -> UUID | None:
    # The path converter has not validated it yet: dependencies run first.
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def audit_admin_read(
    entity_type: str, id_param: str | None = None
) -> Callable[..., Coroutine[Any, Any, None]]:
    """A route dependency that records an admin's read of `entity_type`.

    `id_param` names the path parameter holding the entity's id; leave it out
    for list and search routes, whose rows carry no entity id.
    """

    async def _record(
        request: Request,
        db: AsyncSession = Depends(get_db),
        current_user: User = Depends(_require_admin),
    ) -> None:
        detail: dict[str, Any] = {"path": request.url.path}
        if request.query_params:
            detail["query"] = dict(request.query_params)
        append_audit(
            db,
            entity_type=entity_type,
            entity_id=_as_uuid(request.path_params.get(id_param)) if id_param else None,
            action=VIEW_ACTION,
            actor_id=current_user.id,
            after_state=detail,
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()

    _record.audits_admin_read = True  # read by tests/admin/test_admin_read_audit.py
    return _record
