import time
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionLocal
from app.system.models import SystemSetting
from app.system.schemas import MaintenanceState, VerificationModeState
from app.utils.audit import append_audit

# Key used in the system_settings table for the maintenance toggle.
MAINTENANCE_KEY = "maintenance_mode"


async def get_maintenance(db: AsyncSession) -> MaintenanceState:
    row = await db.get(SystemSetting, MAINTENANCE_KEY)
    if row is None:
        return MaintenanceState(enabled=False)
    value = row.value or {}
    return MaintenanceState(
        enabled=bool(value.get("enabled", False)),
        message=value.get("message"),
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


async def set_maintenance(
    db: AsyncSession, *, enabled: bool, message: str | None, actor_id: UUID | None
) -> MaintenanceState:
    value = {"enabled": enabled, "message": message}
    row = await db.get(SystemSetting, MAINTENANCE_KEY)
    if row is None:
        row = SystemSetting(
            key=MAINTENANCE_KEY,
            value=value,
            description="Global maintenance-mode toggle",
            updated_by=actor_id,
        )
        db.add(row)
    else:
        row.value = value
        row.updated_by = actor_id
    await db.commit()
    _invalidate_maintenance_cache()
    return await get_maintenance(db)


# Key for the KYC/KYB verification mode (see VerificationMode in schemas).
VERIFICATION_MODE_KEY = "verification_mode"
VERIFICATION_MODE_AUTOMATIC = "AUTOMATIC"
VERIFICATION_MODE_MANUAL = "MANUAL"


async def get_verification_mode(db: AsyncSession) -> VerificationModeState:
    """AUTOMATIC unless a system admin has switched it. Read from the DB on
    every verification request (no cache): a switch must take effect on the
    very next submission, and a stale AUTOMATIC would send documents to a
    provider the operator just turned off."""
    # populate_existing: after set_verification_mode commits, the cached row's
    # server-set updated_at is expired and would otherwise lazy-load.
    row = await db.get(SystemSetting, VERIFICATION_MODE_KEY, populate_existing=True)
    mode = (row.value or {}).get("mode") if row is not None else None
    if mode not in (VERIFICATION_MODE_AUTOMATIC, VERIFICATION_MODE_MANUAL):
        mode = VERIFICATION_MODE_AUTOMATIC
    return VerificationModeState(
        mode=mode,
        updated_at=row.updated_at if row is not None else None,
        updated_by=row.updated_by if row is not None else None,
    )


async def is_manual_verification(db: AsyncSession) -> bool:
    return (await get_verification_mode(db)).mode == VERIFICATION_MODE_MANUAL


async def set_verification_mode(
    db: AsyncSession, *, mode: str, actor_id: UUID, ip_address: str | None = None
) -> VerificationModeState:
    """Switch the mode and record who did it. Audited, unlike maintenance:
    MANUAL replaces the provider's identity checks with a person's judgement,
    so when and by whom it was turned on has to be on record."""
    before = (await get_verification_mode(db)).mode
    row = await db.get(SystemSetting, VERIFICATION_MODE_KEY)
    if row is None:
        row = SystemSetting(
            key=VERIFICATION_MODE_KEY,
            value={"mode": mode},
            description="KYC/KYB verification mode: AUTOMATIC (provider) or MANUAL (admin review)",
            updated_by=actor_id,
        )
        db.add(row)
    else:
        row.value = {"mode": mode}
        row.updated_by = actor_id
    append_audit(
        db,
        entity_type="SYSTEM_SETTING",
        entity_id=None,
        action="VERIFICATION_MODE_CHANGED",
        actor_id=actor_id,
        before_state={"mode": before},
        after_state={"mode": mode},
        ip_address=ip_address,
    )
    await db.commit()
    return await get_verification_mode(db)


# --- Lightweight cache so the request middleware doesn't hit the DB every call.
_MAINT_TTL_SECONDS = 5.0
_maint_cache: dict = {"value": None, "ts": 0.0}


def _invalidate_maintenance_cache() -> None:
    _maint_cache["value"] = None
    _maint_cache["ts"] = 0.0


async def is_maintenance_active() -> tuple[bool, str | None]:
    """(enabled, message) for the middleware. Cached for a few seconds.

    HANDOFF-02 Fix A: this now opens its own AsyncSession via `async with
    SessionLocal()` instead of the old sync `SessionLocal(); ...; db.close()`
    pattern -- still independent of the `get_db` request dependency (that
    part of the original design is unchanged), just async now.
    """
    now = time.monotonic()
    if _maint_cache["value"] is None or now - _maint_cache["ts"] > _MAINT_TTL_SECONDS:
        async with SessionLocal() as db:
            state = await get_maintenance(db)
            _maint_cache["value"] = (state.enabled, state.message)
        _maint_cache["ts"] = now
    return _maint_cache["value"]
