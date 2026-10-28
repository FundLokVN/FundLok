# Spec: GPay Virtual Account — Inbound Payments

> **SUPERSEDED 2026-10-28 by ADR-009 (`docs/adr/009-direct-transfer-model.md`) and `docs/specs/direct_transfer/`.** GPay is out (D-020). Do not implement. Kept for history.

| Field | Value |
|---|---|
| **Status** | **SUPERSEDED** (2026-10-28, ADR-009). Was DRAFT. |
| **Owner** | Edward |
| **Implementer(s)** | Edward (backend); Phat (investor/SME UI for requesting a VA, admin UI for the unmatched queue) |
| **Module** | `app/payments/` (new `app/payments/gpay/` package), `app/banking/` (VA ownership model) |
| **Version** | 0.2 |
| **Date** | 2026-10-22 (v0.1: 2026-10-14) |
| **Related ADR** | ADR-008 (payment rails and custody model — **Proposed, blocked on Q-001 and Q-002**), ADR-003 |
| **Depends on** | `docs/outstanding-issues.md` (D-011, Q-001, Q-002, Q-004), `docs/specs/payments/funding-flow.md` (ACCEPTED v1.0 — **its pooled leg table is under review**, see ADR-008), `docs/specs/ledger/ledger-foundation.md`, `docs/specs/banking/account-linking-mock.md` |

---

## 1. Context & Goal

On 2026-10-13 the CEO confirmed that Legal has cleared **GPay Virtual Account** as FundLok's payment method. GPay (G-Group's certified payment portal; not Google Pay) creates a virtual account (VA) at each customer's own bank. When a customer pays into their VA, the bank notifies GPay and GPay notifies FundLok. FundLok never talks to the customer's bank directly.

This spec defines how FundLok **provisions VAs, receives and verifies payment notifications, attributes each payment to a person, and posts it to the ledger**, using the leg table already accepted in `funding-flow.md`. It is written so that everything that does not depend on GPay's exact API (storage, idempotency, matching, ledger posting, unmatched handling, reconciliation) can be built and tested now against a mock, and only a thin `GPayClient` adapter waits for GPay's documentation.

**Goal:** a verified, idempotent path from "customer pays into their VA" to a correct, reconcilable ledger posting, with every unattributable payment held visibly rather than lost.

> **Revision note (2026-10-22).** FundLok must not hold money at any point in the flow (D-011), so the pooled leg shapes this draft borrowed from `funding-flow.md` (`EXTERNAL → OMNIBUS_CASH → INVESTOR_CASH`, `SUSPENSE` as a FundLok-held bucket) may not apply. Whether GPay can move money customer to customer, with no FundLok-owned account in between, is Q-001. The ledger model is Q-002, and ADR-008 proposes a rail-agnostic confirmed-transfer ledger. Until those are answered:
> - **Stands:** `virtual_accounts`, `provider_events`, signature verification, store-before-process, idempotency, attribution by VA number, the mock client, the fee helper, and the daily reconciliation concept.
> - **Suspended (marked `[SUSPENDED Q-001/Q-002]`):** every ledger leg, the `INVESTOR_CASH` and `SUSPENSE` handling, and the acceptance criteria that depend on them.
> - **Reinterpreted:** `inbound_payments` is the provider's record of one transfer. In the confirmed-transfer model it feeds the rail-agnostic transfer record with evidence level `PROVIDER_CONFIRMED`. That record's own spec is not yet written.

---

## 2. Out of Scope

- **Custody and ledger model.** Decided in ADR-008 (pending). This spec records `bank_code` and `custodial_account_id` where it can so either outcome is absorbable, and adds no ledger migration.
- **Outbound payments** (disbursement to SMEs, distributions and withdrawals to investors). Loc says GPay executes payouts (D-008), but whether they can go between third-party accounts without a FundLok account is disputed (Q-001). This spec defines only the adapter method (`initiate_transfer`, §4.1), not the flow. Today `record_disbursement` posts a ledger entry only; no bank instruction is sent.
- **Pooled-account legs and inter-bank rebalancing.** Only needed if ADR-008 resolves to Option B (pooled custodial accounts).
- **KYC/KYB and the verification vendor** (Didit is no longer used; Kalapa is being quoted).
- **Order and listing state rewiring** (`funded_amount`, `FUNDED`, `ACTIVE_FUNDED` moving only on a posted `FUNDING`). That is T10 per `funding-flow.md` §5. This spec produces the posted `FUNDING` transaction it depends on.
- **Late or partial repayment handling and all arrears** (excluded from MVP scope).
- **CIC real-time outbox.** `record_repayment` and `record_funding` will gain a CIC hook under the Decree 94 plan. This spec does not define it, but the posting functions it calls must stay the single place a balance change is recorded.
- **Frontend design.** Phat owns it; the contract is in §4.

---

## 3. Data Model

All tables use UUID primary keys and `TIMESTAMPTZ`. Amounts are `NUMERIC(20,0)` integer VND, matching `ledger_entries.amount`. Schema changes ship as Alembic migrations chained off the current head with hash-based revision ids.

### New table: `virtual_accounts`

```
virtual_accounts
├── id                    UUID          PK
├── user_id               UUID          NOT NULL → users(id) ON DELETE RESTRICT
├── purpose               TEXT          NOT NULL, CHECK IN ('FUNDING','REPAYMENT')
├── contract_id           UUID          nullable → contracts(id) ON DELETE RESTRICT
│                                         -- REPAYMENT VAs: see Open Question 3
├── provider              TEXT          NOT NULL, CHECK IN ('GPAY','MOCK')
├── bank_code             TEXT          NOT NULL   -- the customer's bank, as GPay names it
├── va_number             TEXT          NULL until ACTIVE   -- the account number the payer sends money to
├── provider_va_ref       TEXT          nullable   -- GPay's own id for this VA
├── account_holder_name   TEXT          nullable   -- name shown to the payer at their bank
├── custodial_account_id  UUID          nullable → custodial_accounts(id)
│                                         -- resolved per ADR-008; under Option A always the PLATFORM row
├── status                TEXT          NOT NULL, CHECK IN ('PENDING','ACTIVE','SUSPENDED','CLOSED','FAILED')
├── failure_reason        TEXT          nullable
├── idempotency_key       TEXT          NOT NULL UNIQUE   -- the client's Idempotency-Key for creation
├── created_at / updated_at  TIMESTAMPTZ
UNIQUE (provider, bank_code, va_number) WHERE va_number IS NOT NULL
```

At most one `ACTIVE` or `PENDING` VA per `(user_id, purpose, bank_code, contract_id)` (partial unique index), so a retried creation cannot create a second account at the same bank.

### New table: `provider_events` (raw inbox)

Every notification received, verified or not, stored before any processing.

```
provider_events
├── id                 UUID          PK
├── provider           TEXT          NOT NULL
├── provider_event_id  TEXT          NOT NULL   -- GPay's unique id for the notification
├── event_type         TEXT          NOT NULL
├── signature_valid    BOOLEAN       NOT NULL
├── raw_body           TEXT          NOT NULL   -- exact bytes received, for audit and re-verification
├── headers            JSONB         NOT NULL   -- signature-relevant headers only
├── received_at        TIMESTAMPTZ   NOT NULL DEFAULT now()
├── processing_status  TEXT          NOT NULL, CHECK IN ('RECEIVED','PROCESSED','UNMATCHED','REJECTED','FAILED')
├── processed_at       TIMESTAMPTZ   nullable
├── error              TEXT          nullable
UNIQUE (provider, provider_event_id)
```

`raw_body`, `headers`, `received_at` and `signature_valid` are never updated after insert. Only `processing_status`, `processed_at` and `error` change.

### New table: `inbound_payments`

One row per distinct payment (a payment may arrive in several notifications, e.g. retries or status updates).

```
inbound_payments
├── id                    UUID          PK
├── provider              TEXT          NOT NULL
├── provider_txn_ref      TEXT          NOT NULL   -- GPay/bank transaction reference; the idempotency anchor
├── provider_event_id     UUID          NOT NULL → provider_events(id)   -- the event that created it
├── virtual_account_id    UUID          nullable → virtual_accounts(id)  -- NULL if the VA number is unknown
├── bank_code             TEXT          NOT NULL
├── custodial_account_id  UUID          nullable → custodial_accounts(id)
├── amount                NUMERIC(20,0) NOT NULL, CHECK (amount > 0)
├── currency              TEXT          NOT NULL, CHECK (currency = 'VND')
├── payer_name            TEXT          nullable
├── payer_account_masked  TEXT          nullable   -- never the full number (§6, rule 11)
├── bank_reference        TEXT          nullable   -- narrative / bank-side reference
├── paid_at               TIMESTAMPTZ   NOT NULL   -- time the bank credited the VA
├── status                TEXT          NOT NULL, CHECK IN ('MATCHED','POSTED','UNMATCHED','RESOLVED','REVERSED','REJECTED')
├── user_id               UUID          nullable → users(id)   -- set when MATCHED or RESOLVED
├── ledger_transaction_id UUID          nullable → ledger_transactions(id)
├── resolution_note       TEXT          nullable
├── resolved_by           UUID          nullable → users(id)
├── created_at / updated_at  TIMESTAMPTZ
UNIQUE (provider, provider_txn_ref)
```

### Ledger interaction (no schema change) — **[SUSPENDED Q-001/Q-002]**

> Everything in this subsection assumes the pooled model. It is retained as a record of the v0.1 design and must be rewritten once Q-001 and Q-002 are answered. Do not implement it.

This spec adds no ledger tables of its own, but it **depends on the funding-flow spec being implemented first, and today it is not.** As of 2026-10-14 the code has no `record_funding`, `commit_investor_cash` or `record_withdrawal`; the `INVESTOR_CASH`, `EXTERNAL` and `UNEARNED_INTEREST` account types do not exist in `app/` or in any migration (the `account_type` CHECK still allows only the original five); `ledger_entries.contract_id` is still `NOT NULL`; and `post_transaction`'s pass-through check still has the defect `funding-flow.md` §6.1 describes. The funding-flow implementation (T10) is therefore on the critical path for this spec's ledger postings. `SUSPENSE` is in the CHECK already, and `FUNDING` and `REFUND` are valid entry types, so rule 7's unmatched-payment legs need only the `EXTERNAL` account type and the same fixes. The webhook, storage, matching and mock-client work does not depend on them and can start first.

Postings use the functions and leg shapes accepted in `funding-flow.md`:

| Event | Call | Legs (per `funding-flow.md` §6.1) |
|---|---|---|
| Investor pays into a FUNDING VA | `record_funding(contract_id=None)` | `EXTERNAL → OMNIBUS_CASH → INVESTOR_CASH` |
| SME pays into a REPAYMENT VA | `record_repayment(...)` | repayment legs per T14 (blocked until T14 lands; see Open Question 4) |
| Payment with no matching VA | `record_funding`-style legs into `SUSPENSE` | `EXTERNAL → OMNIBUS_CASH → SUSPENSE` |
| Operator resolves an unmatched payment to an investor | new service function | `SUSPENSE → INVESTOR_CASH` (single internal leg, type `FUNDING`) |
| Provider/bank reverses a posted payment | new service function | reversing legs; the original is never edited |

The ledger idempotency key for every payment posting is `f"{provider}:{provider_txn_ref}"` — the bank's own reference, per `funding-flow.md` §4, never a client-supplied value.

### Alembic migration

`alembic/versions/<hash>_gpay_virtual_accounts.py`, chained off the current head. Creates the three tables above only.

---

## 4. API Contract

### 4.1 Internal: `GPayClient` **[PENDING GPAY DOCS]**

All provider calls go through a protocol, with a `MockGPayClient` for tests and local development (CLAUDE.md: tests never hit a real provider). Method names and return shapes below are fixed by this spec; the HTTP mapping, auth scheme, field names and error codes are not, until GPay's documentation is in hand.

```python
# app/payments/gpay/client.py

class GPayClient(Protocol):
    async def create_virtual_account(
        self, *, bank_code: str, account_holder_name: str,
        purpose: Literal["FUNDING", "REPAYMENT"], reference: str,   # reference = our virtual_accounts.id
    ) -> CreatedVirtualAccount:
        """Returns provider_va_ref and either va_number (synchronous) or status=PENDING (asynchronous).
        MUST be safe to retry with the same `reference` (idempotent on the provider side).
        [PENDING GPAY DOCS: is creation synchronous or confirmed later by webhook?]"""

    async def close_virtual_account(self, *, provider_va_ref: str) -> None: ...

    async def initiate_transfer(
        self, *, from_ref: str, to_ref: str, amount: Decimal, reference: str,   # reference = our transfer id
    ) -> InitiatedTransfer:
        """Instructs a payout from one customer's account to another's. FundLok is the instructing party only.
        MUST be idempotent on `reference`. [PENDING GPAY DOCS] [PENDING Q-001: whether third-party payouts
        are possible without an account FundLok owns; do not implement against GPay until answered]"""

    async def get_transaction(self, *, provider_txn_ref: str) -> ProviderTransaction:
        """Authoritative lookup, used by reconciliation and to re-verify a suspicious webhook.
        [PENDING GPAY DOCS: whether this exists]"""

    async def list_transactions(self, *, bank_code: str | None, date_from: date, date_to: date) -> list[ProviderTransaction]:
        """For daily reconciliation. [PENDING GPAY DOCS: whether this exists]"""

def verify_webhook(*, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
    """Constant-time signature check. [PENDING GPAY DOCS: algorithm, header name, replay window]"""

def parse_payment_event(raw_body: bytes) -> ParsedPayment | ParsedOther:
    """[PENDING GPAY DOCS: payload schema, including the event types for credit, reversal, VA status]"""
```

### 4.2 `POST /payments/virtual-accounts`

Requests a VA for the caller at a chosen bank.

**Auth:** Bearer JWT — roles `INVESTOR` (purpose `FUNDING`) or `SME` (purpose `REPAYMENT`; see Open Question 3 for scope).

**Headers:** `Idempotency-Key: <uuid>` — required.

**Request**
```json
{
  "bank_code": "string",             // required — one of the codes returned by GET /payments/virtual-accounts/banks
  "contract_id": "uuid|null"         // required for purpose REPAYMENT if per-contract (Open Question 3), else null
}
```

**Response 201** (`status` is `PENDING` until the provider confirms; the client polls `GET`)
```json
{
  "id": "uuid",
  "purpose": "FUNDING",
  "bank_code": "string",
  "va_number": "string|null",
  "account_holder_name": "string|null",
  "status": "PENDING",
  "created_at": "2026-10-14T00:00:00Z"
}
```

| Status | Code | Condition |
|---|---|---|
| 400 | `UNSUPPORTED_BANK` | `bank_code` not in the supported list |
| 403 | `KYC_REQUIRED` | caller has no approved verification |
| 409 | `IDEMPOTENCY_CONFLICT` | key reused for a different request body |
| 409 | `VIRTUAL_ACCOUNT_EXISTS` | an ACTIVE or PENDING VA already exists for this user, purpose, bank and contract (returns the existing one) |
| 502 | `PROVIDER_UNAVAILABLE` | provider call failed; the row is left `PENDING` and retried by the worker, so the caller can poll rather than resubmit |
| 422 | — | validation failure |

### 4.3 `GET /payments/virtual-accounts`

**Auth:** `INVESTOR` | `SME`. Returns the caller's own VAs only (list of the 201 shape above). `GET /payments/virtual-accounts/banks` returns the supported `bank_code` list with display names. **[PENDING GPAY DOCS: source of the list]**

### 4.4 `POST /payments/gpay/webhook`

**Auth:** none (public). Authenticated solely by the signature check in §6, rule 1. No JWT.

**Request:** provider-defined body **[PENDING GPAY DOCS]**. The handler reads the raw bytes before any JSON parsing.

**Responses**

| Status | Condition |
|---|---|
| 200 | Event durably stored (and processed, or queued for processing). Also returned for a replay of an already-stored `provider_event_id`. |
| 401 | Signature invalid. The event is stored with `signature_valid = false`, `processing_status = REJECTED`, and nothing else happens. |
| 500 | Storing the event failed. The provider is expected to retry. Never return 200 unless the event is durably stored. |

The body of any non-200 response is `{"detail": "<message>"}` and never leaks internals.

### 4.5 Admin: unmatched payments (contract for Phat's admin UI)

**Auth:** `ADMIN`.

- `GET /admin/payments/unmatched` → list of `inbound_payments` with `status = UNMATCHED`, newest first. Fields: `id`, `amount`, `bank_code`, `payer_name`, `payer_account_masked`, `bank_reference`, `paid_at`, `provider_txn_ref`.
- `POST /admin/payments/unmatched/{id}/resolve` — body `{"user_id": "uuid", "note": "string"}`, header `Idempotency-Key`. Credits `INVESTOR_CASH` for that investor (rule 7), sets `RESOLVED`, records `resolved_by`.
- `POST /admin/payments/unmatched/{id}/reject` — body `{"note": "string"}`. Used when the money must be returned; sets `REJECTED`. The return itself is an outbound payment and is out of scope (§2).

Both write endpoints record an audit-log entry via `app/utils/audit`.

---

## 5. State Machine

### `virtual_accounts.status`

```
PENDING ──[provider confirms, va_number set]──► ACTIVE
PENDING ──[provider rejects / max retries]────► FAILED
ACTIVE  ──[admin or provider suspends]────────► SUSPENDED
SUSPENDED ──[restored]────────────────────────► ACTIVE
ACTIVE | SUSPENDED ──[closed by user/admin/provider]──► CLOSED
```

`FAILED` and `CLOSED` are terminal. A payment arriving on a `SUSPENDED` or `CLOSED` VA is still stored and posted (the bank has already moved the money), but flagged for operator attention (rule 8).

### `inbound_payments.status`

```
(new) ──[VA found, user known]──────► MATCHED ──[ledger posted]──► POSTED
(new) ──[VA not found / ambiguous]──► UNMATCHED ──[operator resolves]──► RESOLVED
(new) ──[invalid, e.g. non-VND]─────► REJECTED
POSTED | RESOLVED ──[provider reversal]──► REVERSED
UNMATCHED ──[operator rejects]──► REJECTED
```

Invalid transition attempts return 400 `"Invalid state transition: {current} → {target}"`.

---

## 6. Business Rules

1. **Verify before anything else.** The handler verifies the signature over the **raw bytes** with a constant-time comparison before parsing or acting on the body. A request that fails is stored (rule 2) with `signature_valid = false` and gets 401. No ledger write, no state change.
2. **Store before processing.** The raw event is committed to `provider_events` before any business logic runs. A crash after commit leaves a `RECEIVED` row that a worker retries; a crash before commit means no 200 was sent and the provider retries.
3. **Idempotent at two levels.** `UNIQUE(provider, provider_event_id)` makes a replayed notification a no-op returning 200. `UNIQUE(provider, provider_txn_ref)` plus the ledger key `"{provider}:{provider_txn_ref}"` guarantees one posting per real payment even if GPay sends the same payment under two different event ids. A replay never creates a second `inbound_payments` row or ledger transaction.
4. **Atomic posting.** Creating or updating the `inbound_payments` row and posting the ledger transaction happen in one database transaction. A failed ledger post leaves no `POSTED` row. Financial work runs in the request transaction or an ARQ job, never `BackgroundTasks` (CLAUDE.md).
5. **Amount and currency.** Only positive integer VND is accepted. Any other currency, a non-positive amount, or a fractional amount is `REJECTED` and alerts operations; it is never posted.
6. **Attribution.** A payment is matched by `(provider, bank_code, va_number)` to exactly one `virtual_accounts` row, which gives the `user_id`. Zero matches or an ambiguous match → `UNMATCHED`. The payer's name or narrative is **never** used to attribute a payment automatically.
7. **[SUSPENDED Q-001/Q-002] Unmatched money is real money.** Cash that has arrived must appear in the ledger. An unmatched payment posts `EXTERNAL → OMNIBUS_CASH → SUSPENSE` immediately, so INV-R1 (funding-flow §6.5) holds. Resolution posts `SUSPENSE → INVESTOR_CASH` for the chosen investor. This implements the SUSPENSE handling that `ledger-foundation.md` Open Q#2 left unbuilt. `SUSPENSE` is already in the account-type CHECK and `FUNDING` is a valid entry type; the legs additionally need the `EXTERNAL` account type from `funding-flow.md` (§3 above).
8. **Payments to non-ACTIVE VAs.** A credit on a `SUSPENDED` or `CLOSED` VA is still posted normally (the money is already in the bank) and raises an operator alert. It is never silently dropped.
9. **[SUSPENDED Q-001/Q-002, also Q-004] FUNDING VA → `INVESTOR_CASH`.** An investor's payment always lands as uncommitted `INVESTOR_CASH` (`record_funding(contract_id=None)`). Committing it to a listing is a separate step (`commit_investor_cash`, T10). The VA never decides which contract the money funds.
10. **[SUSPENDED Q-001/Q-002] Reversals.** A provider-reported reversal of a posted payment is processed as reversing ledger entries and marks the payment `REVERSED`. If the reversed amount has already been committed to a contract, the reversal is flagged for manual review rather than auto-posted, because it would drive `INVESTOR_CASH` negative.
11. **Sensitive data.** Full payer account numbers are never stored in `inbound_payments`. `raw_body` in `provider_events` may contain them, so that table is access-restricted and excluded from logs and from `audit_logs`. The provider secret and credentials come from the secret manager (`GPAY_*` settings in `app/core/config.py`) and are never logged.
12. **Daily reconciliation (post-pilot).** A daily job compares `inbound_payments` with the provider's transaction list per `bank_code`, and with our ledger's confirmed transfers for that rail, and raises an alert on any difference. A pre-payout check runs before any instruction FundLok sends. [The comparison target depends on Q-002.] Depends on `list_transactions` existing **[PENDING GPAY DOCS]**.
13. **Business dates are ICT.** `paid_at` is stored as `TIMESTAMPTZ`. Any "business day" cut-off uses the ICT business-date helper planned in the CIC work.
14. **Rate limiting.** The webhook endpoint is rate-limited per source and size-capped, since it is public.
15. **GPay processing fee — placeholder, borne by FundLok (D-006, D-012).** GPay's fees are treated as a FundLok marketing expense for now. The working figure is 3,300 VND per successful transaction (Q-003, unconfirmed). The fee is recorded **at the time of the money-movement events it relates to** (disbursement and repayment, and any other fee-bearing transfer), not as a separate periodic booking. Mechanics:
    - Config in `app/core/config.py`: `GPAY_FEE_BEARER` (default `FUNDLOK`; `BORROWER` and `INVESTOR` reserved but unimplemented, and any other value fails at startup), and a fee schedule (`GPAY_FEE_PERCENT`, `GPAY_FEE_FLAT_VND`, per event type). The default stays **zero in code** so nothing posts by accident; the 3,300 VND working figure is set in configuration for margin reports once Q-003 confirms what is billed.
    - A single helper `compute_processing_fee(event_type, amount) -> Decimal` (integer VND, rounded half-up) is the only place the fee is calculated. `record_disbursement` and `record_repayment` call it. A zero fee produces **no leg**, never a zero-amount leg (same rule as the FEE leg in `funding-flow.md`).
    - Ledger **[SUSPENDED Q-002]**: a nonzero fee posts one extra expense record tied to the same transfer. Under the pooled model that is the leg `OMNIBUS_CASH → PROCESSING_FEE_EXPENSE`; under the confirmed-transfer model there is no FundLok cash account, so it is an expense entry against the transfer. `PROCESSING_FEE_EXPENSE` is a new platform-level account type (one row, no owner, no contract), which needs the same CHECK-constraint migration as the account types in `funding-flow.md` §3.1. The fee is never netted against `FL_REVENUE`, so reported revenue and processing cost stay separately visible.
    - Because FundLok bears the fee, it changes no investor-facing balance and no reported CIC amount; only `OMNIBUS_CASH` and the expense account move.
    - **Reconciliation:** if GPay or the bank deducts the fee from the amount settled, the omnibus balance falls by the fee and INV-R1 breaks unless the leg is posted. This is why the fee must be recorded at transaction time and not later.
    - Which events GPay actually charges on, and whether it deducts the fee or invoices it later (in which case it would be an accrual, not a cash leg), are Open Question 9 (Q-003). Until then the code path is live but inert.
16. **Mock-first.** `MockGPayClient` and a webhook simulator (a test helper producing correctly signed payloads) exist from the first commit; the full flow is testable with no network.

---

## 7. Error Cases

| Scenario | System behaviour | Response to caller |
|---|---|---|
| Webhook with a bad signature | Stored as REJECTED, `signature_valid=false`; no processing; alert if repeated | 401 |
| Same `provider_event_id` delivered twice | Unique constraint → no-op | 200 |
| Same payment under a new event id | `provider_txn_ref` unique → no second posting | 200 |
| VA number unknown | `inbound_payments` UNMATCHED; `EXTERNAL→OMNIBUS_CASH→SUSPENSE`; appears in admin queue | 200 |
| Currency not VND / amount ≤ 0 | REJECTED; alert | 200 (acknowledge so the provider stops retrying) |
| Ledger post raises | Transaction rolled back; event stays `RECEIVED`/`FAILED`; ARQ retries with backoff | 200 if the event was stored, else 500 |
| DB unavailable while storing the event | Nothing stored | 500 (provider retries) |
| Provider unreachable on VA creation | VA left `PENDING`; worker retries; client polls | 502 on first call |
| Provider confirms VA creation twice | Second confirmation is a no-op | 200 |
| Payment on a SUSPENDED/CLOSED VA | Posted; operator alert | 200 |
| Reversal of an already-committed amount | Not auto-posted; flagged for manual review | 200 |
| Two requests to create a VA race | Partial unique index → one wins, other gets `VIRTUAL_ACCOUNT_EXISTS` | 409 |

---

## 8. Acceptance Criteria

> Criteria marked **[SUSPENDED Q-001/Q-002]** depend on ledger legs and must not be turned into tests until those are answered. Use `skip(reason="PENDING Q-001")` if the test is written early.

- [ ] `test_webhook_rejects_invalid_signature_and_writes_no_ledger_entry`
- [ ] `test_webhook_stores_raw_body_before_processing`
- [ ] `test_webhook_replay_same_event_id_is_noop_returns_200`
- [ ] `test_same_payment_under_different_event_id_posts_once`
- [ ] **[SUSPENDED Q-001/Q-002]** `test_credit_on_funding_va_posts_external_omnibus_investor_cash_legs`
- [ ] **[SUSPENDED Q-001/Q-002]** `test_funding_posting_uses_provider_txn_ref_as_ledger_idempotency_key`
- [ ] **[SUSPENDED Q-001/Q-002]** `test_failed_ledger_post_rolls_back_inbound_payment_row`
- [ ] **[SUSPENDED Q-001/Q-002]** `test_unknown_va_creates_unmatched_payment_and_posts_to_suspense`
- [ ] `test_unmatched_payment_is_never_attributed_by_payer_name`
- [ ] **[SUSPENDED Q-001/Q-002]** `test_admin_resolve_moves_suspense_to_investor_cash_and_marks_resolved`
- [ ] `test_admin_resolve_is_idempotent_and_audited`
- [ ] `test_non_vnd_or_non_positive_amount_is_rejected_not_posted`
- [ ] `test_credit_on_suspended_va_still_posts_and_alerts`
- [ ] **[SUSPENDED Q-001/Q-002]** `test_reversal_posts_reversing_entries_and_marks_reversed`
- [ ] **[SUSPENDED Q-001/Q-002]** `test_reversal_of_committed_amount_is_flagged_not_autoposted`
- [ ] `test_inbound_payments_never_store_full_payer_account_number`
- [ ] `test_create_va_idempotent_on_idempotency_key`
- [ ] `test_create_va_concurrent_requests_yield_single_va`
- [ ] `test_create_va_requires_approved_kyc`
- [ ] `test_create_va_rejects_unsupported_bank`
- [ ] `test_create_va_provider_failure_leaves_pending_and_worker_retries`
- [ ] `test_user_can_list_only_own_virtual_accounts`
- [ ] **[SUSPENDED Q-001/Q-002]** `test_omnibus_balance_equals_sum_of_posted_inbound_payments` (INV-R1 over the GPay flow)
- [ ] `test_virtual_account_and_inbound_payment_record_bank_code_and_custodial_account` (ADR-008 seam)
- [ ] `test_full_flow_against_mock_gpay_client_no_network`
- [ ] **[SUSPENDED Q-001/Q-002]** `test_repayment_va_credit_calls_record_repayment_with_pro_rata_split` (enabled when T14 lands)
- [ ] `test_zero_processing_fee_posts_no_fee_leg`
- [ ] **[SUSPENDED Q-001/Q-002]** `test_nonzero_processing_fee_posts_omnibus_to_processing_fee_expense_leg_in_same_transaction`
- [ ] `test_processing_fee_rounding_is_integer_vnd_half_up`
- [ ] `test_processing_fee_does_not_change_investor_or_borrower_balances`
- [ ] `test_processing_fee_bearer_other_than_fundlok_fails_at_startup`

---

## 9. Open Questions

| # | Question | Owner | Resolution |
|---|---|---|---|
| 1 | **Q-001 and Q-002** (`docs/outstanding-issues.md`): can GPay pay customer to customer with no FundLok-owned account, who holds money in transit, and how the ledger records it. Answered so far: GPay executes payouts (D-008, disputed), MB Bank obsolete (D-009), VA per customer per bank (D-005). | Edward → GPay / Loc / Legal | Partly answered; blocking |
| 2 | **GPay API documentation and sandbox**: auth scheme, VA creation (sync or async), webhook signature, event types, status/error codes, query and statement endpoints, retry policy. Unblocks every **[PENDING GPAY DOCS]** item. | Edward → CEO | — |
| 3 | **VA scope.** Investors: one VA per investor per bank (assumed above). SMEs: one VA per SME, or one per contract? Per contract makes repayment attribution exact; per SME needs an allocation rule when an SME has several contracts. | Edward + Phat | — |
| 4 | **How an SME payment maps to a daily instalment.** Exact-amount match, or any positive amount applied to the schedule? What happens with an over- or under-payment given that arrears are out of MVP scope? Depends on T14. Not to be confused with the *investor* split, which is settled: each investor receives their share in proportion to their holding (e.g. 80% / 20%), computed with the existing largest-remainder apportionment, and each instalment splits into principal and interest pro-rata (OQ-10). | Edward → CEO | Investor split **RESOLVED**; payer-side question open |
| 5 | **When does an investor's money move?** Now Q-004: with no FundLok account, money cannot wait for the funding target, so the leading option is pledge first, then pay the SME directly after the target is met. This replaces v0.1's "pay first, then commit `INVESTOR_CASH`". | Loc / Edward / Phat | See Q-004 |
| 6 | **Relationship to `LinkedAccount`.** The existing mock stores a user's linked bank/e-wallet. Under the closed-loop rule (payouts only to the funding account), should the payer account seen on an inbound payment be checked against the user's linked account, and what happens on a mismatch? | Edward + Phat | — |
| 7 | **Name check.** Does GPay or the bank confirm the VA payer's name matches the VA holder? If so, a mismatch policy is needed. | Edward → GPay | — |
| 8 | **Who owns the GPay adapter?** Backend adapter is Edward's per the ownership table. MB Bank was given to Phat for language reasons. Confirm whether the same applies here if GPay's documentation is Vietnamese. | Edward | — |
| 9 | **GPay fee tariff (Q-003).** Bearer is **RESOLVED**: FundLok, as a marketing expense for now (rule 15). Working figure 3,300 VND per successful transaction, unconfirmed. Still open: does GPay charge per VA, per transaction, or both; on which events (inbound credit, disbursement, repayment, payout); is it deducted from settlement or invoiced later; is VAT applicable. Also whether a transfer is billed once or twice (pay-in and pay-out). | Edward → CEO / GPay | Bearer resolved; tariff open |

---

## Changelog

| Version | Date | Author | Changes |
|---|---|---|---|
| 0.1 | 2026-10-14 | Edward / Claude | Initial draft. Provider-independent design complete; GPay API specifics deferred. Depends on ADR-008 for custody topology. |
| 0.2 | 2026-10-22 | Edward / Claude | FundLok must hold no money (D-011). Ledger-posting sections and the criteria that depend on them suspended pending Q-001 and Q-002. Added `initiate_transfer` to the client protocol. Fee working figure recorded. Open questions mapped to `docs/outstanding-issues.md`. |
