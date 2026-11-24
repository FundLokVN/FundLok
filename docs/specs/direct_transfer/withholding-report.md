# Spec: The SME's monthly tax-withholding report

| Field | Value |
|---|---|
| **Status** | IMPLEMENTED (Edward, 2026-11-22). Loc's answers of 22 Nov applied |
| **Owner** | Edward |
| **Implementer(s)** | Edward (the query and the route; Phat reviews `app/market/` and `app/direct_transfer/repayment.py`, his modules per D-058); Phat (the screen AM-P16 and its wording) |
| **Module** | `app/direct_transfer/repayment.py` (`withholding_report`), `app/market/repayment_router.py`, `app/market/schemas.py` |
| **Version** | 1.1 |
| **Date** | 2026-11-22 |
| **Related ADR** | ADR-009 |
| **Depends on** | `repayment-collection.md` (tax rule, `withholding_bps`, D-045), `late-status.md` (statuses), the Loan Contract Article 7 (FundLok's SME withholds), Handbook 10.3 and 10.7, HANDOFF-07 / SCRUM-623 (AM-E18) |

---

## 1. Context & Goal

An SME pays each lender directly. For an individual lender the SME withholds 5% of the **interest part** of each instalment (`withholding_bps = 500`) and pays that tax to the State, issuing withholding documents (Loan Contract Article 7). So the SME needs, each month, how much it withheld from each individual lender: the sum of the daily `tax_withheld_vnd` on the instalments it **actually paid**.

`repayment.withholding_report()` exists with a test, but it has no route, groups by *due date* and includes days that were never paid, so it would overstate the tax owed.

**Goal:** an SME route returns, per month and per individual lender, the interest and the tax withheld on paid instalments, with the lender's full name and ID number, and totals that equal the sum of the daily `tax_withheld_vnd`.

---

## 2. Out of Scope

- The wording and layout of the report and the tax declaration or withholding certificate (Phat and the accountant); only the data is built.
- Lenders that are companies. Corporate lenders are not withheld from (`tax_withheld_vnd` is 0), so they are not in the report.
- A filed or locked report. The report is read live; it reflects the days' statuses at the moment it is read (rule 5).
- Late interest. It is shown gross and not withheld in the figure; whether the 5% applies to it is an open question with Huy (`late-interest.md`, open question 2). If the answer is yes, this report changes with it. Extension fees carry no withholding.

---

## 3. Data Model

No new table, column or migration. Reads `repayment_days` (`kind`, `status`, `interest_vnd`, `tax_withheld_vnd`, `marked_paid_at`, `matched_at`), `tranches` (`investor_id`, `investor_type`, `window_id`), `funding_windows` (`sme_user_id`) and the lender's identity as the Loan Contract prints it (`investor_agreement.current_party_b`: `name`, `id_number`).

---

## 4. API Contract

`GET /repayment/withholding-report?month=YYYY-MM`, signed-in SME only (suspended accounts allowed, like every repayment read). `month` defaults to the current month in ICT.

Response `200`:
```json
{
  "month": "2026-12",
  "rows": [
    {
      "investor_id": "uuid",
      "lender_name": "NGUYEN VAN A",
      "lender_id_number": "079123456789",
      "paid_days": 21,
      "interest_vnd": 1234567,
      "tax_withheld_vnd": 61728
    }
  ],
  "totals": { "paid_days": 21, "interest_vnd": 1234567, "tax_withheld_vnd": 61728 }
}
```

`lender_name` and `lender_id_number` are the lender's full name and ID number as their Loan Contract prints them (Loc, 22 Nov: "full name and ID is required for the loan contracts anyway"). Rows are ordered by `lender_name`, then `investor_id`. An empty month returns `rows: []` and zero totals, never an error.

Errors: `401` not signed in, `403` not an SME, `422` a `month` that is not `YYYY-MM`.

---

## 5. State Machine

None.

---

## 6. Business Rules

1. **Individual lenders only.** A row exists only for a tranche whose `investor_type = "INDIVIDUAL"`.
2. **Paid instalments only.** A day counts when its `status` is `PRESUMED_PAID` or `MATCHED` (the SME has paid it). `DUE`, `LATE` and `REPORTED_MISSED` days are never counted.
3. **The month is the month it was paid** (Loc, 22 Nov), not the due month: the day's `COALESCE(marked_paid_at, matched_at)`, read in ICT. A day paid late, in a later month, belongs to the later month. Both `INSTALMENT` and `STUB` rows count.
4. **The SME's own rounds only.** Only days on windows where `funding_windows.sme_user_id` is the signed-in SME.
5. **Live, not filed.** The totals follow the days' statuses when read. A day an investor later reports missing (`REPORTED_MISSED`) leaves the report until it is resolved; one that returns to `MATCHED` re-enters in the month it was paid.
6. **Totals are exact.** `totals.tax_withheld_vnd` is the sum of the counted days' `tax_withheld_vnd` and equals the sum of the rows; likewise `interest_vnd` and `paid_days`. No rounding is done here: each day's tax is already rounded to the nearest đồng (D-045).
7. **One row per lender and month**, summed across that SME's rounds.

---

## 7. Error Cases

| Status | Code / detail | When |
|---|---|---|
| 401 | Not authenticated | no token |
| 403 | role | the user is not an SME |
| 422 | invalid `month` | not `YYYY-MM` |

---

## 8. Acceptance Criteria (`tests/direct_transfer/test_withholding_report.py`)

- `test_the_report_counts_only_paid_instalments`
- `test_a_late_or_missed_day_is_not_in_the_report`
- `test_the_month_is_the_month_the_instalment_was_paid`
- `test_the_report_has_one_row_per_individual_lender_with_full_name_and_id`
- `test_a_company_lender_is_not_in_the_report`
- `test_the_totals_equal_the_sum_of_the_daily_tax_withheld`
- `test_a_stub_instalment_counts_in_the_month_it_was_paid`
- `test_only_the_signed_in_smes_own_rounds_are_included`
- `test_an_empty_month_returns_no_rows_and_zero_totals`
- `test_the_route_is_sme_only_and_rejects_a_bad_month`

(The existing `test_withholding_report_aggregates_by_month` in `tests/direct_transfer/test_repayment.py` is replaced by these.)

---

## 9. Open Questions

1. **Phat / the accountant:** wording and layout of the report and the certificate, and whether an exportable file (CSV) is wanted. Not built here.
2. **Edward:** does the lender's ID number always exist? `current_party_b` returns `id_number` from the approved eKYC; a lender with no approved eKYC has none and could not have signed a contract, so the field should always be present. If it can be empty, the row shows `null`, never fails.
