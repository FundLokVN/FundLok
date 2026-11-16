# Spec: Late status for repayment days

| Field | Value |
|---|---|
| **Status** | **ACCEPTED** by Edward, 2026-11-16 (merged as PR #108). Provisional until handbook v3.2 (D-052) |
| **Owner** | Edward |
| **Implementer(s)** | **Phat** (D-058, Jira AM-P8 / SCRUM-633): the repayment engine, router wiring, SME and lender screens, reminder wording. Edward approves, in writing, any PR touching the ledger, the tranche state machine, fees or a migration |
| **Module** | `app/direct_transfer/repayment.py`, `jobs.py`, `worker.py`, `schemas.py`; `app/market/repayment_router.py` and `app/market/schemas.py` (Phat's) |
| **Version** | 1.0 |
| **Date** | 2026-11-15 |
| **Related ADR** | ADR-011 |
| **Depends on** | D-041, D-055, D-046, `repayment-collection.md`, `docs/appendix-m/appendix-m-remediation-decisions.md` (B10, A5), `commit-then-accept-flow.md` |

> **For Phat: read section 4 first.** One new status value, three new response fields, one new error code. The overdue label replaces "paid" wherever a day has no proof.

---

## 1. Context & Goal

Today `presume_paid_days` (`repayment.py:322`) marks every past-due day `PRESUMED_PAID`, even when the SME uploaded nothing, and the day then settles: lenders see money "paid to you" that never arrived, ledger legs post, and lender fees are billed on it (Appendix M 7.3, 8.1). Loc's decision (D-041, B10): a day with no SME proof by the end of its due date is **Late**, shown as overdue to staff, the SME and the lender, never as paid, not counted for fees, and it blocks extension filing.

**Goal:** replace presume-paid with a Late status. Late interest (150% of the loan rate on overdue principal, cap 30%, plus 10% on overdue interest) is **not** in this spec: it is Phase 3 after Edward confirms its ledger legs (D-055). Until it ships, screens say "overdue" with no interest figure.

## 2. Out of Scope

- Late-interest accrual and its ledger claim legs (`late-interest.md`, Phase 3).
- Default, backstop, recovery, arrears states, guarantor notice (M10, after launch).
- Extension filing itself (Phase 3); this spec only provides the "has a Late day" check it needs.
- Holiday handling (ignored, D-046).

## 3. Data Model

### Modified table: `repayment_days`

```
~ status               TEXT   -- CHECK gains 'LATE': IN ('DUE','LATE','PRESUMED_PAID','REPORTED_MISSED','MATCHED')
+ late_marked_at       TIMESTAMPTZ   nullable   -- when the job marked it LATE
+ late_cleared_at      TIMESTAMPTZ   nullable   -- when proof later arrived (informational)
```
Two existing columns carry the proof: `marked_paid_at` / `marked_paid_by` (the SME says paid and attaches a receipt) and `transfer_id` (the receipt's `Transfer`). **Proof** means `marked_paid_at IS NOT NULL` with a receipt on the day's transfer.

`PRESUMED_PAID` stays, but its meaning narrows: **SME proof present, lender silent past `report_deadline`** (silence counts as received). It is never set for a day without proof.

### Alembic migration
New revision off the current head: replace `ck_repayment_days_status` with the new set, add the two columns. **No backfill.** Existing days that were presumed paid and settled keep their ledger legs (the ledger is append-only); this is pre-launch dev data. If any real data exists at deploy time the migration must stop and ask.

## 4. API Contract

No new routes. Existing responses gain fields; Phat's routers pass them through.

### Response changes (`RepaymentRowOut`, `InvestmentDueOut`, `TodaysPaymentsOut`)

```json
{
  "status": "DUE | LATE | PRESUMED_PAID | REPORTED_MISSED | MATCHED",
  "is_overdue": true,                  // status is LATE (or the day is DUE and past its due date with no proof yet, until the job runs)
  "late_since": "2026-11-17",          // date, ICT; due_date + 1; null unless LATE
  "days_late": 3                       // calendar days from late_since to today, ICT; null unless LATE
}
```
`TodaysPaymentsOut` lists LATE days first, oldest first, then today's day.

### `POST /repayment/days/mark-paid` (SME) and `POST /repayment/tranches/{id}/pay` (SME)

| Status | Code | Condition |
|---|---|---|
| 409 | `OLDER_DAYS_UNPAID` | the request marks a day paid while an older day of the same tranche is `DUE` or `LATE` without proof. The body lists the older day ids. Payments apply to the oldest unsettled day first (Appendix M 7.6) |

A LATE day can still be marked paid: proof arrives late, the day leaves `LATE` (`late_cleared_at` set) and follows the normal lender confirmation path.

### Staff
`GET /admin/funding/repayments/overdue` (`AdminOverdueRepaymentsOut`) lists LATE and `REPORTED_MISSED` days with `days_late`. No new route. The existing staff decision on a lender's "Not received" (`/admin/funding/reviews/{dispute_id}/decision`, outcome `NOT_PAID`) now sets the day to `LATE` instead of leaving it unresolved (rule 5).

### Notifications
New events `REPAYMENT_DAY_LATE` (to the lender, in-app, when a day becomes LATE) and `REPAYMENT_DAY_OVERDUE_SME` (to the SME). The existing daily SME reminder (`scheduled_jobs`, Phat's) lists overdue days beside today's. Wording is Phat's.

## 5. State Machine

```
DUE ──[end of due_date, no proof]──────────────────────────────► LATE
DUE ──[SME marks paid with receipt]────────────────────────────► DUE (marked_paid_at set)
DUE (proof) ──[lender confirms]────────────────────────────────► MATCHED  (settled)
DUE (proof) ──[report_deadline passes, lender silent]──────────► PRESUMED_PAID ──► settled
DUE (proof) ──[lender reports Not received]────────────────────► REPORTED_MISSED
REPORTED_MISSED ──[staff: RECEIVED]────────────────────────────► MATCHED
REPORTED_MISSED ──[staff: NOT_PAID]────────────────────────────► LATE
LATE ──[SME marks paid with receipt]───────────────────────────► DUE (proof; late_cleared_at set) ──► as above
```
`PRESUMED_PAID` and `MATCHED` are the only statuses that settle and post ledger legs.

## 6. Business Rules

1. **Late.** A day is LATE from 00:00 ICT of the day after its `due_date` if it has no proof. The job `mark_late_days` (daily 00:05 ICT, idempotent) sets `status = LATE`, `late_marked_at`, and sends the notifications. Calendar days; no holiday exceptions (D-046).
2. **Presumed paid needs proof.** `presume_paid_days` changes to select only days with `marked_paid_at IS NOT NULL`, still `DUE`, and `report_deadline < now`. It never touches a day without proof.
3. **Report window.** `report_deadline` = proof time + 24 calendar hours for the lender's "Not received" (A5; was 2 working days). The 30-day dispute with a bank statement after silence-confirmation is the same route as in `commit-then-accept-flow.md` rule 8, on the repayment side: `POST /repayment/days/{id}/report-missing` stays open until `settled_at + 30 days` with a bank statement attached, and staff decide.
4. **Never paid.** A LATE day is shown as overdue to SME, lender and staff, in the schedule and projection. It is never shown or counted as paid, never settles, and posts no ledger leg.
5. **Staff decision.** `NOT_PAID` on a reported day makes it LATE (it was previously unresolved); `RECEIVED` makes it MATCHED.
6. **Fees.** Investor fee accrual counts only gross received on `MATCHED` and `PRESUMED_PAID` days. A LATE day contributes nothing until it is settled. The accrual month that a late day finally settles in picks it up; no back-dated invoice is re-issued.
7. **Extension filing (Loc, 16 Nov).** A day with no SME proof (`LATE`) blocks filing, as in D-041. A day in dispute (`REPORTED_MISSED`) counts as late and is shown as overdue, **but the SME can still file**: the filing is accepted and held, and is not processed until the dispute is decided. Helpers: `has_late_day(facility_id)` (LATE only) and `has_disputed_day(facility_id)`; the extension work (Phase 3) reads both.
8. **Order.** Payments apply to the oldest unsettled day first; marking a newer day while an older one is `DUE` or `LATE` without proof is refused (`OLDER_DAYS_UNPAID`).
9. **Deployment.** `mark_late_days` and the changed `presume_paid_days` are exposed to Cloud Scheduler (D-049) only after this spec is implemented. Until then they run in the worker or by staff action, as today.
10. **Audit.** Every LATE marking and clearing writes `evidence_log`.

## 7. Error Cases

| Scenario | System behaviour | Response |
|---|---|---|
| SME uploads nothing for day D | At 00:05 on D+1 the day becomes LATE; lender and SME notified | — |
| SME pays day D+1 but day D is LATE | Refused | 409 `OLDER_DAYS_UNPAID` |
| SME pays a LATE day with a receipt on D+3 | Day leaves LATE, enters the confirmation path; `late_cleared_at` set | 200 |
| Lender silent after proof | 24 hours later `PRESUMED_PAID`, settles | — |
| Lender reports Not received, staff say NOT_PAID | Day becomes LATE | — |
| Job runs twice | Second run finds nothing to mark | no change |
| Existing settled presumed-paid rows from before this change | Untouched; ledger is append-only | — |

## 8. Acceptance Criteria

`tests/direct_transfer/test_late_status.py`:

- [ ] `test_day_with_no_proof_becomes_late_at_end_of_due_date`
- [ ] `test_late_day_is_never_marked_presumed_paid_or_settled`
- [ ] `test_day_with_proof_and_lender_silence_is_presumed_paid_after_24_hours`
- [ ] `test_presume_paid_days_ignores_days_without_proof`
- [ ] `test_late_day_posts_no_ledger_leg`
- [ ] `test_late_day_is_not_counted_in_investor_fee_accrual`
- [ ] `test_late_day_cleared_when_sme_pays_with_receipt`
- [ ] `test_marking_a_newer_day_while_an_older_day_is_unpaid_is_refused`
- [ ] `test_staff_not_paid_decision_makes_the_day_late`
- [ ] `test_staff_received_decision_matches_the_day`
- [ ] `test_extension_filing_refused_while_a_day_is_late_without_proof`
- [ ] `test_extension_filing_accepted_but_held_while_a_day_is_in_dispute`
- [ ] `test_disputed_day_is_shown_as_overdue_and_counts_as_late`
- [ ] `test_todays_payments_lists_late_days_first_oldest_first`
- [ ] `test_response_exposes_is_overdue_late_since_and_days_late`
- [ ] `test_mark_late_days_is_idempotent`
- [ ] `test_lender_and_sme_notified_when_a_day_becomes_late`
- [ ] `test_report_missing_window_is_24_hours_then_30_days_with_bank_statement`
- [ ] `test_migration_adds_late_status_and_keeps_existing_rows`

## 9. Open Questions

| # | Question | Owner | Default used |
|---|---|---|---|
| 1 | Does a day under staff review (`REPORTED_MISSED`) block extension filing like a Late day? | Loc | **Resolved 16 Nov:** it counts as late, but filing is allowed and held until the dispute is decided |
| 2 | Is "end of due date" 00:00 ICT of the next calendar day, or a cut-off such as 18:00 ICT on the due date (banks stop crediting)? | Loc | 00:00 ICT of the next calendar day |
| 3 | When a LATE day is paid late and confirmed, is the original ledger date the due date or the settlement date? | Edward | Settlement date; late interest (Phase 3) covers the gap |

---

## Changelog

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0 | 2026-11-15 | Edward / Claude | First draft, from D-041, D-055 and the Appendix M findings 7.3, 7.4, 7.6, 8.1 |
| 1.0 | 2026-11-16 | Edward | Status DRAFT to ACCEPTED; implementer is Phat (D-058) |
| 1.1 | 2026-11-16 | Edward | Loc's answer on disputed days and extension filing (D-060); open question 1 resolved |
