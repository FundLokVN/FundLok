# Spec: Bank Movement Feed — SePay Webhook Adapter (money in & out)

| Field | Value |
|---|---|
| **Status** | DRAFT |
| **Owner** | Phat (draft) — needs Edward's review: `app/banking/`, `app/ledger/`, `app/payments/` and T10 are Edward-owned |
| **Implementer(s)** | Edward (feed core, T10) / Phat (`orders.payment_reference`, admin endpoints + UI) — split to agree in review |
| **Module** | `app/banking/` (feed + movements), `app/market/` (order payment reference), `app/admin/` (ops endpoints) |
| **Version** | 0.1 |
| **Date** | 2026-10-11 |
| **Related ADR** | ADR-002, ADR-003. Likely needs a new ADR "SePay as bank-movement feed" (see OQ-1) |
| **Depends on** | `ledger/ledger-foundation.md` (ACCEPTED), `payments/funding-flow.md` (ACCEPTED), `repayment_schedule/facility-data-model.md`, HANDOFF-03 T10 |

---

## 1. Context & Goal

HANDOFF-03 S2 assumes the bank gives us statement export only, so T10 was designed as one "record a bank movement" code path, fed by manual ops entry now and statement import later. SePay watches a linked bank account and POSTs every movement — in **and** out — to a webhook within seconds, and exposes a transactions API for gap-filling. That turns T10's "later" adapter into a near-real-time one.

This spec defines:

- **(a)** the SePay webhook receiver;
- **(b)** the provider-neutral `bank_movements` record that every adapter (SePay webhook, SePay API poller, statement import, manual entry) writes through one function;
- **(c)** the matching rules that turn a movement into an order fill, a repayment, or a payout confirmation;
- **(d)** the reconciliation that catches anything the webhook misses.

SePay is a convenience feed, not the system of record. Domain rule §5.7 says never to build a production dependency on an endpoint we do not control. The bank statement remains the legal source, and the daily statement reconciliation stays the primary control.

**Goal:** every real movement on a custodial account becomes exactly one `bank_movements` row and at most one ledger transaction. That happens within seconds while SePay is healthy, and within one reconciliation cycle when it is not.

---

## 2. Out of Scope

- **Repayment application:** arrears first, lump-sum forward, partial payments and overpayment. That is T10 (EC-03/04/05/17). This spec hands T10 a matched facility movement.
- **Ledger leg shapes:** defined in `funding-flow.md` §6.1. This spec only says which existing service function a match calls.
- **Executing outbound transfers.** There is no payout API (S2), so ops execute transfers at the bank by hand. This spec records the instruction and confirms it when the money actually leaves.
- **"How to pay" screens** for investors and SMEs (account details, VietQR). Separate frontend spec.
- **Admin UI.** Separate frontend spec (Phat); this spec only fixes the API.
- **Commercial choices:** choosing the custodial bank or the SePay plan.
- **Escrow-mode (`scope='CONTRACT'`) accounts.** The design works per custodial account; provisioning is ledger-foundation OQ-3.
- **Correcting a wrong MATCHED movement** (reversal + re-attribution). Follow-up spec.

---

## 3. Data Model

### 3.1 Payment codes — the D20 reference, made machine-readable

Everything money can be sent for, or sent against, carries a payment code that the payer (or ops) types in the transfer memo. SePay extracts it into the `code` field using templates configured at *Công ty → Cấu hình chung → Cấu trúc mã thanh toán*. We also re-parse `content` ourselves.

| Prefix | Identifies | Lives on | Direction |
|---|---|---|---|
| `FLO` | an investor order | `orders.payment_reference` (new, §3.6) | IN |
| `FLR` | a facility's repayments | `facility.payment_reference` (exists) | IN |
| `FLI` | an investor transfer not tied to an order (→ `INVESTOR_CASH`) — optional for MVP | investor profile (new) | IN |
| `FLP` | a payout instruction | `payout_instructions.payment_code` (new, §3.4) | OUT |

**Format:** prefix + 7 random characters + 1 check character = 11 characters, e.g. `FLO8K2M4QZ7`.

- **Alphabet:** Crockford Base32 uppercase (`0-9 A-Z` minus `I L O U`).
- **Check character:** Luhn mod 32.
- **SePay template per prefix:** suffix min 8 / max 8, type *Số và chữ*.

Why this format:

- No spaces or punctuation, because banks strip them.
- Short enough to survive memo truncation.
- No look-alike characters.
- The check character turns a typo into UNMATCHED instead of a match on someone else's code.

**Generator:** `app/banking/payment_codes.py`.

- `new_payment_code(kind) -> str` retries on a UNIQUE collision.
- `parse_payment_codes(memo) -> list[str]` returns every check-valid code found anywhere in the memo, case-insensitive, and tolerates bank-added prefixes such as `MBVCB.3278907687.FLO8K2M4QZ7.CT tu ...`.
- Callers of `repayment_schedule/origination.py` stop inventing `payment_reference` and call the generator instead.

### 3.2 New table: `bank_feed_events` — the raw inbox

Table names are provider-neutral. The 2026-08 module audit flagged `brankas_transaction_id` for building a vendor into the schema.

```
bank_feed_events
├── id                   UUID          PK, default gen_random_uuid()
├── provider             TEXT          NOT NULL, CHECK IN ('SEPAY')
├── provider_event_id    TEXT          nullable        -- SePay `id`; NULL only if the payload was unparseable
├── raw_body             TEXT          NOT NULL        -- exact bytes that were signed; never re-serialized
├── signature_timestamp  BIGINT        nullable        -- X-SePay-Timestamp
├── source_ip            INET          nullable        -- via TrustedProxyMiddleware
├── received_at          TIMESTAMPTZ   NOT NULL, DEFAULT NOW()
├── processing_status    TEXT          NOT NULL, CHECK IN ('RECEIVED','PROCESSED','REJECTED','FAILED'), default 'RECEIVED'
├── reject_reason        TEXT          nullable        -- 'UNKNOWN_ACCOUNT' | 'INVALID_PAYLOAD'
├── attempts             INTEGER       NOT NULL, default 0
├── last_error           TEXT          nullable        -- exception class + message; never payload content
├── processed_at         TIMESTAMPTZ   nullable
└── bank_movement_id     UUID          nullable → bank_movements(id) ON DELETE RESTRICT
CONSTRAINTS:
  - UNIQUE (provider, provider_event_id)
```

Only requests with a valid signature are stored. `raw_body` contains payer names, which are PII: it is readable by ADMIN only and is never written to logs.

### 3.3 New table: `bank_movements` — T10's single record

One row per real movement on a custodial account, whichever adapter saw it first.

```
bank_movements
├── id                     UUID          PK, default gen_random_uuid()
├── custodial_account_id   UUID          NOT NULL → custodial_accounts(id) ON DELETE RESTRICT
├── direction              TEXT          NOT NULL, CHECK IN ('IN','OUT')
├── amount_vnd             NUMERIC(20,0) NOT NULL, CHECK (amount_vnd > 0)
├── occurred_at            TIMESTAMPTZ   NOT NULL      -- SePay transactionDate, parsed as Asia/Ho_Chi_Minh
├── value_date             DATE          NOT NULL      -- local (UTC+7) date of occurred_at
├── bank_reference         TEXT          nullable      -- SePay referenceCode (the bank's own id)
├── dedupe_key             TEXT          NOT NULL, UNIQUE   -- §6 rule 3; cross-adapter idempotency
├── memo                   TEXT          NOT NULL      -- SePay content
├── payment_code           TEXT          nullable      -- resolved code (§6 rule 6)
├── counterparty_text      TEXT          nullable      -- SePay description; PII, ADMIN-only
├── balance_after_vnd      NUMERIC(20,0) nullable      -- SePay accumulated; informational, never a source of truth
├── first_source           TEXT          NOT NULL, CHECK IN ('SEPAY_WEBHOOK','SEPAY_API','STATEMENT_IMPORT','MANUAL')
├── match_status           TEXT          NOT NULL, CHECK IN ('UNMATCHED','NEEDS_REVIEW','MATCHED')
├── match_reason           TEXT          nullable      -- e.g. 'ORDER_AMOUNT_MISMATCH','AMBIGUOUS_CODE','UNEXPECTED_OUTFLOW'
├── order_id               UUID          nullable → orders(id) ON DELETE RESTRICT
├── facility_id            UUID          nullable → facility(id) ON DELETE RESTRICT
├── investor_id            UUID          nullable → users(id) ON DELETE RESTRICT   -- money parked in INVESTOR_CASH
├── payout_instruction_id  UUID          nullable → payout_instructions(id) ON DELETE RESTRICT
├── ledger_transaction_id  UUID          nullable → ledger_transactions(id) ON DELETE RESTRICT
├── created_at             TIMESTAMPTZ   DEFAULT NOW()
└── updated_at             TIMESTAMPTZ   DEFAULT NOW()
CONSTRAINTS:
  - CHECK at most one of (order_id, facility_id, payout_instruction_id) is non-NULL
  - CHECK (direction = 'OUT') implies (order_id IS NULL AND facility_id IS NULL)
  - CHECK (match_status = 'MATCHED') implies at least one of (order_id, facility_id, investor_id, payout_instruction_id) is non-NULL
```

The fact columns are immutable: `custodial_account_id` through `balance_after_vnd`, plus `first_source`. A `BEFORE UPDATE` trigger rejects changes to them, using the same mechanism as the ledger immutability trigger. Only the match columns change, and every change writes an `append_audit` row.

**Relationship to `inbound_transfer`.** It holds the same facts but is limited to inbound, facility-only movements, and it has no router and no data yet. The proposal is that `bank_movements` supersedes it (OQ-7). Until that is decided, a matched facility movement also writes an `inbound_transfer` row with `external_transaction_id = dedupe_key`.

### 3.4 New table: `payout_instructions` — money out, under dual control

```
payout_instructions
├── id                      UUID          PK, default gen_random_uuid()
├── kind                    TEXT          NOT NULL, CHECK IN ('DISBURSEMENT','INVESTOR_WITHDRAWAL','REFUND_EXCESS','DISTRIBUTION_PAYOUT')
├── payment_code            TEXT          NOT NULL, UNIQUE      -- FLP…; ops type it in the bank transfer memo
├── amount_vnd              NUMERIC(20,0) NOT NULL, CHECK (amount_vnd > 0)
├── contract_id             UUID          nullable → contracts(id) ON DELETE RESTRICT
├── investor_id             UUID          nullable → users(id) ON DELETE RESTRICT
├── destination_account_id  UUID          NOT NULL → linked_accounts(id) ON DELETE RESTRICT
├── note                    TEXT          nullable
├── status                  TEXT          NOT NULL, CHECK IN ('PENDING_APPROVAL','APPROVED','CONFIRMED','CANCELLED')
├── created_by              UUID          NOT NULL → users(id)
├── approved_by             UUID          nullable → users(id)  -- CHECK (approved_by IS NULL OR approved_by <> created_by)
├── approved_at             TIMESTAMPTZ   nullable
├── cancelled_by            UUID          nullable → users(id)
├── cancelled_at            TIMESTAMPTZ   nullable
├── cancel_reason           TEXT          nullable              -- NOT NULL when status='CANCELLED'
├── bank_movement_id        UUID          nullable, UNIQUE → bank_movements(id) ON DELETE RESTRICT
├── created_at              TIMESTAMPTZ   DEFAULT NOW()
└── updated_at              TIMESTAMPTZ   DEFAULT NOW()
```

`destination_account_id` tells ops which verified account to pay. The permitted-destination whitelist is still enforced by the bank (domain §7), not by this table.

### 3.5 New table: `bank_movement_attributions` — manual matches (overrides)

```
bank_movement_attributions
├── id                UUID          PK, default gen_random_uuid()
├── bank_movement_id  UUID          NOT NULL → bank_movements(id) ON DELETE RESTRICT
├── target_kind       TEXT          NOT NULL, CHECK IN ('ORDER','FACILITY','INVESTOR','PAYOUT_INSTRUCTION')
├── target_id         UUID          NOT NULL
├── reason            TEXT          NOT NULL, CHECK (char_length(reason) BETWEEN 10 AND 1000)
├── status            TEXT          NOT NULL, CHECK IN ('PENDING_APPROVAL','APPROVED','WITHDRAWN')
├── proposed_by       UUID          NOT NULL → users(id)
├── proposed_at       TIMESTAMPTZ   NOT NULL, DEFAULT NOW()
├── approved_by       UUID          nullable → users(id)   -- CHECK (approved_by IS NULL OR approved_by <> proposed_by)
└── approved_at       TIMESTAMPTZ   nullable
CONSTRAINTS:
  - PARTIAL UNIQUE (bank_movement_id) WHERE status = 'PENDING_APPROVAL'
```

### 3.6 Modified table: `orders`

```
+ payment_reference   TEXT   nullable, UNIQUE   -- FLO… code; set by place_order for every new order (D20)
```

The column is nullable only because older orders predate it. `app/market/` is owned by Phat.

### 3.7 Config

Declare in `app/core/config.py` (`extra="ignore"` silently drops undeclared vars), `.env.example`, and `docs/deployment/ci-cd-pipeline.md` §6.

```
SEPAY_WEBHOOK_SECRET: str | None = None          # HMAC key; unset ⇒ endpoint rejects everything (fail closed)
SEPAY_API_TOKEN: str | None = None               # reconciliation poller; unset ⇒ poller disabled + alert
SEPAY_SIGNATURE_TOLERANCE_SECONDS: int = 300
```

Custodial account resolution uses the existing `custodial_accounts.bank_account_ref`, which must equal the `accountNumber` SePay reports. No new column is needed.

### Alembic Migration

One migration, chained off the current head `707c1b468a42`. It creates the four tables, adds the fact-column trigger on `bank_movements`, and adds `orders.payment_reference`.

---

## 4. API Contract

### `POST /banking/feeds/sepay`

**Auth:** public, HMAC-SHA256 (no JWT). Must be added to `MAINTENANCE_ALLOW_PREFIXES` in `app/main.py`.

**Headers (sent by SePay)**
```
Content-Type: application/json
X-SePay-Timestamp: 1760155713                 // unix seconds
X-SePay-Signature: sha256=<hex>               // hex(HMAC_SHA256(secret, f"{timestamp}.{raw_body}"))
```

**Request** (SePay's payload, unchanged)
```json
{
  "id": 92704,                              // int — SePay transaction id; same across retries and replays
  "gateway": "string",                      // bank brand — internal only, never shown to users (domain §9)
  "transactionDate": "2026-10-11 11:08:33", // string — Vietnam time, no offset
  "accountNumber": "string",                // must equal an ACTIVE custodial_accounts.bank_account_ref
  "subAccount": "",                         // string|null — virtual account; "" or null when none
  "code": "FLO8K2M4QZ7",                    // string|null — SePay's extraction; null when no template matched
  "content": "FLO8K2M4QZ7 chuyen tien",     // string — transfer memo
  "transferType": "in",                     // "in" | "out"
  "transferAmount": 5000000,                // int VND, > 0
  "accumulated": 105000000,                 // int VND — account balance after this movement; may be 0
  "referenceCode": "FT24012345678",         // string — bank reference; may be ""
  "description": "string"                   // raw bank notification text — contains the payer name
}
```

**Response 200**: exactly this body. SePay treats any other body, or any status other than 200/201, as a failure.
```json
{ "success": true }
```

**Responses**

| Status | Condition | SePay then |
|---|---|---|
| 200 `{"success": true}` | Event durably stored and a decision recorded. The decision can be: processed, duplicate, unmatched, needs-review, or rejected (unknown account / invalid payload). | stops |
| 401 `{"detail": "Invalid signature"}` | Signature missing or wrong, timestamp outside ±300 s, or secret not configured | retries |
| 413 `{"detail": "Payload too large"}` | body > 64 KB | retries |
| 500 `{"detail": "Temporary failure"}` | Could not store the event, or processing hit an unexpected error | retries; each retry re-runs processing idempotently |

Rule of thumb: **200 means "we have it and have decided what to do with it"; 5xx means "try again."** SePay retries 7 times at 1, 1, 2, 3, 5, 8 and 13 minutes, so it gives up about 33 minutes after the first attempt.

---

### `GET /admin/bank-movements`

**Auth:** Bearer JWT — roles: `ADMIN` | `SYSTEM_ADMIN`

**Query:** `direction=IN|OUT`, `match_status=UNMATCHED|NEEDS_REVIEW|MATCHED`, `date_from=YYYY-MM-DD`, `date_to=YYYY-MM-DD` (on `value_date`), `limit` (1–100, default 50), `cursor` (opaque)

**Response 200**
```json
{
  "items": [
    {
      "id": "uuid",
      "direction": "IN",
      "amount_vnd": 5000000,
      "occurred_at": "2026-10-11T04:08:33Z",
      "value_date": "2026-10-11",
      "bank_reference": "FT24012345678",
      "memo": "FLO8K2M4QZ7 chuyen tien",
      "payment_code": "FLO8K2M4QZ7",
      "counterparty_text": "string|null",
      "first_source": "SEPAY_WEBHOOK",
      "match_status": "MATCHED",
      "match_reason": "string|null",
      "matched": { "kind": "ORDER", "id": "uuid" },
      "ledger_transaction_id": "uuid|null",
      "pending_attribution_id": "uuid|null"
    }
  ],
  "next_cursor": "string|null"
}
```

`matched` is `null` when nothing is matched. Otherwise `matched.kind` is one of `ORDER | FACILITY | INVESTOR | PAYOUT_INSTRUCTION`.

### `GET /admin/bank-movements/{id}`

**Auth:** `ADMIN` | `SYSTEM_ADMIN`. Returns the same item, plus:

- `feed_events: [{ "id": "uuid", "provider": "SEPAY", "received_at": "…", "processing_status": "PROCESSED", "attempts": 1 }]`
- `attributions: [{ "id", "target_kind", "target_id", "reason", "status", "proposed_by", "proposed_at", "approved_by", "approved_at" }]`

404 if not found.

### `POST /admin/bank-movements/{id}/attributions`

**Auth:** `ADMIN` | `SYSTEM_ADMIN`. **Headers:** `Idempotency-Key: <uuid>` (required).

Proposes a manual match. Nothing is posted until a second admin approves it.

```json
{ "target_kind": "ORDER", "target_id": "uuid", "reason": "Investor typed FLO8K2M4QZ instead of FLO8K2M4QZ7; confirmed by phone 11 Oct" }
```

**Response 201** `{ "id": "uuid", "status": "PENDING_APPROVAL" }`

| Status | Condition |
|---|---|
| 404 | movement or target not found |
| 409 | movement already `MATCHED`, or already has a pending attribution |
| 422 | reason shorter than 10 characters / validation failure |

### `POST /admin/bank-movements/{id}/attributions/{attribution_id}/approve`

**Auth:** `ADMIN` | `SYSTEM_ADMIN`, and the caller must not be `proposed_by`. **Headers:** `Idempotency-Key`.

Runs the same pipeline as automatic matching (§6 rules 8–11) and posts the ledger transaction. **Response 200** returns the movement item.

| Status | Condition |
|---|---|
| 403 | approver is the proposer |
| 409 | attribution not `PENDING_APPROVAL`, or movement already `MATCHED` |

### `POST /admin/bank-movements/{id}/attributions/{attribution_id}/withdraw`

**Auth:** the proposer only. Sets `WITHDRAWN`. **Response 200.**

### `POST /admin/bank-feed-events/{id}/reprocess`

**Auth:** `SYSTEM_ADMIN`. Only valid for `FAILED` events (409 otherwise). Re-runs processing. **Response 200** `{ "id": "uuid", "processing_status": "PROCESSED|FAILED", "attempts": 3 }`.

---

### `POST /admin/payout-instructions`

**Auth:** `ADMIN` | `SYSTEM_ADMIN`. **Headers:** `Idempotency-Key` (required).

```json
{
  "kind": "DISBURSEMENT",                 // DISBURSEMENT | INVESTOR_WITHDRAWAL | REFUND_EXCESS | DISTRIBUTION_PAYOUT
  "amount_vnd": 200000000,                // int VND > 0
  "contract_id": "uuid|null",             // required for DISBURSEMENT
  "investor_id": "uuid|null",             // required for INVESTOR_WITHDRAWAL / DISTRIBUTION_PAYOUT
  "destination_account_id": "uuid",       // linked_accounts.id, ACTIVE, owned by the payee
  "note": "string|null"
}
```

**Response 201** `{ "id": "uuid", "payment_code": "FLP3XQ9R2M8", "status": "PENDING_APPROVAL", ... }`

`INVESTOR_WITHDRAWAL` is refused with 409 if the amount exceeds the investor's `INVESTOR_CASH` balance.

### `POST /admin/payout-instructions/{id}/approve`

The approver must not be `created_by` (403 otherwise). → `APPROVED`. Ops execute the bank transfer only after this step, with `payment_code` as the memo.

### `POST /admin/payout-instructions/{id}/cancel`

Body `{ "reason": "string, 10–1000 chars" }` → `CANCELLED`. Returns 409 if already `CONFIRMED`.

### `GET /admin/payout-instructions?status=…&limit=&cursor=`

Paginated list, same envelope as `GET /admin/bank-movements`.

---

## 5. State Machine

```
bank_feed_events.processing_status
  RECEIVED ──processed────────────────────────────► PROCESSED
  RECEIVED ──unknown account / invalid payload─────► REJECTED
  RECEIVED ──unexpected error──────────────────────► FAILED
  FAILED   ──SePay retry or /reprocess─────────────► PROCESSED | FAILED

bank_movements.match_status      (initial state is set by automatic matching, §6)
  UNMATCHED    ──approved attribution──► MATCHED
  NEEDS_REVIEW ──approved attribution──► MATCHED
  MATCHED is terminal here (corrections are a follow-up spec).

payout_instructions.status
  PENDING_APPROVAL ──approve (≠ creator)─────────────► APPROVED
  APPROVED         ──matching OUT movement (§6 r.11)─► CONFIRMED
  PENDING_APPROVAL | APPROVED ──cancel + reason──────► CANCELLED
```

Illegal transitions return 409 `{"detail": "Invalid state transition: {current} → {target}"}`.

---

## 6. Business Rules

**Ingestion**

1. **Verify before anything.** Compute the HMAC over `f"{X-SePay-Timestamp}.{raw_body}"` using the raw request bytes, not re-serialized JSON, and compare with `hmac.compare_digest`. The timestamp must be within ±`SEPAY_SIGNATURE_TOLERANCE_SECONDS`. If the secret is unset, reject everything (fail closed, as `/kyc/webhook` does). Nothing unsigned is stored.
2. **Store the raw event and commit before processing.** Dedupe on `(provider, provider_event_id)`. A duplicate whose event is already `PROCESSED` or `REJECTED` returns 200 and does no work. Concurrent duplicates serialize on `SELECT … FOR UPDATE` of the inbox row.
3. **`dedupe_key`:**
   - `"{custodial_account_id}:{direction}:{bank_reference}"` when `referenceCode` is non-empty;
   - `"sepay:{id}"` otherwise.

   Every adapter computes the same key from the same bank facts. As a result, the webhook, the API poller and a statement import can never create two rows for one movement. SePay's API ids are UUIDs, not the webhook's integer `id`, so only the bank reference is shared between them (OQ-9).
4. `accountNumber` must resolve to an `ACTIVE` `custodial_accounts.bank_account_ref`. Otherwise the event is `REJECTED` with `UNKNOWN_ACCOUNT`, an alert is raised, and it is never processed.
5. **Money is `int`.** `transferAmount` must be an integer > 0; floats and numeric strings are `INVALID_PAYLOAD`. `transactionDate` is parsed as `Asia/Ho_Chi_Minh`.

**Matching** (D20: reference only, never amount or payer name)

6. **Resolve the code.**
   - Run `parse_payment_codes(content)`, and also take SePay's `code` if it is check-valid.
   - Exactly one distinct valid code ⇒ use it.
   - None ⇒ `UNMATCHED`.
   - More than one distinct code, or SePay's code disagrees with ours ⇒ `NEEDS_REVIEW` / `AMBIGUOUS_CODE` (EC-11).
   - A valid code that matches no record ⇒ `UNMATCHED`.
7. **The prefix must fit the direction.** `FLO`/`FLR`/`FLI` are only valid on IN, and `FLP` only on OUT. Otherwise `NEEDS_REVIEW` / `DIRECTION_MISMATCH`.
8. **IN + `FLO` (order).** Lock the order and its listing `FOR UPDATE`.
   - If the order is `PENDING_PAYMENT`, the amount equals `orders.amount`, and the listing has room:
     - call `record_funding(investor, amount, contract_id)`;
     - move the order to `FILLED` and set `payment_confirmed_at`;
     - update funded amounts and statuses per T10 / Issue 10.

     Result: `MATCHED` / ORDER.
   - In every other case (amount differs, order `EXPIRED`/`CANCELLED`/`FILLED`, or listing full):
     - call `record_funding(investor, amount, contract_id=None)`. The money is attributed to the investor and parked in `INVESTOR_CASH`;
     - notify the investor and ops.

     Result: `MATCHED` / INVESTOR, with `match_reason` set.
   - Never partially fill an order. The policy is OQ-6.
9. **IN + `FLR` (facility).**
   - If the facility is in a collectable state (T10 owns the list), hand the movement to T10's repayment application, which calls `record_repayment`. Result: `MATCHED` / FACILITY.
   - Otherwise ⇒ `NEEDS_REVIEW` / `FACILITY_NOT_COLLECTABLE`. Any excess on a settled facility goes back through a `REFUND_EXCESS` payout (EC-05).
10. **IN + `FLI`** ⇒ `record_funding(investor, amount, contract_id=None)`. Result: `MATCHED` / INVESTOR.
11. **OUT + `FLP`.** If the instruction is `APPROVED` and the amount is equal:
    - move the instruction to `CONFIRMED`;
    - post its ledger event: `DISBURSEMENT` ⇒ `record_disbursement`, `INVESTOR_WITHDRAWAL` ⇒ `record_withdrawal`. The other two kinds are blocked on OQ-4.

    Result: `MATCHED` / PAYOUT_INSTRUCTION.
12. **Any OUT that does not satisfy rule 11 is an unexpected outflow.**
    - Status `NEEDS_REVIEW` / `UNEXPECTED_OUTFLOW`. Nothing is posted.
    - Page ops and compliance immediately.

    Money left a custodial account without an approved instruction, so treat it as a security incident until explained. Bank fees will trigger this until OQ-5 is settled.
13. UNMATCHED and NEEDS_REVIEW movements are never discarded and never auto-applied on a guess. Whether they post to the ledger is OQ-3.
14. **One movement ⇒ at most one ledger transaction**, with ledger idempotency key `"bank:" + dedupe_key`. The following commit in **one** DB transaction:
    - the movement write;
    - the match;
    - the ledger posting;
    - the order/facility/instruction transition;
    - the event status;
    - the audit rows.

    Notifications and `realtime.publish` run after commit.
15. **Manual attribution is an override** (domain §5.3). The schema enforces who proposed it, why, who approved it (a different person) and when. On approval it runs rules 8–11 exactly as automatic matching would.

**Reconciliation**

16. **Gap-fill poller, every 15 minutes.**
    - Calls `GET https://userapi.sepay.vn/v2/transactions` from the last-seen `since_id`, at ≤ 3 requests/s and `per_page=100`.
    - Each row goes through the same `record_bank_movement()` with `first_source='SEPAY_API'`.
    - Any movement the poller creates is a webhook SePay failed to deliver, so it is counted and alerted.
17. **Balance check.** This runs in the poller, never in the webhook, because delivery order is not guaranteed. `balance_after_vnd` of the latest movement by `occurred_at` must equal `Balance(OMNIBUS_CASH)` for that custodial account (INV-R1). If OQ-3 lands on posting to SUSPENSE this is exact; otherwise subtract the movements not yet posted. A mismatch triggers an alert.
18. **Daily three-way reconciliation** (bank statement ↔ `bank_movements` ↔ ledger) remains the primary control and the go-live gate: zero unmatched items for 5 consecutive days.

**Exposure and signals**

19. The bank brand (`gateway`), payer names (`counterparty_text`) and `raw_body` are ADMIN-only. No investor- or SME-facing API, notification or email names the bank (domain §9) or shows another party's name.
20. **Signals.** T12 events: `transfer.received`, `transfer.matched`, `transfer.unmatched`.
    - **Page:** `UNEXPECTED_OUTFLOW`; balance mismatch; more than 5 signature failures in 10 min; poller failing for more than 1 h.
    - **Ops queue:** UNMATCHED, NEEDS_REVIEW, REJECTED, and any poller gap-fill.
    - New user notification codes (frontend i18n needed): investor payment received, investor funds parked for review, SME repayment received.

---

## 7. Error Cases

| Scenario | System behaviour | Response to SePay / caller |
|---|---|---|
| Same SePay `id` delivered twice (auto-retry, dashboard Replay, two webhooks on one URL) | Inbox UNIQUE; already-processed ⇒ no work | 200 |
| Webhook and poller both see one movement | `dedupe_key` UNIQUE; the second adapter links its event to the existing movement | 200 / — |
| Bad signature or stale timestamp | Nothing stored; WARN log with IP (no body); counter incremented | 401 |
| `SEPAY_WEBHOOK_SECRET` unset | Fail closed; ERROR log | 401 |
| Signed but malformed payload | Stored `REJECTED` / `INVALID_PAYLOAD`; alert | 200 |
| `accountNumber` is not a custodial account | Stored `REJECTED` / `UNKNOWN_ACCOUNT`; alert | 200 |
| DB down while storing the event | Nothing stored | 500 ⇒ SePay retries for about 33 min; the poller covers the rest |
| Error after storing, during processing | Processing rolled back; event `FAILED`, attempts + 1 | 500 ⇒ the retry re-runs processing |
| Endpoint unreachable for more than 33 min (deploy, outage, Cloudflare block) | SePay retries exhausted | Poller gap-fills and alerts; SePay *Incidents → Replay* as a manual backup |
| Memo has no code, or a typo'd code (check character fails) | `UNMATCHED`; ops queue | 200 |
| Two valid codes in one memo, or SePay `code` ≠ our parse | `NEEDS_REVIEW` / `AMBIGUOUS_CODE` | 200 |
| Investor pays the wrong amount, pays an expired order, or loses a race for the last slot | Parked in `INVESTOR_CASH`; `MATCHED` / INVESTOR with reason; investor and ops notified | 200 |
| Two deliveries for one order in parallel | Inbox row lock plus order/listing `FOR UPDATE`; one fills, the other is a duplicate | 200 |
| Repayment for a settled facility | `NEEDS_REVIEW`; ops raises a `REFUND_EXCESS` instruction | 200 |
| Outflow with no approved `FLP` instruction, or amount ≠ instruction | `NEEDS_REVIEW` / `UNEXPECTED_OUTFLOW`; page ops and compliance | 200 |
| Instruction cancelled after ops already sent the money | The outflow arrives and is treated as unexpected (row above) | 200 |
| SePay changes its source IPs | Cloudflare blocks deliveries ⇒ the poller gap-fill alert fires; update the allow-list | — |
| Bank fee debit or interest credit (no code) | `UNEXPECTED_OUTFLOW` / `UNMATCHED` until OQ-5 | 200 |
| Approver is the same admin as the creator/proposer | Rejected | 403 |

---

## 8. Acceptance Criteria

- [ ] `test_sepay_webhook_valid_signature_returns_exact_success_body`: 200 with exactly `{"success": true}`
- [ ] `test_sepay_webhook_rejects_bad_signature`
- [ ] `test_sepay_webhook_rejects_stale_timestamp`
- [ ] `test_sepay_webhook_fails_closed_without_secret`
- [ ] `test_sepay_webhook_verifies_raw_body_not_reserialized_json`: whitespace and key order in the signed body are preserved
- [ ] `test_sepay_webhook_duplicate_id_does_no_work`
- [ ] `test_sepay_webhook_and_poller_same_movement_one_row_one_ledger_txn`
- [ ] `test_sepay_webhook_unknown_account_rejected_not_processed`
- [ ] `test_sepay_webhook_reachable_during_maintenance`
- [ ] `test_sepay_webhook_processing_error_returns_500_and_retry_succeeds`
- [ ] `test_payment_code_check_char_rejects_single_char_typo`
- [ ] `test_payment_code_parsed_from_bank_mangled_memo`: e.g. `MBVCB.3278907687.FLO8K2M4QZ7.CT tu ...`
- [ ] `test_inbound_without_code_is_unmatched_and_never_applied`
- [ ] `test_inbound_with_two_codes_needs_review`
- [ ] `test_order_payment_exact_amount_fills_order_and_posts_funding`
- [ ] `test_order_payment_wrong_amount_parks_in_investor_cash_order_stays_pending`
- [ ] `test_order_payment_after_expiry_parks_in_investor_cash`
- [ ] `test_concurrent_payments_for_last_listing_slot_never_overfund`
- [ ] `test_facility_payment_is_handed_to_repayment_application`
- [ ] `test_outflow_matching_approved_instruction_confirms_and_posts`
- [ ] `test_outflow_without_instruction_flags_unexpected_outflow_and_posts_nothing`
- [ ] `test_payout_instruction_approver_must_differ_from_creator`
- [ ] `test_manual_attribution_requires_reason_and_second_admin`
- [ ] `test_bank_movement_fact_columns_update_blocked_by_trigger`
- [ ] `test_poller_gap_fill_creates_missing_movement_and_alerts`
- [ ] `test_balance_check_alerts_on_mismatch`
- [ ] `test_non_admin_apis_never_expose_bank_name_or_payer_name`
- [ ] `test_migration_upgrade_and_downgrade_clean`

---

## 9. Rollout: SePay dashboard and infrastructure

Configure this per environment. Staging uses SePay **Test mode**, the staging URL and its own secret.

| SePay step | Setting | Value | Why |
|---|---|---|---|
| 1 Cơ bản | Tên webhook | `FundLok custodial – prod` | |
| | URL nhận webhook | `https://<api-host>/banking/feeds/sepay` | |
| | Loại giao dịch | **Tất cả** | One endpoint for in and out. Outflows confirm payouts and expose unexpected ones. |
| | Định dạng dữ liệu | **JSON** | The signature covers the raw JSON body |
| | Tự động gửi lại | **On** | 7 retries over about 33 min |
| 2 Tài khoản | Accounts | the custodial account(s) only | |
| | "Chỉ gửi khi có mã thanh toán" / prefix filter | **Off** | We need every movement, including unmatched ones and bank fees, or INV-R1 cannot hold |
| 3 Bảo mật | Method | **HMAC-SHA256**, auto-generate | The secret is shown once; put it straight into Secret Manager as `SEPAY_WEBHOOK_SECRET` |
| 4 Cảnh báo | Failure alert | ops email / Telegram | |
| Company config | Cấu trúc mã thanh toán | 4 templates `FLO`, `FLR`, `FLI`, `FLP`; suffix 8–8; *Số và chữ* | §3.1 |
| API access | API token | → `SEPAY_API_TOKEN` | Poller (rule 16) |

**Infrastructure**

- **Cloudflare.** The rule that drops requests without `X-Backend-Secret` (see `Frontendfundlok/lib/backend-secret.ts`) will drop SePay.
  - Add an exception for `POST /banking/feeds/sepay` limited to SePay's published source IPs. Re-check them at https://developer.sepay.vn/en/dia-chi-ip before go-live.
    - IPv4 as of 2026-10-11: `172.236.138.20`, `172.233.83.68`, `171.244.35.2`, `151.158.108.68`, `151.158.109.79`, `103.255.238.139`, `45.57.137.67`
    - IPv6: `2400:8905::2000:8cff:fe98:45cd`, `2600:3c15::2000:8aff:fedd:874b`
  - HMAC is the real control; the IP list is defence in depth.
- **Do not route through the Vercel `/api` proxy.** The extra hop risks re-encoding the body, which breaks HMAC, and adds another timeout layer.
- **Maintenance gate.** Add `/banking/feeds/sepay` to `MAINTENANCE_ALLOW_PREFIXES`.
- **Poller and daily reconciliation host.** The ARQ worker is not deployed to production (OQ-11).

---

## 10. Open Questions

| # | Question | Owner | Resolution |
|---|---|---|---|
| 1 | **Whose account is linked to SePay?** Handbook §7 says money never moves into a FundLok-owned account. If SePay watches a FundLok company account, investor money lands there, and this design must stay demo-only (S3, SePay Test mode). For real money, the watched account must be the custodial partner's account on which FundLok holds instruction rights only, and that bank must be supported by SePay. A related point: does a third-party aggregator reading the custodial account need the partner's consent? | CEO / Edward | — |
| 2 | Ownership: `app/banking/`, `app/ledger/`, `app/payments/` and T10 are Edward's. Should this be adopted as T10's ingestion half, or reworked? Who implements which part? | Edward + Phat | — |
| 3 | Should UNMATCHED / NEEDS_REVIEW inbound money be posted to `SUSPENSE` immediately (EXTERNAL → OMNIBUS_CASH → SUSPENSE), so `OMNIBUS_CASH` always mirrors the bank, and moved out of SUSPENSE on attribution? That needs SUSPENSE → target leg shapes, which `funding-flow.md` does not define. Recommendation: yes. | Edward | — |
| 4 | `funding-flow.md` §6.4's DISTRIBUTION leg (debit LENDER / credit OMNIBUS_CASH) **increases** OMNIBUS_CASH under that spec's own convention (debit = decrease; see its disbursement acceptance test). The current `app/payments/service.py` shim does the opposite. There is also no leg for the cash payout of a distribution or an excess refund to `EXTERNAL`. This blocks rule 11 for `DISTRIBUTION_PAYOUT` and `REFUND_EXCESS`. | Edward | — |
| 5 | Which ledger accounts take bank fees debited from, and interest credited to, the custodial account? FundLok's stated position is that interest belongs to beneficiaries. Until this is settled they surface as UNMATCHED / UNEXPECTED_OUTFLOW. | CEO / Edward | — |
| 6 | Order amount mismatch: park the whole amount in INVESTOR_CASH (this draft), or fill the order on overpayment and park only the excess? | Phat + Edward | — |
| 7 | Does `bank_movements` supersede `inbound_transfer` (no router, no data yet)? Recommendation: yes, to keep one source of truth (domain §4). | Edward | — |
| 8 | Does SePay re-sign each retry and dashboard Replay with a fresh `X-SePay-Timestamp`? If not, every retry after 5 min fails our window. Verify in Test mode before go-live. | Phat | — |
| 9 | Is `referenceCode` always non-empty and unique for the custodial bank, and does the v2 API's `reference_number` equal it? `dedupe_key` depends on both. Verify with real test transfers. | Phat | — |
| 10 | Is a Cloudflare path exception (this draft) the right way past the `X-Backend-Secret` rule? How does `/kyc/webhook` get through today? | Edward | — |
| 11 | Where do the 15-minute poller and the daily reconciliation run? ARQ + Redis are not deployed to Cloud Run prod. Options: deploy the worker, or have Cloud Scheduler call an authenticated internal endpoint. | Edward | — |
| 12 | Manual attribution: require second-admin approval always (this draft), or only above a VND threshold? | CEO / Edward | — |
| 13 | Retention period for `raw_body` / `counterparty_text` (payer names). | Edward / compliance | — |

---

## Changelog

| Version | Date | Author | Changes |
|---|---|---|---|
| 0.1 | 2026-10-11 | Phat | Initial draft. SePay webhook + API poller as T10 input adapters; provider-neutral `bank_feed_events` / `bank_movements`; payment-code format; dual-control payout instructions and manual attribution; SePay dashboard configuration; 13 open questions. |
