# Spec: Late interest on repayment days

| Field | Value |
|---|---|
| **Status** | DRAFT (Phat, 2026-11-19). **Needs Edward's approval** (D-058: Edward approves every PR touching money). Ledger legs wait for Edward's confirmation of this spec (D-041, D-055) |
| **Owner** | Phat (repayment engine, D-058); Edward approves |
| **Implementer(s)** | Phat |
| **Module** | `app/direct_transfer/repayment.py` (calculation), `app/market/repayment_router.py` and `app/market/schemas.py` (display) |
| **Version** | 0.1 |
| **Date** | 2026-11-19 |
| **Related ADR** | ADR-009, ADR-011 |
| **Depends on** | `late-status.md` (a day is LATE with no proof by 18:00 ICT on its due date), D-041, D-055, handbook 11.4 |

---

## 1. Context & Goal

D-041 (Loc, 14 Nov): late repayment interest ships at launch. A repayment day the investor has not received by the end of its due date owes late interest, paid by the SME directly to that investor. FundLok computes and shows it; FundLok never collects it (ADR-009: no custody).

**Goal:** one deterministic figure per late day, shown on the SME's next payment instruction to that investor. No ledger leg until Edward confirms this spec (D-055).

## 2. Out of Scope

- Ledger claim legs for late interest (after Edward confirms this spec, D-041/D-055).
- Default and backstop acceleration (rule 4 below is specified but not built: there is no default state and no backstop acceleration at launch, TD-03).
- Collecting, invoicing or confirming that late interest was paid; the investor's later-credit-date override (rule 3) beyond the existing 30-day report with a bank statement.
- Withholding tax on late interest (open question 2).

## 3. Data Model

No schema change. Inputs per repayment day: `principal_vnd` and `interest_vnd` (the overdue parts, gross), `due_date`, and the loan rate `r` (`funding_windows.annual_rate_pct`, the rate the loan formed at, D-044). Parameters in `DirectTransferParams`: `late_principal_rate_pct_of_r = 150`, `late_principal_rate_cap_pct = 30`, `late_interest_on_interest_pct = 10`, `late_interest_basis_days = 365`.

## 4. API Contract

`GET /repayment/tranches/{tranche_id}/due` (SME) — `InvestmentDueOut` gains:

```json
{ "late_interest_vnd": 12345 }   // sum over the days in this instruction that are past their due date; 0 otherwise
```

Shown beside the amount as a separate line the SME pays the investor directly. It is **not** in the VietQR amount (the receipt check matches the instalments' net amount).

## 5. State Machine

None. Late interest is a figure, not a status (the day's status is `late-status.md`'s).

## 6. Business Rules

1. **Rates.** On the overdue principal part: 150% × r, never above 30% a year. On the overdue interest part: 10% a year.
2. **Days.** Calendar days after the due date up to the receipt date: `days = max(0, receipt_date − due_date)`. A 365-day year. `late_interest = P_overdue × min(1.5 r, 30%) × days / 365 + I_overdue × 10% × days / 365`, rounded once to the nearest đồng, 0.5 up. Owed only if the investor has not received the money by the end of the due date (paid on the due date: 0 days, nothing owed).
3. **Receipt date.** The NAPAS 247 transaction date on the SME's proof when amount and reference match; unless the investor shows a later credit date with a bank statement within 30 days (the existing report route, `late-status.md` rule 3), in which case that date. On the payment instruction, before any proof, the receipt date is today (ICT).
4. **After default or the backstop.** The whole amount then due is overdue from that date and both rates apply. *Not built at launch* (no default state, TD-03).
5. **Display.** Shown on the SME's next payment instruction to that investor (`/due`). FundLok never collects it.
6. **Ledger.** No leg until Edward confirms this spec (D-041, D-055).

## 7. Error Cases

| Scenario | System behaviour |
|---|---|
| Day paid on its due date (even after 18:00) | 0 days, no late interest |
| Day paid 3 days late | 3 days of interest at both rates |
| r = 24% | principal rate capped at 30% (not 36%) |
| No day past its due date in the instruction | `late_interest_vnd = 0` |

## 8. Acceptance Criteria

`tests/direct_transfer/test_late_interest.py`:

- [x] `test_late_interest_uses_150_percent_of_rate_on_principal_and_10_percent_on_interest`
- [x] `test_principal_rate_is_capped_at_30_percent`
- [x] `test_calendar_days_after_due_date_on_365_day_year_rounded_half_up`
- [x] `test_no_late_interest_when_received_by_end_of_due_date`
- [x] payment instruction: `tests/market/test_round_lifecycle.py::test_the_business_pays_an_investor_and_uploads_its_receipt` (0 when due today; three days overdue shows the figure, outside the QR amount)

## 9. Open Questions

| # | Question | Owner | Default used |
|---|---|---|---|
| 1 | D-055 says screens show "overdue" with no interest figure until accrual ships. The P1 brief asks to show the figure now. Which prevails? | Edward | Shown now, display only, no ledger |
| 2 | Does the 5% withholding on individuals' interest apply to late interest? | Huy | Not withheld in the figure; shown gross |
| 3 | Is late interest computed on each day row (per investor) and rounded per day, or summed then rounded? | Edward | Per day row, rounded per row |

---

## Changelog

| Version | Date | Author | Changes |
|---|---|---|---|
| 0.1 | 2026-11-19 | Phat / Claude | First draft from D-041 and the P1 brief (P1-2) |
