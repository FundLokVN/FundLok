# Spec: Admin > Transactions > Service fees (mark the SME's fee paid)

| Field | Value |
|---|---|
| **Status** | IMPLEMENTED |
| **Owner** | Phat |
| **Implementer(s)** | Phat (`app/admin/fees.py`, `app/admin/fee_schemas.py`, frontend `admin/transactions`) |
| **Module** | `app/admin/` |
| **Version** | 1.0 |
| **Date** | 2026-11-13 |
| **Related ADR** | ADR-009 |
| **Depends on** | `docs/specs/direct_transfer/fees.md` (Edward: the invoice, `mark_invoice_paid`, the block), `docs/specs/admin/fee-invoice-email.md` |

---

## 1. Context & Goal

When a funding round closes, Edward's `close.py` issues one SME activation-fee
invoice: 3% of the confirmed total plus VAT, paid separately into FundLok's own
account. FundLok has no bank connection, so the invoice turns PAID only when
staff mark it, through `fees.mark_invoice_paid` (records a FEE_PAYMENT transfer,
posts the ledger, logs the event). Nothing called it: no endpoint, no screen.
And `windows.open_funding_window` refuses a business with an unpaid activation
invoice (SME_INVOICE_UNPAID), so every business that finished a round was
blocked from its next one, paid or not.

**Goal:** staff see each fee with the investment package it is charged on (the
round, the business, the investments whose confirmed total the 3% is on), and
mark it paid once the transfer shows on FundLok's statement.

## 2. Out of Scope

- Investor service-fee invoices (`INVESTOR_SERVICE`).
- Matching the statement automatically (SePay bank-feed draft).
- Voiding, refunding or un-marking a fee; changing the formula, VAT or due date.

## 3. Data Model

No new table. Reads `fee_invoices` (Edward), `funding_windows`, `tranches`
(status ACTIVE: the investments the fee is charged on; ACTIVE is a tranche's
last state), `projects`, `users`, `fee_invoice_notices` (email status).

## 4. API Contract

Admins (ADMIN, SYSTEM_ADMIN). Errors `{"detail"}` + `X-Error-Code` where noted.

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/admin/funding/fee-invoices?status=UNPAID\|PAID\|ALL&limit=200` | — | `AdminFeeInvoiceOut[]` (audited read, `FEE_INVOICE`) |
| POST | `/admin/funding/fee-invoices/{id}/mark-paid` | `AdminFeeMarkPaidRequest` | `AdminFeeInvoiceOut` |
| GET | `/admin/funding/rounds` (existing) | — | each round gains `fee_invoice: AdminRoundFeeOut \| null` |

```
AdminFeeInvoiceOut { id, status: ISSUED|PAID|OVERDUE|VOID, is_overdue, invoice_number, reference_code,
                     base_vnd, fee_vnd, vat_vnd, late_fee_vnd, total_vnd, issued_at, due_at,
                     paid_at, paid_bank_txn_id,
                     window_id, round_status, project_id, project_name, sme_user_id, sme_email, sme_full_name,
                     investments: [{ tranche_id, investor_name, investor_email, amount_vnd,
                                     reference_code, confirmed_at }],
                     email_status: SENDING|SENT|FAILED|null, emailed_at }
AdminRoundFeeOut   { id, status, total_vnd, due_at, is_overdue }
AdminFeeMarkPaidRequest { bank_txn_id: str (1–100, one line), reason: str (1–500) }   -- extra fields → 422
```

## 5. State Machine

Edward's: `ISSUED → PAID` (this screen); `ISSUED → OVERDUE` by `invoice_job`
(not deployed). `is_overdue` is computed (unpaid and past `due_at`).

## 6. Business Rules

1. **Unpaid first.** The list orders unpaid before paid, soonest due first.
2. **The package.** `investments` are the round's ACTIVE tranches; their amounts
   add up to `base_vnd`.
3. **Marking paid** calls `mark_invoice_paid` with the bank transaction ID and
   reason. The invoice row is locked first (`SELECT … FOR UPDATE`): that
   function checks "already PAID" without a lock, and two staff pressing at once
   would otherwise record two payments and two ledger postings.
4. **Idempotent.** Marking a PAID invoice again returns it unchanged, with no
   second transfer and no second audit row.
5. **One bank transaction pays one invoice** (409 `DUPLICATE_BANK_TXN`).
6. **Audited** as `FEE_INVOICE_MARKED_PAID` (actor, before/after status, bank
   transaction, total, reason), besides Edward's evidence-log event.
7. **Lifts the block.** Once PAID, `sme_has_unpaid_invoice` is false and the
   business may open its next round.

## 7. Error Cases

| Case | Status | X-Error-Code |
|---|---|---|
| Not an admin / not signed in | 403 / 401 | — |
| Unknown invoice, or not an activation fee | 404 | — |
| Missing/blank bank transaction or reason, line break, extra field | 422 | — |
| Bank transaction already used for another invoice | 409 | `DUPLICATE_BANK_TXN` |
| Invoice void | 409 | `INVOICE_VOID` |

## 8. Acceptance Criteria

Backend (`tests/admin/test_fee_invoices_admin.py`):
- `test_staff_see_each_fee_with_its_investment_package`
- `test_an_unpaid_fee_past_its_due_date_reads_overdue`
- `test_each_round_shows_its_service_fee`
- `test_marking_paid_records_the_payment_and_lifts_the_block`
- `test_one_bank_transaction_pays_one_invoice`
- `test_marking_paid_needs_a_transaction_and_a_reason`
- `test_an_unknown_invoice_is_not_found`
- `test_only_staff_see_or_mark_fees`

Frontend (`admin/transactions/_components/FeeInvoiceSheet.test.tsx`): the
package is shown; a transaction and note are required; the payload sent; the
duplicate-transaction message; a paid fee shows no form.

## 9. Open Questions

- Edward: should `mark_invoice_paid` take the row lock itself (rule 3)?
- Should a paid fee be reversible (a mistaken mark)?
