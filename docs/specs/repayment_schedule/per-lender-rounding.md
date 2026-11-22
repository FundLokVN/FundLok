# Spec: Each lender's schedule is exactly its own contract's figures

| Field | Value |
|---|---|
| **Status** | DRAFT (Edward, 2026-11-22). Open Question 1 waits on Loc |
| **Owner** | Edward |
| **Implementer(s)** | Phat (D-058: `app/direct_transfer/repayment.py`, `app/repayment_schedule/`); Edward approves the PR in writing (it changes the figures behind the ledger claim legs) |
| **Module** | `app/direct_transfer/repayment.py` (`create_instalments`), `app/repayment_schedule/origination.py` and its invariants |
| **Version** | 1.0 |
| **Date** | 2026-11-22 |
| **Related ADR** | ADR-011 |
| **Depends on** | `origination-schedule-generation.md`, `repayment-collection.md`, `late-status.md`, `docs/specs/loans/listing-signing.md` (the Loan Contract's Articles 4 and 7.2), D-045, HANDOFF-07 task E2 |

---

## 1. Context & Goal

Each lender signs its own Loan Contract for its own principal `P`. Article 4 prints that lender's figures (interest `I`, total `T`, the daily instalment and the last instalment) and Article 7.2 says how they are paid. The contract's figures are `repayment_estimate(P, rate, term)`, which is `compute_origination` on that lender's own principal.

The schedule the SME actually pays is built differently. `create_instalments` takes the **pooled** loan's schedule (one `compute_origination` on the whole pool) and splits every day across lenders by `split_gross` (largest remainder). A lender's schedule is therefore **1 to 30 đồng** off its own contract. Loc's example: lender A's last instalment is 976,529 on the schedule and 976,552 on its contract. A test with four uneven lenders shows the same drift on the daily figure (13.7M: schedule 171,794, contract 171,793).

Loc's rule (21 Nov): sync to one rule, each lender's figures exactly as its contract prints them (Articles 4 and 7.2).

**Goal:** every lender's `RepaymentDay` gross amounts equal its own contract's daily and last instalment, to the đồng.

---

## 2. Out of Scope

- Changing the contract text, the rate, the term or how interest is rounded in `compute_origination` (round half up on `I`, floor on the daily instalment, remainder in the last instalment).
- The split of each day's gross into principal and interest (`split_instalment_interest`, D-045) and the withholding tax rounding. Both keep their rules and now run on the lender's own figures.
- Loans already formed. There are none live at launch (`reset-prod-data` has been run); if any exist when this ships they keep their rows.
- Extension and relief re-pricing (`extension.py`): it recomputes from the balance and is not changed here (Open Question 3).

---

## 3. Data Model

No new table or column. `repayment_days.gross_vnd`, `principal_vnd`, `interest_vnd`, `tax_withheld_vnd`, `net_vnd` keep their meaning; only their values change by a few đồng.

The pooled schedule (`scheduled_payments.expected_amount_vnd`, the facility's `T0`, `A0`, `remainder_vnd`, `final_instalment_vnd`) becomes **derived from the lenders' schedules** (rule 2). Whether that needs a migration is Open Question 2.

---

## 4. API Contract

No endpoint or schema changes. Existing responses carry the new values:
- the lender's repayment schedule and each `RepaymentDay` (`gross_vnd`, `net_vnd`, the VietQR amount);
- the SME's amount due per lender per day (the QR, which is already per lender).

---

## 5. State Machine

None.

---

## 6. Business Rules

1. **A lender's schedule is its own origination.** For each tranche with principal `P_i`, rate `r` and term `x` (the window's `annual_rate_pct`, `term_months`), call the same function the contract uses (`compute_origination`, via `repayment_estimate`). Days `1 .. N-1` are `daily_vnd`; day `N` is `final_instalment_vnd` (the remainder lands on the last day). The sum is exactly the contract's total `T_i = P_i + I_i`.
2. **The pooled day is the sum of the lenders' days.** `scheduled_payments.expected_amount_vnd` for day `d` is `Σ_i gross_i(d)`, and the facility's totals are the sums: `T0 = Σ T_i`, `A0 = Σ daily_i`, final instalment `Σ final_i`. The pool is no longer computed separately from its members, so the lenders' amounts sum to the pooled day by construction (Loc's earlier concern that they must).
3. **Invariants.** `INV-7` (the remainder is less than `N`) holds **per lender**. The pooled remainder `T0 − A0·(N−1) − final` follows from the sums and can reach `n·(N−1)` for `n` lenders; the pooled check is relaxed to "sum of the per-lender checks".
4. **Interest and tax.** `split_instalment_interest(grosses, P_i)` and the tax rules run unchanged on the lender's own gross list, so the lender's interest sums to its contract `I_i`.
5. **Offer and listing estimates are unchanged.** The SME's offer screen and the Standing Offer print the estimate for the *target* amount (`repayment_estimate(target, …)`); a lender's contract prints its own. After this change the sum of the lenders' contracts can differ from the target's estimate by a few đồng, as it already can today. Say so in Open Question 4.

---

## 7. Error Cases

No new error. A tranche whose own origination is refused (a rate above the 20% cap) already fails at listing time and is not reachable here.

---

## 8. Acceptance Criteria (`tests/direct_transfer/test_per_lender_rounding.py`)

- `test_each_lenders_schedule_equals_its_own_contract_figures` (written, expected to fail until built)
- `test_the_lenders_days_sum_to_the_pooled_day`
- `test_a_lenders_interest_and_tax_sum_to_its_contract_interest`
- `test_equal_lenders_keep_the_same_schedule_as_before`
- `test_the_pooled_totals_are_the_sums_of_the_lenders_totals`
- `test_a_lenders_last_instalment_absorbs_only_its_own_remainder`
- `test_late_status_and_settlement_work_on_the_new_amounts`
- `test_the_ledger_claim_legs_match_each_lenders_gross`

The first test already exists as a strict `xfail` on `main`: once the build makes it pass, pytest fails on the unexpected pass, which forces whoever builds this to remove the marker.

---

## 9. Open Questions

1. **Loc:** the SME's daily amount becomes the sum of the lenders' amounts, so it can differ from the single pooled figure by a few đồng per day (and by up to `n·(N−1)` đồng on the last day). Proposal: accept it. The SME pays each lender its own amount on its own QR anyway, so the pooled figure is only a total. Confirm the SME's daily total is the sum.
2. **Edward / Phat:** does `facility_data` need a migration? `facility-data-model.md` stores `remainder_vnd` with an invariant `0 <= remainder < N`. If that invariant is a database CHECK, rule 3 needs a migration relaxing it; if it is only application code, none. Check `app/repayment_schedule/models.py` before building.
3. **Edward:** extension and relief re-pricing recompute `A` from the balance. They should keep working on the pooled facility, now derived from the lenders. Confirm no extension test assumes the old pooled remainder.
4. **Loc:** the lenders' contract totals can sum to a few đồng more or less than the SME's offer for the target amount, because each rounds on its own principal. Proposal: accept; the contracts are the binding figures, as the offer screen already says.
