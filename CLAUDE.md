# FundLok — Claude Session Bootstrap

> Read this file at the start of every session. It is the single source of truth for project context.
> Last updated: October 2026 | Version: 1.3

---

## What FundLok Is

FundLok (FL) is a Vietnam-registered P2P lending platform connecting SMB borrowers with lenders.

- **FL does NOT take a position.** It is a marketplace — it matches borrowers and lenders only.
- **Revenue model:** FL charges two fees, each invoiced separately and paid into FL's own account after the fact (ADR-009, D-023): an SME activation fee of 3% of confirmed principal (VAT invoice, due 7 days, never netted from principal) and an investor service fee of 2.5% a year (accrued monthly, invoiced).
- **No custody (ADR-009):** FL never holds money. Money moves directly between the investor's and the SME's own bank accounts (Decree 94 Art. 11.1.b). FL issues a dynamic VietQR, collects uploaded receipts, records evidence, computes schedules and reports. There is no FL cash account, no bank API and no CIC connection yet; confirmations rest on uploaded documents reviewed by staff. ADR-002/003's omnibus account and ADR-008's GPay rail are superseded for all new paths.
- **Ledger:** FL maintains an internal append-only ledger. On the direct-transfer path it is a confirmed-transfer model: `transfers` is the evidence record and claim legs post against a platform-level `EXTERNAL` account; no `OMNIBUS_CASH` legs (D-026, `docs/specs/direct_transfer/ledger-amendment.md`). It is immutable at both the application layer and the database layer (PostgreSQL trigger).
- **Borrower journey:** SMB registers → 3rd-party KYC → describes project/enterprise → applies for loan → ML grading engine assigns interest rate tier → listed on marketplace → lenders fund → disbursement → scheduled repayments → lenders receive pro-rata distributions + converted shares after repayment.
- **Early repayment** is encouraged as a platform value — better practice, better outcomes.
- **Regulatory framework:** Vietnam Decree No. 94/2025/ND-CP (fintech/P2P regulatory sandbox) and Circular 64/2024/TT-NHNN (banking Open API technical standard). See ADR-004.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend API | Python 3.11, FastAPI |
| Database | PostgreSQL (SQLAlchemy 2.x async, Alembic migrations) |
| File storage | Cloudflare R2 (S3-compatible) |
| Auth | JWT (access + refresh), Argon2 password hashing, Google OAuth |
| Payments | Direct bank-to-bank transfer by VietQR (NAPAS 247 / EMVCo). No payment-initiation or bank-data partner today; `app/banking/` stays mocked. A partner may be added later behind the transfer `rail` and `confirmation_level` fields. |
| Frontend | React (Phat owns — Edward does not touch UI) |
| Deployment | Docker, Google Cloud Run |
| CI/CD | GitHub Actions |
| Worker | ARQ (async task queue for ML scoring and scheduled jobs) |

---

## Architecture Decision: Modify, Not Rebuild

The MVP codebase (`/app`) is being extended, not rebuilt. See `docs/adr/001-modify-not-rebuild.md`.

Key carry-forwards:
- All 12 ORM models in `app/lending/models.py` and `app/users/models.py`
- Lending state machine (loans, contracts, market, payments services)
- Idempotency key pattern on `orders` and `ledger_entries`
- Alembic migration history is frozen — never alter existing migrations. Only `0001_initial_schema.py` uses a numeric name; all later migrations use Alembic hash-based revision IDs (e.g. `a7c4f1e2d3b5`). `0001` already contains the `orders`/`ledger_entries` idempotency columns **and** the `refresh_tokens` table. (The old numeric `0002`/`0003` idempotency files were redundant duplicates of columns already in `0001` and have been removed.)
- Auth, RBAC, Cloudflare R2 integration, CI/CD

---

## Module Ownership

**Ownership is strict. Do not modify files outside your boundary without explicit agreement.**

### Edward (CTO) — Backend, Architecture, Compliance
- `app/banking/` — Brankas API client, omnibus account service, webhook handlers (see also `docs/specs/banking/account-linking-mock.md` — Brankas is not expected to happen; this module is mocked indefinitely pending a partner decision)
- `app/verification/` — KYC/KYB identity verification (Didit client, session state, signed webhook)
- `app/underwriting/` — ML grading engine (async)
- `app/contracts/` — loan contract creation from a locked score run (final terms, funding target)
- `app/repayment_schedule/` — amortization engine, early repayment tracking
- `app/share_conversion/` — post-repayment share issuance
- `app/compliance/` — Decree 94 reporting, exposure cap enforcement
- `app/ledger/` — double-entry ledger, custodial/ledger accounts, balance-by-sum (see ADR-003, docs/specs/ledger/ledger-foundation.md)
- `app/direct_transfer/` — funding window, tranches, transfers and evidence, disputes, repayment collection, fees, CIC event log (ADR-009). Phat's routers call its service functions.
- `app/payments/` — legacy omnibus disbursement/repayment shim over `app/ledger/` (deprecated behind a flag once `app/direct_transfer/` is wired)
- `app/system/` — platform/ops settings (e.g. maintenance mode)
- `app/core/` — database session, config, async migration
- `app/utils/` — shared utilities (jwt, rbac, audit, r2, password)
- `alembic/` — all migrations
- `docs/adr/` — all Architecture Decision Records
- `docs/specs/` — all feature specs (Edward writes backend specs; Phat writes frontend specs)

### Phat — Full Stack + Sole Frontend Owner
- All React frontend (Edward does not touch UI under any circumstances)
- `app/users/router.py`, `app/users/schemas.py` — user-facing endpoints
- `app/projects/` — project management endpoints
- `app/loans/` — loan application endpoints
- `app/market/` — marketplace listing and order endpoints
- `app/sme/` — SME-specific flows
- `app/uploads/` — document upload endpoints (UI-integrated)
- `app/contact/` — contact/support endpoints
- `app/oauth/` — OAuth flows (frontend-facing)
- `app/admin/` — admin panel endpoints

### Shared (requires both to agree before changing)
- `app/models/__init__.py` — model re-exports
- `app/lending/models.py`, `app/users/models.py` — ORM models (schema changes via Alembic only)
- `app/auth/` — authentication core
- API contracts (OpenAPI schema) — defined in specs before either side implements

---

## Workflow: Spec-Driven Development

Every feature follows this sequence. No exceptions.

```
1. SPEC      Write spec in docs/specs/<module>/<feature>.md
2. REVIEW    Other engineer reviews spec (async, via PR comment)
3. CONTRACT  Agree on API contract (request/response types) before any code
4. BUILD     Implement against the spec — backend and frontend can work in parallel
5. TEST      Acceptance criteria in the spec become the test cases
6. ADR       If an architectural decision was made, record it in docs/adr/
```

**The spec is the handoff document.** If Phat picks up a spec written by Edward, he should need zero additional verbal/chat context to implement the frontend against it. If he does, the spec is incomplete — fix the spec, not the conversation.

**AI context rule:** Neither Claude nor Antigravity reliably carries context between sessions. All decisions, rationale, and conventions must be in the repo. If it is not written down, it does not exist.

---

## Spec Format

All specs live in `docs/specs/<module>/<feature>.md`. See `docs/specs/_TEMPLATE.md` for the required structure. Required sections:

1. Context & Goal
2. Out of Scope
3. Data Model (typed)
4. API Contract
5. State Machine (if applicable)
6. Business Rules
7. Error Cases
8. Acceptance Criteria
9. Open Questions

---

## Key Conventions

### Database
- All primary keys: UUID
- All timestamps: `TIMESTAMPTZ` (timezone-aware)
- Migrations: Alembic only. Never alter the DB schema directly.
- `ledger_entries` and `ledger_transactions` are append-only at both the app layer and the DB layer (PostgreSQL trigger). Do NOT add UPDATE or DELETE logic ever. Corrections are made by posting reversing entries.
- New migrations chain off the current Alembic head using hash-based revision IDs (`alembic revision --autogenerate`). Do NOT hand-number migrations `0002+` — that scheme is stale and those files were removed.

### API
- All routes return typed Pydantic response models — no raw dicts
- Auth: Bearer JWT in Authorization header
- Idempotency: any endpoint that creates a financial record must accept `Idempotency-Key` header
- Errors: always `{"detail": "<message>"}` — never expose internal exceptions

### Async
- All DB operations use `AsyncSession`. Never use sync `Session` in V2 code.
- All service functions are `async def`
- Background jobs go through ARQ — never `BackgroundTasks` for financial operations

### Testing
- Every new module must have a pytest file under `tests/<module>/`
- Tests use fixtures — never hit a real DB or Brankas API in tests
- Acceptance criteria from the spec map 1:1 to test function names

### Git
- Branch naming: `feature/<module>/<short-description>` (e.g. `feature/banking/brankas-disbursement`)
- PR must include: link to spec, passing tests, updated migration if schema changed
- Specs are reviewed and merged before implementation PRs are opened
- **Before merging any PR that adds a migration** (and whenever `main` has moved since the PR's checks ran), run `scripts/premerge_check.sh <PR number>`. `main` has no branch protection (free GitHub plan), so GitHub will not stop a merge on stale checks; #64 merged that way and left `main` with two Alembic heads. The script trial-merges the PR into the current `main` and prints GO or NO-GO (conflicts, a single migration chain, an empty-database upgrade). It changes nothing in your folder.

---

## Architecture Quick Reference

### Direct-Transfer Money Flow (ADR-009)
```
Investor → [own bank, VietQR]  → SME's own account   evidence: transfer (receipt)   ledger on confirmation
SME      → [own bank, VietQR]  → Investor's account  evidence: repayment day        ledger on settlement
SME      → [own bank]          → FL's own account    FL fee invoices (separate, never netted)
FL holds no money; receipts, statements and staff review are the evidence.
```

### Core Lending State Machine
```
LoanApplication:  DRAFT → SUBMITTED → UNDER_REVIEW → APPROVED | REJECTED
ScoreRun:         RUNNING → READY → LOCKED
Contract:         DRAFT → SIGNED → ACTIVE_PENDING_FUNDING → ACTIVE_FUNDED → CLOSED
Listing:          DRAFT → OPEN → FUNDED → CLOSED
Order:            legacy omnibus path only (deprecated behind a flag)
Tranche:          RESERVED → SIGNED → ATTESTED → CONFIRMED → ACTIVE
                  RESERVED|SIGNED → EXPIRED; SIGNED|ATTESTED → UNDER_REVIEW → CONFIRMED | VOID; CONFIRMED → VOID (before ACTIVE)
```

### User Roles
- `SME` — borrower. Creates projects, submits loan applications.
- `INVESTOR` — lender. Places orders on listings, receives distributions.
- `ADMIN` — FL operations staff. Approves score runs, triggers disbursements.

---

## ADR Index

| ID | Title | Status |
|---|---|---|
| ADR-001 | Modify MVP codebase, do not rebuild | Accepted |
| ADR-002 | Omnibus/custodial account via Brankas | Superseded by ADR-009 for new paths |
| ADR-003 | Omnibus-first with escrow-compatible ledger seam | Accepted (seam kept; no new path posts cash to it) |
| ADR-004 | Vietnam regulatory compliance (Decree 94 + Circular 64) | Draft |
| ADR-005 | Permanent local dev environment + local knowledge MCP server | Accepted |
| ADR-006 | fl-knowledge: BM25 → Pinecone + local embeddings | Accepted |
| ADR-007 | fl-knowledge: PDF ingestion (regulatory source documents) | Accepted |
| ADR-008 | Payment rails and custody model (GPay) | Superseded by ADR-009 |
| ADR-009 | Direct-transfer custody model, funding window, evidence-based confirmation | Accepted |
| ADR-010 | fl-knowledge: hybrid search (semantic + BM25) and stale-vector cleanup | Accepted |

Full ADRs in `docs/adr/`.

---

## What Edward Does NOT Do

Edward is CTO and backend/architecture owner. **Edward does not write UI code.** If a task involves React, CSS, component design, or anything rendered in a browser, it belongs to Phat. Do not suggest or generate frontend code in Edward's sessions.

---

## Local Dev Environment & Knowledge MCP

**Standing local stack (ADR-005):** run the repo's `docker-compose.yml` services — `postgres`, `mailpit`, `minio` — as a long-lived environment (`docker compose up -d`); app code runs on the host via `.venv` + `uvicorn` for hot reload. The dev database is standardized on the compose Postgres: `postgresql+psycopg2://fundlok:fundlok@localhost:5433/fundlok_dev`. Run `docker compose up -d postgres` before `alembic upgrade head` or starting the app.

**fl-knowledge MCP server (ADR-005 → ADR-006 → ADR-007 → ADR-010):** a project-scoped MCP server at `tools/mcp-servers/fl-knowledge/`, registered via `.mcp.json`, that indexes `CLAUDE.md`, `AGENTS.md`, `README.md`, and everything under `docs/` — both `.md` and `.pdf` — exposing `search_fl_docs` and `reindex_fl_docs`. It began on BM25 lexical search (ADR-005), moved to local `sentence-transformers` embeddings + Pinecone (ADR-006), then added PDF ingestion via `pypdf` for external source documents like regulations (ADR-007), and now ranks results by hybrid search — semantic embeddings merged with BM25 keyword matching, so exact identifiers like `UNDER_REVIEW` or `Q-023` are found as well as concepts (ADR-010). **Prefer `search_fl_docs` over re-reading whole specs/ADRs** — it's the token-efficient way to pull context. Requires a one-time `build_index.py` run and a `PINECONE_API_KEY` in a gitignored local `.env`; the server points you at `build_index.py` if the index is missing. External source documents (regulations, standards) go under `docs/regulatory/` — e.g. `docs/regulatory/circular-64-open-api.pdf`. `docs/` stays the source of truth — the server only reads it.

---

## Current Phase

**Phase 0 — Foundation (complete)**
- [x] Behavioral test suite + CI test job (HANDOFF-01)
- [x] Async DB migration — asyncpg + AsyncSession (HANDOFF-02 Fix A)
- [x] DB-backed refresh token revocation — uses `refresh_tokens` table already in `0001` (HANDOFF-02 Fix B)
- [x] Module cleanup — `app/files/` merged into `app/uploads/` (HANDOFF-02 Fix C)
- [ ] ARQ worker scaffold (deferred)

**Phase 1 — Ledger & Banking (in progress)**
- [x] Internal ledger foundation — double-entry ledger, custodial/ledger accounts, immutability trigger (spec ACCEPTED `docs/specs/ledger/ledger-foundation.md`, ADR-003; implemented in `app/ledger/` + migration `862160bce972`, merged PR #18)
- [ ] ~~Banking Open API integration~~ — dropped for MVP (ADR-009): no bank API; evidence-based direct transfer
- [x] Direct-transfer disbursement and repayment (HANDOFF-04, `docs/specs/direct_transfer/`, `app/direct_transfer/`)
- [ ] Compliance module — Decree 94 exposure caps, CIC data, reporting (ADR-004)

**MVP scope (decided 27 Sep 2026, updated 14 Nov 2026)**
MVP is the full platform happy path, end to end: registration → KYC/KYB → project/listing creation → search/browse → lender commitment → SME decision → disbursement → repayment collection → closing. Repayment includes extension/relief re-pricing (`docs/specs/repayment_schedule/fixed-daily-repayment-mechanism-v1.4.md`, D23) but **not** its fee (D27, deferred).

**Updated 14 Nov 2026 (Loc, Appendix M test, confirmed by Edward):** two items moved **into** launch scope. (1) **Late repayment interest.** A day with no SME proof by the end of its due date is Late; late interest is 150% of the loan rate on overdue principal (never above 30% a year) plus 10% a year on overdue interest, in calendar days, paid by the SME to each lender; a Late instalment blocks extension filing. (2) **Automatic extension.** The SME files by the 5th with that month's tax declaration or bank inflow statement and revenue; if the shortfall test passes and nothing is Late, the system extends with no staff step and no fee. Late is a repayment-day status, not a loan state. Both are specified in `docs/specs/` before any build, and finalised when handbook v3.2 lands.

Still **out of scope**: default handling (M10) and its states: no `ARREARS_WARNING`, `ARREARS`, `ACCELERATED`, `IN_RECOVERY` or `WRITTEN_OFF`; backstop, recovery and fraud handling. The practical repayment state machine remains `DRAFT → APPROVED → DISBURSED → ACTIVE ⇄ UNDER_REVIEW → SETTLED`.
