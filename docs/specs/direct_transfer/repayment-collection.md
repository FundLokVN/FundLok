# Spec: Repayment Collection — Daily Rows, Today's Payments, Missed-Day Reports, Withholding

| Field | Value |
|---|---|
| **Status** | DRAFT |
| **Owner** | Edward |
| **Implementer(s)** | Edward (`app/direct_transfer/repayment.py`); Phat (screens, routers) |
| **Module** | `app/direct_transfer/` |
| **Version** | 0.1 |
| **Date** | 2026-10-28 |
| **Related ADR** | ADR-009 |
| **Depends on** | `funding-window.md`, `transfer-and-evidence.md`, `disputes.md`, `ledger-amendment.md`, `cic-event-log.md`, `docs/specs/repayment_schedule/origination-schedule-generation.md`, `facility-state-machine.md`, `fixed-daily-repayment-mechanism-v1.4.md` |
| **Source** | `Loc.md` "Repayment", gaps table (tax), "Timers"; HANDOFF-04 I-16, E3, E5, section 6 |

---

## 1. Context & Goal

In a direct-transfer model the SME pays every investor separately each working day (840 transfers on a 100M, 4-month loan with 10 investors). Repayment stays manual in the MVP: FundLok computes each day's principal, interest, withheld tax and net amount per investor, shows an SME a "today's payments" screen with one VietQR per investor, and treats a day as paid **unless the investor reports it missing by a deadline**. The SME withholds 5% personal income tax on individual investors' interest.

**Goal:** specify how the facility schedule is split pro-rata into per-tranche repayment rows (with golden numbers), the stub-interest rows, tax, the "today's payments" read model, the missed-day report and deadline, settlement and the monthly withholding data.

---

## 2. Out of Scope

- Arrears, lateness, late-rate interest, acceleration, recovery, write-off (MVP: `ARREARS_WARNING`, `ARREARS`, `ACCELERATED`, `IN_RECOVERY`, `WRITTEN_OFF` are not used; `CLAUDE.md` MVP scope).
- Extension and relief re-pricing and the extension fee (D23 is later; D27 deferred).
- Standing orders, batch upload and bank feeds as automation (static instructions only; Epic 2.3).
- The monthly withholding report's wording and the tax declaration (Huy / accountant); only the data is built.
- Weekly settlement (off).

---

## 3. Data Model

Integer VND (`BIGINT`).

### New table: `repayment_days`

```
repayment_days
├── id UUID PK
├── tranche_id UUID NOT NULL → tranches(id)
├── facility_id UUID NOT NULL → facility(id)
├── scheduled_payment_id UUID NULL → scheduled_payment(id)   -- NULL for the stub row
├── kind TEXT NOT NULL CHECK IN ('INSTALMENT','STUB')
├── day_index INT NOT NULL            -- 1..N for instalments; 0 for the stub
├── due_date DATE NOT NULL
├── principal_vnd BIGINT NOT NULL CHECK (>= 0), interest_vnd BIGINT NOT NULL CHECK (>= 0)
├── gross_vnd BIGINT NOT NULL CHECK (gross_vnd = principal_vnd + interest_vnd)
├── tax_withheld_vnd BIGINT NOT NULL CHECK (>= 0 AND <= interest_vnd)
├── net_vnd BIGINT NOT NULL CHECK (net_vnd = gross_vnd - tax_withheld_vnd)
├── status TEXT NOT NULL DEFAULT 'DUE' CHECK IN ('DUE','PRESUMED_PAID','REPORTED_MISSED','MATCHED')
├── report_deadline TIMESTAMPTZ NOT NULL   -- end of the 2nd working day after due_date
├── marked_paid_at TIMESTAMPTZ NULL, marked_paid_by UUID NULL
├── reported_at TIMESTAMPTZ NULL, reported_by UUID NULL
├── matched_at TIMESTAMPTZ NULL
├── settled_at TIMESTAMPTZ NULL            -- ledger posted; counts as collected
├── transfer_id UUID NULL → transfers(id)  -- SME's receipt for this day
└── created_at TIMESTAMPTZ
UNIQUE (tranche_id, kind, day_index)
```
`repayment_days` rows are generated once at window finalize. Amounts never change afterwards (no re-spread in MVP); status columns change.

### Configuration
`withholding_bps = 500` (5%), `missed_day_report_working_days = 2`, `late_report_cap_working_days = 5` (PROVISIONAL(Q-D14)).

---

## 4. API Contract

See Epic 2 of `docs/handoffs/HANDOFF-04-phat-tickets.md`; shapes are normative here.

### `GET /repayment/today?date=YYYY-MM-DD` — SME
Rows with `due_date <= date` whose status is `DUE` or `PRESUMED_PAID` and not settled, **plus** rows due on `date`; `date` defaults to today in ICT.
```json
{ "date": "2026-11-20",
  "rows": [ { "repayment_day_id":"uuid","tranche_id":"uuid","kind":"INSTALMENT|STUB","investor_display":"A. N***",
              "due_date":"2026-11-20","principal_vnd":119048,"interest_vnd":6349,"gross_vnd":125397,
              "tax_withheld_vnd":317,"net_vnd":125080,"status":"DUE",
              "payee":{"bank_name":"string","bank_bin":"970407","account_number":"string","account_name":"string"},
              "reference_code":"FLAB12CD34","vietqr_payload":"000201…" } ],
  "totals": { "gross_vnd":1253968,"tax_withheld_vnd":3170,"net_vnd":1250798 } }
```
### `POST /repayment/days/mark-paid` — SME. Header `Idempotency-Key`.
`{ "repayment_day_ids": ["uuid"], "receipt_file_id": "uuid|null" }` -> updated rows. Allowed for rows with `due_date <= today` in `DUE`; sets `PRESUMED_PAID`, `marked_paid_at`; with a receipt file, a `REPAYMENT` transfer (`rail=MANUAL`, `REPORTED`) is recorded.
### `POST /repayment/days/{id}/report-missing` — INVESTOR (owner of the tranche).
`{ "statement": "string|null", "file_id": "uuid|null" }` -> `RepaymentDayOut`.
### `GET /repayment/tranches/{tranche_id}/schedule` — investor or the window's SME.
### `GET /repayment/withholding-report?month=YYYY-MM` — SME.
### Staff: `POST /admin/direct-transfer/repayment-days/{id}/match` `{ "reason": "string", "transfer_id": "uuid|null" }` -> `MATCHED`.

**Errors:** 400 invalid state; 404 not found/not yours; 409 `REPORT_WINDOW_CLOSED` (beyond the late cap), `ALREADY_REPORTED`, `NOT_DUE_YET`.

---

## 5. State Machine

```
DUE ──[SME marks paid | due date passes with no report (job)]──────────► PRESUMED_PAID
DUE | PRESUMED_PAID ──[investor reports before report_deadline]───────► REPORTED_MISSED   (dispute opened, SME proves first)
REPORTED_MISSED ──[staff: PAID | matched receipt]────────────────────► MATCHED
PRESUMED_PAID ──[staff match with receipt]───────────────────────────► MATCHED
```
`REPORTED_MISSED` after outcome `NOT_PAID` stays `REPORTED_MISSED` (no arrears state in MVP). `settled_at` is set when the row counts as collected (rule 7). The facility `ACTIVE -> SETTLED` transition happens when every row is settled.

---

## 6. Business Rules

1. **Rows from the facility schedule.** At finalize the facility is originated for `P = confirmed_total` (`originate_facility`, `working_days_per_month = 21`, Q-011). For each `scheduled_payment` day `k` (amount `D_k`: `A0`, or `A0 + remainder` on day `N`) the amount is split across ACTIVE tranches **pro rata to principal with largest-remainder rounding**: `g_i(k) = floor(D_k * a_i / T)`, then the leftover đồng go one each to the tranches with the largest fractional remainder (ties by tranche order: `confirmed_at`, then `id`). `SUM_i g_i(k) = D_k` exactly, every day.
2. **Principal and interest.** Principal of tranche `i` on day `k` uses cumulative round-half-up so the principal over the term is exactly `a_i`: `pr_i(k) = R(a_i * k / N) - R(a_i * (k-1) / N)`, `R(x) = round_half_up`. `interest_i(k) = g_i(k) - pr_i(k)`; it must be `>= 0` (invariant error otherwise). Per-investor lifetime interest may differ from exact pro rata by less than `N` đồng; the sum over investors equals the facility interest exactly.
3. **Golden numbers** (100M, 16%, 4 months = 84 working days, 10 investors of 10M): `T0 = 105,333,333`, `A0 = 1,253,968`, remainder 21, final instalment 1,253,989; the first investor's day-1 row: gross 125,397 = principal 119,048 + interest 6,349; tax 317; net 125,080.
4. **Withholding** (PROVISIONAL(Q-D6)): only tranches with `investor_type = INDIVIDUAL`. Per instalment day `tax = floor(interest * 5%)`; on the **final instalment** `tax = round_half_up(5% * SUM(instalment interest)) - SUM(earlier instalment tax)`, so the total equals 5% of total interest (e.g. 84 x 317 = 26,628 against 26,667; the remainder lands on day 84). ENTITY investors: tax 0. The stub row: `tax = floor(5% * stub)`.
5. **Stub rows** (D-027): one `STUB` row per tranche with `stub_interest_vnd > 0`, `due_date = repayment_start_date`, `principal 0`, `interest = stub_interest`, due on the first instalment day, outside `T0`, shown in today's payments with `kind = STUB`.
6. **Today's payments.** The SME pays each investor the **net** amount (gross minus withheld tax) to the investor's registered `APPROVED` account, one VietQR per row (amount = `net_vnd`, memo = the tranche's `reference_code`). The SME declares and pays the withheld tax itself.
7. **Day counts as paid unless reported.** The job `presume_paid_days(now)` moves `DUE` rows with `due_date < today (ICT)` to `PRESUMED_PAID`. The investor may report a row once `due_date <= today` and until `report_deadline` (end of the 2nd working day after `due_date`); reporting sets `REPORTED_MISSED`, opens a `MISSED_DAY` dispute with the SME as `required_next_party` (burden of proof: SME first, `disputes.md`) and emits CIC `MISSED_DAY_CORRECTION`.
8. **Late report** (PROVISIONAL(Q-D14)): after `report_deadline` and within 5 working days, a report is accepted into reconciliation as a `LATE_REPORT` dispute **without** changing the row status (history is never silently rewritten); beyond 5 working days it is refused (`REPORT_WINDOW_CLOSED`).
9. **Settlement.** `settle_days(now)` sets `settled_at` and posts principal ledger legs for `PRESUMED_PAID` rows past `report_deadline` with no open dispute, and for `MATCHED` rows at match time; `facility.collected_to_date_vnd` increases by `gross_vnd`. A `REPORTED_MISSED` row is never settled until resolved.
10. **Loan closed.** When every row of the facility is settled: facility `ACTIVE -> SETTLED` (`OBLIGATION_SATISFIED`, balance 0), window `REPAID`, SME unlocked (one-listing lock, Q-D16), commission eligibility (`fees.md`) evaluated per tranche. A facility with unresolved rows cannot settle.
11. **CIC.** `emit_daily_balances(now)` emits a `DAILY_BALANCE` event per ACTIVE tranche per business day counting `PRESUMED_PAID` and `MATCHED` rows as paid (`cic-event-log.md`).
12. **Early repayment** is encouraged but is out of this spec (no rebate; INV-11). A full early payoff is a later spec.
13. **Withholding report data**: rows per tranche and month: `interest_vnd` and `tax_withheld_vnd` of instalment and stub rows due in the month, with `investor_type`; investor identity masked in responses.
14. **Language**: collections output never uses pressure wording; the first question on a reported miss is "what happened" (`fundlok-domain` section 9).

---

## 7. Error Cases

| Scenario | Behaviour | Response |
|---|---|---|
| SME marks a row not yet due | refused | 409 `NOT_DUE_YET` |
| Investor reports a row twice | refused | 409 `ALREADY_REPORTED` |
| Investor reports a row of another tranche | | 404 |
| Report after the late cap | refused, log row | 409 `REPORT_WINDOW_CLOSED` |
| Job runs twice | idempotent (status guards, unique ledger keys) | — |
| Interest would be negative | invariant error at generation; finalize rolls back | ops alert |
| Facility cannot settle (unsettled row) | transition not attempted | — |
| Mark-paid replay with same key | same result | 200 |

---

## 8. Acceptance Criteria

`tests/direct_transfer/test_repayment.py`, `test_golden.py`

- [ ] `test_golden_origination_100m_16pct_84_days` — A0 1,253,968, remainder 21, T0 105,333,333
- [ ] `test_golden_investor_split_119048_plus_6349` — first investor day 1: gross 125,397, principal 119,048, interest 6,349
- [ ] `test_golden_tax_317_and_net_125080`
- [ ] `test_daily_gross_split_sums_to_schedule_amount_every_day`
- [ ] `test_principal_over_term_equals_tranche_amount_exactly`
- [ ] `test_interest_sum_over_tranches_equals_facility_interest`
- [ ] `test_withholding_total_is_five_percent_with_remainder_on_final_day`
- [ ] `test_withholding_skips_entity_investors`
- [ ] `test_stub_row_is_separate_dated_obligation_on_first_instalment_day`
- [ ] `test_todays_payments_returns_net_amount_and_vietqr_per_investor`
- [ ] `test_todays_payments_totals_match_rows`
- [ ] `test_mark_paid_sets_presumed_paid_and_is_idempotent`
- [ ] `test_mark_paid_rejects_future_rows`
- [ ] `test_presume_paid_job_is_idempotent_and_clock_injected`
- [ ] `test_report_missing_before_deadline_marks_reported_missed_and_opens_dispute`
- [ ] `test_report_deadline_is_two_working_days`
- [ ] `test_late_report_within_cap_goes_to_reconciliation_without_changing_status`
- [ ] `test_late_report_beyond_cap_refused`
- [ ] `test_settle_days_posts_principal_ledger_legs_once`
- [ ] `test_reported_missed_day_blocks_settlement_until_matched`
- [ ] `test_facility_settles_when_all_rows_settled_and_window_repaid_and_sme_unlocked`
- [ ] `test_daily_balance_counts_presumed_paid_and_excludes_reported_missed`
- [ ] `test_withholding_report_aggregates_by_month`
- [ ] `test_investor_cannot_see_other_tranche_schedule_404`

---

## 9. Open Questions

| # | Question | Owner | Resolution |
|---|---|---|---|
| 1 | Withholding for corporate investors and exact rounding (IBL item 6, Q-D6, Q-024) | Legal / Huy | — |
| 2 | Missed-report window 2 working days vs noon next day (Q-026) | Lok / Edward | — |
| 3 | Late-report cap 5 working days (Q-D14) | Lok | — |
| 4 | What happens operationally after outcome `NOT_PAID` (no arrears in MVP) | Lok | — |
| 5 | Should the memo carry a per-day reference rather than the tranche reference? | Phat / Edward | — |

---

## Changelog

| Version | Date | Author | Changes |
|---|---|---|---|
| 0.1 | 2026-10-28 | Edward / Claude | Initial draft from HANDOFF-04 |
