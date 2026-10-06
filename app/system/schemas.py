from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class MaintenanceStatus(BaseModel):
    """Public maintenance status — safe to expose unauthenticated."""

    enabled: bool
    message: str | None = None


class MaintenanceState(MaintenanceStatus):
    """Full maintenance status, incl. audit fields (admin only)."""

    updated_at: datetime | None = None
    updated_by: UUID | None = None


class MaintenanceUpdate(BaseModel):
    enabled: bool
    message: str | None = None


# How KYC/KYB submissions are decided.
#   AUTOMATIC: the verification provider checks the documents and returns a
#              verdict in the same request (the normal path).
#   MANUAL:    the provider is not called at all. Documents are stored and the
#              attempt waits in MANUAL_REVIEW for an admin. Used while the
#              provider is down.
VerificationMode = Literal["AUTOMATIC", "MANUAL"]


class VerificationModeStatus(BaseModel):
    """What a signed-in user may see: which mode is in force."""

    mode: VerificationMode


class VerificationModeState(VerificationModeStatus):
    """Full state for the system settings screen."""

    updated_at: datetime | None = None
    updated_by: UUID | None = None


class VerificationModeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: VerificationMode
