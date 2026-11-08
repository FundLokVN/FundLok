"""Shared pytest fixtures for the FundLok behavioral test suite.

Originally built for HANDOFF-01 (see docs/handoffs/HANDOFF-01-test-suite.md);
updated for HANDOFF-02 Fix A (docs/handoffs/HANDOFF-02-structural-fixes.md),
which moved the app from sync SQLAlchemy to async. Per HANDOFF-01 §4, the
`get_db` override was kept isolated to this one fixture specifically so this
migration only had to touch one seam -- it did.

Design constraints:
- Schema comes from real Alembic migrations (`alembic upgrade head`) against a
  throwaway Postgres, never from `Base.metadata.create_all` — that would hide
  migration drift that `alembic check` exists to catch. Alembic itself stays
  on a sync psycopg2 URL (HANDOFF-02 Fix A) even though the app/tests are
  async, so this file derives a separate sync URL just for that migration
  step.
- Tests are isolated by truncating all tables between tests (not per-test
  transactions), because some app code opens its own session outside of
  `get_db` (e.g. app.system.service.is_maintenance_active uses SessionLocal()
  directly), which would not see an uncommitted outer transaction.
- All external I/O (email, R2, Brankas, Didit, Turnstile) is mocked/disabled.
- Async: test functions are plain `async def` (pytest.ini sets
  asyncio_mode = auto) and the HTTP client is httpx.AsyncClient over the
  ASGI app directly, per HANDOFF-02 Fix A.
"""
import contextlib
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --------------------------------------------------------------------------- #
# Environment must be set BEFORE anything imports app.core.config, since the
# `settings` object (and the SQLAlchemy `engine` bound to it) is constructed
# once at import time.
# --------------------------------------------------------------------------- #
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://test_user:test_password@localhost:5432/test_db",
)
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
# A real Fernet key, so the TOTP tests exercise the encrypted path rather than a
# bypass. Deliberately NOT derived from SECRET_KEY — see app/auth/totp_crypto.py
# for why the two secrets are separate.
os.environ.setdefault("TOTP_ENCRYPTION_KEY", "n5W7jlQOzlCKwRHfeCH4cpb6vUvai3kOX0GnEgEh9i4=")
# Same reasoning for the Gmail app password (app/admin/internal_email/crypto.py),
# and its own key, as in production.
os.environ.setdefault("EMAIL_SETTINGS_ENCRYPTION_KEY", "Xx7VZ9pu2gIfJmp5ZLKE3WMA6Hj6w5Hu0qMj2Bm2F9M=")
os.environ.setdefault("ALGORITHM", "HS256")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "30")
os.environ.setdefault("REFRESH_TOKEN_EXPIRE_DAYS", "14")
# The test client talks to the ASGI app over plain http://testserver (see the
# `client` fixture below), never TLS. config.py defaults COOKIE_SECURE to True
# (correct for prod), but httpx's cookie jar honours the Secure attribute for
# real -- it stores a Secure-flagged cookie after a response but silently
# omits it from every later request on this non-https base_url. Without this
# override, any test that sets a cookie in one call and expects it back in a
# later call (auth/session/remember-me flows) fails with the cookie simply
# missing, which looks like an auth bug rather than a transport mismatch.
# .env.example documents the same COOKIE_SECURE=False for local dev, for the
# same underlying reason (plain-http uvicorn).
os.environ.setdefault("COOKIE_SECURE", "False")
# Explicitly blank (not just "unset") so a developer's local .env can't leak a
# real Turnstile secret into the test run and make verify_turnstile_token try
# a real network call.
os.environ["CLOUDFLARE_TURNSTILE_SECRET_KEY"] = ""
# With no secret, captcha must be explicitly not required, or every protected
# endpoint answers 503 (SEC-08). The captcha tests turn it back on.
os.environ["CAPTCHA_REQUIRED"] = "False"
# Off for the same reason as the Turnstile secret: a local .env must not make
# unrelated tests start getting 429s. tests/core/test_rate_limit.py enables it.
os.environ["RATE_LIMIT_ENABLED"] = "False"
# Belt-and-suspenders: never let tests touch real R2 / Didit even if a local
# .env has them configured.
os.environ["R2_ENDPOINT_URL"] = ""
os.environ["DIDIT_API_KEY"] = ""

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from app.core.base import Base  # noqa: E402
from app.core.database import engine, get_db  # noqa: E402
from app.lending.models import Document  # noqa: E402
from app.main import app  # noqa: E402
from app.users.models import RefreshToken, User  # noqa: E402

TEST_DATABASE_URL = os.environ["DATABASE_URL"]
# Alembic intentionally stays on a sync driver (HANDOFF-02 Fix A) -- derive
# its own psycopg2 URL from whatever async URL the app/tests are using,
# rather than requiring a second env var. A no-op .replace() if DATABASE_URL
# is already a sync URL.
ALEMBIC_DATABASE_URL = TEST_DATABASE_URL.replace("+asyncpg", "+psycopg2")
ALEMBIC_COMMAND = [sys.executable, "-m", "alembic"]


def iter_endpoint_routes(routes):
    """Recursively flatten app.routes into leaf endpoint routes (path +
    methods), independent of how a given Starlette/FastAPI version represents
    included sub-routers (older versions flatten them into APIRoute objects
    directly; newer ones wrap them, e.g. fastapi.routing._IncludedRouter with
    an `original_router` attribute). Used by route-introspection tests that
    assert "no mutation route exists" for a resource."""
    for route in routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            yield from iter_endpoint_routes(original_router.routes)
            continue
        sub_routes = getattr(route, "routes", None)
        if sub_routes:
            yield from iter_endpoint_routes(sub_routes)
        elif hasattr(route, "path") and hasattr(route, "methods"):
            yield route


@contextlib.asynccontextmanager
async def audit_rows_backdatable(db_session):
    """Lift the audit_logs append-only trigger (migration 17ec24f17dba) for
    the current transaction, so a throttle test can age its rows to
    simulate time passing. Test setup only: the app never updates an audit
    row. Re-enabled before the caller commits; if the update fails the
    rollback restores it, because ALTER TABLE is transactional."""
    await db_session.execute(text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only"))
    yield
    await db_session.execute(text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only"))


@pytest.fixture(scope="session", autouse=True)
def _migrate_schema():
    """Build the schema with real Alembic migrations, mirroring CI's
    `alembic upgrade head` — never `Base.metadata.create_all`.

    This intentionally shells out to `alembic upgrade head` as a subprocess
    (exactly like the CI "tests" job's dedicated migration step) rather than
    calling alembic's Python API in-process. alembic/env.py reads
    `settings.DATABASE_URL` directly (not the Config object's
    `sqlalchemy.url`, which command.upgrade()'s in-process API would let us
    override) -- and `settings` is a module-level singleton already
    constructed with the asyncpg URL by the time this fixture runs. A
    subprocess gets its own fresh `settings` from the env we pass it, which
    is the only way to hand Alembic a sync psycopg2 URL while the rest of the
    suite runs against the async one.
    """
    import subprocess

    env = {**os.environ, "DATABASE_URL": ALEMBIC_DATABASE_URL}
    result = subprocess.run(
        [*ALEMBIC_COMMAND, "upgrade", "head"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade head failed (exit {result.returncode}):\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
    yield


@pytest.fixture(autouse=True)
async def db_session():
    """One real AsyncSession per test, wired in as the `get_db` override.

    This is the single seam HANDOFF-01 called out and HANDOFF-02's async
    migration touched.
    """
    TestingSessionLocal = async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = TestingSessionLocal()

    async def _get_db_override():
        yield session

    app.dependency_overrides[get_db] = _get_db_override
    try:
        yield session
    finally:
        app.dependency_overrides.pop(get_db, None)
        # Truncate everything so tests are order-independent. CASCADE handles
        # FK ordering; RESTART IDENTITY keeps id defaults sane (none of our
        # PKs are serial today, but this is cheap insurance).
        #
        # Deliberately reuses THIS session/connection rather than checking
        # out a fresh one from the pool: doing the truncate on a second
        # connection immediately after closing this one raced against
        # asyncpg's own connection-reset bookkeeping (observed as
        # "InterfaceError: cannot perform operation: another operation is in
        # progress" during fixture teardown). rollback() first clears any
        # leftover implicit transaction/aborted state from whatever the test
        # did (e.g. a request that ended in an HTTPException).
        await session.rollback()
        # `custodial_accounts` is excluded alongside `alembic_version`: the
        # ledger-foundation migration seeds exactly one scope='PLATFORM' row
        # as fixed reference data (ADR-003), not per-test state -- truncating
        # it here would silently delete that seed after the first test and
        # break every subsequent resolve_custodial_account() call. Tables
        # that reference it (ledger_accounts, ledger_entries, ...) are still
        # truncated normally.
        #
        # `bank_rate_config` is excluded for the identical reason (T0b): the
        # score-run-persistence migration seeds exactly one board rate
        # (12.0%, effective 2026-09-17) as fixed reference data. Found by
        # actually running this suite against real Postgres (T4, HANDOFF-03)
        # -- every score-run test after the first failed with "no
        # bank_rate_config row is in force" once the seed row was truncated
        # away, because `bank_rate_config.created_by_actor_id` is a FK into
        # `users`, and CASCADE follows that FK the same way it follows
        # `custodial_accounts.contract_id` into `contracts` below.
        tables = [
            t.name
            for t in Base.metadata.sorted_tables
            if t.name not in ("alembic_version", "custodial_accounts", "bank_rate_config")
        ]
        if tables:
            await session.execute(text(f'TRUNCATE TABLE {", ".join(tables)} RESTART IDENTITY CASCADE'))
            # Excluding `custodial_accounts` from the TRUNCATE list above
            # isn't sufficient on its own: `custodial_accounts.contract_id`
            # is a FK to `contracts.id`, and Postgres's TRUNCATE ... CASCADE
            # truncates *every* table with a FK into any table named in the
            # statement -- so truncating `contracts` (correctly, per-test
            # data) cascades into `custodial_accounts` too and wipes the
            # seeded PLATFORM row regardless. Re-seed it idempotently so it
            # survives every test, the same way the migration provisions it
            # once for a real environment.
            await session.execute(
                text(
                    "INSERT INTO custodial_accounts (scope, status) "
                    "SELECT 'PLATFORM', 'ACTIVE' "
                    "WHERE NOT EXISTS (SELECT 1 FROM custodial_accounts WHERE scope = 'PLATFORM')"
                )
            )
            # `bank_rate_config` gets the same CASCADE-from-`users` treatment
            # as `custodial_accounts`, but unlike that idempotent
            # insert-if-missing, this resets to exactly the migration's one
            # baseline row every test: a test that calls set_bank_rate()
            # (T0b's rate-history-is-append-only design means those rows
            # would otherwise accumulate across tests and leak into
            # `get_current_bank_rate()` in a later, unrelated test).
            await session.execute(text("TRUNCATE TABLE bank_rate_config RESTART IDENTITY CASCADE"))
            await session.execute(
                text(
                    "INSERT INTO bank_rate_config (id, rate_pct, effective_from) "
                    "VALUES (gen_random_uuid(), 12.0, '2026-09-17')"
                )
            )
            await session.commit()
        await session.close()


@pytest.fixture(autouse=True)
def _pin_feature_flags(monkeypatch):
    """Behaviour flags start at their code defaults in every test.

    Settings load the developer's local .env, so a flag switched on there
    (GVERIFY_KYB_REQUIRE_REP_MATCH was, on 2026-09-29) silently changed what
    unrelated tests saw: every KYB happy path became a rejection locally while
    CI, which has no .env, stayed green. A test that needs a flag on sets it
    itself; this autouse fixture runs first, so the test's monkeypatch wins.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "GVERIFY_KYB_REQUIRE_REP_MATCH", False)


@pytest.fixture(autouse=True)
def _clear_oauth_key_cache():
    """OAuth discovery/JWKS are cached per process (app/oauth/service.py).
    Tests swap in their own keys via _fetch_json, so start each one empty or
    a previous test's fake keys would answer instead."""
    from app.oauth import service as oauth_service

    oauth_service._JSON_CACHE.clear()
    yield
    oauth_service._JSON_CACHE.clear()


@pytest.fixture(autouse=True)
def _mock_external_io(monkeypatch):
    """Mock all external I/O per HANDOFF-01 guardrail: tests must never hit a
    real SMTP server, Cloudflare R2, Brankas, or Didit endpoint."""
    monkeypatch.setattr("app.auth.service.send_verification_email", lambda *a, **kw: None)
    monkeypatch.setattr("app.auth.service.send_password_reset_email", lambda *a, **kw: None)
    monkeypatch.setattr("app.auth.service.send_existing_account_notice", lambda *a, **kw: None)
    # Imported into two modules, so it needs stubbing at both call sites. A
    # test that asserts on the notice re-patches the same target itself; this
    # autouse fixture runs first, so the test's own monkeypatch still wins.
    monkeypatch.setattr("app.auth.service.send_password_changed_notice", lambda *a, **kw: None)
    monkeypatch.setattr("app.users.service.send_password_changed_notice", lambda *a, **kw: None)

    # Backstop under all of the above. Patching senders one call site at a
    # time is a list that silently goes stale -- send_password_changed_notice
    # was added to two modules and to neither list, and the gap only surfaced
    # in CI, because a developer machine has mailpit on :1025 (ADR-005) and
    # quietly accepts the mail. Every sender in app/utils/email.py bottoms out
    # here, so a new one can no longer reach the network just by being
    # forgotten.
    monkeypatch.setattr("app.utils.email.send_email", lambda *a, **kw: None)

    # Admin > Email talks to Google (OAuth token endpoint, Gmail API) over
    # its own HTTP client, not send_email. Opening it fails loudly in every
    # test; the internal-email tests replace it with a fake Google.
    def _no_google(*a, **kw):
        raise AssertionError("a test tried to contact Google")

    monkeypatch.setattr("app.admin.internal_email.google._client", _no_google)

    # GVerify verification-document retention writes to R2 — never in tests.
    async def _no_store(*a, **kw):
        return None

    monkeypatch.setattr("app.gverify.storage.store_kyc_documents", _no_store)
    monkeypatch.setattr("app.gverify.storage.store_kyb_document", _no_store)

    # Breached-password lookups (app/auth/password_policy.py) see an empty
    # Have I Been Pwned range: no password is breached. The SEC-03 tests
    # re-patch it to return a match.
    async def _no_breaches(prefix: str) -> str:
        return ""

    monkeypatch.setattr("app.auth.password_policy._fetch_range", _no_breaches)

    # Opening a funding window and signing an investor's acceptance store the signed documents in R2.
    from app.direct_transfer.storage import InMemoryStorage

    monkeypatch.setattr("app.market.funding.evidence_storage", InMemoryStorage)
    yield


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# --------------------------------------------------------------------------- #
# Behavioral factory helpers. These call the real API wherever an endpoint
# exists. Two preconditions have no reachable endpoint at all (see comments
# below on approve_kyc_documents and make_admin) -- those use a direct ORM
# write (never raw SQL) purely as test setup, not as the behavior under test.
# All of these are async closures now (HANDOFF-02): callers must `await` them.
# --------------------------------------------------------------------------- #

DEFAULT_PASSWORD = "Str0ngPassw0rd!1"


async def record_consent(db_session, user_id) -> None:
    """The user accepted the current Terms & Services and data consent, as a
    real user does right after sign-up (docs/specs/consent/). Test setup, not
    the behaviour under test: tests/consent/ covers the real flow."""
    from app.consent import documents
    from app.consent.models import UserConsent

    db_session.add(
        UserConsent(
            user_id=uuid.UUID(str(user_id)),
            version=documents.CURRENT_VERSION,
            consents={doc: True for doc in documents.DOCUMENTS},
            verified_by=documents.VERIFIED_BY_EMAIL_OTP,
        )
    )
    await db_session.commit()


@pytest.fixture
def make_user(client, db_session):
    """Register (+optionally self-select a role for) a user; return tokens/headers.

    Every user has accepted the current Terms, as after a real sign-up, unless
    `consent=False` (for tests of the consent gate itself)."""

    async def _make(
        role: str | None = None,
        email: str | None = None,
        full_name: str = "Test User",
        consent: bool = True,
    ):
        email = email or f"user-{uuid.uuid4().hex[:12]}@example.com"
        reg = await client.post(
            "/auth/register",
            json={"email": email, "password": DEFAULT_PASSWORD, "full_name": full_name},
        )
        # Register returns a uniform ack (no user body, 201) so it can't be used
        # to enumerate accounts; the user id comes from the login response.
        assert reg.status_code == 201, reg.text

        login = await client.post("/auth/login", json={"email": email, "password": DEFAULT_PASSWORD})
        assert login.status_code == 200, login.text
        user_id = login.json()["id"]
        if consent:
            await record_consent(db_session, user_id)
        access_token = login.cookies.get("access_token")
        refresh_token = login.cookies.get("refresh_token")
        headers = {"Authorization": f"Bearer {access_token}"}

        if role:
            role_resp = await client.patch("/users/me/role", json={"role": role}, headers=headers)
            assert role_resp.status_code == 200, role_resp.text

        return {
            "id": user_id,
            "email": email,
            "password": DEFAULT_PASSWORD,
            "role": role,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "headers": headers,
        }

    return _make


def auth_headers(user: dict) -> dict:
    return user["headers"]


@pytest.fixture
def make_admin(make_user, db_session):
    """ADMIN is provisioned out-of-band per CLAUDE.md ("FL operations staff"),
    not self-selectable via /users/me/role (only SME/INVESTOR are). There is
    no admin-provisioning endpoint in scope for this suite, so this fixture
    registers a normal user through the real API and then promotes it with a
    direct ORM write -- the only way to reach that state today."""

    async def _make(email: str | None = None):
        user = await make_user(role=None, email=email)
        result = await db_session.execute(select(User).where(User.id == user["id"]))
        row = result.scalar_one()
        row.role = "ADMIN"
        await db_session.commit()
        return await _second_factor_session(db_session, user)

    return _make


@pytest.fixture
def make_system_admin(make_user, db_session):
    """SYSTEM_ADMIN, provisioned out of band like make_admin does for ADMIN."""

    async def _make(email: str | None = None):
        user = await make_user(role=None, email=email)
        result = await db_session.execute(select(User).where(User.id == user["id"]))
        row = result.scalar_one()
        row.role = "SYSTEM_ADMIN"
        await db_session.commit()
        return await _second_factor_session(db_session, user)

    return _make


async def _second_factor_session(db_session, user: dict) -> dict:
    """Swap the admin's password-only tokens for a session opened with a
    second factor, which admin endpoints require (app/utils/jwt.get_current_user).

    Minted through the service as a TOTP sign-in would mint it. Enrolling TOTP
    over the API instead would leave 2FA state and audit rows behind that the
    tests built on these fixtures do not expect. tests/auth/test_admin_second_factor.py
    covers the real sign-in paths.
    """
    from app.auth.service import create_token_pair

    tokens = await create_token_pair(db_session, user["id"], second_factor="otp")
    await db_session.commit()
    user["access_token"] = tokens["access_token"]
    user["refresh_token"] = tokens["refresh_token"]
    user["headers"] = {"Authorization": f"Bearer {tokens['access_token']}"}
    return user


@pytest.fixture
def make_project(client):
    async def _make(sme: dict, legal_name: str | None = None, tax_id: str | None = None):
        legal_name = legal_name or f"Test Co {uuid.uuid4().hex[:8]}"
        tax_id = tax_id if tax_id is not None else f"TAX-{uuid.uuid4().hex[:10]}"
        resp = await client.post(
            "/projects",
            json={"legal_name": legal_name, "tax_id": tax_id},
            headers=auth_headers(sme),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    return _make


@pytest.fixture
def make_loan_application(client):
    async def _make(sme: dict, project_id: str, requested_amount: str = "80000000"):
        resp = await client.post(
            "/loans/applications",
            json={"business_id": project_id, "requested_amount": requested_amount},
            headers=auth_headers(sme),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    return _make


# What a digitally signed agreement looks like to the upload check
# (app/loans/agreement/signature.py): a PDF with a signature dictionary, as a
# signing tool appends it in an incremental update.
SIGNED_AGREEMENT_PDF = (
    b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    b"9 0 obj\n<< /Type /Sig /Filter /Adobe.PPKLite /SubFilter /ETSI.CAdES.detached "
    b"/ByteRange [0 120 4216 380] /Contents <3082> >>\nendobj\n%%EOF\n"
)


async def attach_signed_service_agreement(db_session, application_id) -> None:
    """The SME uploaded its signed SME Service Agreement, as the wizard does
    before Send (submit refuses without it). Test setup: the upload itself is
    covered in tests/loans/test_service_agreement.py."""
    from sqlalchemy import select as _select

    from app.lending.models import LoanApplicationDocument
    from app.loans.agreement.service import SERVICE_AGREEMENT_DOCUMENT_TYPE

    app_id = uuid.UUID(str(application_id))
    existing = await db_session.scalar(
        _select(LoanApplicationDocument.id).where(
            LoanApplicationDocument.loan_application_id == app_id,
            LoanApplicationDocument.document_type == SERVICE_AGREEMENT_DOCUMENT_TYPE,
        )
    )
    if existing is None:
        db_session.add(
            LoanApplicationDocument(
                loan_application_id=app_id,
                document_type=SERVICE_AGREEMENT_DOCUMENT_TYPE,
                file_key=f"test/{app_id}/signed-service-agreement-{uuid.uuid4().hex}.pdf",
                original_filename="hop-dong-dich-vu-da-ky.pdf",
                content_type="application/pdf",
                file_size_bytes=1024,
                status="UPLOADED",
            )
        )
        await db_session.commit()


@pytest.fixture
def submit_loan_application(client, db_session):
    async def _submit(sme: dict, application_id: str):
        await attach_signed_service_agreement(db_session, application_id)
        return await client.post(
            f"/loans/applications/{application_id}/submit",
            headers=auth_headers(sme),
        )

    return _submit


@pytest.fixture
def run_and_lock_score(client):
    """Start a score run (SUBMITTED -> UNDER_REVIEW, side effect on the
    application) and immediately lock it (READY -> LOCKED). Returns the
    score_run id.

    Financials go in first: a run with none decides INSUFFICIENT_DATA, which
    approve_score_run refuses to lock."""

    async def _run(admin: dict, application_id: str):
        # Imported here: a test module, and conftest loads before it.
        from tests.underwriting.test_score_run_persistence import FULL_FINANCIALS

        financials = await client.put(
            f"/underwriting/applications/{application_id}/financials",
            json=FULL_FINANCIALS,
            headers=auth_headers(admin),
        )
        assert financials.status_code == 200, financials.text
        start = await client.post(
            "/underwriting/score-runs",
            json={"application_id": application_id, "mode": None},
            headers=auth_headers(admin),
        )
        assert start.status_code == 201, start.text
        score_run_id = start.json()["id"]
        approve = await client.post(
            f"/underwriting/score-runs/{score_run_id}/approve",
            headers=auth_headers(admin),
        )
        assert approve.status_code == 200, approve.text
        return score_run_id

    return _run


@pytest.fixture
def attach_required_documents(db_session):
    """Put every document the wizard requires on an application, as uploaded.

    An operator approval is refused until they are all on file
    (app/admin/service.py REQUIRED_DOCUMENT_TYPES), and most suites want an
    approved application rather than a rehearsal of the upload flow — which is
    covered in tests/uploads. Rows only: nothing is written to storage.
    """
    from app.admin.service import REQUIRED_DOCUMENT_TYPES
    from app.lending.models import LoanApplicationDocument

    from sqlalchemy import select

    async def _attach(application_id: str, types=REQUIRED_DOCUMENT_TYPES):
        app_id = uuid.UUID(str(application_id))
        # Idempotent: a test that already attached some keeps them.
        held = set((
            await db_session.execute(
                select(LoanApplicationDocument.document_type).where(
                    LoanApplicationDocument.loan_application_id == app_id
                )
            )
        ).scalars())
        for document_type in types:
            if document_type in held:
                continue
            db_session.add(LoanApplicationDocument(
                loan_application_id=app_id,
                document_type=document_type,
                file_key=f"test/{app_id}/{document_type}",
                original_filename=f"{document_type}.pdf",
                status="UPLOADED",
            ))
        await db_session.commit()

    return _attach


@pytest.fixture
def make_contract(client, attach_required_documents):
    """Take an application all the way to a contract.

    A contract needs BOTH approvals (migration 9f2c4a7d1e30): the engine's,
    via a LOCKED score run, and an operator's. The caller has already produced
    the score run, so this fixture supplies the operator decision — every
    downstream suite (market, payments, repayment schedule, ledger) wants a
    contract that exists, not a rehearsal of the approval gate. The gate
    itself is asserted directly in tests/admin/test_admin_approval.py.
    """

    async def _make(admin: dict, application_id: str, score_run_id: str):
        await attach_required_documents(application_id)
        decision = await client.post(
            f"/admin/applications/{application_id}/decision",
            json={"decision": "APPROVED"},
            headers=auth_headers(admin),
        )
        assert decision.status_code in (200, 409), decision.text

        resp = await client.post(
            "/contracts/",
            json={"application_id": application_id, "score_run_id": score_run_id},
            headers=auth_headers(admin),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    return _make


@pytest.fixture
def approve_kyc_documents(db_session):
    """Mark a project's KYC documents APPROVED so it can be listed.

    NOTE: there is no reachable API endpoint anywhere in the app that sets
    Document.status = "APPROVED" (verified by grepping the whole app/ tree).
    project_has_verified_kyc() therefore can never return True through the
    real API surface today -- this is a genuine gap, listed in the PR
    description for Edward to triage. This fixture uses a direct ORM insert
    (not raw SQL) purely as test setup for a precondition that has no service
    to call; it is not the behavior under test.
    """
    from app.lending.kyc import KYC_PURPOSES

    async def _approve(project_id: str):
        for purpose in KYC_PURPOSES:
            db_session.add(
                Document(
                    entity_type="PROJECT",
                    entity_id=project_id,
                    purpose=purpose,
                    filename=f"{purpose}.pdf",
                    status="APPROVED",
                    storage_key=f"test/{project_id}/{purpose}.pdf",
                )
            )
        await db_session.commit()

    return _approve


@pytest.fixture
def make_listing(client):
    async def _make(admin: dict, contract_id: str, target_amount: str = "10000.00", min_ticket: str = "1000.00"):
        resp = await client.post(
            "/market/listings",
            json={"contract_id": contract_id, "target_amount": target_amount, "min_ticket": min_ticket},
            headers=auth_headers(admin),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    return _make


@pytest.fixture
def make_eligible_investor(db_session):
    """Bring an investor to where the market accepts their orders: email
    verified, an APPROVED GVerify KYC attempt and a saved bank account.
    Idempotent per investor. Pass bank_account=False to stop short of the
    account (the order endpoint refuses an investor without one)."""

    async def _make(investor: dict, bank_account: bool = True):
        from app.direct_transfer.models import PaymentAccount
        from app.gverify.models import STATUS_APPROVED, GVerifyVerification

        user = (await db_session.execute(select(User).where(User.id == investor["id"]))).scalar_one()
        user.email_verified = True
        approved = (
            await db_session.execute(
                select(GVerifyVerification).where(
                    GVerifyVerification.user_id == user.id,
                    GVerifyVerification.status == STATUS_APPROVED,
                )
            )
        ).scalar_one_or_none()
        if approved is None:
            db_session.add(
                GVerifyVerification(
                    user_id=user.id,
                    verification_type="KYC",
                    status=STATUS_APPROVED,
                    person_number=uuid.uuid4().hex[:12],
                )
            )
        if bank_account:
            saved = (
                await db_session.execute(select(PaymentAccount.id).where(PaymentAccount.user_id == user.id))
            ).first()
            if saved is None:
                db_session.add(
                    PaymentAccount(
                        user_id=user.id,
                        holder_role="INVESTOR",
                        holder_type="INDIVIDUAL",
                        bank_bin="970436",
                        bank_name="Vietcombank",
                        account_number=uuid.uuid4().int.__str__()[:12],
                        account_name="TEST INVESTOR",
                        status="APPROVED",
                    )
                )
        await db_session.commit()
        return investor

    return _make


@pytest.fixture
def place_order(client, make_eligible_investor):
    async def _place(investor: dict, listing_id: str, amount: str = "1000.00", idempotency_key: str | None = None,
                      ack_risk_disclosure: bool = True, eligible: bool = True):
        # Most order tests are about the order, not the investor; pass
        # eligible=False to place as an unverified, un-KYC'd account.
        if eligible:
            await make_eligible_investor(investor)
        headers = dict(auth_headers(investor))
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return await client.post(
            f"/market/listings/{listing_id}/orders",
            json={"amount": amount, "ackRiskDisclosure": ack_risk_disclosure},
            headers=headers,
        )

    return _place


@pytest.fixture
def open_listing(make_user, make_admin, make_project, make_loan_application, submit_loan_application,
                  run_and_lock_score, make_contract, approve_kyc_documents, make_listing):
    """End-to-end setup reaching an OPEN listing ready to accept orders:
    SME project -> submitted application -> scored & locked -> contract
    (ACTIVE_PENDING_FUNDING) -> KYC approved -> OPEN listing.

    Returns dict with sme, admin, project, application, contract, listing.
    """

    async def _setup(target_amount: str = "10000.00", min_ticket: str = "1000.00"):
        sme = await make_user(role="SME")
        admin = await make_admin()
        project = await make_project(sme)
        # The application keeps make_loan_application's in-range default: its
        # amount must sit inside the engine's loan bounds, while the order and
        # ledger tests built on this fixture size the listing in small units.
        application = await make_loan_application(sme, project["id"])
        submit_resp = await submit_loan_application(sme, application["id"])
        assert submit_resp.status_code == 200, submit_resp.text
        score_run_id = await run_and_lock_score(admin, application["id"])
        contract = await make_contract(admin, application["id"], score_run_id)
        await approve_kyc_documents(project["id"])
        listing = await make_listing(admin, contract["id"], target_amount=target_amount, min_ticket=min_ticket)
        return {
            "sme": sme,
            "admin": admin,
            "project": project,
            "application": application,
            "contract": contract,
            "listing": listing,
        }

    return _setup


@pytest.fixture
def funded_contract(open_listing, place_order, make_user):
    """Fully funds a listing (one order for the whole target_amount) so the
    contract reaches ACTIVE_FUNDED -- the precondition for disbursements and
    repayments. Returns the open_listing() dict plus "investor" and "order"."""

    async def _setup(target_amount: str = "10000.00", min_ticket: str = "1000.00"):
        setup = await open_listing(target_amount=target_amount, min_ticket=min_ticket)
        investor = await make_user(role="INVESTOR")
        order_resp = await place_order(investor, setup["listing"]["id"], amount=target_amount)
        assert order_resp.status_code == 201, order_resp.text
        setup["investor"] = investor
        setup["order"] = order_resp.json()
        return setup

    return _setup


@pytest.fixture
def get_refresh_token_row(db_session):
    """HANDOFF-02 Fix B test helper: look up a user's RefreshToken row(s)
    directly, to assert on hashing/revocation state that isn't visible via
    the API surface."""

    async def _get(user_id: str, *, token_hash: str | None = None):
        stmt = select(RefreshToken).where(RefreshToken.user_id == user_id)
        if token_hash is not None:
            stmt = stmt.where(RefreshToken.token_hash == token_hash)
        stmt = stmt.order_by(RefreshToken.created_at.desc())
        result = await db_session.execute(stmt)
        return result.scalars().all()

    return _get
