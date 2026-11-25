# HANDOFF-07: Go-live fixes after Loc's Appendix M re-test (21 Nov 2026)

Source: Loc's message after the full Appendix M run on `main` 6977af7. Everything new passes; these are the remaining small fixes.
Owner of this file: Edward. Implementer: Sonnet session (Edward's items), Phat (Phat's items).
Follow the repo workflow: spec first, failing test first, one PR per task, branch `feature/<module>/<desc>`. Edward approves any PR that touches money in writing (D-058).

Decisions already made (Edward, 21 Nov 2026):
- SME offer screen: add DRR, s and guarantor (Handbook 2.4). Not left to the PDF.
- Top-up: launch with NO top-up signing. Staff approve late money into the loan before 15:30 (admin action). Top-up signing is post-launch.
- Scheduler routes for the repayment-day and invoice jobs: Edward writes them, Phat reviews, although `app/scheduled_jobs/` is Phat's.

---

## Task E1 (blocks go-live): Cloud Scheduler routes for ARQ-only jobs

Why: no ARQ worker is deployed. Without these, unpaid days never turn Late, paid days never confirm after 24 h, and investors get no fee invoices.

Add to `app/scheduled_jobs/router.py` (same pattern as the existing routes: `require_scheduler`, `include_in_schema=False`, typed `response_model`, idempotent). Each is a thin wrapper over the job functions in `app/direct_transfer/jobs.py`; do not duplicate logic. Call the job with `ctx={"session_factory": ...}` or call the service functions directly in the route's own `db` session, and commit once.

| Route | Wraps | Schedule (ICT) |
|---|---|---|
| `POST /internal/jobs/repayment-day` | `repayment.mark_late_days` + `repayment.settle_days` (`repayment_day_job`) | 00:10 and 18:10 (matches `worker.py` cron `{0,18}:10`) |
| `POST /internal/jobs/invoices` | `fees.mark_overdue_invoices`, `accrue_investor_fees`, `issue_investor_invoices` (`invoice_job`) | 00:20 |
| `POST /internal/jobs/advance-windows` | `advance_windows_job`: `confirm_lapsed_tranches`, dispute deadlines, `close.advance_all_windows` | decide the cadence from `worker.py`; see E1b |
| `POST /internal/jobs/expire-holds` | `expire_holds_job` | decide from `worker.py`; see E1b |
| `POST /internal/jobs/cic-daily-balance` | `cic_daily_balance_job` | shortly after 00:00 |

E1b (check first, do not assume): `advance_windows_job`, `expire_holds_job` and `cic_daily_balance_job` are ALSO ARQ-only, which Loc did not list. Read `app/direct_transfer/worker.py` for their cron, and read whether `/funding-advance` already covers the commit-then-accept path. Add a route only where the commit-then-accept flow still depends on the job (for example SME dispute-window lapse to CONFIRMED, ticket-hold expiry). Report what you found to Edward before adding any.

Also check: does `/fee-invoice-emails` cover investor invoices, or only SME activation invoices? Loc says investors never get invoices; confirm whether `invoice_job` only issues the invoice row and nothing emails it. Report, do not widen scope.

Spec: update `docs/specs/scheduled_jobs/cloud-scheduler-jobs.md` (schedules, auth, idempotency, response shapes) in the same PR, spec section first.
Tests: extend `tests/scheduled_jobs/test_cloud_scheduler_jobs.py`. Cases: 403 without the scheduler identity; a day with no proof past 18:00 becomes LATE; a day with proof settles after 24 h; a second call is a no-op; an investor invoice is issued once.
Acceptance: all new routes appear in the spec table; no ARQ import in the route path; no change to job bodies.

## Task E2: One rounding rule: each lender's schedule equals its own contract

Problem: the schedule is made for the pooled loan (`app/repayment_schedule/origination.py`, interest half-up, remainder in the last instalment), then each day is split across lenders by largest remainder (`app/direct_transfer/repayment.py:59`, `create instalment rows` at line 122). Result: a lender's schedule is 1 to 30 đồng off its contract (A's last instalment 976,529 vs 976,552). Loc's rule: each lender's figures exactly as its contract prints them (Loan Contract Art. 4 and 7.2).

Step 1 (Edward, spec only): add `docs/specs/repayment_schedule/per-lender-rounding.md` from `docs/specs/_TEMPLATE.md`. It must state:
- Each lender's schedule is computed from that lender's own principal with the same function and rounding the contract text uses (find where `app/loans/agreement/loan_contract_text.py` gets its figures and use one shared function for both).
- The lender's last instalment absorbs that lender's own remainder.
- Open question to resolve before build: lender amounts may no longer sum to the pooled day. Decide where the difference sits (proposal: the pooled schedule becomes the sum of the lender schedules; the SME's daily figure on the QR is the sum). Confirm with Loc.
- Acceptance test names, including the A example (976,529 vs 976,552).

Step 2 (Phat implements per D-058, Edward approves in writing because it touches `repayment.py` and the ledger claim legs): failing test first, then change `repayment.py` instalment generation.
Do not alter existing migrations. If a column is needed, new Alembic revision off the current head, and run `scripts/premerge_check.sh <PR>` before merge.

## Task E3: Late money: staff approve into the loan before 15:30

Problem: late money becomes a `TopUpPledge` (`app/direct_transfer/models.py:480`, recorded by `day14.run_day14_cutoff`) that nobody can sign, so the SME holds money with no loan.

Step 1 (Edward, spec): `docs/specs/direct_transfer/late-money-approval.md`. Rules to write:
- Admin action: approve a late-money ticket (`UNDER_REVIEW`, `review_cause` `LATE_MONEY` or `LATE_RECEIPT`) into the round before 15:30 on Day 14. After the cut-off the action is refused and the existing top-up pledge path stands.
- The lender's signature: the lender has not signed the Day-14 copy, so say which text binds (loan contract Art. 5 / standing offer Art. 5 "không ký - ràng buộc theo Điều") and confirm with Phat and Loc.
- Ledger: claim legs post on the normal confirmation path (`ledger_posting.py`), no new leg types. Idempotency key on the action.
- Audit row (`app/utils/audit`), admin role gate, error cases (round closed, ticket not late money, amount over the SME's accepted total).
- Out of scope: top-up signing (post-launch).

Step 2 (build): service function in `app/direct_transfer/` plus an admin endpoint (Phat's `app/admin/` router calls it). Edward owns the service and ledger; Phat owns the router and button. Tests map 1:1 to the spec's acceptance criteria.

## Task E4: SME offer API fields (Edward confirms the contract, Phat builds the screen)

Add DRR, revenue share s and guarantor to the SME's offer response (the endpoint behind the screen the SME sees after the grading engine approves, before it signs on Day 0). Investors already see all three on the listing, so reuse those source fields and format. Write the contract in the spec before Phat builds. No UI work from Edward.

## Task E5: Production verification runbook (docs only, Edward and Phat run it)

New file `docs/runbooks/go-live-checklist.md`. Rows with a command, the expected result and a pass/fail box:
1. Env on the prod-api Cloud Run service: `FUNDING_WINDOW_FLOW=COMMIT_THEN_ACCEPT`; `FUNDLOK_FEE_BANK_BIN` (970407), `FUNDLOK_FEE_ACCOUNT_NUMBER` (116889688), `FUNDLOK_FEE_ACCOUNT_NAME`; `ZALO_ZBS_ACCEPTANCE_TEMPLATE_ID`, `ZALO_ZBS_DAY14_TEMPLATE_ID`. Command: `gcloud run services describe <service> --format=yaml`.
2. Scheduler jobs from E1 plus the existing ones: `funding-advance` (00:00, 15:00), `acceptance-reminders` (09:00), `day14-transfer-reminders` (08:00 body `DAY14_MORNING`, 14:00 body `DAY14_AFTERNOON`), `day14-cutoff` (15:00, 15:35), `repayment-reminders`, `fee-invoice-emails`, `repayment-day`, `invoices`. Command: `gcloud scheduler jobs list`; then `gcloud scheduler jobs run <job>` once each and read the Cloud Run log.
   Note: `funding-advance` is documented at 00:00, 15:00 and 15:40 in the code comment; Loc listed 00:00 and 15:00. Check whether the 15:40 close run is scheduled. The round closes at 15:40 per the router docstring.
3. Real-account checks, not mocks: TPV KYB, CA token with the NEAC check, Zalo OTP and ZBS templates (parameters `company_name`, `amount`, `deadline`), receipt OCR.
4. Scheduler login: the OIDC token reaches `/internal/jobs/*` through `require_scheduler` (app/scheduled_jobs/auth.py).

## Phat's tasks (for reference; not Edward's)
- P1: Load FINAL v1.0 text into all five contracts and bump each to v1.0 (T&S, SME agreement, standing offer, investor agreement, loan contract). Includes loan contract 7.2 (public holidays are billed, not skipped), the Day-13 signature block "Hoặc: không ký - ràng buộc theo Điều [4.2 / 5]" and the unsigned-guarantor line.
- P2: `UNLOCK_SENTENCE` in `app/direct_transfer/standing.py`: final vi sentence, and accept the en sentence too.
- P3: Check the Zalo template IDs are set in production and the parameters are `company_name`, `amount`, `deadline`.
- P4: Fee invoice screen with a QR for lenders, and the QR in the SME's fee email.
- P5: SME offer screen: show DRR, s and guarantor (after E4's contract).
- P6: Admin button for E3.
- P7: Implement E2 step 2.

## Order
E1 first (blocks go-live) with E5 started in parallel; then E2 spec and E3 spec (can be written together); then E4 contract; builds follow once Phat has confirmed the specs.
