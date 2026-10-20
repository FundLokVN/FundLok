# Spec section guide: worked fragments

Short, FundLok-shaped examples for each `_TEMPLATE.md` section. For full
real-world examples, read an ACCEPTED spec in the same area. Good models:
`docs/specs/payments/funding-flow.md` (decisions forced and resolved inline,
"Blocks" header row), `docs/specs/ledger/money-integer-vnd.md` (tight scope),
and `docs/specs/repayment_schedule/facility-state-machine.md`.

## Header
```
| **Status** | DRAFT |
| **Owner** | Edward |
| **Implementer(s)** | Edward (backend) / Phat (frontend) |
| **Module** | `app/market/` |
| **Version** | 1.0 |
| **Date** | 2026-10-20 |
| **Related ADR** | ADR-003 |
| **Depends on** | `docs/specs/payments/funding-flow.md` (ACCEPTED v1.0) |
| **Blocks** | (optional) tasks/specs waiting on this one |
```

## 1. Context & Goal
> Investors can place an order on an OPEN listing today without a verified
> bank feed confirming their cash. HANDOFF-03 Issue 10 requires
> `funded_amount` to move only on a confirmed FUNDING posting.
> **Goal:** an order becomes FILLED only when a matched inbound transfer posts
> its FUNDING leg.

## 2. Out of Scope
- Bank-feed matching itself (`docs/specs/banking/sepay-bank-feed.md`, DRAFT)
- Partial fills across multiple transfers (future spec)

## 3. Data Model
```
order_fundings
├── id               UUID          PK, default gen_random_uuid()
├── order_id         UUID          NOT NULL → orders(id)
├── ledger_txn_id    UUID          NOT NULL → ledger_transactions(id), UNIQUE
├── amount_vnd       NUMERIC(20,0) NOT NULL, CHECK (amount_vnd > 0)   -- integer VND
└── created_at       TIMESTAMPTZ   NOT NULL DEFAULT now()
```
Migration: new Alembic revision chained off current head (hash ID assigned on
`alembic revision --autogenerate`). Append-only: no UPDATE path. A wrong
match is corrected with a reversing ledger transaction plus a new row.

## 4. API Contract
```
POST /market/orders/{order_id}/confirm-funding        Auth: ADMIN
Headers: Idempotency-Key: <uuid>   (required, financial write)
Request  ConfirmFundingRequest  { "bank_transaction_id": "uuid" }
201      OrderFundingResponse   { "order_id": "uuid", "status": "FILLED", "amount_vnd": 50000000, "ledger_txn_id": "uuid" }
Replay with same key + same body → 200 with the original OrderFundingResponse, no new rows
Errors  {"detail": "..."}: 404 order not found · 409 order not PENDING_PAYMENT ·
        409 Idempotency-Key reused with a different body · 403 non-ADMIN
```
Amounts in JSON are integers (VND). Never strings with decimals, never floats.

## 5. State Machine
```
Order: PENDING_PAYMENT ──[confirm-funding, ADMIN]──► FILLED
       PENDING_PAYMENT ──[expiry job, ARQ]─────────► EXPIRED
```
Invalid transition → 409 `{"detail": "Invalid state transition: FILLED → FILLED"}`.

## 6. Business Rules
1. FUNDING posts INVESTOR_CASH → LENDER for the order's contract (funding-flow §6).
2. `amount_vnd` must equal the order amount exactly. No over/under funding in MVP.
3. FundLok is never a counterparty (`fundlok-domain` §1). No leg may credit an FL-owned account except FL_REVENUE via FEE.

## 7. Error Cases
| Scenario | System behaviour | Caller sees |
|---|---|---|
| Same bank_transaction_id confirmed twice | UNIQUE on ledger_txn_id + idempotency → no second posting | 200 original |
| Bank feed down | nothing posts; order stays PENDING_PAYMENT; expiry still runs | — |
| Admin confirms for wrong investor | 409, audit log records attempt | 409 |

## 8. Acceptance Criteria
- [ ] `test_confirm_funding_fills_order_and_posts_funding_leg`
- [ ] `test_confirm_funding_idempotent_replay_returns_existing_and_writes_nothing`
- [ ] `test_confirm_funding_rejects_non_pending_order`
- [ ] `test_confirm_funding_rejects_non_admin`
- [ ] `test_confirm_funding_amount_is_integer_vnd`

## 9. Open Questions
| # | Question | Owner | Resolution |
|---|---|---|---|
| 1 | Does Phat's admin UI need a "pending confirmation" list endpoint? | Phat | — |
| 2 | Expiry window for PENDING_PAYMENT? | Edward | — |

## Known template drift (raise, don't silently fix)
`_TEMPLATE.md` predates two accepted decisions. Follow the decisions, and
mention the drift to the human so Edward can update the template:
- `DECIMAL(15,2)` / `"decimal string"` money → now integer VND (`money-integer-vnd.md`).
- `XXXX_<description>.py (number assigned…)` → hash revision IDs (CLAUDE.md "Database").
- Brankas webhook examples → banking is mocked pending a partner decision
  (`docs/specs/banking/account-linking-mock.md`). Don't design against Brankas
  specifics.
