# Spec: Fees — SME Activation Invoice, Investor Service Accrual, Blocks, Commission Flag

| Field | Value |
|---|---|
| **Status** | DRAFT |
| **Owner** | Edward |
| **Implementer(s)** | Edward (`app/direct_transfer/fees.py`); Phat (invoice views) |
| **Module** | `app/direct_transfer/` |
| **Version** | 0.1 |
| **Date** | 2026-10-28 |
| **Related ADR** | ADR-009 |
| **Depends on** | `funding-window.md`, `repayment-collection.md`, `ledger-amendment.md`, `transfer-and-evidence.md` |
| **Source** | `Loc.md` "Collecting FundLok's fees"; HANDOFF-04 I-11, I-12, E1 (D-023, supersedes S6) |

---

## 1. Context & Goal

Both fees can be collected without touching customer money, because each is paid separately into FundLok's own account as FundLok's revenue. Under E1 the fee-free MVP (S6) is over: fees are **live by default**. The SME activation fee is a separate VAT invoice, never netted from principal; the investor service fee accrues monthly and is invoiced on a credit model. Unpaid invoices block the next listing or the next reservation.

**Goal:** specify the invoice and accrual tables, the formulas (with the golden numbers), due dates, late fee, the blocks, the invoice-provider interface and the commission-eligibility flag.

---

## 2. Out of Scope

- A real e-invoice vendor: `InvoiceProvider` interface and `ManualInvoiceProvider` only (I-13).
- Collecting fees from customer money (never netted, never deducted).
- Rate changes, discounts, waivers.
- Paying commissions (only the eligibility flag).
- HANDOFF-03 T14's fee-from-investor-return split (not used on the new path).

---

## 3. Data Model

### New table: `fee_invoices`

```
fee_invoices
├── id UUID PK
├── kind TEXT NOT NULL CHECK IN ('SME_ACTIVATION','INVESTOR_SERVICE')
├── window_id UUID NOT NULL → funding_windows(id), contract_id UUID NOT NULL → contracts(id)
├── payer_user_id UUID NOT NULL → users(id)
├── period_month DATE NULL                    -- first day of the month, INVESTOR_SERVICE only
├── base_vnd BIGINT NOT NULL                  -- confirmed principal (SME) / amount received in the period (investor)
├── fee_vnd BIGINT NOT NULL CHECK (>= 0)
├── vat_vnd BIGINT NOT NULL CHECK (>= 0)
├── late_fee_vnd BIGINT NOT NULL DEFAULT 0
├── total_vnd BIGINT NOT NULL                 -- fee + vat (+ late fee when assessed)
├── status TEXT NOT NULL DEFAULT 'ISSUED' CHECK IN ('ISSUED','PAID','OVERDUE','VOID')
├── invoice_number TEXT NOT NULL UNIQUE, provider TEXT NOT NULL, provider_ref TEXT NULL
├── reference_code TEXT NOT NULL UNIQUE       -- 'FL' + 8, carried in the payment memo
├── issued_at, due_at TIMESTAMPTZ NOT NULL, paid_at TIMESTAMPTZ NULL, paid_bank_txn_id TEXT NULL
├── created_at TIMESTAMPTZ
```

### New table: `investor_fee_accruals`

```
investor_fee_accruals
├── id UUID PK, tranche_id UUID NOT NULL → tranches(id), investor_id UUID NOT NULL → users(id)
├── period_month DATE NOT NULL, received_gross_vnd BIGINT NOT NULL, fee_vnd BIGINT NOT NULL CHECK (>= 0)
├── invoice_id UUID NULL → fee_invoices(id), created_at TIMESTAMPTZ
UNIQUE (tranche_id, period_month)
```

### Configuration (`DirectTransferParams`, all PROVISIONAL where noted)
`fees_enabled = True` (E1), `sme_fee_bps = 300`, `investor_fee_bps_per_year = 250`, `vat_bps = 1000` (PROVISIONAL(Q-D10)), `sme_invoice_due_days = 7`, `investor_invoice_due_days = 7` (PROVISIONAL(Q-D15)), `late_fee_bps_per_day = 0` (PROVISIONAL(Q-D11)), FundLok's own receiving account for invoices (`fee_account_bin`, `fee_account_number`, `fee_account_name`, from environment; if unset the invoice has no QR).

### Interface
```python
class InvoiceProvider(Protocol):
    async def issue(self, *, invoice: FeeInvoiceDraft) -> ProviderResult   # invoice_number, provider_ref
class ManualInvoiceProvider:   # assigns FL-{year}-{reference_code}; staff issue the real VAT invoice out of band
```

---

## 4. API Contract

### `GET /fees/invoices` — SME or INVESTOR (own)
```json
[ { "id":"uuid","kind":"SME_ACTIVATION|INVESTOR_SERVICE","base_vnd":100000000,"fee_vnd":3000000,"vat_vnd":300000,
    "late_fee_vnd":0,"total_vnd":3300000,"status":"ISSUED","issued_at":"ts","due_at":"ts","paid_at":null,
    "invoice_number":"string","reference_code":"FLAB12CD34","vietqr_payload":"string|null" } ]
```
### `POST /admin/direct-transfer/fees/invoices/{id}/mark-paid` — ADMIN
`{ "bank_txn_id": "string", "reason": "string" }` -> `FeeInvoiceOut`. Header `Idempotency-Key`.
### `GET /admin/direct-transfer/fees/blocks` — ADMIN
`{ "smes": [ { "user_id","invoice_ids":[…] } ], "investors": [ { "user_id","invoice_ids":[…] } ] }`.

**Errors:** 404; 409 `ALREADY_PAID`, `DUPLICATE_BANK_TXN`; 422 missing reason.

---

## 5. State Machine

```
ISSUED ──[mark paid]──► PAID
ISSUED ──[now > due_at (job)]──► OVERDUE ──[mark paid]──► PAID
ISSUED | OVERDUE ──[admin void with reason]──► VOID
```

---

## 6. Business Rules

1. **SME activation fee** (D-023): issued at window finalize when the loan forms (never when the window `FAILED`: no fee below 50%): `fee = round_half_up(confirmed_total * 3%)`; `vat = round_half_up(fee * 10%)` (PROVISIONAL(Q-D10)); `total = fee + vat`; due 7 days after issue; payer = the SME user. **Golden:** confirmed 100,000,000 -> fee 3,000,000 (plus VAT 300,000).
2. **Never netted.** The invoice is paid separately by transfer to FundLok's own account with the invoice reference in the memo; it is never deducted from principal or repayments, and appears in no `repayment_days` row (I-11). The INV-5 cap test runs both ways at finalize (`funding-window.md` rule 13).
3. **Investor service fee** (D-023): fixed at signing/close per tranche: `investor_fee_total = amount * 2.5% * term_months / 12`, rounded to the nearest dong, stored on the tranche (PROVISIONAL(Q-D15)); **golden:** 10,000,000, 4 months -> 83,333 per ticket; for the whole 100M loan the same formula gives 833,333; 40,000,000, 2 months -> 166,667 (not 166,666).
4. **Monthly accrual** (pro rata to what was actually received): for each tranche and each calendar month that has ended, `accrual = (fee_total - fee already accrued) * received_gross_in_month / gross owed at the start of the month (or at disbursement if later)`, rounded to the nearest dong, where `received_gross_in_month` sums rows **settled** with `due_date` in the month and the gross owed is the scheduled gross total less what earlier accruals received; **worked example:** fee_total 166,667, month 1 = 166,667 x 20,499,990 / 41,000,000 = 83,333; the accrual in which the last row settles takes the remainder (`fee_total - SUM(earlier accruals)`), so the accruals sum exactly to `fee_total` when fully received. `UNIQUE (tranche_id, period_month)` makes the job idempotent.
5. **Monthly investor invoice:** `issue_investor_invoices` groups un-invoiced accruals per investor and month into one `INVESTOR_SERVICE` invoice (`base = SUM(received_gross)`, `fee = SUM(accruals)`, VAT as above, due 7 days after issue).
6. **Blocks (D-047, 14 Nov 2026):** an SME with an `SME_ACTIVATION` invoice unpaid 7 days after issue (`now > due_at`) cannot open a new window (`SME_INVOICE_UNPAID`); inside the 7 days nothing is blocked. An investor with an `INVESTOR_SERVICE` invoice unpaid 7 days after issue cannot make a NEW commitment (`INVESTOR_FEE_UNPAID`) and nothing else is locked. No late interest on the SME fee (`late_fee_bps_per_day` stays 0). A SUSPENDED account takes no new offers or investments, but the repayment endpoints stay open to it. The SME invoice payload carries `vietqr_payload` built from `FUNDLOK_FEE_BANK_BIN` / `FUNDLOK_FEE_ACCOUNT_NUMBER` / `FUNDLOK_FEE_ACCOUNT_NAME` (deployment config only; null when unset). When `fees_enabled` is false no invoice is created and no block applies (kill switch for tests and incidents).
7. **Late fee:** `late_fee = floor(fee_vnd * late_fee_bps_per_day * days_overdue / 10000)` recomputed by the overdue job; with the default 0 bps it is 0 but the block still applies (PROVISIONAL(Q-D11)).
8. **Overdue job** `mark_overdue_invoices(now)`: `ISSUED` with `due_at < now` -> `OVERDUE`; idempotent; appends an `evidence_log` row.
9. **Mark paid:** records a `FEE_PAYMENT` transfer (bank txn id, evidence), sets `PAID`, posts the `FEE` ledger leg (`ledger-amendment.md` rule 5); replays return the same invoice.
10. **Commission eligibility** (D-024): `commission_eligible(tranche)` is true only when the tranche is `ACTIVE`, every one of its repayment rows is settled, and it has no open dispute; the flag exposes `settled_at`. Commission is final once paid; nothing here pays it.
11. **Language and disclosure:** the invoice and any notice describe a service fee, never interest or a deduction from the loan.
12. **Cost-basis note (Q-022):** D24 of the repayment spec keeps a fee outside the 20% cap only with documented cost basis, a separate agreement, a VAT invoice and no deduction; the build satisfies the last three; IBL decides the first.

---

## 7. Error Cases

| Scenario | Behaviour |
|---|---|
| Window `FAILED` | no invoice created |
| Mark paid twice | second returns existing PAID invoice (idempotent) |
| Bank txn id reused for another invoice | rejected `DUPLICATE_BANK_TXN` |
| Accrual job re-run | no duplicate rows (unique key) |
| `fees_enabled = False` | no invoices, no blocks |
| Provider failure | invoice stays unissued; finalize does not roll back the loan (invoice retried by job) |

---

## 8. Acceptance Criteria

`tests/direct_transfer/test_fees.py`

- [ ] `test_sme_activation_fee_is_3_percent_of_confirmed_total_golden_3_000_000`
- [ ] `test_sme_invoice_vat_and_total_and_due_in_7_days`
- [ ] `test_sme_invoice_created_at_close_and_never_when_failed`
- [ ] `test_fee_is_never_netted_from_principal_or_repayment_rows`
- [ ] `test_investor_fee_total_golden_83_333_per_10m_ticket`
- [ ] `test_investor_fee_for_whole_loan_golden_833_333`
- [ ] `test_monthly_accrual_pro_rata_to_received_and_sums_to_total`
- [ ] `test_accrual_job_idempotent`
- [ ] `test_monthly_invoice_groups_accruals_per_investor`
- [ ] `test_unpaid_sme_invoice_blocks_new_window`
- [ ] `test_unpaid_investor_invoice_blocks_reservation_and_paying_lifts_it`
- [ ] `test_paying_invoice_lifts_blocks`
- [ ] `test_sme_fee_blocks_next_listing_only_after_seven_days`
- [ ] `test_lender_fee_unpaid_after_seven_days_blocks_new_commitments_only`
- [ ] `test_suspended_account_can_still_repay`
- [ ] `test_sme_fee_invoice_carries_a_qr`
- [ ] `test_overdue_job_marks_overdue_and_assesses_late_fee`
- [ ] `test_mark_paid_posts_fee_ledger_leg_once`
- [ ] `test_fees_disabled_flag_removes_invoices_and_blocks`
- [ ] `test_commission_eligible_only_when_tranche_settled`
- [ ] `test_invoice_provider_interface_manual_provider_assigns_number`

---

## 9. Open Questions

| # | Question | Owner | Resolution |
|---|---|---|---|
| 1 | Does the 3% fee count toward the 20% cap (Q-022, I-11)? | Legal / Loc | — |
| 2 | VAT rate and whether the investor service fee carries VAT (Q-D10) | Huy | — |
| 3 | Late-fee rate and cap in the service agreement (Q-D11) | Loc | — |
| 4 | Investor invoice due date and whether any unpaid invoice (not only overdue) should block (Q-D15) | Loc | — |
| 5 | E-invoice vendor and issuance timing | Minh / Huy | — |

---

## Changelog

| Version | Date | Author | Changes |
|---|---|---|---|
| 0.1 | 2026-10-28 | Edward / Claude | Initial draft from HANDOFF-04; fees live per E1 |
