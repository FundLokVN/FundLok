# Spec: DRR, revenue share and the guarantor on the SME's offer screen

| Field | Value |
|---|---|
| **Status** | DRAFT (Edward, 2026-11-22) |
| **Owner** | Edward |
| **Implementer(s)** | Edward (the three response fields, `app/projects/`); Phat (the screen, all wording) |
| **Module** | `app/projects/` (`schemas.py`, `service.py`, `router.py`) |
| **Version** | 1.0 |
| **Date** | 2026-11-22 |
| **Related ADR** | none |
| **Depends on** | `docs/specs/loans/listing-signing.md` (v1.5: revenue share and guarantor on the signed documents and the investor listing), Handbook 2.4, D-043, D-044, D-060, HANDOFF-07 task E4 |

---

## 1. Context & Goal

After the grading engine approves a request, the SME sees its loan offer before it signs on Day 0. Handbook 2.4 says that screen shows loan size, rate, term, daily instalment, DRR, revenue share and the guarantor. Today it shows loan size, rate, term, total repayment and daily instalment only. The SME sees the revenue share only inside the Standing Offer PDF it signs, and investors already see DRR, revenue share and the guarantor on the listing. Loc and Edward decided (21 Nov 2026) to add the three missing figures to the screen rather than rely on the PDF.

**Goal:** the SME's offer response carries DRR, revenue share and the guarantor's name, with the same meaning and source as the investor listing and the Standing Offer.

---

## 2. Out of Scope

- The screen itself, its layout and every label (Phat; the wording rules of D-043 apply).
- Any change to how DRR, the revenue share or the rate are computed (`app/underwriting/`).
- The Standing Offer, the Loan Contract and the investor listing (already done, `listing-signing.md` v1.5).
- Showing the guarantor's ID number or address (never on this surface).

---

## 3. Data Model

No new table, column or migration. All three values already exist: `score_runs.daily_repayment_rate`, and the guarantor from the approved eKYC (`app/loans/agreement/standing_offer.guarantor_for`).

---

## 4. API Contract

The offer is `loan_application.approval` of `GET /projects` (the signed-in SME's own projects), type `LoanApprovalOut` in `app/projects/schemas.py`. Add three nullable fields; nothing is removed or renamed, so existing clients keep working.

```
LoanApprovalOut {
  ... existing fields ...
  daily_repayment_rate: float | null   -- DRR, a fraction: 0.085 means 8.5%
  revenue_share:        float | null   -- the revenue share (s), a fraction: 0.10 means 10%
  guarantor_name:       string | null  -- the Personal Guarantor's name only
}
```

The names and units are the ones `MarketplaceLoanApplicationOut` already uses for the investor listing, so the web app can share a formatter. The figures are fractions, never percentages; the screen multiplies by 100 and picks the wording.

---

## 5. State Machine

None. The fields follow the existing approval: they appear when the operator's approval does (`LoanApprovalOut` is returned only for an approved request) and never change afterwards.

---

## 6. Business Rules

1. **Source run.** `daily_repayment_rate` is read from the same score run `approval_for` already uses for the rate and score (a LOCKED run when one exists, otherwise the latest graded run). It is the stored value, never recomputed (INV-5). Null when that run was scored before the rate was stored.
2. **Revenue share.** `revenue_share` equals `daily_repayment_rate` today: the Standing Offer and the Loan Contract print the revenue share as the run's daily repayment rate (D-044; Loc, 16 Nov, D-060). It is its own field so the screen never has to know that, and so a later, separate figure changes the server only.
3. **Guarantor.** `guarantor_name` is the signed-in SME user as their approved eKYC names them (`guarantor_for(db, current_user, application).name`), the person whose details the Standing Offer will be filled in with. When the user has no approved eKYC it falls back to the account's full name; null when neither exists. Never the ID number or the address.
4. **Same meaning everywhere.** What the SME sees equals what the Standing Offer prints (`share_pct(terms.revenue_share)`) and what an investor sees on the listing. The documents the SME signs remain the binding version (as `LoanApprovalOut`'s docstring already says).
5. **Nothing internal.** The bank reference rate, factor scores and every other pricing input stay absent from this response.

---

## 7. Error Cases

No new error. A missing figure is `null`, never a 4xx or 5xx: the offer screen must still load for a run scored before the rate was stored.

---

## 8. Acceptance Criteria (`tests/projects/test_sme_offer_fields.py`)

- `test_the_offer_carries_drr_revenue_share_and_the_guarantor_name`
- `test_revenue_share_is_the_locked_runs_daily_repayment_rate_as_a_fraction`
- `test_the_offer_figures_match_the_standing_offer_and_the_investor_listing`
- `test_a_run_scored_before_the_rate_was_stored_gives_null_drr_and_revenue_share`
- `test_the_guarantor_name_is_the_approved_ekyc_name_else_the_account_name`
- `test_the_guarantor_id_number_and_address_are_never_in_the_offer`
- `test_an_unapproved_request_has_no_offer_and_so_no_new_fields`
- `test_existing_offer_fields_are_unchanged`

---

## 9. Open Questions

1. **Loc / Phat:** DRR and the revenue share are the same figure today, so the screen would show one number twice, under two names. Handbook 2.4 lists both. Proposal: show both, each labelled in words ("Daily repayment rate" and "Revenue share: your monthly revenue share"), so the screen matches the handbook and the PDF; if Loc prefers one line, only Phat's screen changes.
2. **Phat:** wording and Vietnamese copy for the three labels (D-043). The API returns fractions only.
