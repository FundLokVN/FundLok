# HANDOFF-06 — Appendix M remediation (commit-then-accept flow, late interest, automatic extension)

| | |
|---|---|
| **Status** | **Phase 1 READY to implement.** Phases 2 and 3 are BLOCKED on specs (listed below; Edward and Claude write them next). Provisional until handbook v3.2 lands (D-052) |
| **From** | Edward |
| **To** | Implementer (Claude Sonnet) for Edward-owned backend; Phat for `app/market` routers, UI and wording (D-051) |
| **Decisions** | D-036 to D-057 and TD-01 to TD-04 in `docs/outstanding-issues.md`; flow in `docs/adr/011-commit-then-accept-flow.md` |
| **Source** | Loc's Appendix M test run, 14 Nov 2026, backend `main` @ `ece64a4`; plan: https://claude.ai/artifact/278iMYJYz1MaNVtBJiw2Gb |

## Handover (D-058, 2026-11-16)

Loc assigned the repayment engine (Jira AM-P6 to AM-P11) to Phat. Agreed: **Phat implements** `app/repayment_schedule/` and the repayment code in `app/direct_transfer/` (Late status, oldest-first payments, late interest, automatic extension) from Edward's specs; **Edward keeps** the specs, ledger claim legs, tranche state machine, fees and migrations and approves every PR that touches money. T3 (rounding) and T4 (holidays) were built by Edward before the handover and are done. Where this file says a Sonnet implementer builds Late status, late interest or extension, read: Phat, or an implementer working for Phat under that rule. Jira: https://fundlok.atlassian.net/issues?jql=project%20%3D%20SCRUM%20AND%20labels%20%3D%20appendix-m

## Read first

1. `CLAUDE.md` (conventions, module ownership, Alembic rules) and the `fundlok-domain` skill. Pull context with `search_fl_docs` instead of reading whole specs.
2. `docs/adr/011-commit-then-accept-flow.md` and the Decided rows D-036 to D-052.
3. `docs/specs/direct_transfer/end-to-end-payment-flow.md` (the 4 Nov flow, Loc's design) and `docs/specs/repayment_schedule/fixed-daily-repayment-mechanism-v1.4.md` (the extension model, `s`, `U`, backstop room).

## Rules for the implementer

- **Spec first.** No behaviour change without an ACCEPTED spec section in `docs/specs/`. Phase 1 tasks below carry their own acceptance criteria; Phase 2 and 3 do not start until Edward marks the spec ACCEPTED.
- **Tests first.** Each acceptance line becomes a pytest in `tests/<module>/` named as written. Run `./scripts/docker_test.sh` before pushing. Never hit a real DB or bank API.
- **Money.** Integer VND only. No ledger leg, account type or claim posting is built on a provisional assumption: ledger work waits for Edward to confirm the spec (D-052).
- **Migrations.** Alembic only, a new hash-based revision off the current single head. Never edit an existing migration. Run `scripts/premerge_check.sh <PR>` before merging any PR with a migration: it must say GO.
- **Ownership.** You may edit `app/direct_transfer/`, `app/repayment_schedule/`, `app/underwriting/`, `app/core/`, `alembic/`, `docs/specs/`. Do **not** edit `app/market/` routers or schemas, `app/loans/`, `app/admin/`, or the frontend: those are Phat's (D-051). If a one-line router edit is unavoidable, make it and call it out in the PR.
- **Branches and PRs.** `feature/<module>/<short-description>`, one PR per task, linked to its spec or this file. Never commit to `main`.
- **Don't re-open decisions.** If a decision looks wrong, add a `Q-` entry to `docs/outstanding-issues.md` and ask Edward.

## Phase 1: ready now (independent, low risk)

Each task is its own PR.

### T1. DRR at 21 working days (Edward-owned: underwriting)
- Change `working_days_per_month` from 22 to 21 in `app/underwriting/grading/params/grading_params_v1.yaml` (line ~365) and bump `params_version`; update the tests that pin the version and the deviations register (`docs/specs/underwriting/grading-engine-deviations.md`). v1.4 D14 already says 21 wins.
- Persist `daily_repayment_rate` on the locked score run so the Offer and Loan Contract can read it as the revenue share `s` (proposed; Loc to confirm, and rounding once at lock to 2 decimals of a percent is also proposed). Only the persistence and the test belong in T1; printing it is Phat's.
- Acceptance: `test_daily_repayment_rate_uses_21_working_days`; `test_locked_score_run_stores_daily_repayment_rate`.

### T2. Remove `investor_return_bps` (D-044)
- Delete the field in `app/direct_transfer/config.py:34`, its use in `app/direct_transfer/close.py:56-58` (`investor_rate_pct`) and `app/market/projection.py:98` (one-line edit, call it out in the PR; tell Phat). Investors earn the full graded rate. `test_investor_return.py` and `tests/direct_transfer/helpers.py:101` already default to `None`.
- Acceptance: `test_investor_earns_the_full_graded_rate`; `test_offer_total_equals_repayment_plan_total` (Appendix M 2.4: 102,534,167 vs 102,500,000 must be one figure).

### T3. Rounding rule (D-045)
- Daily interest = A × I / T rounded **down**; tax rounded to the **nearest** đồng; principal is the remainder; the last instalment closes the total. Locate the builder in `app/repayment_schedule/origination.py` and `app/direct_transfer/repayment.py`.
- Test vectors (P1: 100M at 15%, 2 months, 42 instalments, handbook 10.8):

| Lender | Daily | Day-1 interest | Tax/day | Transfer/day | Last instalment | Total tax |
|---|---|---|---|---|---|---|
| A 40M | 976,190 | 23,809 | 1,190 | 975,000 | 976,210 | 50,000 |
| B 30M | 732,142 | 17,857 | 893 | 731,249 | 732,178 | 37,500 |
| C 20M | 488,095 | 11,904 | 595 | 487,500 | 488,105 | 25,000 |
| D 10M | 244,047 | 5,952 | 298 | 243,749 | 244,073 | 12,500 |

Corrected 2026-11-15 (Edward chose the stated rule): the first version of this table took the interest and tax columns from the old code's output, which contradicted the handbook's own transfer column. Transfer/day = daily − tax. Total tax still closes exactly through the last-day true-up.

- Acceptance: `test_p1_money_check_matches_handbook_10_8` (all four lenders, all columns).

### T4. Holidays ignored (D-046, TD-01)
- Daily repayments stay Monday to Friday, 21 a month, 252 a year. Vietnamese public holidays no longer skip a repayment day and no deadline moves for them. Add a flag (`observe_public_holidays=False`) to `BusinessDayCalendar` (`app/repayment_schedule/calendar.py`), keep the holiday table in place, do not delete it.
- Acceptance: `test_public_holiday_is_a_normal_repayment_day` (24 Nov 2026 and 1 Jan 2027 now carry instalments); `test_weekends_still_skipped`; `test_flag_restores_holiday_skipping`. This reverses Appendix M step 7.5 on purpose.

### T5. Fees blocking and the fee QR (D-047)
- SME fee: blocks the next listing only after 7 days from issue (today from the issue date, M5.7). Lender fee: unpaid after 7 days blocks **new commitments only** (today `INVESTOR_FEE_UNPAID` from the issue date blocks investing, M8.3). Suspended accounts: no new offers or investments, repayments continue (M11).
- Invoice QR for the SME fee uses `FUNDLOK_FEE_BANK_BIN`, `FUNDLOK_FEE_ACCOUNT_NUMBER`, `FUNDLOK_FEE_ACCOUNT_NAME` (already in `config.py`). Set the values in deployment config, never in code: Techcombank 116889688.
- Acceptance: `test_sme_fee_blocks_next_listing_only_after_seven_days`; `test_lender_fee_unpaid_after_seven_days_blocks_new_commitments_only`; `test_suspended_account_can_still_repay`; `test_sme_fee_invoice_carries_a_qr`.

### T6. One borrower follow-ups to Phat's `d2ea9ee` (D-040, D-050)
- **Edward reviews the PR first** (migration `6664b74ae183`, `projects.tax_business`, index on `funding_windows`). Then: replace `uq_projects_tax_business` with a rule of one in-progress project per business, so a repaid business can borrow again; move the per-SME live-listing check into `open_funding_window` instead of leaning on the index `IntegrityError`; leave `check_borrower_cap` as is.
- Acceptance: `test_business_can_borrow_again_after_full_repayment`; `test_second_in_progress_project_for_a_business_is_refused`; `test_open_funding_window_refuses_second_live_listing_for_an_sme`.

### T8. Borrower cap to 100M (D-054)
- In `app/direct_transfer/config.py` set `cross_platform_cap_vnd` from 400_000_000 to 100_000_000 and drop its `PROVISIONAL(Q-D12)` tag in favour of D-054. `check_borrower_cap` (`app/direct_transfer/windows.py`) keeps summing target plus declared outside debt. No other behaviour change.
- Acceptance: `test_borrower_cap_is_100m_including_declared_outside_debt` (a 60M target with 50M declared outside debt is refused with `BORROWER_CAP_EXCEEDED`; a 60M target with 40M passes); `test_per_listing_cap_unchanged`.

### T7. Grading params: confirm one fact before T1 merges
`pricing.py` reads `working_days_per_month` from the params, while `pricing.py`'s docstring says 21. Confirm that only the YAML value is wrong and nothing else hardcodes 22.

## Phase 2: flow redesign (spec ACCEPTED 2026-11-16: `docs/specs/direct_transfer/commit-then-accept-flow.md`)

**Build slices.** The slices touch the same files (`windows.py`, `tranches.py`, `close.py`), so they are built **one at a time, each merged before the next starts**, except where the table says they can run together. One PR per slice, test first, named after section 8 of the spec. Jira in brackets.

| Slice | What | Jira | Depends on |
|---|---|---|---|
| **S1 Foundation** | The one migration: `commitments`, the `funding_windows` columns and `flow`, the two new window states, **recreate both live-window partial indexes**, `lender_standing`, `lender_unlock_acknowledgements`, `acceptance_reminders`, `investor_agreement_codes` changes, new notification events, model and constant updates. No behaviour change: all existing windows are `LEGACY` | AM-E1 to E8 | none |
| **S2 Commit and decide** | `POST /windows/{id}/commitments`, the commit phase ending (target reached or `commit_closes_at`), `AWAITING_SME`, the SME decision route, auto-accept and auto-decline, acceptance folding commitments into tranches, `advance_window` skeleton | AM-E1, AM-E2 | S1 |
| **S2b Loc's answers** | Rework of S2 for D-060: no early advancement (OPEN and FULL until `commit_closes_at`, deadlines fixed), pledge withdrawal route, OTP per pledge (`PLEDGE` purpose: one small migration, code route, commit requires `code`), early-fill code removed | AM-E1, AM-E2 | S2 merged |
| **S3 Sign, pay, no-show** | Day-14 contract code and sign routes, payment gated on signing, `pay_deadline_at` cut-off, no-show expiry, `lender_standing` lock and unlock, staff waiver | AM-E4 (signing), AM-E7 | S2 |
| **S4 Formation** | Pooled loan from CONFIRMED tranches at `CLOSING`, no stub interest, SME fee at formation, window failed with no fee when nothing confirmed | AM-E4 | S3 |
| **S5 Receipt check and disputes** | SME receipt route, 24-hour silence, 30-day dispute route and `dispute_until`, staff decision on "Not received" | AM-E5, AM-E6 | S3 (can run with S4) |
| **S6 Reminders** | `acceptance_reminders` job, in-app, Zalo and email, SMS mock interface, each send logged | AM-E3 | S2 (can run with S4 and S5) |
| **Later** | Cloud Scheduler endpoints (AM-E10, after Phat's Late status AM-P8); shortfall reopening and top-up (AM-E8) | AM-E10, AM-E8 | S4; AM-P8 |

Phat builds the screens (AM-P1 to P5) against the API contract (section 4 of the spec) in parallel with S2 to S6. The Late status (`late-status.md`, AM-P8) is **Phat's** (D-058), not part of these slices.


Edward writes the API contract section first; Phat reviews it and builds screens in parallel. Scope:
- Commitments with no payment hold (a lender may commit several times; no `hold_expires_at`).
- Day-13 15:00 SME accept or decline, silence rule at 50%, manual accept below 50% before the deadline, auto-decline with no loan, no money, no fee (replaces the dormant `repay_from_approval=False` branch).
- Day-14 sign (one contract per lender, OTP plus logged click) and pay before 15:00; one pooled schedule, one loan per lender; no stub interest.
- No-show lock at 15:00 Day 14: re-eKYC plus acknowledgement sentence logged, permanent closure at the second, force-majeure waiver by staff.
- Two-way check on disbursement: SME receipt step, 24-hour windows, 30-day bank-statement dispute with staff decision route; the window is shown beside each auto-confirmation.
- All deadlines in calendar days (rename `*_working_days` config fields).
- **Late status (D-055):** a day with no SME proof by the end of its due date is Late, shown as overdue to staff, SME and lender, never paid, not counted for lender fees, and blocks extension filing; `presume_paid_days` (`repayment.py:322`) is removed for unproven days. Spec: `docs/specs/repayment_schedule/late-status.md`. Late-interest accrual is **not** in this phase; screens show overdue without an interest figure until Phase 3.
- **SME acceptance reminders (D-056):** 3 days and 1 day before the Day-13 deadline, by in-app, Zalo and email, each logged. Build an SMS sender interface with a mock; no SMS provider is chosen (Loc to choose and confirm Zalo plus email for launch).
- Cloud Scheduler endpoints for the money jobs (round advance, Day-13 and Day-14 cut-offs, settlement, invoices, monthly lender fee), added **after** the Late status lands in this phase, so no job ever runs `presume_paid_days` on a schedule (D-049).
- A migration for the new tranche fields; many current tests rewritten (they encode pay-at-commit).

## Phase 3: late interest, automatic extension, rest (BLOCKED on specs)

- **Late-interest accrual and ledger legs** (D-041, D-055): spec `docs/specs/repayment_schedule/late-interest.md`. 150% of the loan rate on overdue principal (cap 30%) plus 10% a year on overdue interest, calendar days, paid by the SME to each lender; ledger claim legs per D-041. Needs Edward's confirmation of the spec before any ledger code. The Late status itself ships in Phase 2.
- **Automatic extension** (D-042): spec `docs/specs/repayment_schedule/automatic-extension.md`. SME files by the 5th; shortfall test; a Late instalment blocks filing; days rounded up to 5 inside the backstop room; new schedule version; no fee, no staff step.
- **Phat:** offer card shows DRR, `s` and guarantor; wording (D-043); SME Service Agreement Art. 5.4 (remove late interest); return-for-correction is TD-02, not now; KYB manual form records legal representative and address (M1.3); lender fee invoice visible to the lender.
- **Open with Loc:** how an extra commitment is signed; counsel on late interest for the SME fee; confirmation of `s` = daily repayment rate; OCR check on extension revenue.

## Definition of done (every task)

Tests green in `./scripts/docker_test.sh`; spec and register updated in the same PR; `premerge_check.sh` GO where there is a migration; Edward approves PRs that touch money, the ledger, the tranche state machine or a migration; checklist below ticked after merge.

## Checklist

- [ ] T1 DRR at 21 and `daily_repayment_rate` persisted
- [ ] T2 `investor_return_bps` removed
- [ ] T3 rounding rule
- [ ] T4 holidays ignored behind a flag
- [ ] T5 fee blocking and fee QR
- [ ] T6 one borrower follow-ups (after Edward's review)
- [ ] T7 confirm no other hardcoded 22
- [ ] T8 borrower cap to 100M
- [ ] Spec: commit-then-accept flow ACCEPTED
- [ ] Spec: late status ACCEPTED (Phase 2)
- [ ] Spec: late-interest accrual and ledger legs ACCEPTED (Phase 3)
- [ ] Spec: automatic extension ACCEPTED
- [ ] Phase 2 built, Appendix M M3 to M6 rerun by Loc
- [ ] Phase 3 built, Appendix M rerun after the smaller fixes
