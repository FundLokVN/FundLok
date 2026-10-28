# Spec: Funding Window — Listing Window, Standing Offer, Tranches, Close

| Field | Value |
|---|---|
| **Status** | DRAFT |
| **Owner** | Edward |
| **Implementer(s)** | Edward (backend service layer, `app/direct_transfer/`); Phat (routers in `app/market/` / `app/admin/` that call it, and all UI) |
| **Module** | `app/direct_transfer/` (`windows.py`, `reservation.py`, `tranches.py`, `signing.py`, `close.py`) |
| **Version** | 0.1 |
| **Date** | 2026-10-28 |
| **Related ADR** | ADR-009 |
| **Depends on** | `transfer-and-evidence.md`, `ledger-amendment.md`, `repayment-collection.md`, `fees.md`, `cic-event-log.md`, `disputes.md`; `docs/specs/repayment_schedule/origination-schedule-generation.md`, `facility-state-machine.md`, `business-day-calendar.md` |
| **Source** | `Loc.md` sections "Recommended disbursement design", "Build spec", "Decisions and points to align"; `Loc2.md`; HANDOFF-04 sections 0.1, 2, 4.3 |

---

## 1. Context & Goal

Money moves directly from each investor's bank to the SME's bank (ADR-009). A listing therefore cannot rely on an escrow to fill atomically; it runs as a **funding window**: the SME signs one standing offer, the listing is open for up to 14 days (or until full), each investor reserves, signs, pays by VietQR and uploads a receipt on their own, and whatever is confirmed at close becomes the loan (capped at the asked amount; below 50% there is no loan). This spec is the Phat-facing contract for the whole disbursement side.

**Goal:** define the window, standing offer, tranche state machine, capacity and hold rules, the close algorithm (50% rule, refunds, common start date, stub interest, facility origination), the timers and the service/API contract, so that backend and frontend can be built in parallel.

---

## 2. Out of Scope

- Receipt parsing and checks, evidence storage, duplicates, the exceptions queue (see `transfer-and-evidence.md`).
- Ledger legs (see `ledger-amendment.md`), repayment collection (see `repayment-collection.md`), fee invoices (see `fees.md`), dispute decisions (see `disputes.md`), CIC events (see `cic-event-log.md`).
- "Extend the window once" (Loc's cons table mentions it; no rule exists) is **not built** (PROVISIONAL(Q-D4)).
- Any arrears, late-rate or lateness handling (MVP scope, `CLAUDE.md`).
- A real remote-signing or e-invoice vendor (interfaces and mocks only).
- Any UI. Refund **after** the loan is ACTIVE.
- Creating the `Contract` and `Listing` rows (existing market/contract flow); this spec opens a window on an existing DRAFT listing.

---

## 3. Data Model

All money is **integer VND** (`BIGINT`, Python `int`, no decimals, no floats). All timestamps `TIMESTAMPTZ`; "dates" for schedule purposes are ICT calendar dates. Primary keys UUID.

Decision: window state lives in a **new 1:1 table** `funding_windows` rather than new columns on `listings`, so that the shared `app/lending/models.py` and the `listings.status` CHECK are not touched (shared-file rule). The listing's own `status` is still driven (`OPEN` at window open, `FUNDED` or `CLOSED` or `CANCELLED` at close). HANDOFF-04 B1 said "listings gains min_raise, window_end, ..."; this is a deliberate deviation, listed in the HANDOFF-04 report.

### New table: `funding_windows`

```
funding_windows
├── id                       UUID         PK
├── listing_id               UUID         NOT NULL UNIQUE → listings(id) ON DELETE RESTRICT
├── contract_id              UUID         NOT NULL → contracts(id) ON DELETE RESTRICT
├── project_id               UUID         NOT NULL → projects(id) ON DELETE RESTRICT   -- the SME (business)
├── sme_user_id              UUID         NOT NULL → users(id)
├── sme_payment_account_id   UUID         NOT NULL → payment_accounts(id)              -- payee of every tranche
├── target_vnd               BIGINT       NOT NULL CHECK (target_vnd > 0 AND target_vnd <= 100000000)
├── min_raise_vnd            BIGINT       NOT NULL  -- ceil(target * 50%)
├── min_ticket_vnd           BIGINT       NOT NULL CHECK (min_ticket_vnd > 0)
├── annual_rate_pct          NUMERIC(6,3) NOT NULL  -- from the locked score run / contract.final_terms; never recomputed
├── term_months              INT          NOT NULL
├── offer_terms              JSONB        NOT NULL  -- snapshot of what the investor is shown (rate, term, engine_version, params_version, window dates)
├── declared_outside_debt_vnd BIGINT      NOT NULL DEFAULT 0  -- SME's signed declaration (I-25)
├── window_start             TIMESTAMPTZ  NOT NULL
├── window_end               TIMESTAMPTZ  NOT NULL  -- window_start + 14 days
├── status                   TEXT         NOT NULL CHECK IN ('OPEN','FULL','CLOSING','REPAYING','FAILED','REPAID','CANCELLED')
├── close_reason             TEXT         NULL     -- FULL | DAY_14 | SME_EARLY
├── closing_started_at       TIMESTAMPTZ  NULL
├── close_wait_until         TIMESTAMPTZ  NULL     -- closing_started_at + 2 working days
├── closed_at                TIMESTAMPTZ  NULL     -- when the wait ended (PROVISIONAL(Q-D4))
├── confirmed_total_vnd      BIGINT       NULL
├── repayment_start_date     DATE         NULL
├── facility_id              UUID         NULL → facility(id)
├── created_at / updated_at  TIMESTAMPTZ
```

One listing per SME: **partial unique index** `uq_funding_windows_one_live_per_project` on `(project_id) WHERE status IN ('OPEN','FULL','CLOSING','REPAYING')`. `FAILED`, `REPAID`, `CANCELLED` are terminal and free the SME. PROVISIONAL(Q-D16): "repaid in full" = facility SETTLED.

### New table: `standing_offers`

```
standing_offers
├── id UUID PK
├── window_id UUID NOT NULL UNIQUE → funding_windows(id)
├── signer_user_id UUID NOT NULL
├── provider TEXT NOT NULL            -- 'MOCK' today
├── file_key TEXT NOT NULL            -- storage key of the signed document
├── file_sha256 TEXT NOT NULL
├── certificate JSONB NOT NULL        -- signer certificate fields
├── signed_at TIMESTAMPTZ NOT NULL
└── valid_until TIMESTAMPTZ NOT NULL  -- = window_end
```

### New table: `tranches` (one investor loan contract, `Loc.md` section "Contract structure")

```
tranches
├── id                    UUID PK
├── window_id             UUID NOT NULL → funding_windows(id)
├── investor_id           UUID NOT NULL → users(id)
├── investor_account_id   UUID NULL → payment_accounts(id)   -- payer account; must be in the investor's own name
├── investor_type         TEXT NOT NULL CHECK IN ('INDIVIDUAL','ENTITY')   -- snapshot, drives withholding
├── amount_vnd            BIGINT NOT NULL CHECK (> 0)
├── status                TEXT NOT NULL CHECK IN ('RESERVED','SIGNED','ATTESTED','CONFIRMED','ACTIVE','EXPIRED','UNDER_REVIEW','VOID')
├── reference_code        TEXT NOT NULL UNIQUE               -- 'FL' + 8 chars A-Z0-9
├── idempotency_key       TEXT NULL UNIQUE
├── reserved_at, hold_expires_at, signed_at, receipt_submitted_at, attested_at, confirm_deadline, confirmed_at,
│   activated_at, expired_at, voided_at   TIMESTAMPTZ NULL
├── void_reason           TEXT NULL   -- NOT_RECEIVED | BELOW_MIN_RAISE | KYB_FAIL | FRAUD | EXCLUDED_AT_CLOSE
├── review_cause          TEXT NULL   -- RECEIPT_MISMATCH | SME_DISPUTE | LATE_RECEIPT (while UNDER_REVIEW)
├── excluded_at_close     BOOL NOT NULL DEFAULT false       -- PROVISIONAL(Q-D7)
├── acceptance_file_key, acceptance_sha256, acceptance_certificate JSONB NULL
├── stub_receipt_date     DATE NULL     -- date the money reached the SME
├── stub_days             INT NOT NULL DEFAULT 0
├── stub_interest_vnd     BIGINT NOT NULL DEFAULT 0
├── investor_fee_total_vnd BIGINT NOT NULL DEFAULT 0   -- fixed at close (fees.md)
├── holding_id            UUID NULL → holdings(id)
└── created_at / updated_at
```

Constraint: **one live tranche per investor per window**: partial unique `(window_id, investor_id) WHERE status IN ('RESERVED','SIGNED','ATTESTED','CONFIRMED','UNDER_REVIEW','ACTIVE')`.

### Modified (runtime only, no schema change)

`listings.status/funded_amount/open_at/close_at`, `contracts.funded_amount/status`, and `holdings` rows are written by the service at open/close. No column is added to `listings`, `contracts` or `holdings`.

### Configuration (`app/direct_transfer/config.py`, `DirectTransferParams`)

| Key | Default | Note |
|---|---|---|
| `hold_hours` | 2 | ticket hold |
| `window_days` | 14 | |
| `min_raise_pct` | 50 | of target, rounded up to whole VND |
| `min_ticket_vnd` | 10,000,000 | Q-021 |
| `max_target_vnd` | 100,000,000 | per borrower per platform |
| `cross_platform_cap_vnd` | 400,000,000 | PROVISIONAL(Q-D12) |
| `dispute_working_days` | 2 | SME dispute window (placeholder) |
| `close_wait_working_days` | 2 | PROVISIONAL(Q-D4) |
| `working_days_per_month` | 21 | Q-011 resolved A |
| `stub_basis_days` | 252 | I-10 |
| `rate_cap_pct` | 20 | INV-5 |

---

## 4. API Contract

Service functions (Edward) are `async def`, take an `AsyncSession` and an injected `now: datetime` (timezone-aware), and raise `DirectTransferError(status_code, detail)`; routers (Phat) map it to `{"detail": "<message>"}`. Money fields are JSON integers. Roles: `SME`, `INVESTOR`, `ADMIN`. A resource that exists but is not the caller's returns **404**, not 403.

### `POST /funding/listings/{listing_id}/window`  — SME opens the window (signs the standing offer)

**Auth:** SME (owner of the listing's project). **Service:** `open_funding_window`.
```json
{ "payment_account_id": "uuid",            // SME's APPROVED registered account
  "declared_outside_debt_vnd": 0,          // signed declaration, integer VND
  "declaration_accepted": true,            // must be true
  "window_days": 14 }                      // optional, 1..14, default 14
```
**Headers:** `Idempotency-Key: <uuid>` (required; a replay returns the same window).
**201** `FundingWindowOut`:
```json
{ "id": "uuid", "listing_id": "uuid", "status": "OPEN", "target_vnd": 100000000, "min_raise_vnd": 50000000,
  "min_ticket_vnd": 10000000, "annual_rate_pct": "16.000", "term_months": 4,
  "window_start": "ts", "window_end": "ts", "confirmed_vnd": 0, "reserved_vnd": 0, "capacity_vnd": 100000000 }
```

### `GET /funding/listings/{listing_id}/window`  — progress (public to verified users)
`FundingWindowOut` plus `days_left` (int), `tranche_count`, `closing_started_at`, `close_wait_until`, `repayment_start_date`. Never returns another investor's identity or amount.

### `POST /funding/windows/{window_id}/reservations`  — investor reserves a ticket
**Auth:** INVESTOR. **Service:** `reserve_ticket`. **Headers:** `Idempotency-Key` required.
```json
{ "amount_vnd": 10000000, "ack_risk_disclosure": true, "payment_account_id": "uuid" }
```
**201** `TrancheOut`:
```json
{ "id": "uuid", "window_id": "uuid", "status": "RESERVED", "amount_vnd": 10000000, "reference_code": "FLAB12CD34",
  "hold_expires_at": "ts", "signed_at": null, "attested_at": null, "confirm_deadline": null,
  "review_cause": null, "void_reason": null }
```

### `POST /funding/tranches/{tranche_id}/accept`  — investor signs the acceptance
**Auth:** INVESTOR (owner). **Service:** `accept_tranche`. Body `{ "document_base64": "…" }` (the acceptance PDF Phat renders) or, with the mock signer, `{}`.
**200** `TrancheOut` (status `SIGNED`) plus `payment` (see `transfer-and-evidence.md`): `{ "bank_name", "account_number", "account_name", "amount_vnd", "reference_code", "vietqr_payload": "000201…6304XXXX" }`. Phat renders the QR image from `vietqr_payload`.

### `POST /funding/tranches/{tranche_id}/confirm` and `/dispute`  — SME
`confirm`: **200** `TrancheOut` (`CONFIRMED`), only from `ATTESTED`. `dispute`: body `{ "statement": "string", "file_id": "uuid|null" }` → **200** (`UNDER_REVIEW`, `review_cause=SME_DISPUTE`), only from `ATTESTED` and before `confirm_deadline`.

### `POST /funding/windows/{window_id}/close-early`  — SME
Allowed only if confirmed total >= `min_raise_vnd` (PROVISIONAL(Q-D4)). **200** `FundingWindowOut` (`CLOSING`).

### `GET /funding/windows/{window_id}/close-summary`
After close: `{ "status": "REPAYING|FAILED", "confirmed_total_vnd", "repayment_start_date", "tranches": [ { "id", "amount_vnd", "stub_days", "stub_interest_vnd", "status" } ], "refunds": [ { "tranche_id", "amount_vnd", "due_by", "status" } ], "activation_fee_invoice_id" }` (SME sees all tranches; an investor sees only their own).

### `GET /funding/me/tranches`
Investor: own tranches with status. 

**Error responses (all `{"detail": "<message>"}`)**

| Status | Condition |
|---|---|
| 400 | Invalid state transition: `"Invalid state transition: {current} -> {target}"`; amount not positive or not integer; declaration not accepted |
| 402 | none (no payments are taken by the API) |
| 404 | window/tranche/listing missing or not the caller's |
| 409 | `WINDOW_NOT_OPEN`; `CAPACITY_EXCEEDED`; `BELOW_MIN_TICKET`; `ALREADY_HAS_LIVE_TRANCHE`; `HOLD_EXPIRED`; `SME_HAS_LIVE_LISTING`; `SME_INVOICE_UNPAID`; `INVESTOR_FEE_UNPAID`; `BORROWER_CAP_EXCEEDED`; `ACCOUNT_NOT_APPROVED`; `BELOW_MIN_RAISE_FOR_EARLY_CLOSE`; `IDEMPOTENCY_CONFLICT` |
| 422 | Pydantic validation failure |

---

## 5. State Machine

### Tranche (Lok's diagram, recovered verbatim in HANDOFF-04 section 4.3, plus three additions)

```
RESERVED     --[investor signs]-----------------------> SIGNED          (1)
SIGNED       --[receipt fields match]-----------------> ATTESTED        (2)  CIC: ATTESTED event
ATTESTED     --[SME confirms | dispute window lapses]-> CONFIRMED       (3)
CONFIRMED    --[window closes with >= 50%]------------> ACTIVE          (4)
RESERVED     --[hold lapses]--------------------------> EXPIRED         (5)
SIGNED       --[receipt mismatch]---------------------> UNDER_REVIEW    (6)  cause RECEIPT_MISMATCH
ATTESTED     --[SME disputes]-------------------------> UNDER_REVIEW    (7)  cause SME_DISPUTE
UNDER_REVIEW --[staff: received]----------------------> CONFIRMED       (8)
UNDER_REVIEW --[staff: not received]------------------> VOID            (9)  SME refunds if money moved
SIGNED       --[hold lapses, no receipt]--------------> EXPIRED         (10) addition, E4
CONFIRMED    --[below 50% | KYB fail | fraud]---------> VOID            (11) addition, E4; tracked refund
EXPIRED      --[late receipt uploaded]----------------> UNDER_REVIEW    (12) addition, PROVISIONAL(Q-D3), cause LATE_RECEIPT
```

Transition (12) is needed because I-8 says a late receipt goes to the exceptions queue and may be revived by staff; no drawn transition allows that. It is only reachable while the window is `OPEN`, `FULL` or `CLOSING`. Terminal: `EXPIRED` (until a late receipt), `VOID`, `ACTIVE` (until the facility settles; no new tranche state, I-26). `Reported` is a confirmation level on the transfer, not a tranche state.

### Window

```
OPEN    --[capacity = 0]---------------------------> FULL
FULL    --[a live tranche lapses, now < window_end]-> OPEN        (PROVISIONAL(Q-D3))
OPEN|FULL --[day 14 | full and no RESERVED/SIGNED | SME early close]--> CLOSING
CLOSING --[no ATTESTED/UNDER_REVIEW left, or close wait over; confirmed >= min_raise]--> REPAYING
CLOSING --[... confirmed < min_raise]-------------------> FAILED
REPAYING --[facility SETTLED]---------------------------> REPAID
OPEN|FULL --[admin cancel before any CONFIRMED; KYB/fraud]--> CANCELLED
```

---

## 6. Business Rules

1. **Open.** A window opens only if: the listing is `DRAFT` or `OPEN` (the legacy `create_listing` opens immediately) with no `FILLED` legacy orders, and its `Contract` is `ACTIVE_PENDING_FUNDING` with a LOCKED score run; the SME payment account is `APPROVED`; the SME has no live window (partial unique index); the SME has no unpaid `fee_invoice` (status `ISSUED` or `OVERDUE`) (`SME_INVOICE_UNPAID`, `fees.md`); `target <= 100,000,000`; and the cap check passes (rule 3). The standing offer is signed with `Signer` and stored with hash and certificate; the window is `OPEN` for 14 days from `window_start`. The listing is set `OPEN`.
2. **Immutability of terms.** `annual_rate_pct`, `term_months` and the engine/params versions come from the locked score run (`score_runs.interest_rate_pct`, `engine_version`, `params_version`) and the application's `duration_months` (`contract.final_terms` is currently empty) and are copied into `offer_terms`; they are never recomputed (INV-5). The rate is within the 20% cap (INV-1).
3. **Borrower cap (I-25, D-022).** Before the window opens and at each reservation, FundLok checks: `target + declared_outside_debt_vnd <= cross_platform_cap_vnd (400M)` (PROVISIONAL(Q-D12)), and own records: no other live window for the project. A `CAP_CHECK` evidence row logs the basis (`OWN_RECORDS_PLUS_DECLARATION`); when CIC connects only the source changes.
4. **Capacity (I-7, PROVISIONAL(Q-D3)).** `capacity = target - SUM(amount of tranches in RESERVED, SIGNED, ATTESTED, CONFIRMED, or UNDER_REVIEW with a cause other than LATE_RECEIPT)`. A late-receipt tranche does not hold capacity until staff revive it (otherwise re-entering review could overbook the window); reviving re-checks capacity. Reservation locks the window row `SELECT ... FOR UPDATE`; capacity never goes below 0 under concurrent reservations.
5. **Reserve.** Allowed only when the window is `OPEN` and `now < window_end`, `ack_risk_disclosure` is true, `amount >= min_ticket` and `amount <= capacity`, except the **last-ticket rule** (I-6): if `capacity < min_ticket`, a reservation of exactly `capacity` is allowed. One live tranche per investor per window. Blocked if the investor has any unpaid `INVESTOR_SERVICE` `fee_invoice` (status `ISSUED` or `OVERDUE`; credit model, `fees.md`) (`INVESTOR_FEE_UNPAID`). The investor's payment account must be `APPROVED`. A 2 hour hold starts (`hold_expires_at = now + 2h`). If capacity reaches 0 the window becomes `FULL`. An investor who is also the SME's user cannot reserve.
6. **Idempotency.** Same `Idempotency-Key` returns the same tranche and changes nothing; a different request body with the same key is `IDEMPOTENCY_CONFLICT`.
7. **Reference code.** `FL` + 8 characters from `A-Z0-9` drawn from a CSPRNG, unique in `tranches.reference_code`; no diacritics (`transfer-and-evidence.md`).
8. **Sign.** `RESERVED -> SIGNED` requires `now <= hold_expires_at`; the acceptance is signed with `Signer`; the contract is concluded when the acceptance reaches the SME through the platform and takes effect when funds are confirmed (Civil Code Art. 400.1, 401.1). The hold continues until `hold_expires_at`; a tranche with a submitted receipt (`receipt_submitted_at` not null) does **not** expire (it awaits staff).
9. **Hold lapse.** `expire_holds(now)` moves `RESERVED` and `SIGNED` tranches with `hold_expires_at <= now` and no submitted receipt to `EXPIRED` and frees capacity; a `FULL` window with `now < window_end` returns to `OPEN`. Idempotent.
10. **SME confirmation.** `attested_at` sets `confirm_deadline = end of the 2nd working day after attested_at` (ICT). `sme_confirm` or `confirm_lapsed_tranches(now)` (silence counts as received) moves `ATTESTED -> CONFIRMED`. Only the SME of the window may confirm or dispute.
11. **Window close trigger.** `advance_window(now)`: (a) `OPEN`/`FULL` starts closing when `now >= window_end`, or when `FULL` and no tranche is `RESERVED` or `SIGNED`, or on SME early close (confirmed >= min_raise). Starting close sets `closing_started_at`, `close_wait_until = 2 working days later`, `close_reason`, and at `window_end` / early close expires any `RESERVED`/`SIGNED` tranche (acceptance after the window forms no contract, Art. 394.1). (b) `CLOSING` finalizes when no tranche is `ATTESTED` or `UNDER_REVIEW`, or `now >= close_wait_until`; lapsed `ATTESTED` tranches are confirmed first.
12. **Finalize** sets `closed_at = now` and:
    1. A tranche still `UNDER_REVIEW` is `excluded_at_close` (PROVISIONAL(Q-D7)); it never joins.
    2. `confirmed_total = SUM(CONFIRMED)` (always <= target by rule 4).
    3. **If `confirmed_total < min_raise_vnd` (below 50%):** every `CONFIRMED` tranche -> `VOID(BELOW_MIN_RAISE)` with a tracked refund `transfer` (kind `REFUND`, due in `refund_working_days = 2`); window `FAILED`; listing `CANCELLED`; **no fee invoice**; CIC `VOID` events. PROVISIONAL(Q-D8).
    4. **Else:** `repayment_start_date` = first working day **after** `closed_at`'s date (PROVISIONAL(Q-D4)); per-tranche stub interest (rule 13); the facility is originated for `principal = confirmed_total` at the contract's rate and term with `working_days_per_month = 21` (reuse `originate_facility`, `docs/specs/repayment_schedule/origination-schedule-generation.md`), then moved `DRAFT -> APPROVED -> DISBURSED -> ACTIVE`; a `Holding` is created per confirmed tranche (`principal = amount`, `share_ratio = amount / total`); every confirmed tranche -> `ACTIVE`; repayment rows are generated (`repayment-collection.md`); the SME activation `fee_invoice` is issued (`fees.md`); ledger claim legs are posted (`ledger-amendment.md`); CIC `ATTESTED` events already exist, `DAILY_BALANCE` starts at the first instalment day; window `REPAYING`; listing `FUNDED` if `confirmed_total == target` else `CLOSED`; `contract.funded_amount = confirmed_total`, contract `ACTIVE_FUNDED`.
13. **Stub interest (E3, I-10, PROVISIONAL(Q-D5)).** Per tranche: `stub_days` = number of **business days** `d` with `receipt_date <= d < repayment_start_date` (receipt date = ICT date of the transfer; business days per `business-day-calendar.md`); `stub_interest = round_half_up(amount * rate_pct/100 * stub_days / 252)`. It is a **separate dated obligation** due on `repayment_start_date`, outside `T0`; it never changes the facility schedule or `T0`. **Cap test (INV-5), both ways**: `implied = ((T0 + SUM(stub)) / P - 1) * 12 / term` and the same with the 3% activation fee included in the numerator; both results are logged as evidence rows; if the no-fee test would exceed `rate_cap_pct`, stubs are reduced (largest-remainder) to the headroom and the reduction is logged.
14. **Early close and early window (Q-D4).** An SME may close early only when confirmed >= `min_raise_vnd`. "Extend once" does not exist.
15. **Money above a ticket or without reservation** is not part of the loan and not FundLok's liability; the receipt amount must equal the ticket exactly (otherwise `RECEIPT_MISMATCH`).
16. **Void after the loan is ACTIVE** is out of MVP; `CONFIRMED -> VOID` is allowed only while the window is `CLOSING`, or via admin KYB-fail/fraud cancellation before finalize.
17. **Refund tracking.** A voided tranche whose money moved gets a `transfers` row (`kind=REFUND`, `payer = SME account`, `payee = investor account`, `status` due) and the investor/SME upload a receipt like any transfer. A tranche voided as `NOT_RECEIVED` has nothing to refund.
18. **Audit.** Every transition appends an `evidence_log` row (`event_type=TRANCHE_TRANSITION`, from, to, actor, reason).
19. **Language.** Responses and notifications must not imply FundLok is a lender, bank or guarantor, or promise a return (`fundlok-domain` section 9); returns are shown as a range only.

### Timers (Loc.md "Timers"; every timer is an idempotent service function with an injected clock, wrapped by an ARQ job in `app/direct_transfer/jobs.py`, registered in `app/direct_transfer/worker.py`)

| Timer | Value | Fires | Service function | ARQ job (ICT cron) |
|---|---|---|---|---|
| Ticket hold | 2 hours | `RESERVED`/`SIGNED` without a receipt -> `EXPIRED`, capacity freed, `FULL` may reopen | `expire_holds` | `expire_holds_job`, every 5 min |
| SME dispute window | end of the 2nd working day after attestation (placeholder) | `ATTESTED -> CONFIRMED` | `confirm_lapsed_tranches` | `advance_windows_job`, every 10 min |
| Funding window | up to 14 days; early when full / SME early close | `OPEN`/`FULL -> CLOSING` | `advance_window` | `advance_windows_job` |
| Close wait | end of the 2nd working day after close begins (PROVISIONAL(Q-D4)) | `CLOSING -> REPAYING`/`FAILED` | `advance_window` | `advance_windows_job` |
| Dispute response | end of the 2nd working day per step (placeholder) | turn passes to staff | `advance_dispute_deadlines` | `advance_windows_job` |
| Missed-day report | end of the 2nd working day after the due date | day settles | `presume_paid_days`, `settle_days` | `repayment_day_job`, 00:10 |
| Refund | 2 working days (Q-020) | tracked `REFUND` transfer `due_by` | `create_refund_transfer` | n/a (due date only) |
| SME fee invoice | due 7 days after issue (proposed) | `OVERDUE`, late fee, block | `mark_overdue_invoices` | `invoice_job`, 00:20 |
| Investor fee invoice | monthly; due 7 days (PROVISIONAL(Q-D15)) | accrual and invoice | `accrue_investor_fees`, `issue_investor_invoices` | `invoice_job`, 00:20 |
| CIC daily balance | previous business day | log event | `emit_daily_balances` | `cic_daily_balance_job`, 00:30 |

The jobs are not added to `app/scheduler/worker.py` because its existing tests assert that worker's exact job set and a single cron entry; merging is a one-line change plus a test update for Edward.

---

## 7. Error Cases

| Scenario | System behaviour | Response |
|---|---|---|
| Two investors reserve the last capacity concurrently | Row lock serialises; the second sees capacity 0 | 409 `CAPACITY_EXCEEDED` |
| Reservation replayed with the same Idempotency-Key | Returns the first tranche | 201 same body |
| Hold expired before signing | No state change except `expire_holds` | 409 `HOLD_EXPIRED` |
| Investor uploads a receipt after expiry | Transfer REPORTED, tranche `EXPIRED -> UNDER_REVIEW(LATE_RECEIPT)` | 202; staff queue |
| Capacity re-reserved before a late receipt is revived | Staff cannot revive; tranche `VOID`; SME must refund | staff decision error 409 `CAPACITY_EXCEEDED` |
| SME confirms a tranche that is not `ATTESTED` | none | 400 invalid transition |
| SME disputes after `confirm_deadline` | none | 409 `DISPUTE_WINDOW_CLOSED` |
| Window closes with a tranche `UNDER_REVIEW` | `excluded_at_close`; later resolution never joins; SME refunds if for the investor | — |
| Facility origination fails an invariant | Whole finalize rolls back; window stays `CLOSING`; error logged to ops | job retries next run |
| SME opens a second window | partial unique index | 409 `SME_HAS_LIVE_LISTING` |
| Two jobs run `advance_window` at once | window row lock; second is a no-op | — |
| Reservation by the SME's own user | refused | 404 |

---

## 8. Acceptance Criteria

Each maps 1:1 to a test in `tests/direct_transfer/test_funding_window.py` (and the files named in brackets).

- [ ] `test_open_window_signs_offer_and_opens_listing` — window `OPEN`, 14 days, offer row with hash and certificate, listing `OPEN`
- [ ] `test_open_window_rejects_target_above_100m`
- [ ] `test_open_window_blocked_when_sme_has_live_window` — second window 409 `SME_HAS_LIVE_LISTING`; allowed after the first is `FAILED`
- [ ] `test_open_window_blocked_when_sme_invoice_unpaid`
- [ ] `test_open_window_idempotent`
- [ ] `test_open_window_requires_approved_account`
- [ ] `test_reserve_happy_path_and_reference_code_format` — `RESERVED`, 2 h hold, `FL` + 8 `[A-Z0-9]`
- [ ] `test_reserve_below_min_ticket_rejected`
- [ ] `test_reserve_above_capacity_rejected`
- [ ] `test_reserve_last_ticket_below_min_allowed_when_equal_to_remaining` (PROVISIONAL(Q-D3))
- [ ] `test_reserve_one_live_tranche_per_investor`
- [ ] `test_reserve_idempotency_key_replay_returns_same_tranche`
- [ ] `test_reserve_blocked_when_investor_fee_unpaid`
- [ ] `test_reserve_requires_risk_ack`
- [ ] `test_capacity_never_exceeds_target_under_concurrent_reservations` (two sessions)
- [ ] `test_window_becomes_full_at_zero_capacity_and_reopens_on_lapse`
- [ ] `test_accept_moves_reserved_to_signed_and_stores_signature`
- [ ] `test_accept_after_hold_expiry_rejected`
- [ ] `test_expire_holds_expires_reserved_and_signed_without_receipt`
- [ ] `test_expire_holds_skips_tranche_with_submitted_receipt`
- [ ] `test_sme_confirm_attested_tranche`
- [ ] `test_confirm_deadline_is_two_working_days` [uses calendar with a weekend]
- [ ] `test_lapsed_dispute_window_auto_confirms`
- [ ] `test_only_window_sme_can_confirm_or_dispute`
- [ ] `test_sme_dispute_moves_to_under_review_with_cause`
- [ ] `test_invalid_transition_returns_400_message`
- [ ] `test_close_early_blocked_below_min_raise_and_allowed_above`
- [ ] `test_window_closes_when_full_and_no_unsigned_tranches`
- [ ] `test_window_closes_on_day_14_and_expires_unsigned`
- [ ] `test_close_waits_up_to_two_working_days_for_open_review`
- [ ] `test_close_below_50_percent_voids_everything_with_refunds_and_no_fee`
- [ ] `test_close_success_creates_facility_holdings_and_activates_tranches`
- [ ] `test_close_start_date_is_first_working_day_after_close` (weekend/holiday case)
- [ ] `test_stub_interest_basis_and_separate_obligation_not_in_t0`
- [ ] `test_cap_test_runs_both_ways_and_logs_both` (INV-5)
- [ ] `test_stub_interest_reduced_to_cap_headroom`
- [ ] `test_under_review_at_close_is_excluded_and_never_joins`
- [ ] `test_late_receipt_goes_to_exceptions_queue` [`test_exceptions.py`]
- [ ] `test_golden_origination_100m_16pct_84_days` [`test_golden.py`]: A0 = 1,253,968; remainder 21
- [ ] `test_window_cancelled_before_any_confirmed_frees_sme`
- [ ] `test_every_transition_appends_evidence_log`

---

## 9. Open Questions

| # | Question | Owner | Resolution |
|---|---|---|---|
| 1 | IBL: does the standing-offer wording overcome Art. 391.1 (Q-019)? | Legal / Loc | — |
| 2 | Is "extend the window once" wanted? No rule exists; not built (Q-D4) | Loc | — |
| 3 | Is the 400M cross-platform figure applied to the SME's declaration as written (Q-D12)? | Loc | — |
| 4 | Min ticket 10M or 20M (Q-021) | Loc | — |
| 5 | Refund period in the contract (Q-020) | Legal | — |
| 6 | Where does the `Listing`'s creation endpoint call `open_funding_window`? Phat to wire | Phat | — |

---

## Changelog

| Version | Date | Author | Changes |
|---|---|---|---|
| 0.1 | 2026-10-28 | Edward / Claude | Initial draft from HANDOFF-04 |
