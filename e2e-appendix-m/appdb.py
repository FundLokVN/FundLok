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
    """Run one coroutine on a fresh event loop, then drop the engine's pooled connections: they are bound to the loop
    that opened them, and a script may call several helpers (several loops) in a row."""
    async def wrapped():
        from app.core import database
        try:
            return await coro
        finally:
            await database.engine.dispose()
    return asyncio.run(wrapped())


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


# --------------------------------------------------------------------------- commit-then-accept (Money flow FINAL v1.0)
def _fake_neac(monkeypatch_target):
    """NEAC verifies the SME's enterprise signature; it cannot be reached with a test certificate. Same approach as
    conftest.fake_neac: the PDF carries %FAKE-NEAC-SIGNER:<id> and the fake returns that signer."""
    import re as _re

    from app.market.investor_agreement import neac

    async def _verify(content: bytes, filename: str):
        ids = [m.decode() for m in _re.findall(rb"%FAKE-NEAC-SIGNER:([A-Za-z0-9]+)", content)] or ["000000000000"]
        sigs = [neac.NeacSignature(
            issuer={"cn": "FPT Certification Authority SHA256", "o": "FPT CORPORATION", "c": "VN"},
            signer={"cn": "E2E SIGNER", "c": "VN", "cccd": f"CCCD:{i}"}, intact="Không bị thay đổi",
            certificate_validity="Thời điểm có hiệu lực: 2026-03-18; Thời điểm hết hiệu lực: 2027-03-26",
            validity="Hợp lệ tại thời điểm ký", timestamp_status="Không",
            signed_time="Ngày 19 tháng 11 năm 2026, 10:00:00 [UTC+07:00]", reason_invalidity="",
            ocsp="Chứng thư số chưa bị thu hồi.", crl="Chứng thư số chưa bị thu hồi") for i in ids]
        return neac.NeacResult(message="Success", signatures=sigs)

    monkeypatch_target.verify_document = _verify


def sme_sign_window_contracts(window_id: str, sme_user_id: str, tax_id: str, only: list[str] | None = None) -> list[str]:
    """The SME signs each lender's Day-13 loan contract with its enterprise certificate: the app's own
    day13_contracts.record_sme_signed, with NEAC faked (see _fake_neac). Returns the contract numbers signed."""
    async def go():
        from sqlalchemy import select

        from app.core.database import SessionLocal
        from app.core.timezone import ICT
        from app.direct_transfer.models import FundingWindow
        from app.market import day13_contracts, funding
        from app.market.investor_agreement import neac
        from app.users.models import User
        from tests.loans.test_listing_signing import _fill_signature_fields, _markers
        _fake_neac(neac)
        storage = funding.evidence_storage()
        done = []
        async with SessionLocal() as db:
            window = await db.get(FundingWindow, uuid.UUID(window_id))
            actor = await db.get(User, uuid.UUID(sme_user_id))
            for row in await day13_contracts.contracts_for(db, window.id):
                if row.status != "GENERATED" or (only and row.contract_no not in only):
                    continue
                pdf = await storage.get(row.file_key, max_bytes=10_000_000)
                signed = _markers(_fill_signature_fields(pdf, ("SME_Signature",)), f"FAKE-NEAC-SIGNER:{tax_id}")
                await day13_contracts.record_sme_signed(
                    db, window=window, row=row, content=signed, file_name="signed.pdf", storage=storage,
                    now=dt.datetime.now(ICT), actor=actor)
                done.append(row.contract_no)
            await db.commit()
        return done
    return _run(go())


def run_job(name: str, **kw) -> dict:
    """A Cloud Scheduler job, run in-process with the real clock (the /internal/jobs routes need Google OIDC)."""
    async def go():
        from app.core.database import SessionLocal
        from app.scheduled_jobs import acceptance, day14
        fn = {"funding-advance": acceptance.run_funding_advance,
              "acceptance-reminders": acceptance.send_acceptance_reminders,
              "day14-cutoff": day14.run_day14_cutoff,
              "day14-transfer-reminders": day14.send_day14_reminders}[name]
        async with SessionLocal() as db:
            out = await fn(db, **kw)
            await db.commit()
            return out
    return _run(go())
