from fastapi import HTTPException, status

from app.users.models import User


def require_verified_email(user: User, *, method: str = "a passkey") -> None:
    """Require proof of email ownership before adding a sign-in credential."""
    if not user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Verify your email address before adding {method}.",
        )
