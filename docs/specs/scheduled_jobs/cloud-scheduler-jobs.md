# Spec: Scheduled jobs through Google Cloud Scheduler (and the daily repayment reminder, by email and Zalo)

| Field | Value |
|---|---|
| **Status** | IMPLEMENTED |
| **Owner** | Phat |
| **Implementer(s)** | Phat; Edward wrote the `repayment-day` and `invoices` routes (HANDOFF-07 E1, Phat reviews) (`app/scheduled_jobs/`, `app/admin/system_email.py`, `app/phone_verification/zalo.py`) |
| **Module** | `app/scheduled_jobs/` |
| **Version** | 1.4 (`funding-advance` also lapses the SME's 24 hours on every round, the loan formed or not). 1.3: repayment-day also hands expired dispute turns to staff; the unused ARQ worker is removed |
| **Date** | 2026-11-25 |
| **Related ADR** | ADR-009 |
| **Depends on** | `docs/specs/admin/internal-email.md` v2.1 (reminder sender), `docs/specs/admin/fee-invoice-email.md`, Edward's `app/direct_transfer/repayment.todays_payments` |

---

## 1. Context & Goal

Production deploys only the API (`prod-api` on Cloud Run); the ARQ worker
never had a deployed service (`docs/deployment/ci-cd-pipeline.md`) and was
removed in v1.3, so no scheduled job runs outside these routes. Rather than
deploy and pay for an always-on worker, Google Cloud Scheduler calls a
protected endpoint on `prod-api` at a set time: Cloud Scheduler's free tier
covers a few jobs, and Cloud Run bills only while the call runs.

**Goal:** a safe way for Cloud Scheduler to run jobs on prod-api, and the first
two jobs: the daily repayment reminder to each SME, and a daily retry of SME
fee invoice emails.

## 2. Out of Scope

- Money timers beyond the ones under *Money timers* below. They can be
  exposed the same way, but they change money state and are Edward's to decide.
- Reminders to investors; lateness, arrears or late fees (out of MVP scope).
- A Zalo message for the fee-invoice job, or for earlier unpaid days (the
  template says "due today"; staff see late days in red on Admin >
  Transactions > Repayments, `docs/specs/admin/overdue-repayments-admin.md`).
- A VietQR image in the email.

## 3. Data Model

### Table: `repayment_reminders` (migration `47604f91d6f7`)

```
sme_user_id    UUID  → users(id) ON DELETE CASCADE   ┐ PK
reminder_date  DATE  (the ICT working day)            ┘
status         TEXT  SENDING | SENT | FAILED
channel        TEXT  NULL | GMAIL | PLATFORM
from_address   TEXT  NULL;  sent_to TEXT NOT NULL
due_count INT, due_vnd BIGINT, earlier_count INT      -- what the email said
attempts INT DEFAULT 1, last_error TEXT NULL           -- a code, never a message
created_at, updated_at TIMESTAMPTZ; sent_at TIMESTAMPTZ NULL
```

### Table: `repayment_zalo_reminders` (migration `5b8e2c1d9f40`)

```
tranche_id     UUID  → tranches(id) ON DELETE CASCADE ┐ PK
reminder_date  DATE  (the ICT working day)             ┘
sme_user_id    UUID  → users(id) ON DELETE CASCADE
status         TEXT  SENDING | SENT | FAILED
sent_to        TEXT  the 84xxxxxxxxx number
amount_vnd BIGINT, days_left INT                       -- what the message said
attempts INT DEFAULT 1, last_error TEXT NULL           -- ZALO_<code> or a short name
created_at, updated_at TIMESTAMPTZ; sent_at TIMESTAMPTZ NULL
```

### Configuration (environment on prod-api)

| Variable | Value |
|---|---|
| `SCHEDULER_SERVICE_ACCOUNT` | `cloud-scheduler@fundlok.iam.gserviceaccount.com` |
| `SCHEDULER_AUDIENCE` | the prod-api URL the jobs use as OIDC audience |
| `ZALO_ZBS_REMINDER_TEMPLATE_ID` | `666161` (the approved `fundlok_reminder` template). Unset: no Zalo reminder |

Either scheduler variable unset: every job answers 503. The Zalo reminder also
needs the OTP's `ZALO_APP_ID`, `ZALO_APP_SECRET`, `ZALO_OA_REFRESH_TOKEN`,
`ZALO_TOKEN_ENCRYPTION_KEY` (already set on prod-api).

## 4. API Contract

Not in the OpenAPI schema; the web app never calls them.

| Method | Path | Returns |
|---|---|---|
| POST | `/internal/jobs/repayment-reminders` | `JobResult` |
| POST | `/internal/jobs/fee-invoice-emails` | `JobResult` |
| POST | `/internal/jobs/repayment-day` | `RepaymentDayResult` |
| POST | `/internal/jobs/invoices` | `InvoicesResult` |
| POST | `/internal/jobs/cic-daily-balance` | `CicDailyBalanceResult` |

```
Authorization: Bearer <Google-signed OIDC ID token>
JobResult { job: str, sent: int, failed: int, skipped: str | null,   -- skipped: NOT_A_WORKING_DAY
            zalo: { sent: int, failed: int, skipped: str | null } | null }
-- zalo: repayment-reminders only; skipped NOT_CONFIGURED | NOT_A_WORKING_DAY
```
```
RepaymentDayResult { job: "repayment-day", late: int, settled: int, disputes: int, failed: int }
InvoicesResult     { job: "invoices", overdue: int, accrued: int, invoiced: int }
CicDailyBalanceResult { job: "cic-daily-balance", events: int }
```
401 no/invalid token (an app session is not one), 403 a token for another
account or unverified email, 503 not configured or maintenance mode.

## 5. State Machine

`repayment_reminders` (per SME per day) and `repayment_zalo_reminders` (per
ticket per day), as `fee_invoice_notices`:
`(none) → SENDING → SENT | FAILED`; `FAILED → SENDING` on a later run that day
while `attempts < 3`; `SENDING` left by a crash is never retried.

## 6. Business Rules

1. **Who may run a job.** `require_scheduler` verifies the token with
   google-auth (Google's signature, issuer, expiry, audience =
   `SCHEDULER_AUDIENCE`) and that `email` is `SCHEDULER_SERVICE_ACCOUNT` with
   `email_verified`. prod-api is public, so Cloud Run does not check it; this
   does.
2. **Safe to call twice.** Cloud Scheduler retries on a non-2xx or timeout;
   each job records what it sent and skips it next time.
3. **Repayment reminder: when.** Daily at 15:00 ICT; on a day that is not a
   working day (the app's VN calendar, `default_calendar().is_business_day`)
   nothing is sent.
4. **Repayment reminder: who and what.** Every non-suspended SME with a round
   in OPEN/FULL/CLOSING/REPAYING and at least one repayment day still `DUE`
   (not marked paid) for today or earlier. Read from `todays_payments`, the
   same list the dashboard's "Today's repayments" shows. The email gives
   today's count and total to transfer (net of tax withheld) and, separately,
   earlier days not yet marked paid. It says the SME pays each investor
   directly and links to the dashboard (https only). Never "late"/"overdue".
5. **Fee invoice emails.** Daily at 15:05 ICT, `send_pending_fee_invoice_emails`
   (`fee-invoice-email.md`): retries a failed send and catches any invoice
   issued without an Advance.
6. **Who sends.** `app/admin/system_email.deliver`: the reminder sender's
   Gmail account (System settings) through the Gmail API, else the platform
   SMTP relay. Gmail sends count towards that admin's 200 recipients an hour;
   past it, the relay takes over.
7. **Zalo repayment reminder** (`app/scheduled_jobs/zalo_reminders.py`). Same
   call as the email, after it. The `fundlok_reminder` ZBS template names one
   investment code, so the SME gets **one message per investor ticket** with a
   day due **today** still `DUE` (no receipt uploaded; uploading makes it
   `PRESUMED_PAID`, so an SME that paid before 15:00 gets nothing). Earlier
   unpaid days alone send nothing. Parameters, all strings: `company_name`
   (project legal name, at most 200 characters), `reference_code` (the
   ticket's memo), `due_date` (today, dd/mm/yyyy), `amount_due` (today's net
   for that investor, digits only), `days_left` (the ticket's repayment days
   after today). Sent only to an SME that is not suspended and has proven its
   phone (`phone_verified_at`): an unproven number may be someone else's.
   Through `zalo.send_template`, the OTP's client and token chain;
   `tracking_id` = `rr` + yymmdd + 24 hex of the ticket id (32 characters).
   Each message costs (400 đ by phone at approval), so it is claimed before it
   is sent, like the email.

### Money timers (HANDOFF-07 E1: run here because no worker is deployed)

No body. All jobs are idempotent (`app/scheduled_jobs/money_timers.py`). Repayment-day commits late marking, each
round’s settlement, and dispute handovers independently. The other money timers commit once per run.

- `POST /internal/jobs/repayment-day` runs at 00:10 and 18:10 ICT (the worker's cron). `repayment.mark_late_days`:
  a DUE day with no payment proof by 18:00 ICT on its due date becomes LATE (so the 18:10 run marks the same day, the
  00:10 run catches anything missed). `repayment.settle_days`: a PRESUMED_PAID day whose investor 24-hour report window
  has passed, with no open dispute, is settled and posted to the ledger. `disputes.advance_dispute_deadlines`: an open
  dispute whose required party (investor or SME) has not answered by its `response_deadline` passes to staff
  (`required_next_party = STAFF`, deadline cleared, a `NO_RESPONSE` evidence event); it is never decided
  automatically (`docs/specs/direct_transfer/disputes.md`). The hand-over happens at the first run after the deadline,
  so up to about 18 hours late. `late` and `settled` are the days changed by this call, `disputes` the disputes handed
  to staff. `failed` counts round settlements rolled back independently. A nonzero count returns HTTP 500 for
  Cloud Scheduler to retry, and already committed changes are skipped on retry.
- `POST /internal/jobs/invoices` runs at 00:20 ICT. `fees.mark_overdue_invoices`, `fees.accrue_investor_fees` (ended
  months, pro rata to money received) and `fees.issue_investor_invoices`. `invoiced` is the investor invoices issued by
  this call.
- `POST /internal/jobs/cic-daily-balance` runs at 00:30 ICT. `cic.emit_daily_balances` for the previous day when it was
  a business day: one `DAILY_BALANCE` event per ACTIVE tranche (Decree 94 CIC event log; no sender). `events` is the
  number newly logged; a second call logs none, and a call the morning after a weekend or holiday logs none.
- Not covered here, on purpose: `tranches.expire_holds` (a commit-then-accept ticket's hold runs to `pay_deadline_at`
  and the Day-14 no-show step is `day14-cutoff`; expiring it earlier would pre-empt the lock) and
  `close.advance_all_windows` (for commit-then-accept rounds `funding-advance` already calls the same
  `close.advance_window`). Their ARQ wrappers were removed with the worker in v1.3. See Open Questions.

## 7. Error Cases

| Case | Result |
|---|---|
| Not configured | 503, nothing runs |
| Maintenance mode on | 503 (maintenance gate); Cloud Scheduler retries |
| Gmail cannot send | platform relay sends |
| Relay fails | row `FAILED`, retried by a later call that day (up to 3) |
| Zalo refuses or is unreachable | Zalo row `FAILED` (`ZALO_<code>`), retried by a later call that day (up to 3); the email is unaffected |
| A `ZALO_*` setting missing | one row `FAILED` (`ZALO_NOT_CONFIGURED`), the rest of the run skipped |
| `ZALO_ZBS_REMINDER_TEMPLATE_ID` unset | no Zalo message; `zalo.skipped = NOT_CONFIGURED` |

## 8. Acceptance Criteria (`tests/scheduled_jobs/test_cloud_scheduler_jobs.py`)

- `test_jobs_do_nothing_until_configured`
- `test_only_the_scheduler_s_own_token_runs_a_job`
- `test_an_admin_session_is_not_a_scheduler`
- `test_each_sme_is_reminded_once_a_working_day`
- `test_nothing_is_sent_on_a_day_off`
- `test_days_marked_paid_are_not_reminded`
- `test_only_earlier_days_still_get_a_reminder`
- `test_a_failed_reminder_is_retried_by_the_next_run`
- `test_the_money_timer_routes_need_the_scheduler_token`
- `test_funding_advance_answers_500_when_a_round_failed_so_the_scheduler_retries`

`funding-advance`'s lapse of the SME's 24 hours (`tests/direct_transfer/test_commit_then_accept.py`):
- `test_a_ticket_attested_after_the_loan_formed_is_voided_with_a_refund_when_its_24_hours_pass`
- `test_a_failed_lapse_is_counted_and_the_rounds_still_advance`

Money timers (`tests/direct_transfer/test_money_timers.py`, the rest above in the first file):
- `test_repayment_day_marks_an_unpaid_day_late_after_18h_and_not_before`
- `test_repayment_day_settles_a_paid_day_after_the_investor_s_24_hours`
- `test_repayment_day_called_twice_changes_nothing_the_second_time`
- `test_repayment_day_hands_an_expired_dispute_turn_to_staff`
- `test_invoices_issues_an_investor_invoice_once`
- `test_invoices_marks_an_unpaid_invoice_overdue`
- `test_cic_daily_balance_logs_the_previous_business_day_once`

Zalo (`tests/scheduled_jobs/test_zalo_repayment_reminders.py`):
- `test_the_template_gets_every_parameter_as_text`
- `test_each_ticket_due_today_and_not_paid_gets_one_message`
- `test_a_ticket_the_sme_uploaded_a_receipt_for_gets_nothing`
- `test_only_earlier_unpaid_days_send_nothing`
- `test_an_unproven_phone_gets_nothing`
- `test_nothing_without_the_template_or_on_a_day_off`
- `test_a_message_zalo_refused_is_retried_by_the_next_run`
- `test_a_missing_zalo_setting_stops_the_run_after_one_try`
- `test_the_scheduler_call_reports_the_zalo_messages_too`

## 9. Setting up Cloud Scheduler (once, after the code is deployed)

```bash
PROJECT=fundlok; REGION=asia-southeast1
SA=cloud-scheduler@fundlok.iam.gserviceaccount.com

gcloud services enable cloudscheduler.googleapis.com --project $PROJECT
# No roles needed: prod-api checks the token itself.
gcloud iam service-accounts create cloud-scheduler --display-name "Cloud Scheduler jobs" --project $PROJECT
URL=$(gcloud run services describe prod-api --region $REGION --project $PROJECT --format='value(status.url)')
gcloud run services update prod-api --region $REGION --project $PROJECT \
  --update-env-vars "SCHEDULER_SERVICE_ACCOUNT=$SA,SCHEDULER_AUDIENCE=$URL"

gcloud scheduler jobs create http repayment-reminders --location $REGION --project $PROJECT \
  --schedule "0 15 * * *" --time-zone "Asia/Ho_Chi_Minh" --http-method POST \
  --uri "$URL/internal/jobs/repayment-reminders" \
  --oidc-service-account-email $SA --oidc-token-audience "$URL" \
  --attempt-deadline 300s --max-retry-attempts 3 --min-backoff 5m
gcloud scheduler jobs create http fee-invoice-emails --location $REGION --project $PROJECT \
  --schedule "5 15 * * *" --time-zone "Asia/Ho_Chi_Minh" --http-method POST \
  --uri "$URL/internal/jobs/fee-invoice-emails" \
  --oidc-service-account-email $SA --oidc-token-audience "$URL" \
  --attempt-deadline 300s --max-retry-attempts 3 --min-backoff 5m

# Run one now to check: gcloud scheduler jobs run repayment-reminders --location $REGION --project $PROJECT
```

### Day-14 transfer rule jobs (docs/specs/direct_transfer/day14-transfer-rule.md rules 9-10)

`POST /internal/jobs/day14-cutoff` (no body) locks unpaid and late lenders at 15:00 on rounds paying out today; the
15:35 run, after the 15:30 receipt cut-off, decides (no-show, a top-up pledge for clean late money, or unlocking a
lender whose receipt proved them in time). `POST
/internal/jobs/day14-transfer-reminders` with `{"kind": "DAY14_MORNING" | "DAY14_AFTERNOON"}` sends "sign and pay by
14:45" in the app, by email and (with `ZALO_ZBS_DAY14_TEMPLATE_ID`) on Zalo. Both are idempotent and touch only rounds
with Day-14 cut-offs. The Zalo template is registered in the ZBS console with the parameters `company_name`,
`amount`, `deadline` and a button to FundLok (app/scheduled_jobs/day14.py `zalo_template_data`). It deliberately
carries no reference code: a Day-14 transfer must use a QR's own code.

```bash
gcloud scheduler jobs create http day14-cutoff --location $REGION --project $PROJECT \
  --schedule "0,35 15 * * *" --time-zone "Asia/Ho_Chi_Minh" --http-method POST \
  --uri "$URL/internal/jobs/day14-cutoff" \
  --oidc-service-account-email $SA --oidc-token-audience "$URL" \
  --attempt-deadline 300s --max-retry-attempts 3 --min-backoff 2m
gcloud scheduler jobs create http day14-reminders-morning --location $REGION --project $PROJECT \
  --schedule "0 8 * * *" --time-zone "Asia/Ho_Chi_Minh" --http-method POST \
  --uri "$URL/internal/jobs/day14-transfer-reminders" --headers "Content-Type=application/json" \
  --message-body '{"kind": "DAY14_MORNING"}' \
  --oidc-service-account-email $SA --oidc-token-audience "$URL" \
  --attempt-deadline 300s --max-retry-attempts 3 --min-backoff 5m
gcloud scheduler jobs create http day14-reminders-afternoon --location $REGION --project $PROJECT \
  --schedule "0 14 * * *" --time-zone "Asia/Ho_Chi_Minh" --http-method POST \
  --uri "$URL/internal/jobs/day14-transfer-reminders" --headers "Content-Type=application/json" \
  --message-body '{"kind": "DAY14_AFTERNOON"}' \
  --oidc-service-account-email $SA --oidc-token-audience "$URL" \
  --attempt-deadline 300s --max-retry-attempts 3 --min-backoff 5m
```

### Day 13 of a commit-then-accept round (docs/specs/direct_transfer/day13-loan-contracts.md)

`POST /internal/jobs/funding-advance` (no body) runs at 00:00 and 15:00 ICT, and again at 15:40 (`funding-advance-day14`,
after the 15:35 `day14-cutoff` run): at 00:00 on Day 13 the pledge list is final and each lender's loan contract is
generated; at 15:00 the silence rule decides a round the SME did not decide; from 15:40 on Day 14 the round closes and
its pooled loan forms with everyone who paid, once no ticket is pending or 2 days after the 14:45 pay deadline.
`POST /internal/jobs/acceptance-reminders` (no body) runs at 09:00 ICT and reminds the SME 3 days and 1 day before the
15:00 Day-13 deadline (in the app, by email, and on Zalo once `ZALO_ZBS_ACCEPTANCE_TEMPLATE_ID` is set). Both are
idempotent and only touch commit-then-accept rounds (`FUNDING_WINDOW_FLOW=COMMIT_THEN_ACCEPT`), with one exception:
before the rounds, `funding-advance` confirms every ticket whose SME 24 hours have passed (`ATTESTED` past
`confirm_deadline`), on any round and in its own transaction (`tranches.confirm_lapsed_tranches`, rows locked
`FOR UPDATE SKIP LOCKED`, idempotent). That step used to run only from the removed ARQ worker. It matters most once a
round's loan has formed: the round is `REPAYING` and no longer advanced, so a ticket left out of the loan would stay
`ATTESTED` for ever; confirmed now, it is `VOID` and the SME owes its refund (`commit-then-accept-flow.md` rule 8). A
failure in that step is rolled back, logged and counted in `failed` like a failed round. `funding-advance`
commits each round on its own and answers 500 (same body) when any round failed, so the retries below run it again
within minutes; rounds already advanced are no-ops on the retry. A round that fails every time keeps the job red
until someone looks at it.

```bash
gcloud scheduler jobs create http funding-advance --location $REGION --project $PROJECT \
  --schedule "0 0,15 * * *" --time-zone "Asia/Ho_Chi_Minh" --http-method POST \
  --uri "$URL/internal/jobs/funding-advance" \
  --oidc-service-account-email $SA --oidc-token-audience "$URL" \
  --attempt-deadline 300s --max-retry-attempts 3 --min-backoff 2m
gcloud scheduler jobs create http funding-advance-day14 --location $REGION --project $PROJECT \
  --schedule "40 15 * * *" --time-zone "Asia/Ho_Chi_Minh" --http-method POST \
  --uri "$URL/internal/jobs/funding-advance" \
  --oidc-service-account-email $SA --oidc-token-audience "$URL" \
  --attempt-deadline 300s --max-retry-attempts 3 --min-backoff 2m
gcloud scheduler jobs create http acceptance-reminders --location $REGION --project $PROJECT \
  --schedule "0 9 * * *" --time-zone "Asia/Ho_Chi_Minh" --http-method POST \
  --uri "$URL/internal/jobs/acceptance-reminders" \
  --oidc-service-account-email $SA --oidc-token-audience "$URL" \
  --attempt-deadline 300s --max-retry-attempts 3 --min-backoff 5m
```

### Money timers (HANDOFF-07 E1)

```bash
gcloud scheduler jobs create http repayment-day --location $REGION --project $PROJECT \
  --schedule "10 0,18 * * *" --time-zone "Asia/Ho_Chi_Minh" --http-method POST \
  --uri "$URL/internal/jobs/repayment-day" \
  --oidc-service-account-email $SA --oidc-token-audience "$URL" \
  --attempt-deadline 300s --max-retry-attempts 3 --min-backoff 5m
gcloud scheduler jobs create http invoices --location $REGION --project $PROJECT \
  --schedule "20 0 * * *" --time-zone "Asia/Ho_Chi_Minh" --http-method POST \
  --uri "$URL/internal/jobs/invoices" \
  --oidc-service-account-email $SA --oidc-token-audience "$URL" \
  --attempt-deadline 300s --max-retry-attempts 3 --min-backoff 5m
```

```bash
gcloud scheduler jobs create http cic-daily-balance --location $REGION --project $PROJECT \
  --schedule "30 0 * * *" --time-zone "Asia/Ho_Chi_Minh" --http-method POST \
  --uri "$URL/internal/jobs/cic-daily-balance" \
  --oidc-service-account-email $SA --oidc-token-audience "$URL" \
  --attempt-deadline 300s --max-retry-attempts 3 --min-backoff 5m
```

## 10. Open Questions

- Resolved (v1.2): the worker's repayment-day presume/settle and invoice jobs are exposed as `repayment-day` and
  `invoices`. `cic-daily-balance` was added in v1.2.
- Resolved (v1.3): the dispute-deadline step (`disputes.advance_dispute_deadlines`), which only the never-deployed ARQ
  worker ran, now runs inside `repayment-day` (00:10 and 18:10 ICT), so a missed dispute deadline reaches staff in
  production. The ARQ worker and its job wrappers are removed. Not run by any route: the legacy-flow window close
  (`close.advance_all_windows`) and hold expiry (`tranches.expire_holds`); see *Money timers* for why. Edward decides
  whether either needs a route before launch.
- Should investors get a reminder too (e.g. to report a missing payment)?
