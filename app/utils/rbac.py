from collections.abc import Callable

from fastapi import Depends, HTTPException, status

from app.users.models import Role, User
from app.utils.jwt import get_current_user, get_current_user_allow_suspended


def require_roles(*allowed: Role, allow_suspended: bool = False) -> Callable[..., User]:
    allowed_values = {r.value for r in allowed}

    session_user = get_current_user_allow_suspended if allow_suspended else get_current_user

    def _dep(current_user: User = Depends(session_user)) -> User:
        if current_user.role not in allowed_values:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized for this action",
            )
        return current_user

    return _dep
