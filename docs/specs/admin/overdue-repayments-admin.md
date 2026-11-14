# Spec: Admin > Transactions > Repayments: late repayments in red

| Field | Value |
|---|---|
| **Status** | IMPLEMENTED |
| **Owner** | Phat |
| **Implementer(s)** | Phat (`app/admin/repayments.py`, `app/admin/schemas.py`, frontend `admin/transactions`) |
| **Module** | `app/admin/` |
| **Version** | 1.0 |
| **Date** | 2026-11-13 |
| **Related ADR** | ADR-009 |
| **Depends on** | `docs/specs/direct_transfer/repayment-collection.md` (Edward: `repayment_days`, `mark_days_paid`, `presume_paid_days`) |

---

## 1. Context & Goal

The business repays each investor directly, every working day, and uploads its
bank receipt, which marks the days paid (`DUE -> PRESUMED_PAID`). The admin
Repayments tab listed only those receipts, so a business that paid nothing at
all never appeared there: staff could not see who was behind.

**Goal:** staff see, in red, every investor ticket a business has not repaid on
time: how many days are unpaid, how many working days late, and how much it
owes. On-time tickets are not flagged.

## 2. Out of Scope

- Any arrears handling: no `ARREARS_WARNING`/`ARREARS` state, no late interest,
  no status change, no email to the business or investor (MVP scope,
  `CLAUDE.md`). This is a read-only display.
- The business's own dashboard ("Today's repayments") and the daily reminder
  email, which still say "not yet marked paid", never "late".

## 3. Data Model

No new table. Reads `repayment_days`, `tranches`, `funding_windows`, `projects`,
`users`.

## 4. API Contract

### `GET /admin/funding/repayments/overdue` (admin; read audited as `TRANSFER`)

```json
{
  "as_of": "2026-11-13",
  "tickets": 3, "businesses": 1, "overdue_days": 21, "overdue_net_vnd": 5325730,
  "items": [
    {
      "tranche_id": "uuid", "window_id": "uuid", "project_name": "ABC Co",
      "reference_code": "FLS1JVAK41",
      "business": {"id": "uuid", "full_name": "...", "email": "..."},
      "investor": {"id": "uuid", "full_name": "...", "email": "..."},
      "overdue_days": 7, "oldest_due_date": "2026-11-04",
      "working_days_late": 7, "overdue_net_vnd": 1775248
    }
  ]
}
```

Items are ordered most working days late first, then largest amount.

### `GET /admin/funding/repayments` (existing), four new fields per receipt

`tranche_overdue_days`, `tranche_overdue_net_vnd`, `tranche_oldest_overdue_date`
(null when none), `tranche_working_days_late`: the same figures for the
receipt's ticket, so the receipts table can flag a ticket that paid some days
but is late on others.

## 5. Business Rules

1. **A day is late** when its status is still `DUE` (the business has not
   marked it paid; no receipt) and its `due_date` is before today in ICT.
   There is no cut-off hour: `mark_days_paid` accepts a day at any time on its
   due date, and the 15:00 ICT reminder only says what is due today. So a day
   is late from the next calendar day.
2. `PRESUMED_PAID`, `REPORTED_MISSED` (the investor's "not received" goes
   through the dispute queue) and `MATCHED` days are not late.
3. **Days unpaid** counts due dates: the first-period-interest row due with
   day 1 is the same day.
4. **Working days late** = working days (VN calendar,
   `app/repayment_schedule/calendar.py`) from the oldest late due date
   (included) to today (excluded). Due Monday and unpaid on Tuesday: 1. Due
   Friday and unpaid the next Monday: still 1.
5. **Amount** is the net the business owes the investor for those days (what
   it transfers, after the tax it withholds).
6. Only rounds the business is asked to pay (`OPEN`, `FULL`, `CLOSING`,
   `REPAYING`, as `todays_payments` reads) are listed.

**Caveat:** `presume_paid_days` (the ARQ `repayment_day_job`, not deployed)
would turn every past-due `DUE` day `PRESUMED_PAID` at midnight. If that job is
ever deployed, this rule must change to "not marked by the business"
(`marked_paid_at` and `transfer_id` both null), or nothing will ever show late.

## 6. UI

- The Repayments tab carries a red count of late tickets, visible from any tab.
- Above the receipts table, a red alert lists every late ticket: investment,
  business -> investor, a red "N working days late" badge with "N days unpaid
  · due {date}", and the amount owed. Hidden when nothing is late.
- In the receipts table, a late ticket's row gets a red tint and rail and a red
  "Late · N days" badge whose tooltip gives the date, working days and amount.

## 7. Acceptance Criteria (tests)

`tests/admin/test_overdue_repayments.py`:
- `test_a_day_is_not_late_on_its_own_due_date`
- `test_a_day_still_due_the_next_day_is_one_working_day_late`
- `test_the_weekend_does_not_count_as_working_days_late`
- `test_working_days_late_run_from_the_oldest_late_day_and_amounts_add_up`
- `test_first_period_interest_due_with_day_one_is_the_same_late_day`
- `test_days_marked_paid_reported_or_confirmed_are_not_late`
- `test_staff_see_a_ticket_the_business_has_not_repaid_on_time`
- `test_a_late_day_the_business_then_pays_is_no_longer_late`
- `test_the_receipts_list_flags_a_ticket_with_a_later_day_still_unpaid`
- `test_a_round_no_longer_repaying_is_not_listed`

Frontend: `OverdueRepayments.test.tsx`, `RepaymentQueue.test.tsx`.

## 8. Open Questions

1. Should the business also see "late" on its own dashboard, or the reminder
   email say it? Out of MVP scope today (the reminder deliberately does not).
