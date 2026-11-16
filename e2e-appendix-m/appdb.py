"""Steps that cannot be driven through the API on a local machine, done with the app's OWN service code.

Each one is a documented workaround (see README "What is bypassed"):
  * mint_staff_sessions   -- admin endpoints need a second factor; local .env has no TOTP_ENCRYPTION_KEY, so TOTP
                             cannot be enrolled. Same approach as conftest._second_factor_session.
  * record_agreements     -- the Lender Service Agreement needs a Zalo OTP and a signed PDF. Same as
                             conftest.make_eligible_investor(signed_agreement=True).
  * attach_and_activate   -- SME signing needs three CA-signed PDFs verified with NEAC. Same as
                             conftest.attach_signed_listing_documents, then the real activation service.
  * run_jobs              -- worker jobs (app/direct_transfer/jobs.py) are not deployed in production; run once
                             with a fixed clock to show what they would do.
"""
import asyncio
import datetime as dt
import uuid

import e2e_lib as L  # noqa: F401  (sets cwd, sys.path, DATABASE_URL)


def _run(coro):
    return asyncio.run(coro)


def mint_staff_sessions() -> dict:
    async def go():
        from sqlalchemy import text

        from app.auth.service import create_token_pair
        from app.core.database import SessionLocal
        out = {}
        async with SessionLocal() as db:
            for key, email in (("admin", "admin@fundlok.vn"), ("sysadmin", "sysadmin@fundlok.vn")):
                uid = (await db.execute(text("select id from users where email=:e"), {"e": email})).scalar_one()
                tokens = await create_token_pair(db, str(uid), second_factor="otp")
                await db.commit()
                out[key] = {"email": email, "id": str(uid), "token": tokens["access_token"]}
        return out
    return _run(go())


def record_agreements(user_ids: list[str]) -> None:
    async def go():
        from sqlalchemy import select

        from app.core.database import SessionLocal
        from app.market.investor_agreement.models import InvestorServiceAgreement
        from app.market.investor_agreement.service import VERSION
        async with SessionLocal() as db:
            for uid_s in user_ids:
                uid = uuid.UUID(uid_s)
                have = (await db.execute(select(InvestorServiceAgreement.id).where(
                    InvestorServiceAgreement.investor_id == uid, InvestorServiceAgreement.version == VERSION))).first()
                if not have:
                    db.add(InvestorServiceAgreement(
                        investor_id=uid, version=VERSION, signed_at=dt.datetime.now(dt.timezone.utc), party_b={},
                        signed_file_key=f"e2e/investor-agreement/{uid}.pdf", document_sha256="0" * 64,
                        file_name="signed.pdf", file_size_bytes=1024))
            await db.commit()
    _run(go())


def attach_and_activate(application_id: str, admin_id: str) -> bool:
    async def go():
        from app.core.database import SessionLocal
        from app.projects.service import activate_project_if_approved
        from conftest import attach_signed_listing_documents
        async with SessionLocal() as db:
            await attach_signed_listing_documents(db, uuid.UUID(application_id))
            ok = await activate_project_if_approved(db, uuid.UUID(application_id), actor_id=uuid.UUID(admin_id))
            await db.commit()
            return ok
    return _run(go())


def run_jobs(clocks: list[dt.datetime]) -> list[dict]:
    """Run repayment_day_job then invoice_job once per clock, all in one event loop (the async engine's pool is
    bound to the loop that first used it)."""
    async def go():
        from app.direct_transfer import jobs
        out = []
        for clock in clocks:
            ctx = {"clock": lambda clock=clock: clock}
            out.append({"clock": clock.isoformat(), "repayment_day_job": await jobs.repayment_day_job(ctx),
                        "invoice_job": await jobs.invoice_job(ctx)})
        return out
    return _run(go())


def openapi_paths() -> list[str]:
    from app.main import app
    return list(app.openapi()["paths"].keys())
