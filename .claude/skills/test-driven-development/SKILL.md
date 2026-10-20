---
name: test-driven-development
description: >-
  Test-first workflow for the FundLok FastAPI backend: write the failing
  pytest first (named after the spec's acceptance criterion), make it pass,
  refactor. Covers how the suite actually runs (root `conftest.py` builds the
  schema with real Alembic migrations on a throwaway Postgres 15, truncates
  between tests, mocks all external I/O), which fixtures to reuse, where tests
  live (`tests/MODULE/`), and what FundLok changes must always be tested
  (idempotent replay, role gates, state transitions, ledger append-only,
  integer VND, migrations). Use this whenever you implement or change
  behaviour in `app/`, fix a bug (reproduce it in a failing test first), add
  an endpoint, model, migration, ledger posting or state transition, or when
  asked to add, run or debug tests or a red CI "Run Test Suite" job in this
  repo.
---

# Test-Driven Development — FundLok backend

FundLok moves real money, and the ledger is the legal record. A test proves a
change does what the spec says *and* that it can't double-post, can't be done
by the wrong role, and can't rewrite history. Writing the test first keeps
that proof honest: you see it fail for the right reason before you trust it
passing.

## The loop

1. **Red**: write a test for the next behaviour (an acceptance criterion from
   the spec, or the bug's reproduction). Run it and confirm it fails *for the
   reason you expect*. A test that fails with an ImportError or a 404 because
   the route doesn't exist yet hasn't proved anything about the behaviour.
2. **Green**: write the least code that makes it pass. Run the module's
   tests.
3. **Refactor**: tidy with tests green, then run the full suite before you
   commit (see `git-workflow-and-versioning`).

**Bug fixes (prove-it pattern):** reproduce the report as a failing test
*before* touching the code. Then fix it and keep the test as the regression
guard. If you can't make it fail, you don't understand the bug yet.

## Where tests come from and where they go

- **Names come from the spec.** `docs/specs/<module>/<feature>.md` §8
  Acceptance Criteria lists names like
  `test_post_transaction_idempotent_replay_returns_existing_and_writes_nothing`.
  Use them verbatim, one function per criterion. That 1:1 mapping lets a
  reviewer check spec coverage by grepping. If there's no spec and the change
  isn't a pure bug fix, see `spec-driven-development` first.
- **Location:** `tests/<module>/test_<feature>.py`, mirroring `app/<module>/`.
  A new module gets its own `tests/<module>/__init__.py` and at least one test
  file.
- **Style:** plain `async def test_…` (pytest.ini sets `asyncio_mode = auto`,
  so no decorator), HTTP through the `client` fixture (httpx `AsyncClient`
  over the ASGI app), Arrange-Act-Assert.

## How the suite really runs

The root `conftest.py` is the contract. Read its docstring once. In short:

| What | Behaviour | Why it matters to you |
|---|---|---|
| Schema | `_migrate_schema` (session, autouse) shells out to `alembic upgrade head` | Your new migration runs in every test session. A broken migration fails the whole suite, and models without a migration fail CI's `alembic check`. Never use `Base.metadata.create_all`, because it would hide migration drift. |
| Database | Real Postgres via `DATABASE_URL`, default `postgresql+asyncpg://test_user:test_password@localhost:5432/test_db` | Constraints, `CHECK`s, unique indexes and the ledger's append-only **trigger** are really exercised. |
| Isolation | All tables **truncated** between tests (not per-test transactions) | Never point `DATABASE_URL` at the dev DB (`:5433/fundlok_dev`) or anything shared. The suite wipes it. |
| External I/O | `_mock_external_io` (autouse) stubs email, Gmail, R2/GVerify storage. Turnstile, R2 and Didit env vars are blanked | Add a stub there when you add a new outbound integration, or CI (which has no mailpit) will catch it later. Bank/partner calls are mocked at the client boundary. |
| Flags | `_pin_feature_flags` pins feature flags | Tests don't depend on a developer's `.env`. |

**Running it:**
```bash
bun run docker:test                      # CI-identical: throwaway postgres:15 + python:3.11, no local setup
bun run docker:test tests/ledger -q      # any pytest args pass through
bun run test                             # ./.venv/bin/pytest -q against a Postgres you run on :5432
./.venv/bin/pytest tests/market/test_orders.py::test_name -q   # single test, fastest red/green
```
`tests/tooling/` (db-copy tool) is skipped unless `DB_MIGRATE_TESTS=1`.

**About the "never hit a real DB" line in CLAUDE.md.** CLAUDE.md → Testing
says tests "never hit a real DB or Brankas API". The suite and CI actually run
against a real *throwaway* Postgres 15, and that is deliberate (HANDOFF-01),
because the ledger trigger and constraints can't be tested otherwise. Read the
line as "never a real *shared/production* DB or partner API". If this matters
to a change, mention the discrepancy to the human so Edward can reword
CLAUDE.md. Don't "fix" it by mocking the session.

## Reuse the fixtures

The fixtures drive the **real API** to build state, so tests exercise the
same paths users do. Prefer them over hand-inserting rows:

`client`, `db_session`, `make_user(role=…)`, `make_admin`, `make_system_admin`,
`make_project`, `make_loan_application`, `submit_loan_application`,
`run_and_lock_score`, `attach_required_documents`, `make_contract`,
`approve_kyc_documents`, `make_eligible_investor`, `make_listing`,
`open_listing`, `place_order`, `funded_contract`, `get_refresh_token_row`.

They are async factories, so `await make_user(role="INVESTOR")`. If you need
a new precondition several tests share, add a factory to `conftest.py` in the
same style rather than duplicating setup across files.

**Mock only at the boundary.** Patch the outbound client (email sender, R2,
Didit, bank client), never the `AsyncSession` or the service under test. A
mocked session can't fire the append-only trigger or a unique constraint, so
the test passes while production double-posts. The one accepted exception is
pure auth-gate tests that override `get_db` with a fake to prove a request is
rejected *before* any DB work (see `tests/test_role_selection.py`).

## What a FundLok change must prove

Pick what applies. These are where real bugs in this repo have come from:

- **Idempotency**: any endpoint or service that creates a financial record:
  the same `Idempotency-Key` replayed returns the original and writes **no**
  new rows (count rows, don't just check the status code). The same key with
  a different body is rejected.
- **Authorization**: the wrong role gets 403, the unauthenticated caller gets
  401, and another user's resource is not readable/writable (IDOR).
  Eligibility gates too, e.g. an investor without approved KYC can't order
  (`fix(market)` history).
- **State machine**: every allowed transition works, and representative
  invalid ones are rejected with `{"detail": …}` and leave state unchanged.
- **Ledger**: legs balance; balances are a sum of postings, never a stored
  column; corrections are reversing entries; UPDATE/DELETE on
  `ledger_entries`/`ledger_transactions` raises (existing trigger tests in
  `tests/ledger/test_ledger_foundation.py` show the pattern).
- **Money**: integer VND end to end (`Numeric(20,0)` columns, `int` in
  schemas). Assert exact equality, and that invariants such as
  `collected + outstanding == total` hold exactly. See `fundlok-domain` for
  the invariants.
- **Errors**: shape is `{"detail": "<message>"}` with no stack trace or
  internal exception text.
- **Migrations**: new revision upgrades cleanly from the previous head and
  downgrades (`tests/ledger/test_migration.py` pattern), and `alembic check`
  is clean.

## Done when

- [ ] Each new behaviour or acceptance criterion has a test that was seen failing first
- [ ] Bug fixes include the reproduction test
- [ ] Relevant items from "What a FundLok change must prove" are covered
- [ ] No new network call escapes the mocks; no session/service mocked to make a test pass
- [ ] `bun run docker:test` (or `pytest -q` on :5432) is fully green, and `alembic check` is clean if models changed

`references/test-quality.md` covers general test-writing guidance: test
state not interactions, DAMP over DRY, the test-size pyramid, and the
anti-patterns table.
