# Spec: Ledger Amendment — Confirmed-Transfer Model (no FundLok cash account)

| Field | Value |
|---|---|
| **Status** | DRAFT |
| **Owner** | Edward |
| **Implementer(s)** | Edward |
| **Module** | `app/direct_transfer/ledger_posting.py` (new); `app/ledger/` unchanged except one migration that widens the account-type CHECK |
| **Version** | 0.1 |
| **Date** | 2026-10-28 |
| **Related ADR** | ADR-009 (supersedes the omnibus assumption for new paths), ADR-003 (seam kept) |
| **Depends on** | `docs/specs/ledger/ledger-foundation.md` (ACCEPTED), `docs/specs/payments/funding-flow.md` (ACCEPTED; section 6 superseded for the new path), `transfer-and-evidence.md`, `funding-window.md`, `repayment-collection.md`, `fees.md` |
| **Source** | HANDOFF-04 I-2, decision E2 (D-026) |

---

## 1. Context & Goal

`ledger-foundation.md` and `funding-flow.md` model a pooled FundLok bank account (`OMNIBUS_CASH`). Under ADR-009 FundLok holds no money, so no new path may post to `OMNIBUS_CASH`. The `transfers` table is the evidence record; the ledger records the **claims** that follow from confirmed transfers, using a platform-level `EXTERNAL` account as the counterparty meaning "the parties' own banks, outside FundLok's perimeter".

**Goal:** specify exactly when the direct-transfer path posts to the ledger, which legs, with which idempotency keys, without altering the ledger code or its immutability trigger.

**Correction to HANDOFF-04 E2:** the brief says to use "the existing `EXTERNAL` account". `EXTERNAL` is defined in `funding-flow.md` section 3.3 but was **never implemented**: `ledger_accounts.account_type` is CHECK-constrained to five types (`OMNIBUS_CASH`, `LENDER`, `BORROWER`, `FL_REVENUE`, `SUSPENSE`) and no code references it. This spec therefore adds `EXTERNAL` to that CHECK in the HANDOFF-04 migration (Q-D9). No ledger service code changes.

---

## 2. Out of Scope

- The legacy omnibus path (`app/payments/`, `place_order`), unchanged and deprecated behind a flag.
- Interest, tax and fee obligations in the ledger (`UNEARNED_INTEREST`, T14-style splits): they live in `repayment_days` and `fee_invoices` until a later spec (Q-D9).
- Reversals after ACTIVE (refund after ACTIVE is out of MVP).
- Reconciliation against a bank (no bank data).
- Changing `post_transaction`'s pass-through check (this spec is shaped around it: see rule 3).

---

## 3. Data Model

No new tables. One CHECK change (in the HANDOFF-04 migration):

```
ledger_accounts.ck_ledger_accounts_type  IN ('OMNIBUS_CASH','LENDER','BORROWER','FL_REVENUE','SUSPENSE','EXTERNAL')
```

`ledger_accounts` for the new path:

| account_type | owner_user_id | contract_id | meaning |
|---|---|---|---|
| `EXTERNAL` | NULL | NULL | the parties' own bank accounts; platform-level singleton (get-or-create); its `custodial_account_id` is the existing PLATFORM registry row, which here is a reference record, not a cash account |
| `LENDER` | investor | contract | the investor's outstanding claim on the contract (credited at establishment, debited by principal distributions, `funding-flow.md` Issue 15) |
| `BORROWER` | SME user | contract | the SME's outstanding principal obligation on the contract |
| `FL_REVENUE` | NULL | NULL | FundLok's fee revenue |

Ledger entry types reused: `FUNDING`, `DISBURSEMENT`, `REPAYMENT`, `DISTRIBUTION`, `FEE`, `REFUND`. No new type.

Alembic: part of the single HANDOFF-04 migration (hash revision id recorded in the report).

---

## 4. API Contract

Internal service functions only (`app/direct_transfer/ledger_posting.py`); no HTTP endpoints.

```python
async def post_tranche_claims(db, *, tranche, contract_id, sme_user_id, created_by=None) -> list[LedgerTransaction]
async def post_repayment_day_settled(db, *, repayment_day, tranche, contract_id, sme_user_id, created_by=None) -> list[LedgerTransaction]
async def post_fee_paid(db, *, invoice, created_by=None) -> LedgerTransaction | None
async def contract_claim_summary(db, *, contract_id) -> ClaimSummary   # lender_claims, borrower_obligation, per-account balances
```

All return the original transactions on replay (idempotent).

---

## 5. State Machine

Not applicable (ledger rows are immutable facts). Posting is triggered by:

| Event | Source | Legs |
|---|---|---|
| Tranche joins the loan (`CONFIRMED -> ACTIVE`) | `close.finalize_window` | rule 2 |
| Repayment day settled (rule 5 of `repayment-collection.md`) | `repayment.settle_days`, `match_day` | rule 4 |
| Fee invoice marked paid | `fees.mark_invoice_paid` | rule 5 |

---

## 6. Business Rules

1. **No `OMNIBUS_CASH` leg on any new path**, and no leg to or from an account owned by FundLok other than `FL_REVENUE` for fees. A test scans every account touched by new-path transactions.
2. **Tranche claims**, posted once per tranche when it becomes `ACTIVE` (not at `CONFIRMED`: a pre-ACTIVE void then needs no reversal, PROVISIONAL(Q-D9)), as two **separate** ledger transactions (so `EXTERNAL`, the shared counterparty, is touched once per transaction and `post_transaction`'s pass-through check is never tripped):
   - key `dt:claim:{tranche_id}:lender`: `FUNDING`, debit `EXTERNAL`, credit `LENDER(investor, contract)`, amount = `tranche.amount_vnd`.
   - key `dt:claim:{tranche_id}:borrower`: `DISBURSEMENT`, debit `EXTERNAL`, credit `BORROWER(sme, contract)`, amount = `tranche.amount_vnd`.
3. **Why two transactions:** `post_transaction` treats an account touched by more than one leg in a transaction as a pass-through hub that must net to zero. `EXTERNAL` would be debited twice in a single two-leg transaction. The pass-through narrowing flagged in `funding-flow.md` section 6.3 is therefore not needed and the ledger service is untouched.
4. **Repayment day settled** (principal component only, `principal_vnd > 0`): two transactions, keys `dt:repay:{day_id}:borrower` (`REPAYMENT`: debit `BORROWER`, credit `EXTERNAL`, amount = principal) and `dt:repay:{day_id}:lender` (`DISTRIBUTION`: debit `LENDER`, credit `EXTERNAL`, amount = principal). At full repayment both `BORROWER` and every `LENDER` balance return to 0. Interest, tax and stub rows post nothing (Q-D9).
5. **Fee paid**: key `dt:fee:{invoice_id}`, `FEE`, debit `EXTERNAL`, credit `FL_REVENUE`, amount = `fee_vnd + late_fee_vnd` (VAT is owed to the tax authority and is not revenue; it stays on the invoice). `contract_id` = the invoice's contract.
6. **Idempotency**: every transaction has the key above; a replay returns the existing transaction and writes nothing.
7. **Integer VND**: amounts are `Decimal(int)`; legs must be positive (a zero principal day posts nothing).
8. **Invariants** (asserted in tests): (a) `Balance(BORROWER, contract) = SUM(principal of ACTIVE tranches) - SUM(principal settled)`; (b) `SUM(Balance(LENDER_i)) = Balance(BORROWER)`; (c) no entry touches `OMNIBUS_CASH`; (d) the append-only trigger still rejects UPDATE/DELETE (existing test unchanged).
9. **The ledger is not the evidence of a transfer**: a transfer being `CONFIRMED` does not itself post; posting follows the business events above.

---

## 7. Error Cases

| Scenario | Behaviour |
|---|---|
| Posting replayed | returns existing transactions, no new rows |
| Tranche not `ACTIVE` when posting claims | `LedgerPostingError("tranche must be ACTIVE")`, nothing written |
| Amount zero or negative | `LedgerImbalanceError` from `post_transaction`; for a zero-principal day the poster skips |
| `EXTERNAL` account missing | get-or-create on first use |
| Contract mismatch between legs | `CrossContractTransactionError` from the ledger service (unchanged) |

---

## 8. Acceptance Criteria

`tests/direct_transfer/test_ledger_posting.py`

- [ ] `test_external_account_type_is_allowed_by_migration`
- [ ] `test_tranche_claims_post_lender_and_borrower_legs_against_external`
- [ ] `test_tranche_claims_are_idempotent`
- [ ] `test_no_new_path_leg_touches_omnibus_cash`
- [ ] `test_repayment_day_posts_principal_legs_only`
- [ ] `test_full_repayment_returns_borrower_and_lender_balances_to_zero`
- [ ] `test_lender_claims_sum_equals_borrower_obligation_throughout`
- [ ] `test_fee_paid_posts_fee_leg_to_fl_revenue_net_of_vat`
- [ ] `test_zero_principal_day_posts_nothing`
- [ ] `test_ledger_still_append_only` (existing trigger rejects UPDATE/DELETE on rows created by the new path)
- [ ] `test_posting_requires_active_tranche`

---

## 9. Open Questions

| # | Question | Owner | Resolution |
|---|---|---|---|
| 1 | Post at `CONFIRMED` (HANDOFF-04 E2) or at `ACTIVE` (this spec)? Posting at ACTIVE avoids reversals for a pre-ACTIVE void (Q-D9) | Edward | — |
| 2 | Interest and tax in the ledger: build an `UNEARNED_INTEREST` account later (Q-D9)? | Edward | — |
| 3 | Should `EXTERNAL` be split per party type for reporting? | Edward | — |

---

## Changelog

| Version | Date | Author | Changes |
|---|---|---|---|
| 0.1 | 2026-10-28 | Edward / Claude | Initial draft; notes that `EXTERNAL` was never implemented |
